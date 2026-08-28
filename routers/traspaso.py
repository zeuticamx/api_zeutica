import mysql.connector, os, mov_reg, html, asyncio
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from typing import List
from dotenv import load_dotenv
from servicios.telegram.notificacion import send_telegram_alert

router =APIRouter(tags=["/traspasos"],responses={404: {"Mensaje":"No encontrado"}})
load_dotenv()

# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

class traspaso(BaseModel): # molde para recibir informacion de traspaso
    sku: str
    stock_bodega: int

class LoteTraspaso(BaseModel):
    usuario: str
    movimientos: List[traspaso]
    almacen: str

@router.post("/traspaso")
async def traspaso_multiple(lote: LoteTraspaso):
    """
    Realiza traspaso de stock entre stock_bodega y stock_full.
    """
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    
    try:
        # Iniciamos el proceso para todos los items
        for item in lote.movimientos:
            # A. Verificar stock
            cursor.execute("SELECT stock_bodega FROM productos WHERE sku = %s", (item.sku,))
            res = cursor.fetchone()
            
            if not res or res['stock_bodega'] < item.stock_bodega:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Error en SKU {item.sku}: Stock insuficiente o no existe."
                )

            # B. Actualización doble: Resta de 'cantidad', suma a 'full'
            sql_update = """
                UPDATE productos 
                SET stock_bodega = stock_bodega - %s, 
                    stock_full = stock_full + %s 
                WHERE sku = %s
            """
            cursor.execute(sql_update, (item.stock_bodega,item.stock_bodega, item.sku))

            # C. Historial
            cursor.execute(
                "INSERT INTO stock_actual (sku, cantidad, almacen, usuario) VALUES (%s, %s, %s, %s)",
                (item.sku, item.stock_bodega,lote.almacen, lote.usuario)
            )

        # D. Si TODO salió bien, guardamos cambios en MySQL
        connection.commit()

        mov_reg.registrar_movimiento(lote.usuario, f"Realizó traspaso de full {chr(10).join(f'• SKU: {s.sku}, Cantidad: {s.stock_bodega}' for s in lote.movimientos)} items", "Traspasos")

        # Enviamos notificación a Telegram
        message = (
            f"🔄 <b>Traspaso de Stock</b>\n\n"
            f"• <b>Usuario:</b> {html.escape(lote.usuario)}\n"
            f"• <b>Almacén:</b> {html.escape(lote.almacen)}\n"
            f"• <b>Movimientos:</b> \n{chr(10).join(f'• SKU: {s.sku}, Cantidad: {s.stock_bodega}' for s in lote.movimientos)}\n"
        )
        asyncio.create_task(send_telegram_alert(message))

        return {"status": "success", "mensaje": f"{len(lote.movimientos)} movimientos procesados"}

    except Exception as e:
        connection.rollback() # Si uno falla, ninguno se guarda (mantiene integridad)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        connection.close()

@router.get("/traspasos/reporte") # Endpoint para consultar traspasos realizados.
async def consulta_traspasos():
    """
    Consulta los traspasos registrados en DB.
    """
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    sql = ("SELECT sku, cantidad, almacen, fecha_registro FROM stock_actual ORDER BY fecha_registro DESC LIMIT 100")

    try:
        cursor.execute(sql)
        tras = cursor.fetchall()

        if not tras:
            raise HTTPException(status_code=404, detail="No se han encontrado registro de traspasos")
        
        return tras
    
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")
    
    finally:
        cursor.close()
        connection.close()

@router.post("/traspaso/clean")
async def traspaso_multiple(lote: LoteTraspaso):
    """
    Realiza traspaso stock entre stock_bodega a stock_clean.
    """
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    
    try:
        # Iniciamos el proceso para todos los items
        for item in lote.movimientos:
            # A. Verificar stock
            cursor.execute("SELECT stock_bodega FROM productos WHERE sku = %s", (item.sku,))
            res = cursor.fetchone()
            
            if not res or res['stock_bodega'] < item.stock_bodega:
                raise HTTPException(
                    status_code=400, 
                    detail=f"Error en SKU {item.sku}: Stock insuficiente o no existe."
                )

            # B. Resta de bodega y suma a clean — COALESCE por si clean trae NULL
            sql_update = """
                UPDATE productos
                SET stock_bodega = stock_bodega - %s,
                    stock_clean = COALESCE(stock_clean, 0) + %s
                WHERE sku = %s
            """
            cursor.execute(sql_update, (item.stock_bodega, item.stock_bodega, item.sku))

            # C. Historial
            cursor.execute(
                "INSERT INTO stock_actual (sku, cantidad, almacen, usuario) VALUES (%s, %s, %s, %s)",
                (item.sku, item.stock_bodega, lote.almacen, lote.usuario)
            )

        # D. Si TODO salió bien, guardamos cambios en MySQL
        connection.commit()

        mov_reg.registrar_movimiento(lote.usuario, f"Realizó traspaso a clean de {chr(10).join(f'• SKU: {s.sku}, Cantidad: {s.stock_bodega}' for s in lote.movimientos)} items", "Traspasos")

        # Enviamos notificación a Telegram
        message = (
            f"🔄 <b>Traspaso de Stock a Clean</b>\n\n"
            f"• <b>Usuario:</b> {html.escape(lote.usuario)}\n"
            f"• <b>Almacén:</b> {html.escape(lote.almacen)}\n"
            f"• <b>Movimientos:</b> \n{chr(10).join(f'• SKU: {s.sku}, Cantidad: {s.stock_bodega}' for s in lote.movimientos)}\n"
        )
        asyncio.create_task(send_telegram_alert(message))

        return {"status": "success", "mensaje": f"{len(lote.movimientos)} movimientos procesados"}

    except Exception as e:
        connection.rollback() # Si uno falla, ninguno se guarda (mantiene integridad)
        print(f"Error en traspaso a clean: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        connection.close()