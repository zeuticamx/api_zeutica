# Job vencimientos de cotizaciones (migración del workflow n8n).
# Diario 9:30 CDMX + disparo manual. Solo lectura en cotizaciones.
#
# Mejoras vs n8n: incluye vencidas (el mensaje las anunciaba pero el SQL no
# las traía), UN mensaje agrupado (no uno por cotización) y persiste en
# `notificaciones` además de Telegram. Sin dedup: es lista de acción diaria.
import os
from datetime import date, datetime, timedelta
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

def _fecha(d):
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    try:
        return datetime.fromisoformat(str(d)[:10]).date()
    except ValueError:
        return None


def partir(ordenes: list, hoy: date, dias: int = 3) -> tuple:
    """(vencidas, proximas): sin factura. Vencidas = fecha < hoy; próximas = hoy..hoy+días."""
    vencidas, proximas = [], []
    for o in ordenes:
        if o.get("relacion_factura"):
            continue
        f = _fecha(o.get("fecha_vencimiento"))
        if not f:
            continue
        if f < hoy:
            vencidas.append(o)
        elif f <= hoy + timedelta(days=dias):
            proximas.append(o)
    key = lambda o: str(_fecha(o.get("fecha_vencimiento")))
    return sorted(vencidas, key=key), sorted(proximas, key=key)


def linea(o) -> str:
    import html
    esc = lambda s: html.escape(str(s or ""), quote=False)
    f = _fecha(o.get("fecha_vencimiento"))
    ddmm = f"{f.day:02d}/{f.month:02d}" if f else "?"
    return (f"🔹 <code>{esc(o.get('codigo_cotizacion'))}</code> · {esc(o.get('empresa'))} · "
            f"vence {ddmm}")


def build_mensaje(vencidas: list, proximas: list) -> list:
    out = ["📋 <b>Cotizaciones por vencer / vencidas</b>"]
    if vencidas:
        out.append(f"\n🚨 <b>Vencidas sin factura ({len(vencidas)}):</b>")
        out += [linea(o) for o in vencidas]
    if proximas:
        out.append(f"\n📦 <b>Vencen en 3 días ({len(proximas)}):</b>")
        out += [linea(o) for o in proximas]
    if not vencidas and not proximas:
        out.append("\nSin cotizaciones vencidas ni próximas a vencer sin factura.")
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

class CotizJobError(Exception):
    pass


def cargar_cotizaciones() -> list:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            """SELECT codigo_cotizacion, empresa, fecha_vencimiento, relacion_factura FROM cotizaciones
               WHERE fecha_vencimiento < DATE_ADD(CURDATE(), INTERVAL 4 DAY)
                 AND (relacion_factura IS NULL OR relacion_factura = '')
               ORDER BY fecha_vencimiento""")
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


# ---------- Orquestador ----------

async def run_job(dry_run: bool | None = None, motivo: str = "manual", telegram: bool = True) -> dict:
    import mov_reg
    from servicios.telegram.notificacion import send_telegram_alert
    hoy = datetime.now(CDMX).date()
    resumen = {"motivo": motivo, "inicio": datetime.now(CDMX).isoformat()}
    try:
        vencidas, proximas = partir(cargar_cotizaciones(), hoy)
        stats = {"vencidas": len(vencidas), "proximas": len(proximas)}
        if vencidas or proximas:
            for chunk in build_mensaje(vencidas, proximas):
                if telegram:
                    await send_telegram_alert(chunk)
            try:
                import notificaciones_service
                await notificaciones_service.crear_y_notificar_todos(
                    "Cotizaciones por vencer",
                    f"{len(vencidas)} vencidas y {len(proximas)} por vencer en 3 días, sin factura.",
                    "warn" if vencidas else "info")
            except Exception as err:
                print(f"Job cotizaciones: falló persistir notificaciones: {err}")
        else:
            stats["sin_pendientes"] = True
        try:
            mov_reg.registrar_movimiento(
                "job-cotizaciones", f"Vencimientos {motivo}: {stats['vencidas']} vencidas, "
                f"{stats['proximas']} próximas", "Cotizaciones")
        except Exception as err:
            print(f"Job cotizaciones: falló bitácora: {err}")
        resumen.update({"estado": "ok", "stats": stats})
    except Exception as err:
        resumen.update({"estado": "error", "error": str(err)})
        print(f"Job cotizaciones error: {err}")
        if telegram:
            await send_telegram_alert("⚠️ <b>Job cotizaciones falló</b>")
    resumen["fin"] = datetime.now(CDMX).isoformat()
    LAST_RUN.clear()
    LAST_RUN.update(resumen)
    try:
        from routers.sofi_notificaciones import manager as _ws_manager
        await _ws_manager.broadcast({"tipo": "job", "job": "cotizaciones", "estado": resumen.get("estado"),
                                     "stats": resumen.get("stats"), "fin": resumen.get("fin")})
    except Exception as err:
        print(f"Job cotizaciones: no se pudo avisar por WS: {err}")
    return resumen
