# Tests de la reversión de traspasos (routers/traspaso.py + traspaso_reversion_logica.py):
# baja FULL y traspaso a Clean, plazo de 15 días (409), 409 por ya revertido / no revertible /
# clean insuficiente / SKU eliminado, registro con el usuario del token y fila compensatoria.
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from servicios import traspaso_reversion_logica as logica
from servicios.permisos import usuario_autenticado
from routers import traspaso

AHORA = datetime(2026, 11, 22, 12, 0, 0)


# ---------- Lógica pura ----------

def test_dentro_de_plazo_es_ventana_movil_inclusiva():
    assert logica.dentro_de_plazo(AHORA - timedelta(days=15), AHORA, 15) is True
    assert logica.dentro_de_plazo(AHORA - timedelta(days=15, seconds=1), AHORA, 15) is False
    assert logica.dentro_de_plazo(AHORA - timedelta(days=1), AHORA, 15) is True
    assert logica.dentro_de_plazo(None, AHORA, 15) is False


def test_dias_reversion_lee_env_y_cae_a_15(monkeypatch):
    monkeypatch.delenv("TRASPASO_REVERSION_DIAS", raising=False)
    assert logica.dias_reversion() == 15
    monkeypatch.setenv("TRASPASO_REVERSION_DIAS", "7")
    assert logica.dias_reversion() == 7
    for malo in ("abc", "0", "-3", ""):
        monkeypatch.setenv("TRASPASO_REVERSION_DIAS", malo)
        assert logica.dias_reversion() == 15


def test_plan_reversion_baja_y_clean():
    p = logica.plan_reversion("BAJA", -5)
    assert p == {"tipo": "BAJA", "piezas": 5, "sumar_bodega": 5, "restar_clean": 0,
                 "almacen_rev": "REV-BAJA", "cantidad_rev": 5}
    p = logica.plan_reversion(" clean ", 8)
    assert p["tipo"] == "CLEAN" and p["restar_clean"] == 8 and p["cantidad_rev"] == -8
    # Reversiones, almacén viejo o signo incoherente: no revertibles.
    assert logica.plan_reversion("REV-BAJA", 5) is None
    assert logica.plan_reversion("REV-CLEAN", -5) is None
    assert logica.plan_reversion("CEDIS", 5) is None
    assert logica.plan_reversion("BAJA", 5) is None
    assert logica.plan_reversion("CLEAN", -5) is None
    assert logica.plan_reversion(None, 5) is None


def test_es_revertible_combina_tipo_plazo_y_reversion_previa():
    reciente = AHORA - timedelta(days=2)
    assert logica.es_revertible("BAJA", -1, reciente, AHORA, False, 15) is True
    assert logica.es_revertible("BAJA", -1, reciente, AHORA, True, 15) is False
    assert logica.es_revertible("BAJA", -1, AHORA - timedelta(days=20), AHORA, False, 15) is False
    assert logica.es_revertible("REV-BAJA", 1, reciente, AHORA, False, 15) is False


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
        if q.startswith("SELECT id, sku, cantidad, almacen, usuario, fecha_registro, NOW() AS ahora FROM stock_actual WHERE id"):
            m = db.movimientos.get(params[0])
            self._resultado = [{**m, "ahora": db.ahora}] if m else []
        elif q.startswith("SELECT id FROM traspasos_reversiones WHERE stock_actual_id"):
            r = [x for x in db.reversiones if x["stock_actual_id"] == params[0]]
            self._resultado = [{"id": r[0]["id"]}] if r else []
        elif q.startswith("SELECT stock_bodega, stock_clean, eliminado FROM productos"):
            p = db.productos.get(params[0])
            self._resultado = [dict(p)] if p else []
        elif q.startswith("UPDATE productos SET stock_bodega = COALESCE(stock_bodega, 0) + %s, stock_clean = COALESCE(stock_clean, 0) - %s"):
            piezas, _, sku, minimo = params
            p = db.productos[sku]
            if (p["stock_clean"] or 0) >= minimo:
                p["stock_bodega"] = (p["stock_bodega"] or 0) + piezas
                p["stock_clean"] = (p["stock_clean"] or 0) - piezas
                self.rowcount = 1
        elif q.startswith("UPDATE productos SET stock_bodega = COALESCE(stock_bodega, 0) + %s"):
            piezas, sku = params
            db.productos[sku]["stock_bodega"] = (db.productos[sku]["stock_bodega"] or 0) + piezas
            self.rowcount = 1
        elif q.startswith("INSERT INTO traspasos_reversiones"):
            (sid, sku, cantidad, almacen, usuario_orig, fecha_orig, usuario, fecha, motivo) = params
            db.reversiones.append({"id": len(db.reversiones) + 1, "stock_actual_id": sid, "sku": sku,
                                   "cantidad": cantidad, "almacen": almacen, "usuario_original": usuario_orig,
                                   "fecha_original": fecha_orig, "usuario": usuario, "fecha": fecha, "motivo": motivo})
        elif q.startswith("INSERT INTO stock_actual"):
            sku, cantidad, usuario, almacen = params
            nuevo_id = max(db.movimientos) + 1
            db.movimientos[nuevo_id] = {"id": nuevo_id, "sku": sku, "cantidad": cantidad, "almacen": almacen,
                                        "usuario": usuario, "fecha_registro": db.ahora}
        elif "FROM stock_actual s LEFT JOIN traspasos_reversiones r" in q:
            revertidos = {x["stock_actual_id"] for x in db.reversiones}
            filas = sorted(db.movimientos.values(), key=lambda m: m["id"], reverse=True)
            self._resultado = [{**m, "revertido": int(m["id"] in revertidos), "ahora": db.ahora} for m in filas]
        elif q.startswith("SELECT id, stock_actual_id, sku, cantidad, almacen, usuario_original"):
            self._resultado = [dict(x) for x in reversed(db.reversiones)]

    def fetchone(self):
        return self._resultado[0] if self._resultado else None

    def fetchall(self):
        return list(self._resultado)

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        self.ahora = AHORA
        self.movimientos = {
            1: {"id": 1, "sku": "A", "cantidad": -5, "almacen": "BAJA", "usuario": "luis", "fecha_registro": AHORA - timedelta(days=3)},
            2: {"id": 2, "sku": "B", "cantidad": 8, "almacen": "CLEAN", "usuario": "luis", "fecha_registro": AHORA - timedelta(days=1)},
            3: {"id": 3, "sku": "A", "cantidad": -2, "almacen": "BAJA", "usuario": "luis", "fecha_registro": AHORA - timedelta(days=16)},
            4: {"id": 4, "sku": "A", "cantidad": 5, "almacen": "REV-BAJA", "usuario": "ana", "fecha_registro": AHORA - timedelta(days=1)},
        }
        self.productos = {
            "A": {"stock_bodega": 10, "stock_clean": 0, "eliminado": 0},
            "B": {"stock_bodega": 1, "stock_clean": 10, "eliminado": 0},
        }
        self.reversiones = []
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
    monkeypatch.setattr(traspaso, "get_db_connection", lambda: base)
    bitacora, avisos = [], []
    monkeypatch.setattr(traspaso.mov_reg, "registrar_movimiento", lambda *a, **k: bitacora.append(a))

    async def _aviso(mensaje):
        avisos.append(mensaje)
    monkeypatch.setattr(traspaso, "send_telegram_alert", _aviso)
    base.bitacora, base.avisos = bitacora, avisos
    return base


@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(traspaso.router)
    app.dependency_overrides[usuario_autenticado] = lambda: "ana"   # no es gerencia: cualquier nivel puede
    return TestClient(app)


def test_revertir_baja_devuelve_piezas_a_bodega_y_deja_registro(client, db):
    r = client.post("/traspasos/1/revertir", json={"motivo": "Se capturó mal"})
    assert r.status_code == 200
    assert db.productos["A"]["stock_bodega"] == 15 and db.productos["A"]["stock_clean"] == 0
    rev = db.reversiones[0]
    assert rev["stock_actual_id"] == 1 and rev["cantidad"] == 5 and rev["usuario"] == "ana"
    assert rev["usuario_original"] == "luis" and rev["motivo"] == "Se capturó mal"
    # Fila compensatoria con signo inverso, a nombre de quien revirtió.
    comp = db.movimientos[max(db.movimientos)]
    assert (comp["almacen"], comp["cantidad"], comp["usuario"]) == ("REV-BAJA", 5, "ana")
    assert db.commits == 1 and db.bitacora and db.bitacora[0][0] == "ana" and len(db.avisos) == 1


def test_revertir_clean_resta_de_clean_y_suma_a_bodega(client, db):
    r = client.post("/traspasos/2/revertir")          # sin cuerpo: motivo opcional
    assert r.status_code == 200
    assert db.productos["B"] == {"stock_bodega": 9, "stock_clean": 2, "eliminado": 0}
    assert db.reversiones[0]["motivo"] is None
    comp = db.movimientos[max(db.movimientos)]
    assert (comp["almacen"], comp["cantidad"]) == ("REV-CLEAN", -8)


def test_revertir_clean_con_stock_clean_insuficiente_da_409_y_no_escribe(client, db):
    db.productos["B"]["stock_clean"] = 3
    r = client.post("/traspasos/2/revertir")
    assert r.status_code == 409 and "Clean" in r.json()["detail"]
    assert db.productos["B"]["stock_bodega"] == 1 and db.reversiones == [] and db.commits == 0


def test_fuera_de_plazo_da_409(client, db):
    r = client.post("/traspasos/3/revertir")
    assert r.status_code == 409 and "15 días" in r.json()["detail"]
    assert db.reversiones == [] and db.productos["A"]["stock_bodega"] == 10


def test_ya_revertido_da_409_y_no_duplica(client, db):
    assert client.post("/traspasos/1/revertir").status_code == 200
    assert client.post("/traspasos/1/revertir").status_code == 409
    assert len(db.reversiones) == 1 and db.productos["A"]["stock_bodega"] == 15


def test_una_reversion_no_se_revierte_e_inexistente_404(client, db):
    assert client.post("/traspasos/4/revertir").status_code == 409
    assert client.post("/traspasos/99/revertir").status_code == 404
    assert db.reversiones == []


def test_sku_eliminado_o_inexistente(client, db):
    db.productos["A"]["eliminado"] = 1
    r = client.post("/traspasos/1/revertir")
    assert r.status_code == 409 and "restaur" in r.json()["detail"].lower()
    del db.productos["A"]
    assert client.post("/traspasos/1/revertir").status_code == 404
    assert db.reversiones == []


def test_motivo_demasiado_largo_da_422(client, db):
    assert client.post("/traspasos/1/revertir", json={"motivo": "x" * 501}).status_code == 422


def test_reporte_marca_revertible_y_revertido(client, db):
    client.post("/traspasos/1/revertir")
    filas = {f["id"]: f for f in client.get("/traspasos/reporte").json()}
    assert filas[1]["revertido"] is True and filas[1]["revertible"] is False
    assert filas[2]["revertible"] is True and filas[2]["usuario"] == "luis"
    assert filas[3]["revertible"] is False and filas[3]["revertido"] is False   # fuera de plazo
    assert filas[4]["revertible"] is False                                       # es una reversión
    assert all("ahora" not in f for f in filas.values())


def test_listado_de_reversiones(client, db):
    client.post("/traspasos/1/revertir")
    r = client.get("/traspasos/reversiones")
    assert r.status_code == 200 and r.json()["total"] == 1
    assert r.json()["registros"][0]["usuario"] == "ana"
