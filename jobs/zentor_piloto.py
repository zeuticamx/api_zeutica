# Job del Motor Zentor (piloto automático de Mercado Ads). Evalúa los anuncios de los últimos 7 días con
# las reglas guardadas (routers/mercado_ads.leer_reglas → zentor_logica) y, SOLO si el piloto está
# encendido y el motor activo, pausa los anuncios con diagnóstico «pausar» (nunca reactiva ni toca pujas:
# reactivar gasta dinero). Tope de pausas por corrida (ZENTOR_MAX_PAUSAS, 10 por defecto); las demás
# quedan avisadas por Telegram para la siguiente. Diario 13:00 CDMX (ZENTOR_JOB_HORA, ZENTOR_JOB_ENABLED).
# El mismo run_job atiende «Ejecutar evaluación ahora» (POST /mercado-ads/evaluar). Sin tabla
# `notificaciones` ni datos por WS: es solo de gerencia y esos avisos los ven todos los usuarios.
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

CDMX = ZoneInfo("America/Mexico_City")

LAST_RUN: dict = {"estado": "nunca", "detalle": None}

DIAS_VENTANA = 7


def crear_trigger(hora: str = "13:00"):
    """Diario a la hora dada, en CDMX (el contenedor corre en UTC)."""
    from apscheduler.triggers.cron import CronTrigger
    hh, mm = (hora.split(":") + ["0"])[:2]
    return CronTrigger(hour=int(hh), minute=int(mm), timezone=CDMX)


def tope_pausas() -> int:
    try:
        return max(0, int(os.getenv("ZENTOR_MAX_PAUSAS", "10")))
    except ValueError:
        return 10


# ---------- Mensaje (puro, testeable) ----------

def _esc(texto) -> str:
    import html
    return html.escape(str(texto or ""), quote=False)


def build_mensaje(pausados: list, omitidos: list, fallidos: list, dry_run: bool = False) -> str:
    cab = "🧪 <b>Zentor (simulación)" if dry_run else "🛑 <b>Zentor"
    lineas = [f"{cab}: {len(pausados)} anuncio(s) pausado(s) por el piloto</b>"]
    for p in pausados:
        lineas.append(f"• <code>{_esc(p['item_id'])}</code> {_esc(p['titulo'])[:60]} — {_esc(p['diagnostico']['motivo'])}")
    if omitidos:
        lineas.append(f"\n⚠️ {len(omitidos)} más superan el tope por corrida y quedan para la siguiente:")
        lineas += [f"• <code>{_esc(p['item_id'])}</code> {_esc(p['titulo'])[:60]}" for p in omitidos[:10]]
    if fallidos:
        lineas.append(f"\n❌ {len(fallidos)} no se pudieron pausar:")
        lineas += [f"• <code>{_esc(i)}</code> {_esc(e)[:80]}" for i, e in fallidos[:10]]
    return "\n".join(lineas)[:3800]


# ---------- Orquestador ----------

async def run_job(dry_run: bool | None = None, motivo: str = "manual", telegram: bool = True) -> dict:
    import mov_reg
    import zentor_logica as zl
    import zentor_service
    from routers import mercado_ads
    from servicios.telegram.notificacion import send_telegram_alert
    resumen = {"motivo": motivo, "inicio": datetime.now(CDMX).isoformat()}
    try:
        reglas = mercado_ads.leer_reglas()
        if reglas["estado_motor"] == zl.MOTOR_PAUSA:
            resumen.update({"estado": "omitido", "detalle": "El motor está en pausa"})
        else:
            hoy = datetime.now(CDMX).date()
            desde, hasta = hoy - timedelta(days=DIAS_VENTANA - 1), hoy
            crudos = await zentor_service.listar_anuncios(desde, hasta)
            pubs, evaluadas = zl.evaluar(crudos, mercado_ads._reglas_de_diagnostico(reglas))
            kpis = zl.agregar_kpis(pubs, evaluadas)
            stats = {"evaluados": kpis["publicaciones"], "en_alerta": kpis["en_alerta"],
                     "por_pausar": kpis["por_pausar"], "pausados": 0, "omitidos": 0, "fallidos": 0,
                     "piloto": reglas["auto_pilot"]}
            if reglas["auto_pilot"]:
                a_pausar, omitidos = zl.pausas_a_aplicar(evaluadas, tope_pausas())
                pausados, fallidos = [], []
                for pub in a_pausar:
                    if dry_run:
                        pausados.append(pub)
                        continue
                    try:
                        await zentor_service.cambiar_estado(pub["item_id"], "paused")
                        pausados.append(pub)
                        try:
                            mov_reg.registrar_movimiento(
                                "job-zentor", f"Zentor piloto ({motivo}): pausó {pub['item_id']} ({pub['diagnostico']['motivo']})",
                                mercado_ads.SECCION)
                        except Exception as err:
                            print(f"Job zentor: falló bitácora: {err}")
                    except zentor_service.MercadoAdsError as err:
                        # Un anuncio que falla no aborta el lote: se anota y se sigue con los demás.
                        fallidos.append((pub["item_id"], str(err)))
                stats.update({"pausados": len(pausados), "omitidos": len(omitidos), "fallidos": len(fallidos)})
                if telegram and (pausados or omitidos or fallidos):
                    await send_telegram_alert(build_mensaje(pausados, omitidos, fallidos, bool(dry_run)))
            resumen.update({"estado": "ok", "stats": stats})
    except zentor_service.MercadoAdsError as err:
        resumen.update({"estado": "error", "error": str(err), "sin_acceso": err.sin_acceso})
        print(f"Job zentor error: {err}")
        if telegram and not err.sin_acceso:
            await send_telegram_alert("⚠️ <b>Job Zentor (Mercado Ads) falló</b>")
    except Exception as err:
        resumen.update({"estado": "error", "error": str(err)})
        print(f"Job zentor error: {err}")
        if telegram:
            await send_telegram_alert("⚠️ <b>Job Zentor (Mercado Ads) falló</b>")
    resumen["fin"] = datetime.now(CDMX).isoformat()
    LAST_RUN.clear()
    LAST_RUN.update(resumen)
    try:
        from routers.sofi_notificaciones import manager as _ws_manager
        await _ws_manager.broadcast({"tipo": "job", "job": "zentor", "estado": resumen.get("estado"),
                                     "fin": resumen.get("fin")})
    except Exception as err:
        print(f"Job zentor: no se pudo avisar por WS: {err}")
    return resumen
