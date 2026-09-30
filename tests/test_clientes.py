# Tests alta de clientes (POST /clientenuevo/{usuario}): validación, duplicados y
# persistencia de uso_cfdi / dias_credito. MySQL, Telegram y bitácora van simulados.
from unittest.mock import AsyncMock

import mysql.connector
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import clientes


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._resultado = []
        self.lastrowid = None

    def execute(self, query, params=None):
        q = " ".join(query.split())
        self.db.ejecutados.append((q, params))
        if self.db.error_en and self.db.error_en in q:
            raise self.db.error
        if q.startswith("SELECT id FROM clientes WHERE LOWER(TRIM(nombre))"):
            self._resultado = [(7,)] if params[0].lower() in self.db.nombres else []
        elif q.startswith("INSERT INTO clientes"):
            self.lastrowid = 42

    def fetchone(self):
        return self._resultado[0] if self._resultado else None

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        self.ejecutados = []
        self.nombres = set()  # nombres (en minúsculas) ya registrados
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

    def inserts(self):
        return [(q, p) for q, p in self.ejecutados if q.startswith("INSERT INTO clientes")]


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


def cliente_valido(**extra):
    datos = {
        "nombre": "Ferretería Nueva", "email": "a@b.mx", "empresa": "Ferretería Nueva SA",
        "contacto": "Ana", "telefono": 3312345678, "direccion": "Calle 1",
        "rfc": "XAXX010101000", "cp": 44100, "regimen": "601", "uso_cfdi": "G03",
        "frecuencia": "Mensual", "usuario": "tester", "credito": True,
        "monto_credito": 5000, "dias_credito": 30,
    }
    datos.update(extra)
    return datos


def test_alta_valida_devuelve_id_y_persiste_uso_cfdi_y_dias_credito(client, db):
    r = client.post("/clientenuevo/tester", json=cliente_valido())
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == 42 and body["id "] == 42  # "id " se conserva por compatibilidad
    assert body["nombre"] == "Ferretería Nueva"
    (q, p), = db.inserts()
    assert "usocfdi" in q and "dias_credito" in q
    assert "G03" in p and 30 in p
    assert db.commits == 1 and db.rollbacks == 0


def test_usocdfi_historico_sigue_aceptandose(client, db):
    datos = cliente_valido(usocdfi="P01")
    datos.pop("uso_cfdi")
    assert client.post("/clientenuevo/tester", json=datos).status_code == 200
    assert "P01" in db.inserts()[0][1]


def test_nombre_se_recorta_antes_de_guardar(client, db):
    r = client.post("/clientenuevo/tester", json=cliente_valido(nombre="  Cliente X  "))
    assert r.status_code == 200
    assert r.json()["nombre"] == "Cliente X"
    assert "Cliente X" in db.inserts()[0][1]


@pytest.mark.parametrize("nombre", ["", "   "])
def test_nombre_vacio_da_422_y_no_inserta(client, db, nombre):
    r = client.post("/clientenuevo/tester", json=cliente_valido(nombre=nombre))
    assert r.status_code == 422
    assert "nombre" in r.json()["detail"].lower()
    assert db.inserts() == []


@pytest.mark.parametrize("campo", ["empresa", "contacto", "telefono", "nombre"])
def test_campos_obligatorios_faltantes_dan_422(client, db, campo):
    datos = cliente_valido()
    datos.pop(campo)
    assert client.post("/clientenuevo/tester", json=datos).status_code == 422
    assert db.inserts() == []


def test_nombre_duplicado_da_409_sin_importar_mayusculas(client, db):
    db.nombres.add("ferretería nueva")
    r = client.post("/clientenuevo/tester", json=cliente_valido(nombre="FERRETERÍA NUEVA"))
    assert r.status_code == 409
    assert "ya existe" in r.json()["detail"].lower()
    assert db.inserts() == []


def test_error_de_mysql_da_500_con_rollback(client, db):
    db.error_en = "INSERT INTO clientes"
    db.error = mysql.connector.Error("boom")
    r = client.post("/clientenuevo/tester", json=cliente_valido())
    assert r.status_code == 500
    assert db.rollbacks == 1 and db.commits == 0
