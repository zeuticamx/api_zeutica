# Tests de consulta (GET /clientes), edición (POST /editcliente/{usuario}) y clientes
# potenciales. MySQL, Telegram y bitácora van simulados.
from unittest.mock import AsyncMock

import mysql.connector
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import clientes


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = 0

    def execute(self, query, params=None):
        q = " ".join(query.split())
        self.db.ejecutados.append((q, params))
        if self.db.error_en and self.db.error_en in q:
            raise self.db.error
        if q.startswith("UPDATE clientes SET") or q.startswith("UPDATE clientes_potenciales"):
            self.rowcount = self.db.filas_afectadas

    def executemany(self, query, valores):
        self.db.ejecutados.append((" ".join(query.split()), valores))
        self.rowcount = len(valores)

    def fetchall(self):
        return self.db.filas

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        self.ejecutados = []
        self.filas = []
        self.filas_afectadas = 1
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

    def updates(self, tabla="clientes"):
        return [(q, p) for q, p in self.ejecutados if q.startswith(f"UPDATE {tabla} ")]


@pytest.fixture
def db(monkeypatch):
    base = FakeDB()
    monkeypatch.setattr(clientes, "get_db_connection", lambda: base)
    monkeypatch.setattr(clientes.mov_reg, "registrar_movimiento", lambda *a, **k: None)
    monkeypatch.setattr(clientes, "send_telegram_alert", AsyncMock())
    return base


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(clientes.router)
    return TestClient(app)


def edicion_valida(**extra):
    datos = {
        "id": 42, "nombre": "Cliente Editado", "email": "a@b.mx", "empresa": "Empresa SA",
        "contacto": "Ana", "telefono": 3312345678, "direccion": "Calle 1",
        "rfc": "XAXX010101000", "cp": 44100, "regimen": "601", "uso_cfdi": "G03",
        "frecuencia": "Mensual", "usuario": "tester", "credito": True,
        "monto_credito": 5000, "dias_credito": 30,
    }
    datos.update(extra)
    return datos


# ---------- GET /clientes ----------

def test_listado_devuelve_clientes(client, db):
    db.filas = [{"id": 2, "nombre": "B"}, {"id": 1, "nombre": "A"}]
    r = client.get("/clientes")
    assert r.status_code == 200
    assert r.json() == db.filas
    assert "ORDER BY id DESC" in db.ejecutados[0][0]


def test_listado_vacio_da_404(client, db):
    assert client.get("/clientes").status_code == 404


def test_listado_con_error_de_mysql_da_500(client, db):
    db.error_en = "SELECT * FROM clientes"
    db.error = mysql.connector.Error("boom")
    assert client.get("/clientes").status_code == 500


# ---------- POST /editcliente/{usuario} ----------

def test_edicion_valida_actualiza_y_hace_commit(client, db):
    r = client.post("/editcliente/tester", json=edicion_valida())
    assert r.status_code == 200
    assert r.json() == {"mensaje": "Cliente actualizado con éxito", "id": 42}
    (q, p), = db.updates()
    assert q.endswith("WHERE id = %s") and p[-1] == 42
    assert "Cliente Editado" in p and 30 in p
    assert db.commits == 1 and db.rollbacks == 0


def test_edicion_persiste_uso_cfdi_que_envia_el_panel(client, db):
    # El panel manda uso_cfdi; antes de corregirlo la edición lo ignoraba y guardaba None.
    client.post("/editcliente/tester", json=edicion_valida(uso_cfdi="G03"))
    assert "G03" in db.updates()[0][1]


def test_edicion_acepta_usocdfi_historico(client, db):
    datos = edicion_valida(usocdfi="P01")
    datos.pop("uso_cfdi")
    assert client.post("/editcliente/tester", json=datos).status_code == 200
    assert "P01" in db.updates()[0][1]


def test_edicion_de_cliente_inexistente_da_404(client, db):
    db.filas_afectadas = 0
    r = client.post("/editcliente/tester", json=edicion_valida(id=999))
    assert r.status_code == 404
    assert "no encontrado" in r.json()["detail"].lower()


@pytest.mark.parametrize("campo", ["id", "nombre", "empresa", "contacto", "telefono", "frecuencia", "dias_credito", "usuario", "credito"])
def test_edicion_con_campos_obligatorios_faltantes_da_422(client, db, campo):
    datos = edicion_valida()
    datos.pop(campo)
    assert client.post("/editcliente/tester", json=datos).status_code == 422
    assert db.updates() == []


def test_edicion_con_error_de_mysql_da_500_con_rollback(client, db):
    db.error_en = "UPDATE clientes SET"
    db.error = mysql.connector.Error("boom")
    r = client.post("/editcliente/tester", json=edicion_valida())
    assert r.status_code == 500
    assert db.rollbacks == 1 and db.commits == 0


# ---------- Clientes potenciales ----------

def test_potenciales_excluyen_descartados_por_defecto(client, db):
    db.filas = [{"id": 1}]
    assert client.get("/clientes-potenciales").status_code == 200
    assert db.ejecutados[0][1] == (0,)


def test_potenciales_descartados_vacios_devuelven_lista_vacia(client, db):
    r = client.get("/clientes-potenciales?descartados=true")
    assert r.status_code == 200 and r.json() == []
    assert db.ejecutados[0][1] == (1,)


def test_potenciales_sin_registros_da_404(client, db):
    assert client.get("/clientes-potenciales").status_code == 404


def test_actualizar_potencial_sin_correo_no_toca_la_columna(client, db):
    r = client.patch("/clientes-potenciales/5", json={"id": 5, "revisado": True})
    assert r.status_code == 200
    (q, p), = db.updates("clientes_potenciales")
    assert "correo_encontrado" not in q
    assert p == (True, False, 5)


def test_actualizar_potencial_con_correo_lo_guarda(client, db):
    client.patch("/clientes-potenciales/5", json={"id": 5, "revisado": True, "correo_encontrado": "x@y.mx"})
    (q, p), = db.updates("clientes_potenciales")
    assert "correo_encontrado = %s" in q
    assert p == (True, False, "x@y.mx", 5)


def test_notas_lote_no_se_confunde_con_el_id(client, db):
    r = client.patch("/clientes-potenciales/notas-lote", json=[{"id": 1, "notas": "a"}, {"id": 2, "notas": "b"}])
    assert r.status_code == 200
    assert r.json()["total_actualizados"] == 2
    assert db.ejecutados[0][1] == [("a", 1), ("b", 2)]
