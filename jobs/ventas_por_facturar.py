# Job ventas por facturar: ventas del portal marcadas "requiere factura" al cobrar
# (ventasRegistro.facturable = 1) que aún no tienen factura (facturada = 0).
# Lunes a viernes 10:45 CDMX + disparo manual. Solo lectura en ventasRegistro.
#
# Una venta tiene una fila por partida: se agrupa por id_ventas. Sin dedup: la
# venta sale sola del query al relacionar la factura en su cotización
# (POST /relacionFactura) o al marcarla en Reportes (PATCH /ventas/{id}/facturada).
# Las ventas de marketplaces (MeLi, Amazon, Cleanest) nunca entran: sus INSERT no
# ponen facturable y queda el default 0.
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import mysql.connector
from dotenv import load_dotenv

load_dotenv()

CDMX = ZoneInfo("America/Mexico_City")

LAST_RUN: dict = {"estado": "nunca", "detalle": None}


def get_db_connection():
    return mysql.connector.connect(host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
                                   password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"))


def crear_trigger(hora: str = "10:45"):
    """Lunes a viernes a la hora dada, en CDMX (zona explícita: el contenedor corre en UTC)."""
    from apscheduler.triggers.cron import CronTrigger
    hh, mm = (hora.split(":") + ["0"])[:2]
    return CronTrigger(day_of_week="mon-fri", hour=int(hh), minute=int(mm), timezone=CDMX)


# ---------- Lógica pura (testeable) ----------

def dias_desde(fecha_registro, hoy: date) -> int | None:
    if fecha_registro is None:
        return None
    try:
        d = fecha_registro.date() if isinstance(fecha_registro, datetime) else \
            fecha_registro if isinstance(fecha_registro, date) else date.fromisoformat(str(fecha_registro)[:10])
    except ValueError:
        return None
    return max((hoy - d).days, 0)


def linea(v, hoy: date) -> str:
    import html
    esc = lambda s: html.escape(str(s or ""), quote=False)
    dias = dias_desde(v.get("fecha_registro"), hoy)
    partes = [f"🔹 <code>#{esc(v.get('id_ventas'))}</code>", esc(v.get("comprador")) or "—",
              f"${float(v.get('total') or 0):,.2f}", f"vendedor {esc(v.get('vendedor')) or '—'}"]
    if dias is not None:
        partes.append("hoy" if dias == 0 else f"hace {dias} día{'s' if dias != 1 else ''}")
    if v.get("cotizacion"):
        partes.append(f"cot. <code>{esc(v.get('cotizacion'))}</code>")
    return " · ".join(partes)


def build_mensaje(filas: list, hoy: date) -> list:
    out = [f"🧾 <b>Ventas por facturar ({len(filas)})</b>\nRequieren factura y aún no se relaciona:"]
    out += [linea(v, hoy) for v in filas]
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

SQL_POR_FACTURAR = """
    SELECT id_ventas, MIN(nombreComprador) AS comprador, MIN(usuario) AS vendedor,
           MIN(cotizacion) AS cotizacion, MIN(fecha_registro) AS fecha_registro,
           SUM(precio * cantidad) AS total, COUNT(*) AS partidas
    FROM ventasRegistro
    WHERE facturable = 1 AND COALESCE(facturada, 0) = 0 AND estatus = 'activa'
    GROUP BY id_ventas
    ORDER BY MIN(fecha_registro), id_ventas
"""


def cargar_ventas_por_facturar() -> list:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(SQL_POR_FACTURAR)
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
        filas = cargar_ventas_por_facturar()
        stats = {"pendientes": len(filas)}
        if filas:
            hoy = datetime.now(CDMX).date()
            for chunk in build_mensaje(filas, hoy):
                if telegram:
                    await send_telegram_alert(chunk)
            try:
                import notificaciones_service
                await notificaciones_service.crear_y_notificar_todos(
                    "Ventas por facturar",
                    f"{len(filas)} venta(s) requieren factura y aún no se relaciona.", "warn")
            except Exception as err:
                print(f"Job por-facturar: falló persistir notificaciones: {err}")
        else:
            stats["sin_pendientes"] = True
        try:
            mov_reg.registrar_movimiento(
                "job-ventas-facturar", f"Por facturar {motivo}: {stats['pendientes']} pendientes", "Ventas")
        except Exception as err:
            print(f"Job por-facturar: falló bitácora: {err}")
        resumen.update({"estado": "ok", "stats": stats})
    except Exception as err:
        resumen.update({"estado": "error", "error": str(err)})
        print(f"Job por-facturar error: {err}")
        if telegram:
            await send_telegram_alert("⚠️ <b>Job ventas por facturar falló</b>")
    resumen["fin"] = datetime.now(CDMX).isoformat()
    LAST_RUN.clear()
    LAST_RUN.update(resumen)
    try:
        from routers.sofi_notificaciones import manager as _ws_manager
        await _ws_manager.broadcast({"tipo": "job", "job": "ventas-facturar", "estado": resumen.get("estado"),
                                     "stats": resumen.get("stats"), "fin": resumen.get("fin")})
    except Exception as err:
        print(f"Job por-facturar: no se pudo avisar por WS: {err}")
    return resumen
