import mysql.connector, html, asyncio
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from typing import Optional, List
import os
from dotenv import load_dotenv
from datetime import date
import mov_reg
from servicios.telegram.notificacion import send_telegram_alert

router =APIRouter(tags=["/cleanest"],responses={404: {"Mensaje":"No encontrado"}})
load_dotenv()

# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

class OrdenModel(BaseModel):
    numero_orden: str
    sku: str
    cantidad: int
    fecha_promesa: date
    status: str
    envio1: int
    envio2: int
    envio3: int

@router.post("/ordenes/{usuario}")
async def crear_orden(ordenes: List[OrdenModel], usuario: str):
    """
    Ingresa una o varias ordenes de Cleanest Choice para su debido tracking.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        sql = """
            INSERT INTO cleanestChoice (numero_orden, sku, cantidad, fecha_promesa, status, envio1, envio2, envio3)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        # Armo la lista de tuplas para el bulk insert
        val_list = [
            (o.numero_orden, o.sku, o.cantidad, o.fecha_promesa, o.status, o.envio1, o.envio2, o.envio3)
            for o in ordenes
        ]
        cursor.executemany(sql, val_list)
        conn.commit()

        # Registramos el movimiento en el historial de movimientos
        mov_reg.registrar_movimiento(usuario, f"Registró una orden para {ordenes[0].numero_orden} artículos", "Ordenes Cleanest Choice")

        # Enviamos notificación a Telegram
        numero_orden_safe = html.escape(str(ordenes[0].numero_orden))
        usuario_safe = html.escape(str(usuario))
        cantidad_safe = html.escape(str(len(ordenes)))

        message = (
            f"📋 <b>Nueva Orden Cleanest Choice Generada</b>\n\n"
            f"• <b>Código:</b> <code>{numero_orden_safe}</code>\n"
            f"• <b>Usuario:</b> {usuario_safe}\n"
            f"• <b>Cantidad:</b> {cantidad_safe}"
        )
        asyncio.create_task(send_telegram_alert(message))

        return {
            "msg": "Ordenes creadas",
            "cantidad": len(ordenes),
            "numeros_orden": [o.numero_orden for o in ordenes]
        }
    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error en BD: {err}")
        raise HTTPException(status_code=500, detail="Error al guardar las ordenes en BD")
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()

@router.get("/cleanest")
async def obtener_pedidos():
    """
    Dependencia para obtener las ordenes actuales.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("SELECT * FROM cleanestChoice ORDER BY id DESC")
        return cursor.fetchall()
    
    except mysql.connector.Error as err:
        print(f"Error en BD: {err}")
        raise HTTPException(status_code=500, detail="Error al obtener las órdenes")
    
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()

class EfirmaModel(BaseModel):
    numero_orden: str
    firma_base64: str
    fecha_firma: str
    usuario: str

@router.post("/efirma")
async def efirma(payload: EfirmaModel):
    """
    Dependencia para ingresar una firma en base64 a su debida orden.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        query = "UPDATE cleanestChoice SET firma_digital = %s, fecha_firma = %s, usuario = %s WHERE numero_orden = %s"
        cursor.execute(query, (payload.firma_base64,  payload.fecha_firma, payload.usuario, payload.numero_orden))
        conn.commit()

        # Registramos el movimiento en el historial de movimientos
        mov_reg.registrar_movimiento(payload.usuario, f"Registró una firma para la orden {payload.numero_orden}", "Firmas")

        # Enviamos notificación a Telegram
        numero_orden_safe = html.escape(str(payload.numero_orden))
        usuario_safe = html.escape(str(payload.usuario))

        message = (
            f"📋 <b>Firma En Envio Cleanest Registrada</b>\n\n"
            f"• <b>Código:</b> <code>{numero_orden_safe}</code>\n"
            f"• <b>Usuario:</b> {usuario_safe}"
        )
        asyncio.create_task(send_telegram_alert(message))

        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Ticket no encontrado")
        return {"msg": "Firma registrada", "ticket_id": payload.numero_orden}
    
    except mysql.connector.Error as err:
        print(f"Error en BD: {err}")

        raise HTTPException(status_code=500, detail="Error al guardar la firma")
    
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()

@router.get("/obtener-firma")
async def obtener_firma(numero_orden: str):
    """
    Dependencia para consultar firmas ya almacenadas en DB.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # Busco la firma de esa orden puntual
        cursor.execute(
            "SELECT firma_digital, fecha_firma FROM cleanestChoice WHERE numero_orden = %s",
            (numero_orden,)
        )
        res = cursor.fetchone()

        if not res:
            raise HTTPException(status_code=404, detail="Orden no encontrada")

        # Si no tiene firma todavía, lo digo claro
        if res["firma_digital"] is None:
            raise HTTPException(status_code=404, detail="La orden no tiene firma registrada")

        return {"numero_orden": numero_orden, "firma_digital": res["firma_digital"], "fecha_firma":res["fecha_firma"]}

    except mysql.connector.Error as err:
        print(f"Error en BD: {err}")
        raise HTTPException(status_code=500, detail="Error al obtener la firma")
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


class OrdenUpdateModel(BaseModel):
    numero_orden: Optional[str] = None
    sku: Optional[str] = None
    cantidad: Optional[int] = None
    fecha_promesa: Optional[date] = None
    status: Optional[str] = None
    envio1: Optional[int] = None
    envio2: Optional[int] = None
    envio3: Optional[int] = None

@router.patch("/cleanest/{pedido_id}/{usuario}")
async def actualizar_pedido(pedido_id: int, payload: OrdenUpdateModel, usuario: str):
    """
    Dependencia para actualizar las ordenes en cuanto a envios de mercancia.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        campos = {k: v for k, v in payload.model_dump().items() if v is not None}

        if not campos:
            raise HTTPException(status_code=400, detail="No se enviaron campos para actualizar")
        
        set_clause = ", ".join(f"{k} = %s" for k in campos)
        valores = list(campos.values()) + [pedido_id]
        cursor.execute(f"UPDATE cleanestChoice SET {set_clause} WHERE id = %s", valores)
        conn.commit()

        # Registramos el movimiento en el historial de movimientos
        mov_reg.registrar_movimiento(usuario, f"Actualizó la orden {pedido_id}", "Ordenes Cleanest Choice")

        # Enviamos notificación a Telegram
        pedido_id_safe = html.escape(str(pedido_id))
        usuario_safe = html.escape(str(usuario))

        message = (
            f"📋 <b>Orden Cleanest Choice Actualizada</b>\n\n"
            f"• <b>Código:</b> <code>{pedido_id_safe}</code>\n"
            f"• <b>Usuario:</b> {usuario_safe}"
        )
        asyncio.create_task(send_telegram_alert(message))

        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Orden no encontrada")
        
        return {"msg": "Orden actualizada", "id": pedido_id}
    
    except mysql.connector.Error as err:
        print(f"Error en BD: {err}")

        raise HTTPException(status_code=500, detail="Error al actualizar la orden")
    
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


# Definimos un modelo para recibir los datos en JSON (Body)
class VentaSchema(BaseModel):
    id_venta: str
    sku: str
    producto: str
    stock_clean: int
    precio: float
    fecha: str
    nombreComprador: str
    otros: str
    plataforma: str
    usuario: str
    condicion_pago: str

@router.post("/cleanest/venta")
async def ingresar_venta(venta: VentaSchema):
    """
    Ingresa la venta de la orden correspondiente al terminar su tracking y ser cerrada.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # Traigo el stock actual antes de modificarlo
        cursor.execute("SELECT stock_clean FROM productos WHERE sku = %s", (venta.sku,))
        res_existe = cursor.fetchone()

        if not res_existe:
            raise HTTPException(status_code=404, detail="sku no encontrado")

        # Si stock_clean es NULL en BD, lo trato como 0
        stock_actual = res_existe["stock_clean"] if res_existe["stock_clean"] is not None else 0

        if stock_actual < venta.stock_clean:
            raise HTTPException(status_code=400, detail=f"Sku: {venta.sku} Stock insuficiente. Disponible: {stock_actual}")

        cursor.execute("UPDATE productos SET stock_clean = stock_clean - %s WHERE sku = %s", (venta.stock_clean, venta.sku))

        sql_insert = """
            INSERT INTO ventasRegistro
            (id_ventas, sku, producto, cantidad, precio, fecha, nombreComprador, otros, plataforma, usuario, condicion_pago, saldo_pendiente)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        valores = (
            venta.id_venta,
            venta.sku,
            venta.producto,
            venta.stock_clean,
            venta.precio,
            venta.fecha,
            venta.nombreComprador,
            venta.otros,
            venta.plataforma,
            venta.usuario,
            venta.condicion_pago,
            0
        )
        cursor.execute(sql_insert, valores)

        # Si insert no insertó nada, revierto el descuento de stock
        if cursor.rowcount == 0:
            conn.rollback()
            raise HTTPException(status_code=500, detail="No se registró la venta")

        # Enviamos notificación a Telegram
        sku_safe = html.escape(str(venta.sku))
        usuario_safe = html.escape(str(venta.usuario))
        cantidad_safe = html.escape(str(venta.stock_clean))

        message = (
            f"📋 <b>Nueva Venta Cleanest Choice Registrada</b>\n\n"
            f"• <b>Código:</b> <code>{sku_safe}</code>\n"
            f"• <b>Usuario:</b> {usuario_safe}\n"
            f"• <b>Cantidad:</b> {cantidad_safe}"
        )
        asyncio.create_task(send_telegram_alert(message))

        conn.commit()

        return {
            "message": "Venta aplicada exitosamente",
            "sku": venta.sku,
            "nuevo_stock": stock_actual - venta.stock_clean,
            "saldo_pendiente": 0
        }

    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error en BD: {err}")
        raise HTTPException(status_code=500, detail="Error al descontar stock")
    
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()