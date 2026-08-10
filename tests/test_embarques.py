# Tests basicos del endpoint PATCH /embarques/{id}/etapas/{tipo}.
# El monto se captura directo en MXN (sin conversion). El tipo de cambio de
# la fecha de pago se guarda solo como referencia de auditoria: se asigna
# automaticamente al completar, y solo se vuelve a consultar si cambia la
# fecha de pago (nunca por cambios de monto).
from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from routers import embarques as embarques_router
from routers.embarques import EtapaTipo, EtapaUpdate


class FakeCursor:
    """Cursor falso: entrega fetchone() en el orden en que se pide y
    registra cada UPDATE/SELECT ejecutado para poder inspeccionarlo."""

    def __init__(self, fetchone_secuencia):
        self._fetchone_secuencia = list(fetchone_secuencia)
        self.ejecutados = []

    def execute(self, query, params=None):
        self.ejecutados.append((query.strip(), params))

    def fetchone(self):
        return self._fetchone_secuencia.pop(0)

    def close(self):
        pass


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, dictionary=False):
        return self._cursor

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def _preparar_mocks(monkeypatch, fetchone_secuencia, tipo_cambio_banxico=None):
    cursor = FakeCursor(fetchone_secuencia)
    conn = FakeConn(cursor)
    monkeypatch.setattr(embarques_router, "get_db_connection", lambda: conn)
    monkeypatch.setattr(embarques_router.mov_reg, "registrar_movimiento", lambda *a, **k: None)

    banxico_mock = AsyncMock(return_value=tipo_cambio_banxico or (18.4200, date(2026, 7, 15)))
    monkeypatch.setattr(embarques_router, "obtener_tipo_cambio_fecha_especifica", banxico_mock)
    return cursor, banxico_mock


def _update_params(cursor):
    """Devuelve (query, params) del UPDATE embarque_etapas ejecutado."""
    for query, params in cursor.ejecutados:
        if query.startswith("UPDATE embarque_etapas"):
            return query, params
    raise AssertionError("No se ejecuto ningun UPDATE sobre embarque_etapas")


@pytest.mark.asyncio
async def test_completar_etapa_primera_vez_consulta_banxico_y_guarda_monto_tal_cual(monkeypatch):
    fecha_pago = date(2026, 7, 15)
    etapa_previa = {"id": 10, "fecha_pago": None, "tipo_cambio_referencia": None, "nota": None}
    etapa_final = {
        "id": 10, "embarque_id": 1, "tipo": "ANTICIPO_CHINA", "completado": True,
        "fecha_pago": fecha_pago, "monto_mxn": 230275.10, "tipo_cambio_referencia": 18.4200,
        "fecha_captura": None, "nota": None,
    }
    cursor, banxico_mock = _preparar_mocks(
        monkeypatch,
        fetchone_secuencia=[{"id": 1}, etapa_previa, etapa_final],
        tipo_cambio_banxico=(18.4200, fecha_pago),
    )

    payload = EtapaUpdate(completado=True, fecha_pago=fecha_pago, monto_mxn=230275.10, usuario="tester")
    resultado = await embarques_router.marcar_etapa(1, EtapaTipo.ANTICIPO_CHINA, payload)

    banxico_mock.assert_awaited_once_with(fecha_pago)
    _, params = _update_params(cursor)
    assert params[0] == fecha_pago            # fecha_pago tal cual la mando el usuario
    assert params[1] == 230275.10             # monto_mxn tal cual, sin ninguna conversion
    assert params[2] == 18.4200               # tipo_cambio_referencia obtenido de Banxico
    assert resultado["monto_mxn"] == 230275.10
    assert resultado["tipo_cambio_referencia"] == 18.4200


@pytest.mark.asyncio
async def test_editar_monto_sin_cambiar_fecha_no_vuelve_a_consultar_banxico(monkeypatch):
    fecha_pago = date(2026, 7, 15)
    etapa_previa = {"id": 11, "fecha_pago": fecha_pago, "tipo_cambio_referencia": 18.4200, "nota": None}
    etapa_final = {
        "id": 11, "embarque_id": 1, "tipo": "LIQUIDADO_CHINA", "completado": True,
        "fecha_pago": fecha_pago, "monto_mxn": 500000.00, "tipo_cambio_referencia": 18.4200,
        "fecha_captura": None, "nota": None,
    }
    cursor, banxico_mock = _preparar_mocks(
        monkeypatch,
        fetchone_secuencia=[{"id": 1}, etapa_previa, etapa_final],
    )

    # Misma fecha_pago que ya tenia guardada, solo cambia el monto.
    payload = EtapaUpdate(completado=True, fecha_pago=fecha_pago, monto_mxn=500000.00, usuario="tester")
    resultado = await embarques_router.marcar_etapa(1, EtapaTipo.LIQUIDADO_CHINA, payload)

    banxico_mock.assert_not_awaited()
    _, params = _update_params(cursor)
    assert params[1] == 500000.00             # el monto si se actualiza
    assert params[2] == 18.4200               # la referencia se reusa, no se recalcula
    assert resultado["tipo_cambio_referencia"] == 18.4200


@pytest.mark.asyncio
async def test_editar_fecha_pago_recalcula_tipo_cambio_referencia(monkeypatch):
    fecha_vieja = date(2026, 7, 15)
    fecha_nueva = date(2026, 7, 20)
    etapa_previa = {"id": 12, "fecha_pago": fecha_vieja, "tipo_cambio_referencia": 18.4200, "nota": None}
    etapa_final = {
        "id": 12, "embarque_id": 1, "tipo": "HL_LIQUIDADA", "completado": True,
        "fecha_pago": fecha_nueva, "monto_mxn": 300000.00, "tipo_cambio_referencia": 18.6000,
        "fecha_captura": None, "nota": None,
    }
    cursor, banxico_mock = _preparar_mocks(
        monkeypatch,
        fetchone_secuencia=[{"id": 1}, etapa_previa, etapa_final],
        tipo_cambio_banxico=(18.6000, fecha_nueva),
    )

    payload = EtapaUpdate(completado=True, fecha_pago=fecha_nueva, monto_mxn=300000.00, usuario="tester")
    resultado = await embarques_router.marcar_etapa(1, EtapaTipo.HL_LIQUIDADA, payload)

    banxico_mock.assert_awaited_once_with(fecha_nueva)
    _, params = _update_params(cursor)
    assert params[0] == fecha_nueva
    assert params[2] == 18.6000
    assert resultado["tipo_cambio_referencia"] == 18.6000


@pytest.mark.asyncio
async def test_desmarcar_etapa_limpia_pago_pero_mantiene_fecha_captura(monkeypatch):
    etapa_previa = {"id": 13, "fecha_pago": date(2026, 7, 15), "tipo_cambio_referencia": 18.4200, "nota": None}
    etapa_final = {
        "id": 13, "embarque_id": 1, "tipo": "ANTICIPO_CHINA", "completado": False,
        "fecha_pago": None, "monto_mxn": None, "tipo_cambio_referencia": None,
        "fecha_captura": "2026-07-15T10:00:00", "nota": None,
    }
    cursor, banxico_mock = _preparar_mocks(
        monkeypatch,
        fetchone_secuencia=[{"id": 1}, etapa_previa, etapa_final],
    )

    payload = EtapaUpdate(completado=False, usuario="tester")
    resultado = await embarques_router.marcar_etapa(1, EtapaTipo.ANTICIPO_CHINA, payload)

    banxico_mock.assert_not_awaited()
    query, params = _update_params(cursor)
    assert "completado = FALSE" in query
    assert "fecha_pago = NULL" in query
    assert "monto_mxn = NULL" in query
    assert "tipo_cambio_referencia = NULL" in query
    assert "fecha_captura" not in query        # no se toca al desmarcar
    assert params[-1] == etapa_previa["id"]
    assert resultado["completado"] is False
    assert resultado["monto_mxn"] is None
    assert resultado["tipo_cambio_referencia"] is None
    assert resultado["fecha_captura"] is not None  # se mantiene la ultima captura real


@pytest.mark.asyncio
async def test_completar_sin_fecha_pago_o_monto_devuelve_400(monkeypatch):
    banxico_mock = AsyncMock()
    monkeypatch.setattr(embarques_router, "obtener_tipo_cambio_fecha_especifica", banxico_mock)

    payload = EtapaUpdate(completado=True, fecha_pago=None, monto_mxn=None, usuario="tester")
    with pytest.raises(HTTPException) as exc_info:
        await embarques_router.marcar_etapa(1, EtapaTipo.ANTICIPO_CHINA, payload)

    assert exc_info.value.status_code == 400
    banxico_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_marcar_etapa_embarque_inexistente_devuelve_404(monkeypatch):
    cursor, banxico_mock = _preparar_mocks(monkeypatch, fetchone_secuencia=[None])

    payload = EtapaUpdate(completado=True, fecha_pago=date.today(), monto_mxn=100, usuario="tester")
    with pytest.raises(HTTPException) as exc_info:
        await embarques_router.marcar_etapa(999, EtapaTipo.ANTICIPO_CHINA, payload)

    assert exc_info.value.status_code == 404
    banxico_mock.assert_not_awaited()


def test_etapa_update_rechaza_fecha_pago_futura():
    with pytest.raises(Exception):
        EtapaUpdate(completado=True, fecha_pago=date.today() + timedelta(days=1), monto_mxn=100)


def test_etapa_update_rechaza_monto_no_positivo():
    with pytest.raises(Exception):
        EtapaUpdate(completado=True, fecha_pago=date.today(), monto_mxn=0)
