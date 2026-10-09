# Job alerta stock bajo Full (migración del workflow n8n).
# Diario 10:30 CDMX + disparo manual. Solo lectura en MeLi.
#
# Mejoras vs n8n: IDs y umbral por env (no editar código), chequeo real de
# logistic_type == fulfillment, UN mensaje agrupado (no uno por item),
# contador de días en bajo por publicación y aviso en tabla notificaciones.
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

load_dotenv()

CDMX = ZoneInfo("America/Mexico_City")

LAST_RUN: dict = {"estado": "nunca", "detalle": None}


def _env(nombre: str, default: str = "") -> str:
    return (os.getenv(nombre) or default).strip()


def config() -> tuple:
    ids = [x.strip() for x in _env("MELI_FULL_IDS", "").split(",") if x.strip()]
    try:
        umbral = int(_env("MELI_FULL_UMBRAL", "20"))
    except ValueError:
        umbral = 20
    return ids, umbral


def get_db_connection():
    import mysql.connector
    return mysql.connector.connect(host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
                                   password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"))


# ---------- Lógica pura (testeable) ----------

def evaluar(detalles: list, umbral: int) -> tuple:
    """(alertas, salidas_full): bajo+Full vs bajo pero ya no fulfillment."""
    alertas, salidas = [], []
    for det in detalles:
        try:
            qty = int(det.get("available_quantity") or 0)
        except (TypeError, ValueError):
            continue
        if qty > umbral:
            continue
        logistic = ((det.get("shipping") or {}).get("logistic_type")) or ""
        item = {"id": det.get("id"), "sku": det.get("seller_sku") or "Sin SKU",
                "titulo": det.get("title") or "", "stock": qty,
                "precio": det.get("price") or 0, "url": det.get("permalink") or "",
                "bodega": logistic or "?"}
        (alertas if logistic == "fulfillment" else salidas).append(item)
    return alertas, salidas


def linea(item, dias: int | None = None) -> str:
    import html
    esc = lambda s: html.escape(str(s or ""), quote=False)
    desc = str(item.get("titulo") or "")[:40]
    r = (f"🔹 <code>{esc(item.get('id'))}</code> · {esc(item.get('sku'))} · {esc(desc)} · "
         f"×{item.get('stock')} · ${float(item.get('precio') or 0):,.2f}")
    if dias and dias > 1:
        r += f" · {dias} días en bajo"
    if item.get("url"):
        r += f'\n    <a href="{esc(item["url"])}">Ver publicación</a> · {esc(item.get("bodega"))}'
    return r


def build_mensaje(alertas: list, salidas: list, dias_map: dict) -> list:
    out = ["⚠️ <b>Stock bajo Full</b>"]
    if alertas:
        out.append(f"\n<b>{len(alertas)} en bajo:</b>")
        out += [linea(a, dias_map.get(str(a.get("id")))) for a in alertas]
    if salidas:
        out.append("\n🚧 <b>Bajo pero ya no es Full (revisar):</b>")
        out += [linea(s) for s in salidas]
    if not alertas and not salidas:
        out.append("\nSin publicaciones bajo el umbral.")
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

class MeliFullError(Exception):
    pass


def asegurar_tabla_estado():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS meli_full_estado (
                   item_id VARCHAR(32) NOT NULL PRIMARY KEY,
                   ultimo_stock INT NOT NULL DEFAULT 0,
                   dias_bajo INT NOT NULL DEFAULT 0,
                   ultimo_aviso DATE NULL)""")
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def cargar_estado() -> dict:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT item_id, ultimo_stock, dias_bajo FROM meli_full_estado")
        return {str(f["item_id"]): f for f in cur.fetchall()}
    finally:
        cur.close()
        conn.close()


def guardar_estado(alertas: list, hoy: date):
    if not alertas:
        return
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.executemany(
            "INSERT INTO meli_full_estado (item_id, ultimo_stock, dias_bajo, ultimo_aviso) "
            "VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE ultimo_stock = VALUES(ultimo_stock), "
            "dias_bajo = VALUES(dias_bajo), ultimo_aviso = VALUES(ultimo_aviso)",
            [(str(a["id"]), a["stock"], a["_dias"], hoy) for a in alertas])
        conn.commit()
    finally:
        cur.close()
        conn.close()


async def _detalles(client: httpx.AsyncClient, token: str, ids: list) -> list:
    from jobs.meli_ventas import _get, MELI_API
    if not ids:
        return []
    data = await _get(client, f"{MELI_API}/items", token,
                      {"ids": ",".join(ids),
                       "attributes": "id,title,available_quantity,price,status,permalink,shipping,seller_sku"})
    out = []
    for r in (data if isinstance(data, list) else [data]):
        if isinstance(r, dict) and r.get("code") == 200 and isinstance(r.get("body"), dict):
            out.append(r["body"])
    return out


# ---------- Orquestador ----------

async def run_job(dry_run: bool | None = None, motivo: str = "manual", telegram: bool = True) -> dict:
    import mov_reg
    from servicios.telegram.notificacion import send_telegram_alert
    dry = (os.getenv("MELI_FULL_DRY_RUN", "0") == "1") if dry_run is None else dry_run
    ids, umbral = config()
    resumen = {"motivo": motivo, "dry_run": dry, "inicio": datetime.now(CDMX).isoformat()}
    try:
        if not ids:
            resumen.update({"estado": "sin_config", "detalle": "MELI_FULL_IDS vacío"})
            return resumen
        asegurar_tabla_estado()
        from jobs.meli_ventas import _meli_token
        async with httpx.AsyncClient(timeout=30.0) as client:
            token = await _meli_token(client)
            detalles = await _detalles(client, token, ids)
        alertas, salidas = evaluar(detalles, umbral)
        estado_prev = cargar_estado()
        for a in alertas:
            prev = estado_prev.get(str(a["id"]), {})
            a["_dias"] = (int(prev.get("dias_bajo") or 0) + 1
                          if prev and int(prev.get("ultimo_stock") or 0) <= umbral else 1)
        dias_map = {str(a["id"]): a["_dias"] for a in alertas}
        stats = {"revisados": len(detalles), "bajos": len(alertas),
                 "salidas_full": len(salidas), "dry_run": dry}
        if dry:
            resumen.update({"estado": "ok", "stats": stats})
        else:
            if alertas or salidas:
                for chunk in build_mensaje(alertas, salidas, dias_map):
                    if telegram:
                        await send_telegram_alert(chunk)
                try:
                    import notificaciones_service
                    await notificaciones_service.crear_y_notificar_todos(
                        "Stock bajo Full",
                        f"{len(alertas)} publicaciones Full bajo el umbral ({umbral})."
                        + (f" {len(salidas)} bajas que ya no son Full." if salidas else ""),
                        "warn")
                except Exception as err:
                    print(f"Job full: falló persistir notificaciones: {err}")
            guardar_estado(alertas, datetime.now(CDMX).date())
            try:
                mov_reg.registrar_movimiento(
                    "job-meli-full", f"Stock Full {motivo}: {stats['bajos']} en bajo, "
                    f"{stats['salidas_full']} salidas de Full", "Productos")
            except Exception as err:
                print(f"Job full: falló bitácora: {err}")
            resumen.update({"estado": "ok", "stats": stats})
    except Exception as err:
        resumen.update({"estado": "error", "error": str(err)})
        print(f"Job full error: {err}")
        if telegram:
            await send_telegram_alert("⚠️ <b>Job stock Full falló</b>")
    resumen["fin"] = datetime.now(CDMX).isoformat()
    LAST_RUN.clear()
    LAST_RUN.update(resumen)
    try:
        from routers.sofi_notificaciones import manager as _ws_manager
        await _ws_manager.broadcast({"tipo": "job", "job": "meli-full", "estado": resumen.get("estado"),
                                     "dry_run": resumen.get("dry_run"), "stats": resumen.get("stats"),
                                     "fin": resumen.get("fin")})
    except Exception as err:
        print(f"Job full: no se pudo avisar por WS: {err}")
    return resumen
