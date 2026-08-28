import mysql.connector, os, mov_reg, html, asyncio
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from servicios.telegram.notificacion import send_telegram_alert

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
async def consultar_ventas(f1: str, f2: str):
    """
    Consulta ventas por rango de fecha definido por frontend.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True) # Usamos dictionary=True para que devuelva claves como 'sku'
    
    query = "SELECT * FROM ventasRegistro WHERE DATE(fecha_registro) BETWEEN %s AND %s ORDER BY fecha_registro DESC"
    
    try:
        cursor.execute(query,(f1, f2))
        ventas = cursor.fetchall()
        
        if not ventas:
            raise HTTPException(status_code=404, detail="No se han encontrado registro de ventas")
            
        return ventas # FastAPI lo convierte automáticamente a JSON

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

    query = "SELECT id_ventas, sku, producto, cantidad, nombreComprador, saldo_pendiente, fecha, fecha_vencimiento FROM ventasRegistro WHERE saldo_pendiente > 0 "

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
    connection = get_db_connection()
    try:
        with connection.cursor(dictionary=True) as cursor:
            
            # A. Verificar stock (Se mantiene igual)
            sql_check = "SELECT stock_bodega FROM productos WHERE sku = %s"
            cursor.execute(sql_check, (venta.sku,))
            resultado = cursor.fetchone()

            if not resultado:
                raise HTTPException(status_code=404, detail="Producto no encontrado")
            
            if resultado['stock_bodega'] < venta.stock_bodega:
                raise HTTPException(status_code=400, detail=f"Stock insuficiente. Solo hay {resultado['stock_bodega']}")
        
            # B. Aplicar el descuento al inventario (Se mantiene igual)
            sql_restar = "UPDATE productos SET stock_bodega = stock_bodega - %s WHERE sku = %s" 
            cursor.execute(sql_restar, (venta.stock_bodega, venta.sku))

            # --- NUEVA LÓGICA DE CRÉDITO ---
            # Calculamos el saldo inicial. Si es CRÉDITO, el saldo es el total (precio * cantidad)
            # Si es CONTADO, el saldo es 0.
            total_operacion = venta.precio * venta.stock_bodega
            saldo_inicial = total_operacion if venta.condicion_pago == "CREDITO" else 0.00

            # C. Registrar venta en el historial (Actualizado con nuevas columnas)            
            sql_insert = """
                INSERT IGNORE INTO ventasRegistro 
                (id_ventas, sku, producto, cantidad, precio, fecha, nombreComprador, otros, plataforma, usuario, condicion_pago, saldo_pendiente) 
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            
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
            cursor.execute(sql_insert, valores)

            # D. Confirmar cambios
            connection.commit()

            mov_reg.registrar_movimiento(
                venta.usuario,
                f"Registró venta para SKU '{venta.sku}'",
                "Ventas"
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
        raise HTTPException(status_code=500, detail=f"Error en base de datos: {err}")
    
    finally:
        if connection.is_connected():
            connection.close()