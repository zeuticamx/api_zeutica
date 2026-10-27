# Tests del borrado lógico de SKUs (routers/productos.py): DELETE /producto/{sku},
# POST /producto/{sku}/restaurar, GET /productos (oculta eliminados) y alta de un SKU eliminado.
# MySQL y bitácora van simulados; el usuario sale del token (permisos.usuario_autenticado).
import mysql.connector
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import permisos
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
        if db.error_en and db.error_en in q:
            raise db.error
        self._resultado, self.rowcount = [], 0

        if q.startswith("SELECT nombre_usuario FROM usuarios WHERE token"):
            u = db.tokens.get(params[0])
            self._resultado = [{"nombre_usuario": u}] if u else []
        elif "FROM productos WHERE sku = %s AND eliminado = 0 FOR UPDATE" in q:
            p = db.productos.get(params[0])
            self._resultado = [dict(p)] if p and not p["eliminado"] else []
        elif "FROM productos WHERE sku = %s AND eliminado = 1 FOR UPDATE" in q:
            p = db.productos.get(params[0])
            self._resultado = [dict(p)] if p and p["eliminado"] else []
        elif q.startswith("SELECT sku, eliminado FROM productos"):
            p = db.productos.get(params[0])
            self._resultado = [(p["sku"], p["eliminado"])] if p else []
        elif "FROM embarque_items" in q:
            self._resultado = [{"n": db.embarques}]
        elif "FROM compras" in q:
            self._resultado = [{"n": db.compras}]
        elif q.startswith("DELETE FROM stock_ubicacion"):
            self.rowcount = db.tarjetas
        elif q.startswith("UPDATE productos"):
            self.rowcount = 1
        elif q.startswith("SELECT sku, nombre, categoria"):
            eliminados = "eliminado = 1" in q
            self._resultado = [dict(p) for p in db.productos.values() if bool(p["eliminado"]) == eliminados]

    def fetchone(self):
        return self._resultado[0] if self._resultado else None

    def fetchall(self):
        return list(self._resultado)

    def close(self):
        pass


def producto(sku, bodega=0, fba=0, clean=0, total=0, eliminado=0):
    return {"sku": sku, "nombre": f"Producto {sku}", "stock_bodega": bodega, "stock_fba": fba,
            "stock_clean": clean, "stock_total": total, "eliminado": eliminado}


class FakeDB:
    def __init__(self):
        self.tokens = {"tok-ger": "gerencia", "tok-ana": "ana"}
        self.productos = {
            "VACIO": producto("VACIO"),
            "BODEGA": producto("BODEGA", bodega=5, total=5),
            "FBA": producto("FBA", fba=2, total=2),
            "NEGATIVO": producto("NEGATIVO", bodega=-1, total=-1),
            "ELIM": producto("ELIM", eliminado=1),
        }
        self.embarques = 0
        self.compras = 0
        self.tarjetas = 3
        self.ejecutados = []
        self.commits = 0
        self.rollbacks = 0
        self.error_en = None
        self.error = None

    def cursor(self, dictionary=False, buffered=False):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass

    def is_connected(self):
        return True

    def consultas(self, inicio):
        return [(q, p) for q, p in self.ejecutados if q.startswith(inicio)]


@pytest.fixture
def db(monkeypatch):
    base = FakeDB()
    monkeypatch.setattr(productos, "get_db_connection", lambda: base)
    monkeypatch.setattr(permisos, "get_db_connection", lambda: base)
    monkeypatch.setenv("USUARIOS_GERENCIA", "gerencia")
    movimientos = []
    monkeypatch.setattr(productos.mov_reg, "registrar_movimiento", lambda *a, **k: movimientos.append(a))
    base.movimientos = movimientos
    return base


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(productos.router)
    return TestClient(app)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


ANA, GER = auth("tok-ana"), auth("tok-ger")


# ---------- Regla pura ----------

def test_motivos_sin_bloqueo_con_todo_en_cero_o_nulo():
    assert productos.motivos_bloqueo_eliminar({"stock_bodega": None, "stock_total": 0}, 0, 0) == []


@pytest.mark.parametrize("campo", ["stock_bodega", "stock_fba", "stock_clean", "stock_total"])
@pytest.mark.parametrize("valor", [3, -1])
def test_cualquier_existencia_distinta_de_cero_bloquea(campo, valor):
    motivos = productos.motivos_bloqueo_eliminar({campo: valor}, 0, 0)
    assert len(motivos) == 1 and "inventario" in motivos[0]


def test_embarques_y_compras_pendientes_bloquean():
    motivos = productos.motivos_bloqueo_eliminar({}, 2, 1)
    assert len(motivos) == 2


# ---------- DELETE /producto/{sku} ----------

def test_eliminar_marca_eliminado_borra_tarjetas_y_registra_bitacora(client, db):
    r = client.delete("/producto/VACIO", headers=GER)
    assert r.status_code == 200
    assert r.json()["tarjetas_eliminadas"] == 3
    q, p = db.consultas("UPDATE productos SET eliminado = 1")[0]
    assert p == ("gerencia", "VACIO")
    assert db.consultas("DELETE FROM stock_ubicacion")[0][1] == ("VACIO",)
    assert db.commits == 1
    assert db.movimientos[0][0] == "gerencia" and "VACIO" in db.movimientos[0][1]


def test_eliminar_exige_gerencia(client, db):
    r = client.delete("/producto/VACIO", headers=ANA)
    assert r.status_code == 403
    assert not db.consultas("UPDATE productos")


@pytest.mark.parametrize("sku", ["BODEGA", "FBA", "NEGATIVO"])
def test_eliminar_con_inventario_da_409_y_no_toca_nada(client, db, sku):
    r = client.delete(f"/producto/{sku}", headers=GER)
    assert r.status_code == 409
    assert "inventario" in r.json()["detail"]
    assert not db.consultas("UPDATE productos") and not db.consultas("DELETE FROM stock_ubicacion")
    assert db.rollbacks == 1


def test_eliminar_con_embarque_o_compra_pendiente_da_409(client, db):
    db.embarques, db.compras = 1, 2
    r = client.delete("/producto/VACIO", headers=GER)
    assert r.status_code == 409
    detalle = r.json()["detail"]
    assert "embarques" in detalle and "ubicar" in detalle
    assert not db.consultas("UPDATE productos")


def test_eliminar_inexistente_o_ya_eliminado_da_404(client, db):
    assert client.delete("/producto/NOEXISTE", headers=GER).status_code == 404
    assert client.delete("/producto/ELIM", headers=GER).status_code == 404


def test_eliminar_error_de_bd_da_500_sin_filtrar_el_texto(client, db):
    db.error_en = "UPDATE productos"
    db.error = mysql.connector.Error("detalle interno secreto")
    r = client.delete("/producto/VACIO", headers=GER)
    assert r.status_code == 500
    assert "secreto" not in r.json()["detail"]
    assert db.rollbacks == 1


# ---------- Restaurar ----------

def test_restaurar_devuelve_el_sku(client, db):
    r = client.post("/producto/ELIM/restaurar", headers=GER)
    assert r.status_code == 200
    assert db.consultas("UPDATE productos SET eliminado = 0")
    assert "ELIM" in db.movimientos[0][1]


def test_restaurar_exige_gerencia_y_404_si_no_esta_eliminado(client, db):
    assert client.post("/producto/ELIM/restaurar", headers=ANA).status_code == 403
    assert client.post("/producto/VACIO/restaurar", headers=GER).status_code == 404


# ---------- Listado ----------

def test_listado_normal_oculta_eliminados(client, db):
    r = client.get("/productos", headers=ANA)
    assert r.status_code == 200
    assert "ELIM" not in [p["sku"] for p in r.json()]
    assert "eliminado = 0" in db.consultas("SELECT sku, nombre")[0][0]


def test_listado_de_eliminados_es_solo_de_gerencia(client, db):
    assert client.get("/productos?eliminados=true", headers=ANA).status_code == 403
    r = client.get("/productos?eliminados=true", headers=GER)
    assert r.status_code == 200
    assert [p["sku"] for p in r.json()] == ["ELIM"]


# ---------- Alta de un SKU eliminado ----------

def test_alta_de_sku_eliminado_da_409_explicito(client, db):
    body = {"sku": "ELIM", "nombre": "x", "categoria": "c", "medida": "m", "ubicacion": "u",
            "stock_minimo": 1, "numero_referencia": 0, "costo_total": 0, "precio": 0,
            "precio_2": 0, "precio_3": 0, "usuario": "gerencia"}
    r = client.post("/producto/nuevo", json=body, headers=GER)
    assert r.status_code == 409
    assert "eliminado" in r.json()["detail"]
