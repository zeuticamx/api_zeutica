# Job punto de reorden: avisa por Telegram los SKU cuya mercancía total (existencia +
# en tránsito) ya está en o bajo su punto de reorden («▲ COMPRAR»). Mismo cálculo que
# la página (routers/reorden.cargar_reorden → reorden_logica). Solo lectura.
# Semanal por defecto (lunes 09:00 CDMX: la compra se planea por semana; diario repetiría
# la misma lista) + disparo manual de gerencia. Sin tabla `notificaciones` ni WS con datos:
# la página es solo de gerencia y esos avisos los ven todos los usuarios.
from datetime import datetime
from zoneinfo import ZoneInfo

CDMX = ZoneInfo("America/Mexico_City")

LAST_RUN: dict = {"estado": "nunca", "detalle": None}


def crear_trigger(hora: str = "09:00", dias: str = "mon"):
    """Días (formato APScheduler: 'mon', 'mon-fri'…) y hora en CDMX (el contenedor corre en UTC)."""
    from apscheduler.triggers.cron import CronTrigger
    hh, mm = (hora.split(":") + ["0"])[:2]
    return CronTrigger(day_of_week=dias, hour=int(hh), minute=int(mm), timezone=CDMX)


# ---------- Lógica pura (testeable) ----------

def _num(x) -> str:
    x = float(x or 0)
    return f"{x:,.0f}" if x == int(x) else f"{x:,.1f}"


def linea(f) -> str:
    import html
    esc = lambda s: html.escape(str(s or ""), quote=False)
    partes = [f"🔺 <code>{esc(f.get('sku'))}</code>", esc(f.get("nombre")) or "—",
              f"total {_num(f.get('total_mercancia'))} / ROP {_num(f.get('punto_reorden'))}",
              f"{_num(f.get('meses_stock'))} meses de stock"]
    if f.get("volatilidad") == "VOLATIL":
        partes.append("volátil")
    if f.get("historial_corto"):
        partes.append("historial corto")
    return " · ".join(partes)


def build_mensaje(filas: list, desde: str, hasta: str) -> list:
    out = [f"📦 <b>Punto de reorden: {len(filas)} SKU por comprar</b>\n"
           f"Mercancía total ≤ punto de reorden (consumo {desde} a {hasta}, sin el mes en curso):"]
    out += [linea(f) for f in filas]
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

def cargar_reorden() -> dict:
    from routers.reorden import cargar_reorden as _cargar
    return _cargar()


# ---------- Orquestador ----------

async def run_job(dry_run: bool | None = None, motivo: str = "manual", telegram: bool = True) -> dict:
    import mov_reg
    import reorden_logica as rl
    from servicios.telegram.notificacion import send_telegram_alert
    resumen = {"motivo": motivo, "inicio": datetime.now(CDMX).isoformat()}
    try:
        datos = cargar_reorden()
        comprar = [f for f in datos["filas"] if f["decision"] == rl.COMPRAR]
        stats = {"comprar": len(comprar), "evaluados": len(datos["filas"])}
        if comprar:
            if telegram:
                for chunk in build_mensaje(comprar, datos["meses"][0], datos["meses"][-1]):
                    await send_telegram_alert(chunk)
        else:
            stats["sin_compras"] = True
        try:
            mov_reg.registrar_movimiento(
                "job-reorden", f"Punto de reorden {motivo}: {stats['comprar']} por comprar", "Punto de reorden")
        except Exception as err:
            print(f"Job reorden: falló bitácora: {err}")
        resumen.update({"estado": "ok", "stats": stats})
    except Exception as err:
        resumen.update({"estado": "error", "error": str(err)})
        print(f"Job reorden error: {err}")
        if telegram:
            await send_telegram_alert("⚠️ <b>Job punto de reorden falló</b>")
    resumen["fin"] = datetime.now(CDMX).isoformat()
    LAST_RUN.clear()
    LAST_RUN.update(resumen)
    try:
        from routers.sofi_notificaciones import manager as _ws_manager
        await _ws_manager.broadcast({"tipo": "job", "job": "reorden", "estado": resumen.get("estado"),
                                     "fin": resumen.get("fin")})
    except Exception as err:
        print(f"Job reorden: no se pudo avisar por WS: {err}")
    return resumen
