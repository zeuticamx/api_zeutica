# Tests del job ventas por facturar: funciones puras, trigger y orquestador sin MySQL.
from datetime import date, datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from jobs import ventas_por_facturar as job

CDMX = ZoneInfo("America/Mexico_City")
HOY = date(2026, 10, 20)


def _venta(**extra):
    v = {"id_ventas": "1234567890", "comprador": "Cliente <A>", "vendedor": "ana", "cotizacion": None,
         "fecha_registro": datetime(2026, 10, 17, 9, 30), "total": 1500.5, "partidas": 2}
    v.update(extra)
    return v


def test_mensaje_agrupado_por_venta():
    texto = "\n".join(job.build_mensaje([_venta(), _venta(id_ventas="99", cotizacion="ZTC-7",
                                                         fecha_registro=datetime(2026, 10, 20, 8, 0))], HOY))
    assert "Ventas por facturar (2)" in texto
    assert "<code>#1234567890</code>" in texto and "$1,500.50" in texto
    assert "vendedor ana" in texto and "hace 3 días" in texto
    assert "Cliente &lt;A&gt;" in texto  # escapado para el parse_mode HTML de Telegram
    assert "cot. <code>ZTC-7</code>" in texto and "hoy" in texto


def test_mensaje_se_parte_bajo_el_limite_de_telegram():
    filas = [_venta(id_ventas=str(i)) for i in range(200)]
    chunks = job.build_mensaje(filas, HOY)
    assert len(chunks) > 1 and all(len(c) <= 3800 for c in chunks)
    assert sum(c.count("🔹") for c in chunks) == 200


@pytest.mark.parametrize("valor,esperado", [
    (datetime(2026, 10, 19, 23, 59), 1), (date(2026, 10, 20), 0), ("2026-10-10", 10), (None, None), ("x", None),
])
def test_dias_desde(valor, esperado):
    assert job.dias_desde(valor, HOY) == esperado


def test_query_solo_facturables_sin_factura_y_activas():
    q = " ".join(job.SQL_POR_FACTURAR.split())
    assert "facturable = 1 AND COALESCE(facturada, 0) = 0 AND estatus = 'activa'" in q
    assert "GROUP BY id_ventas" in q


@pytest.mark.parametrize("desde,esperado", [
    (datetime(2026, 10, 20, 9, 0, tzinfo=CDMX), datetime(2026, 10, 20, 10, 45)),   # martes, antes de la hora
    (datetime(2026, 10, 23, 11, 0, tzinfo=CDMX), datetime(2026, 10, 26, 10, 45)),  # viernes después → lunes
    (datetime(2026, 10, 24, 10, 0, tzinfo=CDMX), datetime(2026, 10, 26, 10, 45)),  # sábado → lunes
])
def test_trigger_lunes_a_viernes_10_45_cdmx(desde, esperado):
    nxt = job.crear_trigger("10:45").get_next_fire_time(None, desde)
    assert nxt.astimezone(CDMX).replace(tzinfo=None) == esperado


def test_trigger_respeta_cdmx_aunque_ref_sea_utc():
    nxt = job.crear_trigger().get_next_fire_time(None, datetime(2026, 10, 20, 12, 0, tzinfo=ZoneInfo("UTC")))
    assert nxt.astimezone(ZoneInfo("UTC")).hour == 16 and nxt.astimezone(CDMX).hour == 10


@pytest.fixture
def avisos(monkeypatch):
    import mov_reg
    import notificaciones_service
    from routers import sofi_notificaciones
    from servicios.telegram import notificacion
    tg, notif, ws = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(notificacion, "send_telegram_alert", tg)
    monkeypatch.setattr(notificaciones_service, "crear_y_notificar_todos", notif)
    monkeypatch.setattr(sofi_notificaciones.manager, "broadcast", ws)
    monkeypatch.setattr(mov_reg, "registrar_movimiento", lambda *a, **k: None)
    return tg, notif, ws


@pytest.mark.asyncio
async def test_run_job_avisa_telegram_tabla_y_ws(monkeypatch, avisos):
    tg, notif, ws = avisos
    monkeypatch.setattr(job, "cargar_ventas_por_facturar", lambda: [_venta()])
    r = await job.run_job(motivo="scheduler")
    assert r["estado"] == "ok" and r["stats"] == {"pendientes": 1}
    assert tg.await_count == 1 and notif.await_count == 1
    assert ws.await_args.args[0]["job"] == "ventas-facturar"
    assert job.LAST_RUN["estado"] == "ok"


@pytest.mark.asyncio
async def test_run_job_sin_pendientes_no_manda_avisos(monkeypatch, avisos):
    tg, notif, _ = avisos
    monkeypatch.setattr(job, "cargar_ventas_por_facturar", lambda: [])
    r = await job.run_job()
    assert r["stats"] == {"pendientes": 0, "sin_pendientes": True}
    tg.assert_not_awaited()
    notif.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_job_error_de_bd_queda_en_estado(monkeypatch, avisos):
    tg, _, _ = avisos

    def caida():
        raise RuntimeError("sin conexión")

    monkeypatch.setattr(job, "cargar_ventas_por_facturar", caida)
    r = await job.run_job()
    assert r["estado"] == "error" and "sin conexión" in r["error"]
    assert "falló" in tg.await_args.args[0]
