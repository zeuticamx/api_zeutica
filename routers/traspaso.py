import mysql.connector, os, mov_reg, html, asyncio
from pydantic import BaseModel, Field
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
    sku: str = Field(min_length=1)
    stock_bodega: int = Field(gt=0)

class LoteTraspaso(BaseModel):
    usuario: str = Field(min_length=1)
    movimientos: List[traspaso] = Field(min_length=1)
    #almacen: str


def _validar_lote(lote: LoteTraspaso):
    """Rechaza SKUs repetidos: obliga a consolidar cantidades en una sola partida."""
    vistos = set()
    for item in lote.movimientos:
        if item.sku in vistos:
            raise HTTPException(
                status_code=422,
                detail=f"el SKU '{item.sku}' viene repetido; junta las cantidades en una sola partida",
            )
        vistos.add(item.sku)


def _insert_stock_actual(cursor, sku: str, cantidad: int, usuario: str, almacen: str):
    """Historial con almacén para auditoría. Tolera esquemas sin columna almacen."""
    try:
        cursor.execute(
            "INSERT INTO stock_actual (sku, cantidad, usuario, almacen) VALUES (%s, %s, %s, %s)",
            (sku, cantidad, usuario, almacen)
        )
    except mysql.connector.Error as err:
        # 1054 = columna desconocida (BD vieja sin almacen): reintento sin ella.
        if getattr(err, "errno", None) != 1054:
            raise
        cursor.execute(
            "INSERT INTO stock_actual (sku, cantidad, usuario) VALUES (%s, %s, %s)",
            (sku, cantidad, usuario)
        )

@router.post("/traspaso")
async def traspaso_multiple(lote: LoteTraspaso):
    """
    Baja de stock_bodega (almacén FULL eliminado). Resta sin destino a propósito;
    cada movimiento queda en stock_actual con almacen='BAJA' para auditoría.
    """
    _validar_lote(lote)
    # Orden fijo para evitar deadlocks entre lotes que comparten SKUs.
    items = sorted(lote.movimientos, key=lambda i: i.sku)
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        # Iniciamos el proceso para todos los items
        for item in items:
            # A. Verificar stock bloqueando la fila.
            cursor.execute("SELECT stock_bodega FROM productos WHERE sku = %s FOR UPDATE", (item.sku,))
            res = cursor.fetchone()

            if not res:
                connection.rollback()
                raise HTTPException(
                    status_code=404,
                    detail=f"Error en SKU {item.sku}: no existe."
                )
            disponible = res['stock_bodega'] if res['stock_bodega'] is not None else 0
            if disponible < item.stock_bodega:
                connection.rollback()
                raise HTTPException(
                    status_code=400,
                    detail=f"Error en SKU {item.sku}: Stock insuficiente. Disponible: {disponible}."
                )

            # B. Baja: resta de bodega con guard ante carreras.
            sql_update = """
                UPDATE productos
                SET stock_bodega = stock_bodega - %s
                WHERE sku = %s AND stock_bodega >= %s
            """
            cursor.execute(sql_update, (item.stock_bodega, item.sku, item.stock_bodega))
            if cursor.rowcount == 0:
                connection.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=f"Error en SKU {item.sku}: Stock insuficiente al confirmar, intente nuevamente."
                )

            # C. Historial con anterior/nuevo para conciliar diferencias.
            _insert_stock_actual(cursor, item.sku, -item.stock_bodega, lote.usuario, "BAJA")

        # D. Si TODO salió bien, guardamos cambios en MySQL
        connection.commit()

        detalle = ", ".join(f"{s.sku} x{s.stock_bodega}" for s in lote.movimientos)
        try:
            mov_reg.registrar_movimiento(lote.usuario, f"Realizó baja de bodega: {detalle} (almacen BAJA)", "Traspasos")
        except mysql.connector.Error as err:
            print(f"Baja registrada, pero falló la bitácora: {err}")

        # Enviamos notificación a Telegram
        message = (
            f"🔄 <b>Baja de Stock (ex-FULL)</b>\n\n"
            f"• <b>Usuario:</b> {html.escape(lote.usuario)}\n"
            f"• <b>Almacén:</b> BAJA\n"
            f"• <b>Movimientos:</b> \n{chr(10).join(f'• SKU: {s.sku}, Cantidad: {s.stock_bodega}' for s in lote.movimientos)}\n"
        )
        asyncio.create_task(send_telegram_alert(message))

        return {"status": "success", "mensaje": f"{len(lote.movimientos)} movimientos procesados"}

    except HTTPException:
        try:
            connection.rollback()
        except Exception:
            pass
        raise
    except mysql.connector.Error as e:
        try:
            connection.rollback()
        except Exception:
            pass
        print(f"Error en traspaso: {e}")
        raise HTTPException(status_code=500, detail="Error de base de datos en traspaso")
    except Exception as e:
        try:
            connection.rollback() # Si uno falla, ninguno se guarda (mantiene integridad)
        except Exception:
            pass
        print(f"Error en traspaso: {e}")
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
async def traspaso_multiple_clean(lote: LoteTraspaso):
    """
    Realiza traspaso stock entre stock_bodega a stock_clean.
    """
    _validar_lote(lote)
    items = sorted(lote.movimientos, key=lambda i: i.sku)
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:
        # Iniciamos el proceso para todos los items
        for item in items:
            # A. Verificar stock bloqueando la fila.
            cursor.execute("SELECT stock_bodega FROM productos WHERE sku = %s FOR UPDATE", (item.sku,))
            res = cursor.fetchone()

            if not res:
                connection.rollback()
                raise HTTPException(
                    status_code=404,
                    detail=f"Error en SKU {item.sku}: no existe."
                )
            disponible = res['stock_bodega'] if res['stock_bodega'] is not None else 0
            if disponible < item.stock_bodega:
                connection.rollback()
                raise HTTPException(
                    status_code=400,
                    detail=f"Error en SKU {item.sku}: Stock insuficiente. Disponible: {disponible}."
                )

            # B. Resta de bodega y suma a clean — COALESCE por si clean trae NULL
            sql_update = """
                UPDATE productos
                SET stock_bodega = stock_bodega - %s,
                    stock_clean = COALESCE(stock_clean, 0) + %s
                WHERE sku = %s AND stock_bodega >= %s
            """
            cursor.execute(sql_update, (item.stock_bodega, item.stock_bodega, item.sku, item.stock_bodega))
            if cursor.rowcount == 0:
                connection.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=f"Error en SKU {item.sku}: Stock insuficiente al confirmar, intente nuevamente."
                )

            # C. Historial
            _insert_stock_actual(cursor, item.sku, item.stock_bodega, lote.usuario, "CLEAN")

        # D. Si TODO salió bien, guardamos cambios en MySQL
        connection.commit()

        detalle = ", ".join(f"{s.sku} x{s.stock_bodega}" for s in lote.movimientos)
        try:
            mov_reg.registrar_movimiento(lote.usuario, f"Realizó traspaso a clean: {detalle}", "Traspasos")
        except mysql.connector.Error as err:
            print(f"Traspaso a clean registrado, pero falló la bitácora: {err}")

        # Enviamos notificación a Telegram
        message = (
            f"🔄 <b>Traspaso de Stock a Clean</b>\n\n"
            f"• <b>Usuario:</b> {html.escape(lote.usuario)}\n"
            f"• <b>Almacén:</b> A CLEAN\n"
            f"• <b>Movimientos:</b> \n{chr(10).join(f'• SKU: {s.sku}, Cantidad: {s.stock_bodega}' for s in lote.movimientos)}\n"
        )
        asyncio.create_task(send_telegram_alert(message))

        return {"status": "success", "mensaje": f"{len(lote.movimientos)} movimientos procesados"}

    except HTTPException:
        try:
            connection.rollback()
        except Exception:
            pass
        raise
    except mysql.connector.Error as e:
        try:
            connection.rollback()
        except Exception:
            pass
        print(f"Error en traspaso a clean: {e}")
        raise HTTPException(status_code=500, detail="Error de base de datos en traspaso a clean")
    except Exception as e:
        try:
            connection.rollback() # Si uno falla, ninguno se guarda (mantiene integridad)
        except Exception:
            pass
        print(f"Error en traspaso a clean: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cursor.close()
        connection.close()