# Job cotizaciones facturadas sin vender (migración del workflow n8n).
# Diario 13:00 CDMX + disparo manual. Solo lectura en cotizaciones.
#
# Mejora vs n8n: UN mensaje agrupado (no uno por cotización) y persiste en
# `notificaciones` además de Telegram. Sin dedup: al registrar la venta la
# fila sale sola del query (vendido=1).
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import mysql.connector
from dotenv import load_dotenv

load_dotenv()

CDMX = ZoneInfo("America/Mexico_City")

LAST_RUN: dict = {"estado": "nunca", "detalle": None}


def get_db_connection():
    return mysql.connector.connect(host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
                                   password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"))


# ---------- Lógica pura (testeable) ----------

def linea(o) -> str:
    import html
    esc = lambda s: html.escape(str(s or ""), quote=False)
    return (f"🔹 <code>{esc(o.get('codigo_cotizacion'))}</code> · {esc(o.get('empresa'))} · "
            f"${float(o.get('total') or 0):,.2f} · factura {esc(o.get('relacion_factura'))}")


def build_mensaje(filas: list) -> list:
    out = [f"🔴 <b>Registra la venta ({len(filas)})</b>\nCotizaciones con factura y sin venta:"]
    out += [linea(o) for o in filas]
    chunks, cur = [], ""
    for ln in out:
        if cur and len(cur) + 1 + len(ln) > 3800:
            chunks.append(cur)
            cur = ""
        cur += ("" if not cur else "\n") + ln
    if cur:
        chunks.append(cur)
    return chunks


# ---------- IO ----------

def cargar_cotizaciones_por_vender() -> list:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            """SELECT codigo_cotizacion, empresa, total, relacion_factura FROM cotizaciones
               WHERE vendido = 0 AND relacion_factura IS NOT NULL AND TRIM(relacion_factura) <> ''
               ORDER BY codigo_cotizacion""")
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


# ---------- Orquestador ----------

async def run_job(dry_run: bool | None = None, motivo: str = "manual", telegram: bool = True) -> dict:
    import mov_reg
    from servicios.telegram.notificacion import send_telegram_alert
    resumen = {"motivo": motivo, "inicio": datetime.now(CDMX).isoformat()}
    try:
        filas = cargar_cotizaciones_por_vender()
        stats = {"pendientes": len(filas)}
        if filas:
            for chunk in build_mensaje(filas):
                if telegram:
                    await send_telegram_alert(chunk)
            try:
                import notificaciones_service
                await notificaciones_service.crear_y_notificar_todos(
                    "Cotizaciones por vender",
                    f"{len(filas)} cotizaciones con factura y sin venta registrada.", "warn")
            except Exception as err:
                print(f"Job por-vender: falló persistir notificaciones: {err}")
        else:
            stats["sin_pendientes"] = True
        try:
            mov_reg.registrar_movimiento(
                "job-cotizaciones", f"Por vender {motivo}: {stats['pendientes']} pendientes", "Cotizaciones")
        except Exception as err:
            print(f"Job por-vender: falló bitácora: {err}")
        resumen.update({"estado": "ok", "stats": stats})
    except Exception as err:
        resumen.update({"estado": "error", "error": str(err)})
        print(f"Job por-vender error: {err}")
        if telegram:
            await send_telegram_alert("⚠️ <b>Job cotizaciones por vender falló</b>")
    resumen["fin"] = datetime.now(CDMX).isoformat()
    LAST_RUN.clear()
    LAST_RUN.update(resumen)
    try:
        from routers.sofi_notificaciones import manager as _ws_manager
        await _ws_manager.broadcast({"tipo": "job", "job": "cotizaciones-vender", "estado": resumen.get("estado"),
                                     "stats": resumen.get("stats"), "fin": resumen.get("fin")})
    except Exception as err:
        print(f"Job por-vender: no se pudo avisar por WS: {err}")
    return resumen
