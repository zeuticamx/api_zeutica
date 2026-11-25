# Seguimiento de cotizaciones: catálogo cerrado + detalle libre de OTROS. BD falsa, sin MySQL.
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from servicios import cotizacion_seguimiento as cs
from routers import cotizacionesBack


def test_normaliza_catalogo_y_vacio():
    assert cs.normalizar_seguimiento(" pagado ", "ignorado") == ("PAGADO", None)
    assert cs.normalizar_seguimiento("", "x") == (None, None)
    assert cs.normalizar_seguimiento(None, None) == (None, None)
    assert cs.normalizar_seguimiento("otros", " a   b ") == ("OTROS", "a b")


@pytest.mark.parametrize("valor,otros", [("LLAMAR", None), ("OTROS", ""), ("OTROS", "x" * 256)])
def test_invalidos_dan_422(valor, otros):
    with pytest.raises(Exception) as e:
        cs.normalizar_seguimiento(valor, otros)
    assert e.value.status_code == 422


class _Cur:
    def __init__(self, db): self.db, self.rowcount = db, 1
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, q, p=None):
        self.db.ejecutados.append((" ".join(q.split()), p))
        self.rowcount = self.db.filas


class _DB:
    def __init__(self): self.ejecutados, self.filas = [], 1
    def cursor(self): return _Cur(self)
    def commit(self): pass
    def rollback(self): pass
    def close(self): pass


@pytest.fixture
def api(monkeypatch):
    db = _DB()
    monkeypatch.setattr(cotizacionesBack, "get_db_connection", lambda: db)
    monkeypatch.setattr(cotizacionesBack.mov_reg, "registrar_movimiento", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(cotizacionesBack.router)
    return TestClient(app), db


def test_guarda_valor_y_detalle(api):
    client, db = api
    r = client.post("/cotizaciones/seguimiento", json={"codigo_cotizacion": "ZTC-1", "seguimiento": "otros", "seguimiento_otros": "Espera anticipo"})
    assert r.status_code == 200
    assert db.ejecutados[0][1] == ("OTROS", "Espera anticipo", "ZTC-1")


def test_texto_libre_viejo_da_422_y_no_toca_bd(api):
    client, db = api
    r = client.post("/cotizaciones/seguimiento", json={"codigo_cotizacion": "ZTC-1", "seguimiento": "le llamé"})
    assert r.status_code == 422 and db.ejecutados == []


def test_null_limpia_y_404_si_no_existe(api):
    client, db = api
    assert client.post("/cotizaciones/seguimiento", json={"codigo_cotizacion": "ZTC-1", "seguimiento": None}).status_code == 200
    assert db.ejecutados[0][1] == (None, None, "ZTC-1")
    db.filas = 0
    assert client.post("/cotizaciones/seguimiento", json={"codigo_cotizacion": "X", "seguimiento": "PAGADO"}).status_code == 404
