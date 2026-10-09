# Job de recordatorios Cleanest Choice (migración del workflow n8n).
# Diario 11:00 CDMX + disparo manual. Solo lectura en cleanestChoice.
#
# Mejoras vs n8n: un solo mensaje agrupado (no uno por orden), incluye
# vencidas (n8n las excluía), no repite a diario lo ya avisado (tabla
# cleanest_avisos: se avisa 1 vez al entrar a ventana y diario solo si
# vencen en <=3 días), y persiste en `notificaciones` además de Telegram.
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import mysql.connector
from dotenv import load_dotenv

load_dotenv()

CDMX = ZoneInfo("America/Mexico_City")
VENTANA_DIAS = 10
UMBRAL_DIARIO = 3

LAST_RUN: dict = {"estado": "nunca", "detalle": None}


def _env(nombre: str, default: str = "") -> str:
    return (os.getenv(nombre) or default).strip()


def get_db_connection():
    return mysql.connector.connect(host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
                                   password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"))


# ---------- Lógica pura (testeable) ----------

def _fecha(d) -> date | None:
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    try:
        return datetime.fromisoformat(str(d)[:10]).date()
    except ValueError:
        return None


def partir_por_vencimiento(ordenes: list, hoy: date) -> tuple:
    """(vencidas, proximas) por fecha_promesa. Sin fecha o fuera de ventana se omiten."""
    vencidas, proximas = [], []
    for o in ordenes:
        f = _fecha(o.get("fecha_promesa"))
        if not f or not (o.get("sku") or "").strip():
            continue
        if f < hoy:
            vencidas.append(o)
        elif f <= hoy + timedelta(days=VENTANA_DIAS):
            proximas.append(o)
    key = lambda o: str(_fecha(o.get("fecha_promesa")))
    return sorted(vencidas, key=key), sorted(proximas, key=key)


def decidir_incluir(proximas: list, hoy: date, avisadas: set) -> tuple:
    """(a_incluir, omitidas): entra si vence en <=UMBRAL_DIARIO o nunca avisada."""
    incluir, omitidas = [], 0
    for o in proximas:
        dias = (_fecha(o.get("fecha_promesa")) - hoy).days
        if dias <= UMBRAL_DIARIO or str(o.get("numero_orden")) not in avisadas:
            incluir.append(o)
        else:
            omitidas += 1
    return incluir, omitidas


def linea(o) -> str:
    import html
    esc = lambda s: html.escape(str(s or ""), quote=False)
    f = _fecha(o.get("fecha_promesa"))
    ddmm = f"{f.day:02d}/{f.month:02d}" if f else "?"
    return (f"🔹 <code>{esc(o.get('numero_orden'))}</code> · {esc(o.get('sku'))} "
            f"×{o.get('cantidad') or '?'} · vence {ddmm} · {esc(o.get('status'))}")


def build_mensaje(vencidas: list, incluir: list) -> list:
    out = ["📋 <b>Órdenes Cleanest próximas a vencer</b>"]
    if vencidas:
        out.append(f"\n🚨 <b>Vencidas ({len(vencidas)}):</b>")
        out += [linea(o) for o in vencidas]
    if incluir:
        out.append(f"\n📦 <b>Próximas ({len(incluir)}):</b>")
        out += [linea(o) for o in incluir]
    if not vencidas and not incluir:
        out.append("\nSin órdenes próximas a vencer ni vencidas pendientes.")
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

class CleanestJobError(Exception):
    pass


def asegurar_tabla_avisos():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS cleanest_avisos (
                   numero_orden VARCHAR(64) NOT NULL PRIMARY KEY,
                   ultimo_aviso DATE NULL)""")
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def cargar_ordenes() -> list:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute(
            """SELECT numero_orden, sku, cantidad, fecha_promesa, status FROM cleanestChoice
               WHERE fecha_promesa >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
                 AND fecha_promesa < DATE_ADD(CURDATE(), INTERVAL 11 DAY)
                 AND status <> 'Entregado' AND sku IS NOT NULL AND sku <> ''
               ORDER BY fecha_promesa""")
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


def cargar_avisadas() -> set:
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT numero_orden FROM cleanest_avisos WHERE ultimo_aviso IS NOT NULL")
        return {str(fila[0]) for fila in cur.fetchall()}
    finally:
        cur.close()
        conn.close()


def marcar_avisadas(ordenes: list, hoy: date):
    if not ordenes:
        return
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.executemany(
            "INSERT INTO cleanest_avisos (numero_orden, ultimo_aviso) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE ultimo_aviso = VALUES(ultimo_aviso)",
            [(str(o.get("numero_orden")), hoy) for o in ordenes])
        conn.commit()
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
        asegurar_tabla_avisos()
        ordenes = cargar_ordenes()
        vencidas, proximas = partir_por_vencimiento(ordenes, hoy)
        incluir, omitidas = decidir_incluir(proximas, hoy, cargar_avisadas())
        stats = {"vencidas": len(vencidas), "proximas": len(incluir),
                 "omitidas_repetidas": omitidas}
        if vencidas or incluir:
            for chunk in build_mensaje(vencidas, incluir):
                if telegram:
                    await send_telegram_alert(chunk)
            marcar_avisadas(incluir, hoy)
            try:
                import notificaciones_service
                await notificaciones_service.crear_y_notificar_todos(
                    "Órdenes Cleanest próximas a vencer",
                    f"{len(vencidas)} vencidas y {len(incluir)} próximas a vencer.",
                    "warn" if vencidas else "info")
            except Exception as err:
                print(f"Job cleanest: falló persistir notificaciones: {err}")
        else:
            stats["sin_pendientes"] = True
        try:
            mov_reg.registrar_movimiento(
                "job-cleanest", f"Recordatorios Cleanest {motivo}: {stats['vencidas']} vencidas, "
                f"{stats['proximas']} próximas, {stats['omitidas_repetidas']} omitidas", "Ordenes Cleanest Choice")
        except Exception as err:
            print(f"Job cleanest: falló bitácora: {err}")
        resumen.update({"estado": "ok", "stats": stats})
    except Exception as err:
        resumen.update({"estado": "error", "error": str(err)})
        print(f"Job cleanest error: {err}")
        if telegram:
            await send_telegram_alert(f"⚠️ <b>Job Cleanest falló</b>")
    resumen["fin"] = datetime.now(CDMX).isoformat()
    LAST_RUN.clear()
    LAST_RUN.update(resumen)
    try:
        from routers.sofi_notificaciones import manager as _ws_manager
        await _ws_manager.broadcast({"tipo": "job", "job": "cleanest", "estado": resumen.get("estado"),
                                     "stats": resumen.get("stats"), "fin": resumen.get("fin")})
    except Exception as err:
        print(f"Job cleanest: no se pudo avisar por WS: {err}")
    return resumen
