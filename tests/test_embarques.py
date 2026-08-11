# Tests basicos del endpoint PATCH /embarques/{id}/etapas/{tipo}.
# Ahora solo prueba completado + nota (sin monto/tipo_cambio).
from datetime import date
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


def _preparar_mocks(monkeypatch, fetchone_secuencia):
    cursor = FakeCursor(fetchone_secuencia)
    conn = FakeConn(cursor)
    monkeypatch.setattr(embarques_router, "get_db_connection", lambda: conn)
    monkeypatch.setattr(embarques_router.mov_reg, "registrar_movimiento", lambda *a, **k: None)
    return cursor


def _update_params(cursor):
    """Devuelve (query, params) del UPDATE embarque_etapas ejecutado."""
    for query, params in cursor.ejecutados:
        if query.startswith("UPDATE embarque_etapas"):
            return query, params
    raise AssertionError("No se ejecuto ningun UPDATE sobre embarque_etapas")


@pytest.mark.asyncio
async def test_marcar_etapa_completada(monkeypatch):
    """Al marcar completada, se actualiza completado=True."""
    etapa_previa = {"id": 10, "nota": None}
    etapa_final = {
        "id": 10, "embarque_id": 1, "tipo": "ANTICIPO_CHINA", "completado": True, "nota": None,
    }
    cursor = _preparar_mocks(
        monkeypatch,
        fetchone_secuencia=[{"id": 1}, etapa_previa, etapa_final],
    )

    payload = EtapaUpdate(completado=True, usuario="tester")
    resultado = await embarques_router.marcar_etapa(1, EtapaTipo.ANTICIPO_CHINA, payload)

    query, params = _update_params(cursor)
    assert "completado = %s" in query
    assert params[0] is True
    assert resultado["completado"] is True


@pytest.mark.asyncio
async def test_marcar_etapa_incompleta(monkeypatch):
    """Al desmarcar, se actualiza completado=False."""
    etapa_previa = {"id": 11, "nota": None}
    etapa_final = {
        "id": 11, "embarque_id": 1, "tipo": "LIQUIDADO_CHINA", "completado": False, "nota": None,
    }
    cursor = _preparar_mocks(
        monkeypatch,
        fetchone_secuencia=[{"id": 1}, etapa_previa, etapa_final],
    )

    payload = EtapaUpdate(completado=False, usuario="tester")
    resultado = await embarques_router.marcar_etapa(1, EtapaTipo.LIQUIDADO_CHINA, payload)

    query, params = _update_params(cursor)
    assert "completado = %s" in query
    assert params[0] is False
    assert resultado["completado"] is False


@pytest.mark.asyncio
async def test_actualizar_nota_etapa(monkeypatch):
    """Actualiza nota sin cambiar completado."""
    etapa_previa = {"id": 12, "nota": "Nota vieja"}
    etapa_final = {
        "id": 12, "embarque_id": 1, "tipo": "HL_LIQUIDADA", "completado": True, "nota": "Nota nueva",
    }
    cursor = _preparar_mocks(
        monkeypatch,
        fetchone_secuencia=[{"id": 1}, etapa_previa, etapa_final],
    )

    payload = EtapaUpdate(completado=True, nota="Nota nueva", usuario="tester")
    resultado = await embarques_router.marcar_etapa(1, EtapaTipo.HL_LIQUIDADA, payload)

    query, params = _update_params(cursor)
    assert params[1] == "Nota nueva"
    assert resultado["nota"] == "Nota nueva"


@pytest.mark.asyncio
async def test_marcar_etapa_embarque_inexistente_devuelve_404(monkeypatch):
    cursor = _preparar_mocks(monkeypatch, fetchone_secuencia=[None])

    payload = EtapaUpdate(completado=True, usuario="tester")
    with pytest.raises(HTTPException) as exc_info:
        await embarques_router.marcar_etapa(999, EtapaTipo.ANTICIPO_CHINA, payload)

    assert exc_info.value.status_code == 404
