# Tests de gastos operativos: editar (PUT) y eliminar (DELETE, borrado lógico) solo para
# gerencia, 403 para otros usuarios, 404 si no existe y exclusión de eliminados en listados.
# MySQL, bitácora y Telegram van simulados; la app de prueba monta solo gastos.router.
from unittest.mock import AsyncMock

import mysql.connector
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import permisos
from routers import gastos


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._resultado = []

    def execute(self, query, params=None):
        q = " ".join(query.split())
        self.db.ejecutados.append((q, params))
        if self.db.error_en and self.db.error_en in q:
            raise self.db.error
        self._resultado = []
        if q.startswith("SELECT id, descripcion, costo, cantidad FROM gastos WHERE id"):
            g = self.db.gastos.get(params[0])
            if g and not g["eliminado"]:
                self._resultado = [dict(g)]
        elif q.startswith("SELECT id, descripcion, costo, cantidad, total"):
            self._resultado = [dict(g) for g in self.db.gastos.values() if not g["eliminado"]]
        elif q.startswith("SELECT nombre_usuario FROM usuarios"):
            u = self.db.tokens.get(params[0])
            self._resultado = [{"nombre_usuario": u}] if u else []
        elif q.startswith("SELECT COLUMN_NAME"):
            self._resultado = [(c,) for c in self.db.columnas]

    def fetchone(self):
        return self._resultado[0] if self._resultado else None

    def fetchall(self):
        return list(self._resultado)

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        self.gastos = {1: {"id": 1, "descripcion": "Gasolina", "costo": 500.0, "cantidad": 2, "eliminado": 0}}
        self.tokens = {"tok-gerencia": "gerencia", "tok-fparra": "fparra", "tok-ventas": "ventas"}
        self.columnas = ["id", "descripcion", "costo", "cantidad"]
        self.ejecutados = []
        self.commits = 0
        self.rollbacks = 0
        self.error_en = None
        self.error = None

    def cursor(self, dictionary=False):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass

    def escrituras(self):
        return [(q, p) for q, p in self.ejecutados if q.startswith(("UPDATE", "DELETE", "INSERT", "ALTER"))]


@pytest.fixture
def db(monkeypatch):
    base = FakeDB()
    monkeypatch.setattr(gastos, "get_db_connection", lambda: base)
    monkeypatch.setattr(permisos, "get_db_connection", lambda: base)
    movimientos = []
    monkeypatch.setattr(gastos.mov_reg, "registrar_movimiento", lambda *a, **k: movimientos.append(a))
    monkeypatch.setattr(gastos, "send_telegram_alert", AsyncMock())
    base.movimientos = movimientos
    return base


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(gastos.router)
    return TestClient(app)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


CAMBIOS = {"descripcion": "Gasolina camioneta", "costo": 650.5, "cantidad": 3}


# ---------- PUT /gastos/{id} ----------

def test_gerencia_edita_gasto(client, db):
    r = client.put("/gastos/1", json=CAMBIOS, headers=auth("tok-gerencia"))
    assert r.status_code == 200
    assert r.json()["id"] == 1
    (q, p), = db.escrituras()
    assert q.startswith("UPDATE gastos SET descripcion")
    assert p == ("Gasolina camioneta", 650.5, 3, 1)
    assert db.commits == 1
    assert db.movimientos[0][0] == "gerencia" and "Editó el gasto 1" in db.movimientos[0][1]


def test_fparra_tambien_es_gerencia(client, db):
    assert client.put("/gastos/1", json=CAMBIOS, headers=auth("tok-fparra")).status_code == 200


def test_editar_sin_gerencia_da_403_y_no_escribe(client, db):
    r = client.put("/gastos/1", json=CAMBIOS, headers=auth("tok-ventas"))
    assert r.status_code == 403
    assert db.escrituras() == []


def test_editar_token_invalido_da_401(client, db):
    r = client.put("/gastos/1", json=CAMBIOS, headers=auth("no-existe"))
    assert r.status_code == 401
    assert db.escrituras() == []


def test_editar_sin_token_no_pasa(client, db):
    r = client.put("/gastos/1", json=CAMBIOS)
    assert r.status_code in (401, 403)
    assert db.escrituras() == []


def test_editar_gasto_inexistente_da_404(client, db):
    r = client.put("/gastos/999", json=CAMBIOS, headers=auth("tok-gerencia"))
    assert r.status_code == 404
    assert db.escrituras() == []


def test_editar_gasto_eliminado_da_404(client, db):
    db.gastos[1]["eliminado"] = 1
    assert client.put("/gastos/1", json=CAMBIOS, headers=auth("tok-gerencia")).status_code == 404


@pytest.mark.parametrize("cambio", [
    {"descripcion": "   "},
    {"descripcion": ""},
    {"costo": -1},
    {"cantidad": 0},
    {"cantidad": "x"},
])
def test_editar_con_datos_invalidos_da_422(client, db, cambio):
    r = client.put("/gastos/1", json={**CAMBIOS, **cambio}, headers=auth("tok-gerencia"))
    assert r.status_code == 422
    assert db.escrituras() == []


def test_editar_error_mysql_da_500_con_rollback(client, db):
    db.error_en = "UPDATE gastos SET descripcion"
    db.error = mysql.connector.Error("boom")
    r = client.put("/gastos/1", json=CAMBIOS, headers=auth("tok-gerencia"))
    assert r.status_code == 500
    assert db.rollbacks == 1 and db.commits == 0


# ---------- DELETE /gastos/{id} (borrado lógico) ----------

def test_gerencia_elimina_gasto_con_borrado_logico(client, db):
    r = client.delete("/gastos/1", headers=auth("tok-gerencia"))
    assert r.status_code == 200
    (q, p), = db.escrituras()
    assert q.startswith("UPDATE gastos SET eliminado = 1")
    assert not q.startswith("DELETE")
    assert p == ("gerencia", 1)
    assert db.commits == 1
    assert "Eliminó el gasto 1" in db.movimientos[0][1]


def test_eliminar_sin_gerencia_da_403_y_no_escribe(client, db):
    r = client.delete("/gastos/1", headers=auth("tok-ventas"))
    assert r.status_code == 403
    assert db.escrituras() == []


def test_eliminar_gasto_inexistente_da_404(client, db):
    r = client.delete("/gastos/999", headers=auth("tok-gerencia"))
    assert r.status_code == 404
    assert db.escrituras() == []


def test_eliminar_dos_veces_da_404_la_segunda(client, db):
    # El fake marca eliminado al ver el UPDATE, como haría la BD
    db.gastos[1]["eliminado"] = 1
    assert client.delete("/gastos/1", headers=auth("tok-gerencia")).status_code == 404


def test_eliminar_error_mysql_da_500_con_rollback(client, db):
    db.error_en = "UPDATE gastos SET eliminado"
    db.error = mysql.connector.Error("boom")
    r = client.delete("/gastos/1", headers=auth("tok-gerencia"))
    assert r.status_code == 500
    assert db.rollbacks == 1 and db.commits == 0


# ---------- listados: incluyen id y excluyen eliminados (dashboard) ----------

def test_listados_filtran_eliminados_e_incluyen_id(client, db):
    consultas = []
    original = FakeCursor.execute

    def espia(self, query, params=None):
        consultas.append(" ".join(query.split()))
        return original(self, query, params)

    FakeCursor.execute = espia
    try:
        assert client.get("/gastos").status_code == 200
        assert client.get("/consultagastos", params={"usuario": "gerencia"}).status_code == 200
        assert client.get("/consultagastos", params={"usuario": "ventas"}).status_code == 200
    finally:
        FakeCursor.execute = original
    selects = [q for q in consultas if "FROM gastos" in q]
    assert len(selects) == 3
    assert all(q.startswith("SELECT id,") and "eliminado = 0" in q for q in selects)


# ---------- migración de columnas ----------

def test_asegurar_columnas_agrega_las_faltantes(db):
    gastos.asegurar_columnas_eliminado()
    alters = [q for q, _ in db.escrituras() if q.startswith("ALTER TABLE gastos ADD COLUMN")]
    assert len(alters) == 3
    assert any("eliminado TINYINT" in q for q in alters)


def test_asegurar_columnas_es_idempotente(db):
    db.columnas += ["eliminado", "eliminado_por", "fecha_eliminado"]
    gastos.asegurar_columnas_eliminado()
    assert db.escrituras() == []


# ---------- permisos ----------

def test_es_gerencia(monkeypatch):
    monkeypatch.delenv("USUARIOS_GERENCIA", raising=False)
    assert permisos.es_gerencia("gerencia") and permisos.es_gerencia(" FParra ")
    assert not permisos.es_gerencia("ventas")
    assert not permisos.es_gerencia(None) and not permisos.es_gerencia("")


def test_usuarios_gerencia_configurables_por_entorno(monkeypatch):
    monkeypatch.setenv("USUARIOS_GERENCIA", "ana, luis")
    assert permisos.es_gerencia("Ana") and permisos.es_gerencia("luis")
    assert not permisos.es_gerencia("gerencia")
