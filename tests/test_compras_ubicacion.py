# Tests de compras por ubicar (routers/compras.py + compras_ubicacion_logica.py):
# listado agrupado por factura, reparto en varias celdas, cierre al completar, 409/422/404
# y migración (backfill solo cuando se crea la columna `ubicada`).
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import compras_ubicacion_logica as logica
from permisos import usuario_autenticado
from routers import compras


# ---------- Lógica pura ----------

def test_pendiente_nunca_es_negativo():
    assert logica.pendiente(10, 4) == 6
    assert logica.pendiente(10, 12) == 0
    assert logica.pendiente(None, None) == 0


def test_validar_cantidad():
    assert logica.validar_cantidad(5, 10, 0) is None
    assert logica.validar_cantidad(10, 10, 0) is None
    assert "excede" in logica.validar_cantidad(7, 10, 4)
    assert logica.validar_cantidad(0, 10, 0) is not None


def test_queda_ubicada_solo_al_completar():
    assert logica.queda_ubicada(10, 0, 10) is True
    assert logica.queda_ubicada(10, 4, 6) is True
    assert logica.queda_ubicada(10, 4, 5) is False


def test_agrupar_por_factura_junta_items_y_calcula_pendiente():
    filas = [
        {"id": 1, "sku": "A", "nombre": "a", "cantidad": 10, "cantidad_ubicada": 4, "num_factura": "F1", "proveedor": "P", "fecha_registro": "x"},
        {"id": 2, "sku": "B", "nombre": "b", "cantidad": 3, "cantidad_ubicada": 0, "num_factura": "F1", "proveedor": "P", "fecha_registro": "x"},
        {"id": 3, "sku": "C", "nombre": "c", "cantidad": 1, "cantidad_ubicada": None, "num_factura": "F2", "proveedor": "Q", "fecha_registro": "y"},
    ]
    grupos = logica.agrupar_por_factura(filas)
    assert [g["num_factura"] for g in grupos] == ["F1", "F2"]
    assert [i["pendiente"] for i in grupos[0]["items"]] == [6, 3]
    assert grupos[1]["items"][0]["cantidad_ubicada"] == 0


# ---------- Endpoints ----------

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
        if "FROM compras WHERE COALESCE(ubicada, 0) = 0" in q:
            self._resultado = [dict(c) for c in db.compras.values() if not c["ubicada"]]
        elif q.startswith("SELECT id, sku, stock_bodega, cantidad_ubicada, ubicada FROM compras"):
            c = db.compras.get(params[0])
            self._resultado = [dict(c)] if c else []
        elif q.startswith("SELECT id, sku, ubicada FROM compras"):
            c = db.compras.get(params[0])
            self._resultado = [dict(c)] if c else []
        elif q.startswith("SELECT id, cantidad FROM stock_ubicacion"):
            t = db.existentes.get((params[0], params[1].strip().lower()))
            self._resultado = [{"id": t["id"], "cantidad": t["cantidad"]}] if t else []
        elif q.startswith("UPDATE stock_ubicacion SET cantidad"):
            cajas, tid = params
            for t in db.existentes.values():
                if t["id"] == tid:
                    t["cantidad"] += cajas
            db.actualizadas.append(params)
        elif q.startswith("INSERT INTO stock_ubicacion"):
            db.tarjetas.append(params)
            sku, almacen, cama, cajas = params
            db.existentes[(sku, almacen.strip().lower())] = {"id": 100 + len(db.tarjetas), "cantidad": cajas, "cama": cama}
        elif q.startswith("UPDATE compras SET cantidad_ubicada"):
            cantidad, completa, _, _, _, cid = params
            c = db.compras[cid]
            c["cantidad_ubicada"] += cantidad
            c["ubicada"] = completa
        elif q.startswith("UPDATE compras SET ubicada = 1, sin_ubicacion = 1"):
            c = db.compras[params[1]]
            c["ubicada"], c["sin_ubicacion"] = 1, 1
        elif "FROM information_schema.COLUMNS" in q:
            self._resultado = [(col,) for col in db.columnas]
        elif q.startswith("ALTER TABLE compras ADD COLUMN"):
            db.alter.append(q)
        elif q.startswith("UPDATE compras SET ubicada = 1, cantidad_ubicada"):
            db.backfill = True
            self.rowcount = len(db.compras)

    def fetchone(self):
        return self._resultado[0] if self._resultado else None

    def fetchall(self):
        return list(self._resultado)

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        self.compras = {
            1: {"id": 1, "sku": "BOLECO38", "nombre": "Bolsa", "cantidad": 10, "stock_bodega": 10,
                "cantidad_ubicada": 0, "ubicada": 0, "num_factura": "F1", "proveedor": "P", "fecha_registro": "2026-10-20"},
            2: {"id": 2, "sku": "GEL", "nombre": "Gel", "cantidad": 5, "stock_bodega": 5,
                "cantidad_ubicada": 5, "ubicada": 1, "num_factura": "F1", "proveedor": "P", "fecha_registro": "2026-10-20"},
        }
        self.tarjetas = []
        self.existentes = {}   # tarjetas ya en stock_ubicacion: (sku, celda en minúsculas) -> fila
        self.actualizadas = []
        self.ejecutados = []
        self.alter = []
        self.backfill = False
        self.columnas = ["id", "sku"]
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
    monkeypatch.setattr(compras, "get_db_connection", lambda: base)
    movimientos = []
    monkeypatch.setattr(compras.mov_reg, "registrar_movimiento", lambda *a, **k: movimientos.append(a))
    base.movimientos = movimientos
    return base


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(compras.router)
    app.dependency_overrides[usuario_autenticado] = lambda: "ana"
    return TestClient(app)


def test_por_ubicar_solo_trae_las_pendientes_agrupadas(client):
    r = client.get("/compras/por-ubicar")
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["total"] == 1
    assert cuerpo["facturas"][0]["items"][0]["sku"] == "BOLECO38"
    assert cuerpo["facturas"][0]["items"][0]["pendiente"] == 10


def test_ubicar_en_varias_celdas_cierra_al_completar(client, db):
    # BOLECO38: 5 piezas por caja → 5 pzas = 1 caja, 5 pzas = 1 caja.
    r = client.post("/compras/1/ubicar", json={"warehouse_id": "A-1", "cantidad": 5, "cama": 2})
    assert r.status_code == 200 and r.json()["ubicada"] is False and r.json()["pendiente"] == 5
    assert r.json()["cajas"] == 1 and r.json()["multiplo"] == 5 and r.json()["tarjeta_nueva"] is True
    assert db.compras[1]["ubicada"] == 0
    r = client.post("/compras/1/ubicar", json={"warehouse_id": "B-2", "cantidad": 5})
    assert r.status_code == 200 and r.json()["ubicada"] is True and r.json()["pendiente"] == 0
    assert db.compras[1]["ubicada"] == 1 and db.compras[1]["cantidad_ubicada"] == 10
    assert db.tarjetas == [("BOLECO38", "A-1", 2, 1), ("BOLECO38", "B-2", 0, 1)]
    assert all(m[0] == "ana" for m in db.movimientos) and len(db.movimientos) == 2
    assert client.get("/compras/por-ubicar").json()["total"] == 0


def test_ubicar_mas_de_lo_pendiente_da_422_y_no_escribe(client, db):
    r = client.post("/compras/1/ubicar", json={"warehouse_id": "A-1", "cantidad": 11})
    assert r.status_code == 422 and db.tarjetas == []
    assert client.post("/compras/1/ubicar", json={"warehouse_id": "A-1", "cantidad": 0}).status_code == 422
    assert client.post("/compras/1/ubicar", json={"warehouse_id": "   ", "cantidad": 1}).status_code == 422


def test_ubicar_convierte_piezas_a_cajas_y_exige_multiplo_exacto(client, db):
    r = client.post("/compras/1/ubicar", json={"warehouse_id": "A-1", "cantidad": 7})
    assert r.status_code == 422 and "múltiplo de 5" in r.json()["detail"]
    assert db.tarjetas == [] and db.compras[1]["cantidad_ubicada"] == 0
    r = client.post("/compras/1/ubicar", json={"warehouse_id": "A-1", "cantidad": 10})
    assert r.status_code == 200 and r.json()["cajas"] == 2
    assert db.tarjetas == [("BOLECO38", "A-1", 0, 2)]
    assert db.compras[1]["cantidad_ubicada"] == 10  # la compra sigue contando piezas


def test_ubicar_en_celda_con_tarjeta_existente_suma_y_no_duplica(client, db):
    db.existentes[("BOLECO38", "a-1")] = {"id": 7, "cantidad": 3, "cama": 4}
    # Distinta capitalización/espacios de la misma celda.
    r = client.post("/compras/1/ubicar", json={"warehouse_id": " A-1 ", "cantidad": 10, "cama": 9})
    assert r.status_code == 200
    assert r.json()["tarjeta_nueva"] is False and r.json()["tarjeta_id"] == 7
    assert db.tarjetas == []                       # ningún INSERT
    assert db.actualizadas == [(2, 7)]
    assert db.existentes[("BOLECO38", "a-1")]["cantidad"] == 5
    assert db.existentes[("BOLECO38", "a-1")]["cama"] == 4  # se conserva la cama


def test_ubicar_en_oficina_no_convierte(client, db):
    r = client.post("/compras/1/ubicar", json={"warehouse_id": "Oficina", "cantidad": 7})
    assert r.status_code == 200 and r.json()["cajas"] == 7 and r.json()["multiplo"] == 1
    assert db.tarjetas == [("BOLECO38", "Oficina", 0, 7)]


def test_piezas_a_cajas():
    assert logica.piezas_a_cajas(30, 10) == (3, None)
    assert logica.piezas_a_cajas(7, 1) == (7, None)
    assert logica.piezas_a_cajas(7, 5)[0] is None


def test_ubicar_compra_ya_ubicada_da_409_e_inexistente_404(client, db):
    assert client.post("/compras/2/ubicar", json={"warehouse_id": "A-1", "cantidad": 1}).status_code == 409
    assert client.post("/compras/99/ubicar", json={"warehouse_id": "A-1", "cantidad": 1}).status_code == 404
    assert db.tarjetas == []


def test_sin_ubicacion_saca_la_compra_sin_crear_tarjetas(client, db):
    r = client.post("/compras/1/sin-ubicacion")
    assert r.status_code == 200
    assert db.compras[1]["ubicada"] == 1 and db.compras[1]["sin_ubicacion"] == 1 and db.tarjetas == []
    assert client.post("/compras/1/sin-ubicacion").status_code == 409
    assert client.post("/compras/99/sin-ubicacion").status_code == 404


def test_migracion_marca_lo_anterior_como_ubicado_solo_si_crea_la_columna(db):
    rep = compras.asegurar_columnas_ubicacion()
    assert "ubicada" in rep["columnas_agregadas"] and db.backfill is True and rep["backfill"] == 2
    # Segunda pasada: las columnas ya existen, no hay backfill (no pisa compras nuevas).
    db.columnas = ["id", "sku", *compras.COLUMNAS_UBICACION]
    db.backfill, db.alter = False, []
    rep = compras.asegurar_columnas_ubicacion()
    assert rep["columnas_agregadas"] == [] and db.backfill is False and db.alter == []
