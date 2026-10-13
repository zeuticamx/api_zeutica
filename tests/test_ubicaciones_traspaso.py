# Tests de traspasos informativos entre tarjetas (routers/productos.py):
# mismo SKU, origen/destino existentes y distintos, stock suficiente.
# Solo tocan stock_ubicacion + ubicaciones_traspasos; productos queda intacto.
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import productos


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._resultado = []
        self.rowcount = 0

    def execute(self, query, params=None):
        q = " ".join(query.split())
        db = self.db
        db.ejecutados.append((q, params))
        self._resultado, self.rowcount = [], 0
        if q.startswith("CREATE TABLE IF NOT EXISTS ubicaciones_traspasos"):
            return
        if q.startswith("SELECT id, sku, warehouse_id, cantidad FROM stock_ubicacion WHERE id"):
            fila = db.tarjetas.get(params[0])
            self._resultado = [dict(fila)] if fila else []
        elif q.startswith("UPDATE stock_ubicacion SET cantidad"):
            fila = db.tarjetas.get(params[1])
            if fila:
                fila["cantidad"] = params[0]
                db.updates.append((params[1], params[0]))
                self.rowcount = 1
        elif q.startswith("INSERT INTO ubicaciones_traspasos"):
            db.inserts.append(params)
            self.rowcount = 1
        elif q.startswith("SELECT * FROM ubicaciones_traspasos WHERE sku"):
            self._resultado = [dict(f) for f in db.historial if f["sku"] == params[0]]

    def fetchone(self):
        return self._resultado[0] if self._resultado else None

    def fetchall(self):
        return list(self._resultado)

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        # Mismo SKU en dos tarjetas; tercera de otro SKU para el test de mezcla.
        self.tarjetas = {
            1: {"id": 1, "sku": "BOLECO17", "warehouse_id": "CEDIS-A1", "cantidad": 10},
            2: {"id": 2, "sku": "BOLECO17", "warehouse_id": "CEDIS-B2", "cantidad": 4},
            3: {"id": 3, "sku": "OTRO", "warehouse_id": "CEDIS-A1", "cantidad": 99},
        }
        self.historial = [
            {"id": 1, "sku": "BOLECO17", "origen_warehouse_id": "CEDIS-A1",
             "destino_warehouse_id": "CEDIS-B2", "cantidad": 2, "usuario": "ana"},
        ]
        self.updates = []
        self.inserts = []
        self.ejecutados = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, dictionary=False):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


@pytest.fixture
def db(monkeypatch):
    base = FakeDB()
    monkeypatch.setattr(productos, "get_db_connection", lambda: base)
    movimientos = []
    monkeypatch.setattr(productos.mov_reg, "registrar_movimiento", lambda *a, **k: movimientos.append(a))
    base.movimientos = movimientos
    return base


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(productos.router)
    return TestClient(app)


def base_payload(**extra):
    p = {"sku": "BOLECO17", "origen_id": 1, "destino_id": 2, "cantidad": 3, "usuario": "ana"}
    p.update(extra)
    return p


def test_ruta_feliz_resta_origen_suma_destino_y_registra(client, db):
    r = client.post("/ubicaciones/traspaso", json=base_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["origen"] == {"id": 1, "warehouse_id": "CEDIS-A1", "cantidad": 7}
    assert body["destino"] == {"id": 2, "warehouse_id": "CEDIS-B2", "cantidad": 7}
    assert db.updates == [(1, 7), (2, 7)] and len(db.inserts) == 1
    assert db.inserts[0][:6] == ("BOLECO17", 1, "CEDIS-A1", 2, "CEDIS-B2", 3)
    assert db.commits == 1
    assert any("Traspasó 3" in m[1] and m[0] == "ana" for m in db.movimientos)
    # Nada toca productos: ningún UPDATE/SELECT sobre esa tabla.
    assert all("productos" not in q for q, _ in db.ejecutados)


def test_mismo_origen_y_destino_da_422_sin_escribir(client, db):
    r = client.post("/ubicaciones/traspaso", json=base_payload(destino_id=1))
    assert r.status_code == 422
    assert db.updates == [] and db.inserts == [] and db.commits == 0


@pytest.mark.parametrize("payload", [
    base_payload(cantidad=0),
    base_payload(cantidad=-2),
    base_payload(origen_id=0),
    base_payload(sku=""),
    base_payload(usuario=""),
])
def test_payload_invalido_da_422_sin_escribir(client, db, payload):
    assert client.post("/ubicaciones/traspaso", json=payload).status_code == 422
    assert db.updates == [] and db.inserts == [] and db.commits == 0


def test_stock_insuficiente_da_422_sin_escribir(client, db):
    r = client.post("/ubicaciones/traspaso", json=base_payload(cantidad=11))
    assert r.status_code == 422 and "disponible 10" in r.json()["detail"]
    assert db.updates == [] and db.inserts == [] and db.commits == 0


def test_tarjeta_inexistente_da_404(client, db):
    assert client.post("/ubicaciones/traspaso", json=base_payload(origen_id=999)).status_code == 404
    assert client.post("/ubicaciones/traspaso", json=base_payload(destino_id=999)).status_code == 404
    assert db.updates == [] and db.inserts == []


def test_mezclar_skus_da_422(client, db):
    r = client.post("/ubicaciones/traspaso", json=base_payload(destino_id=3))
    assert r.status_code == 422 and "mismo SKU" in r.json()["detail"]
    assert db.updates == [] and db.inserts == []


def test_historial_por_sku_y_vacio_da_lista_vacia(client, db):
    r = client.get("/ubicaciones/traspasos/BOLECO17")
    assert r.status_code == 200 and r.json() == db.historial
    r = client.get("/ubicaciones/traspasos/SIN-NADA")
    assert r.status_code == 200 and r.json() == []
