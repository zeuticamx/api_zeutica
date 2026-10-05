import mysql.connector, os, mov_reg, html, asyncio
from datetime import date, timedelta
from typing import List, Optional
from fastapi import APIRouter, HTTPException
from mysql.connector import errorcode
from pydantic import BaseModel, Field, field_validator
from dotenv import load_dotenv
from servicios.telegram.notificacion import send_telegram_alert
from routers import comisiones

router =APIRouter(tags=["/ventas"],responses={404: {"Mensaje":"No encontrado"}})
load_dotenv() # Carga de credenciales .env

# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

@router.get("/ventas/{f1}/{f2}")
async def consultar_ventas(f1: date, f2: date):
    """
    Consulta ventas por rango de fecha_registro (ambos días incluidos) definido por frontend.
    Un rango sin ventas es un resultado válido: devuelve [] en lugar de 404.
    """
    if f1 > f2:
        raise HTTPException(status_code=422, detail="La fecha inicial no puede ser posterior a la fecha final")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True) # Usamos dictionary=True para que devuelva claves como 'sku'

    # Rango semiabierto [f1, f2 + 1 día) en lugar de DATE(fecha_registro) BETWEEN:
    # incluye todo el día final y permite usar un índice sobre fecha_registro.
    query = "SELECT * FROM ventasRegistro WHERE fecha_registro >= %s AND fecha_registro < %s ORDER BY fecha_registro DESC"

    try:
        cursor.execute(query, (f1, f2 + timedelta(days=1)))
        return cursor.fetchall() # FastAPI lo convierte automáticamente a JSON

    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")
    
    finally:
        cursor.close()
        conn.close()
        
@router.get("/verifica-venta/{norden}")
async def verificar_venta(norden: str):
    """
    Verifica venta en DB.
    """
    conn = get_db_connection()
    # Usamos buffered=True para descargar el resultado de inmediato
    cursor = conn.cursor(dictionary=True, buffered=True)

    query = "SELECT id FROM ventasRegistro WHERE id_ventas = %s"

    try:
        cursor.execute(query, (norden,))
        existe = cursor.fetchone()

        # Consumimos cualquier otro resultado pendiente por seguridad
        while cursor.nextset():
            pass

        # Validamos después de asegurar que el cursor terminó su trabajo
        if not existe:
            # Cerramos antes del raise para liberar la conexión en AWS de inmediato
            cursor.close()
            conn.close()
            raise HTTPException(status_code=404, detail="El registro de venta no existe")
        
        return existe
    
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")
    
    finally:
        # El bloque finally solo actuará si no entró en el if not existe
        if conn.is_connected():
            cursor.close()
            conn.close()

@router.get("/ventas-credito") # Enpoint para mostrar clientes a credito
async def verificar_venta():
    """
    Consulta clientes con credito activo.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    query = "SELECT id_ventas, sku, producto, cantidad, nombreComprador, saldo_pendiente, fecha_vencimiento FROM ventasRegistro WHERE saldo_pendiente > 0 "

    try:
        cursor.execute(query)
        existe = cursor.fetchall()

        if not existe:
            raise HTTPException(status_code=404, detail="El registro de venta no existe")
        
        return existe
    
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")
    
    finally:
        cursor.close()
        conn.close()

SQL_INSERT_VENTA = """
    INSERT INTO ventasRegistro
    (id_ventas, sku, producto, cantidad, precio, fecha, nombreComprador, otros, plataforma, usuario, condicion_pago, saldo_pendiente)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

SQL_RESTAR_STOCK = "UPDATE productos SET stock_bodega = stock_bodega - %s WHERE sku = %s AND stock_bodega >= %s"


def mensaje_venta_duplicada(id_venta) -> str:
    return f"La venta '{id_venta}' ya fue registrada previamente"


def existe_id_venta(cursor, id_venta) -> bool:
    """
    Indica si id_ventas ya tiene partidas en ventasRegistro.
    FOR UPDATE toma un bloqueo sobre el índice id_ventas hasta el commit/rollback, así
    que una segunda petición con el mismo id espera (o cae en deadlock, que se responde
    como 409) en lugar de pasar la validación al mismo tiempo que la primera.
    id_ventas es texto: el parámetro va como str para que MySQL use el índice.
    """
    cursor.execute(
        "SELECT id FROM ventasRegistro WHERE id_ventas = %s LIMIT 1 FOR UPDATE",
        (str(id_venta),),
    )
    # fetchall y no fetchone: con cursor sin buffer, dejar filas sin leer rompe el siguiente execute
    return len(cursor.fetchall()) > 0


def es_conflicto_de_venta(err: mysql.connector.Error) -> bool:
    """Choque con una llave única (meli_key/amazon_key) o carrera con otra petición del mismo id."""
    return err.errno in (errorcode.ER_DUP_ENTRY, errorcode.ER_LOCK_DEADLOCK)


# Definimos un modelo para recibir los datos en JSON (Body)
class VentaSchema(BaseModel):
    id_venta: int
    sku: str
    producto: str
    stock_bodega: int
    precio: float
    fecha: str
    nombreComprador: str
    otros: str
    plataforma: str
    usuario: str
    condicion_pago: str

@router.post("/producto/venta")
async def registrar_venta(venta: VentaSchema):
    """
    Registro de venta, se verifica que el stock sea suficiente para continuar.
    """
    if venta.stock_bodega <= 0:
        raise HTTPException(status_code=400, detail="La cantidad a descontar debe ser mayor a 0")

    connection = get_db_connection()
    try:
        with connection.cursor(dictionary=True) as cursor:

            # 0. El id de venta no debe existir: un reintento (doble clic, timeout, reenvío)
            # no puede volver a registrar la venta ni a descontar stock.
            if existe_id_venta(cursor, venta.id_venta):
                connection.rollback()
                raise HTTPException(status_code=409, detail=mensaje_venta_duplicada(venta.id_venta))

            # A. Verificar stock y bloquear la fila (FOR UPDATE) para evitar que dos ventas
            # concurrentes del mismo SKU lean el mismo stock y ambas pasen la validación
            sql_check = "SELECT stock_bodega FROM productos WHERE sku = %s FOR UPDATE"
            cursor.execute(sql_check, (venta.sku,))
            resultado = cursor.fetchone()

            if not resultado:
                connection.rollback()
                raise HTTPException(status_code=404, detail="Producto no encontrado")

            if resultado['stock_bodega'] < venta.stock_bodega:
                connection.rollback()
                raise HTTPException(status_code=400, detail=f"Stock insuficiente. Solo hay {resultado['stock_bodega']}")

            # --- NUEVA LÓGICA DE CRÉDITO ---
            # Calculamos el saldo inicial. Si es CRÉDITO, el saldo es el total (precio * cantidad)
            # Si es CONTADO, el saldo es 0.
            total_operacion = venta.precio * venta.stock_bodega
            saldo_inicial = total_operacion if venta.condicion_pago == "CREDITO" else 0.00

            # B. Registrar venta en el historial primero. INSERT normal (no IGNORE): el duplicado
            # ya se descartó arriba y así un choque de llave única o un dato inválido es un error
            # visible en lugar de una advertencia que MySQL se traga.
            valores = (
                venta.id_venta,
                venta.sku,
                venta.producto,
                venta.stock_bodega,
                venta.precio,
                venta.fecha,
                venta.nombreComprador,
                venta.otros,
                venta.plataforma,
                venta.usuario,
                venta.condicion_pago, # Nuevo campo
                saldo_inicial         # Nuevo campo calculado
            )
            cursor.execute(SQL_INSERT_VENTA, valores)

            # C. Aplicar el descuento al inventario, solo ahora que sabemos que la venta es nueva.
            # La condición stock_bodega >= %s es una segunda barrera de seguridad ante carreras.
            cursor.execute(SQL_RESTAR_STOCK, (venta.stock_bodega, venta.sku, venta.stock_bodega))

            if cursor.rowcount == 0:
                connection.rollback()
                raise HTTPException(status_code=409, detail="Stock insuficiente al confirmar la venta, intente nuevamente")

            # D. Confirmar cambios
            connection.commit()

            mov_reg.registrar_movimiento(
                venta.usuario,
                f"Registró venta para SKU '{venta.sku}'",
                "Ventas"
            )

            comisiones.registrar_comisiones_seguro(
                venta.id_venta, venta.usuario, venta.nombreComprador, venta.plataforma, venta.fecha,
                [{"sku": venta.sku, "producto": venta.producto, "cantidad": venta.stock_bodega, "precio": venta.precio}],
            )

            asyncio.create_task(send_telegram_alert(
                f"🔄 <b>Venta Registrada</b>\n\n"
                f"• <b>ID Venta:</b> {venta.id_venta}\n"
                f"• <b>Usuario:</b> {html.escape(venta.usuario)}\n"
                f"• <b>SKU:</b> {html.escape(venta.sku)}\n"
                f"• <b>Producto:</b> {html.escape(venta.producto)}\n"
                f"• <b>Cantidad:</b> {venta.stock_bodega}\n"
                f"• <b>Precio Unitario:</b> ${venta.precio:,.2f}\n"
                f"• <b>Total:</b> ${total_operacion:,.2f}\n"                
                f"• <b>Nombre Comprador:</b> {html.escape(venta.nombreComprador)}\n"
                f"• <b>Otros:</b> {html.escape(venta.otros)}\n"
                f"• <b>Plataforma:</b> {html.escape(venta.plataforma)}\n"
                f"• <b>Fecha:</b> {html.escape(venta.fecha)}\n"
                f"• <b>Usuario Registro:</b> {html.escape(venta.usuario)}\n"
                f"• <b>Condición de Pago:</b> {html.escape(venta.condicion_pago)}\n"
                f"• <b>Saldo Inicial:</b> ${saldo_inicial:,.2f}\n"
                f"• <b>Saldo Pendiente:</b> ${saldo_inicial:,.2f}"
            ))

            return {
                "message": "Venta aplicada exitosamente", 
                "sku": venta.sku, 
                "nuevo_stock": resultado['stock_bodega'] - venta.stock_bodega,
                "saldo_pendiente": saldo_inicial
            }

    except mysql.connector.Error as err:
        connection.rollback()
        print(f"Error SQL: {err}")
        if es_conflicto_de_venta(err):
            raise HTTPException(status_code=409, detail=mensaje_venta_duplicada(venta.id_venta))
        raise HTTPException(status_code=500, detail=f"Error en base de datos: {err}")

    finally:
        if connection.is_connected():
            connection.close()


class ItemVentaSchema(BaseModel):
    sku: str = Field(min_length=1)
    producto: str
    cantidad: int = Field(gt=0)
    precio: float = Field(ge=0)


class VentaCompletaSchema(BaseModel):
    """Venta del portal con todas sus partidas: se registra completa o no se registra."""
    id_venta: int
    fecha: str
    nombreComprador: str
    otros: str
    plataforma: str
    usuario: str
    condicion_pago: str
    cotizacion: Optional[str] = None  # folio de la cotización cargada en el formulario, si la hubo
    items: List[ItemVentaSchema] = Field(min_length=1)

    @field_validator("items")
    @classmethod
    def skus_sin_repetir(cls, items):
        vistos = set()
        for item in items:
            if item.sku in vistos:
                raise ValueError(f"el SKU '{item.sku}' viene repetido; junta las cantidades en una sola partida")
            vistos.add(item.sku)
        return items


@router.post("/ventas/registrar")
async def registrar_venta_completa(venta: VentaCompletaSchema):
    """
    Registra una venta del portal con todas sus partidas en una sola transacción.
    Primero valida que id_ventas no exista (409 si existe); luego valida stock de
    cada SKU, inserta una fila por partida y descuenta inventario. Si algo falla
    no queda nada registrado.
    """
    es_credito = venta.condicion_pago == "CREDITO"
    # Los SKU se bloquean siempre en el mismo orden para que dos ventas que comparten
    # productos no se bloqueen mutuamente (deadlock).
    items_por_sku = sorted(venta.items, key=lambda i: i.sku)
    stock_previo = {}

    connection = get_db_connection()
    try:
        with connection.cursor(dictionary=True) as cursor:

            # A. El id de venta no debe existir.
            if existe_id_venta(cursor, venta.id_venta):
                connection.rollback()
                raise HTTPException(status_code=409, detail=mensaje_venta_duplicada(venta.id_venta))

            # B. Verificar stock de cada SKU bloqueando su fila.
            for item in items_por_sku:
                cursor.execute("SELECT stock_bodega FROM productos WHERE sku = %s FOR UPDATE", (item.sku,))
                fila = cursor.fetchone()
                if not fila:
                    connection.rollback()
                    raise HTTPException(status_code=404, detail=f"Producto '{item.sku}' no encontrado")
                if fila["stock_bodega"] < item.cantidad:
                    connection.rollback()
                    raise HTTPException(
                        status_code=400,
                        detail=f"Stock insuficiente para '{item.sku}'. Solo hay {fila['stock_bodega']}",
                    )
                stock_previo[item.sku] = fila["stock_bodega"]

            # C. Una fila por partida, en el orden del carrito. Igual que en /producto/venta,
            # a crédito el saldo inicial de cada partida es su total.
            for item in venta.items:
                cursor.execute(SQL_INSERT_VENTA, (
                    venta.id_venta,
                    item.sku,
                    item.producto,
                    item.cantidad,
                    item.precio,
                    venta.fecha,
                    venta.nombreComprador,
                    venta.otros,
                    venta.plataforma,
                    venta.usuario,
                    venta.condicion_pago,
                    item.precio * item.cantidad if es_credito else 0.00,
                ))

            # D. Descontar inventario; stock_bodega >= %s es la segunda barrera ante carreras.
            for item in items_por_sku:
                cursor.execute(SQL_RESTAR_STOCK, (item.cantidad, item.sku, item.cantidad))
                if cursor.rowcount == 0:
                    connection.rollback()
                    raise HTTPException(
                        status_code=409,
                        detail=f"Stock insuficiente al confirmar '{item.sku}', intente nuevamente",
                    )

            connection.commit()

    except mysql.connector.Error as err:
        connection.rollback()
        print(f"Error SQL: {err}")
        if es_conflicto_de_venta(err):
            raise HTTPException(status_code=409, detail=mensaje_venta_duplicada(venta.id_venta))
        raise HTTPException(status_code=500, detail=f"Error en base de datos: {err}")

    finally:
        if connection.is_connected():
            connection.close()

    # E. La venta ya quedó confirmada: un fallo en la bitácora no debe responder
    # error y provocar que el usuario la vuelva a capturar.
    total_operacion = sum(i.precio * i.cantidad for i in venta.items)
    saldo_inicial = total_operacion if es_credito else 0.00
    for item in venta.items:
        try:
            mov_reg.registrar_movimiento(venta.usuario, f"Registró venta para SKU '{item.sku}'", "Ventas")
        except mysql.connector.Error as err:
            print(f"Venta {venta.id_venta} registrada, pero falló la bitácora de '{item.sku}': {err}")

    comisiones.registrar_comisiones_seguro(
        venta.id_venta, venta.usuario, venta.nombreComprador, venta.plataforma, venta.fecha,
        [{"sku": i.sku, "producto": i.producto, "cantidad": i.cantidad, "precio": i.precio} for i in venta.items],
        cotizacion=venta.cotizacion,
    )

    partidas = "\n".join(
        f"   - {html.escape(i.sku)} · {html.escape(i.producto)} × {i.cantidad} @ ${i.precio:,.2f}"
        for i in venta.items
    )
    asyncio.create_task(send_telegram_alert(
        f"🔄 <b>Venta Registrada</b>\n\n"
        f"• <b>ID Venta:</b> {venta.id_venta}\n"
        f"• <b>Usuario:</b> {html.escape(venta.usuario)}\n"
        f"• <b>Partidas:</b>\n{partidas}\n"
        f"• <b>Total:</b> ${total_operacion:,.2f}\n"
        f"• <b>Nombre Comprador:</b> {html.escape(venta.nombreComprador)}\n"
        f"• <b>Otros:</b> {html.escape(venta.otros)}\n"
        f"• <b>Plataforma:</b> {html.escape(venta.plataforma)}\n"
        f"• <b>Fecha:</b> {html.escape(venta.fecha)}\n"
        f"• <b>Condición de Pago:</b> {html.escape(venta.condicion_pago)}\n"
        f"• <b>Saldo Pendiente:</b> ${saldo_inicial:,.2f}"
    ))

    return {
        "message": "Venta registrada exitosamente",
        "id_venta": venta.id_venta,
        "partidas": len(venta.items),
        "total": total_operacion,
        "saldo_pendiente": saldo_inicial,
        "nuevo_stock": {i.sku: stock_previo[i.sku] - i.cantidad for i in venta.items},
    }