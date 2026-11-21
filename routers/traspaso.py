import mysql.connector, os, mov_reg, html, asyncio
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException
from typing import List
from dotenv import load_dotenv
from servicios.telegram.notificacion import send_telegram_alert
from errores_db import detalle_error_db
from permisos import usuario_autenticado
from pendientes_tiempo_logica import ahora_mx
import traspaso_reversion_logica as rev

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


class RevertirModel(BaseModel):
    motivo: str | None = Field(default=None, max_length=500)


# Registro de reversiones: una fila por traspaso revertido (UNIQUE evita revertir dos veces).
SQL_TABLA_REVERSIONES = """
    CREATE TABLE IF NOT EXISTS traspasos_reversiones (
        id INT AUTO_INCREMENT PRIMARY KEY,
        stock_actual_id INT NOT NULL,
        sku VARCHAR(100) NOT NULL,
        cantidad INT NOT NULL,
        almacen VARCHAR(150) NULL,
        usuario_original VARCHAR(100) NULL,
        fecha_original DATETIME NULL,
        usuario VARCHAR(100) NOT NULL,
        fecha DATETIME NOT NULL,
        motivo VARCHAR(500) NULL,
        UNIQUE KEY uq_trev_stock_actual (stock_actual_id),
        INDEX idx_trev_fecha (fecha)
    )
"""


def crear_tabla_reversiones():
    """Crea traspasos_reversiones si falta (se llama en el lifespan)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(SQL_TABLA_REVERSIONES)
        conn.commit()
    finally:
        cursor.close()
        conn.close()


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
            cursor.execute("SELECT stock_bodega FROM productos WHERE sku = %s AND eliminado = 0 FOR UPDATE", (item.sku,))
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
        raise HTTPException(status_code=500, detail=detalle_error_db(e, "Error interno del servidor"))
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

    # `ahora` es el reloj de la BD (el mismo que escribe fecha_registro): no se mezcla con la hora de Python.
    sql = ("SELECT s.id, s.sku, s.cantidad, s.almacen, s.usuario, s.fecha_registro, "
           "(r.id IS NOT NULL) AS revertido, NOW() AS ahora "
           "FROM stock_actual s LEFT JOIN traspasos_reversiones r ON r.stock_actual_id = s.id "
           "ORDER BY s.fecha_registro DESC, s.id DESC LIMIT 100")

    try:
        cursor.execute(sql)
        tras = cursor.fetchall()

        if not tras:
            raise HTTPException(status_code=404, detail="No se han encontrado registro de traspasos")

        dias = rev.dias_reversion()
        salida = []
        for t in tras:
            ahora = t.pop("ahora", None)
            t["revertido"] = bool(t.get("revertido"))
            t["revertible"] = rev.es_revertible(t.get("almacen"), t.get("cantidad"), t.get("fecha_registro"),
                                                ahora, t["revertido"], dias)
            salida.append(t)
        return salida

    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))
    
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
            cursor.execute("SELECT stock_bodega FROM productos WHERE sku = %s AND eliminado = 0 FOR UPDATE", (item.sku,))
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
        raise HTTPException(status_code=500, detail=detalle_error_db(e, "Error interno del servidor"))
    finally:
        cursor.close()
        connection.close()

# ---- Reversión de traspasos (Traspaso FULL = baja, y Clean) ----

@router.post("/traspasos/{traspaso_id}/revertir")
async def revertir_traspaso(traspaso_id: int, datos: RevertirModel | None = None,
                            usuario: str = Depends(usuario_autenticado)):
    """Revierte un traspaso (una fila de stock_actual): devuelve las piezas a bodega.

    Cualquier sesión puede hacerlo; el usuario sale del token. Solo `BAJA` y `CLEAN`, dentro de
    TRASPASO_REVERSION_DIAS (15) según el reloj de la BD; fuera de plazo, ya revertido o no
    revertible → 409. No se borra nada: queda el registro en traspasos_reversiones y una fila
    compensatoria (REV-BAJA / REV-CLEAN) en stock_actual. Una sola transacción con la fila del
    movimiento y la del producto bloqueadas.
    """
    motivo = ((datos.motivo if datos else None) or "").strip() or None
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, sku, cantidad, almacen, usuario, fecha_registro, NOW() AS ahora "
            "FROM stock_actual WHERE id = %s FOR UPDATE", (traspaso_id,))
        mov = cursor.fetchone()
        if not mov:
            raise HTTPException(status_code=404, detail="Traspaso no encontrado")

        plan = rev.plan_reversion(mov["almacen"], mov["cantidad"])
        if plan is None:
            raise HTTPException(status_code=409, detail=(
                "Este movimiento no se puede revertir (solo bajas a FULL y traspasos a Clean; "
                "las reversiones no se revierten)"))
        dias = rev.dias_reversion()
        if not rev.dentro_de_plazo(mov["fecha_registro"], mov["ahora"], dias):
            raise HTTPException(status_code=409, detail=f"Solo se pueden revertir traspasos de los últimos {dias} días")

        cursor.execute("SELECT id FROM traspasos_reversiones WHERE stock_actual_id = %s", (traspaso_id,))
        if cursor.fetchone():
            raise HTTPException(status_code=409, detail="Este traspaso ya fue revertido")

        cursor.execute("SELECT stock_bodega, stock_clean, eliminado FROM productos WHERE sku = %s FOR UPDATE",
                       (mov["sku"],))
        prod = cursor.fetchone()
        if not prod:
            raise HTTPException(status_code=404, detail=f"El SKU {mov['sku']} no existe")
        if prod["eliminado"]:
            raise HTTPException(status_code=409, detail=f"El SKU {mov['sku']} está eliminado; pide a gerencia que lo restaure antes de revertir")

        if plan["restar_clean"]:
            cursor.execute(
                "UPDATE productos SET stock_bodega = COALESCE(stock_bodega, 0) + %s, "
                "stock_clean = COALESCE(stock_clean, 0) - %s WHERE sku = %s AND COALESCE(stock_clean, 0) >= %s",
                (plan["sumar_bodega"], plan["restar_clean"], mov["sku"], plan["restar_clean"]))
            if cursor.rowcount == 0:
                raise HTTPException(status_code=409, detail=(
                    f"No se puede revertir: Clean ya no tiene {plan['piezas']} piezas de {mov['sku']} "
                    f"(disponible: {prod['stock_clean'] or 0})"))
        else:
            cursor.execute(
                "UPDATE productos SET stock_bodega = COALESCE(stock_bodega, 0) + %s WHERE sku = %s",
                (plan["sumar_bodega"], mov["sku"]))

        try:
            cursor.execute(
                "INSERT INTO traspasos_reversiones "
                "(stock_actual_id, sku, cantidad, almacen, usuario_original, fecha_original, usuario, fecha, motivo) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (traspaso_id, mov["sku"], plan["piezas"], plan["tipo"], mov["usuario"], mov["fecha_registro"],
                 usuario, ahora_mx(), motivo))
        except mysql.connector.Error as err:
            if getattr(err, "errno", None) == 1062:   # UNIQUE: otra reversión ganó la carrera
                raise HTTPException(status_code=409, detail="Este traspaso ya fue revertido")
            raise
        _insert_stock_actual(cursor, mov["sku"], plan["cantidad_rev"], usuario, plan["almacen_rev"])
        conn.commit()

        destino = "Clean" if plan["tipo"] == "CLEAN" else "FULL (baja)"
        try:
            mov_reg.registrar_movimiento(
                usuario,
                f"Revirtió traspaso #{traspaso_id} a {destino}: {mov['sku']} x{plan['piezas']} "
                f"(hecho por {mov['usuario'] or 'desconocido'})" + (f". Motivo: {motivo}" if motivo else ""),
                "Traspasos")
        except Exception as err:
            print(f"Traspaso revertido, pero falló la bitácora: {err}")

        mensaje = (
            f"↩️ <b>Reversión de Traspaso</b>\n\n"
            f"• <b>Usuario:</b> {html.escape(usuario)}\n"
            f"• <b>Traspaso:</b> #{traspaso_id} a {destino}\n"
            f"• <b>SKU:</b> {html.escape(str(mov['sku']))}, Cantidad: {plan['piezas']}\n"
            + (f"• <b>Motivo:</b> {html.escape(motivo)}\n" if motivo else ""))
        asyncio.create_task(send_telegram_alert(mensaje))

        return {"mensaje": "Traspaso revertido", "id": traspaso_id, "sku": mov["sku"],
                "cantidad": plan["piezas"], "tipo": plan["tipo"]}
    except HTTPException:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    except mysql.connector.Error as err:
        try:
            conn.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))
    finally:
        cursor.close()
        conn.close()


@router.get("/traspasos/reversiones")
async def traspasos_reversiones():
    """Registro de reversiones (las 200 más recientes): quién revirtió, cuándo y qué traspaso era."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, stock_actual_id, sku, cantidad, almacen, usuario_original, fecha_original, "
            "usuario, fecha, motivo FROM traspasos_reversiones ORDER BY fecha DESC, id DESC LIMIT 200")
        filas = cursor.fetchall()
        return {"total": len(filas), "registros": filas}
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))
    finally:
        cursor.close()
        conn.close()
