# Tests de consulta (GET /clientes), edición (POST /editcliente/{usuario}) y clientes
# potenciales. MySQL, Telegram y bitácora van simulados.
from unittest.mock import AsyncMock

import mysql.connector
import pytest
from fastapi import FastAPI, HTTPException
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
        if q.startswith("SELECT"):
            self.db.selects.append(q)

    def executemany(self, query, valores):
        self.db.ejecutados.append((" ".join(query.split()), valores))
        self.rowcount = len(valores)

    def fetchall(self):
        return self.db.filas

    def fetchone(self):
        # Las consultas de existencia devuelven, en orden, las filas de db.respuestas_one
        return self.db.respuestas_one.pop(0) if self.db.respuestas_one else None

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        self.ejecutados = []
        self.filas = []
        self.selects = []
        self.respuestas_one = []
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
    app.dependency_overrides[clientes.requerir_gerencia] = lambda: "gerencia"
    return TestClient(app)


@pytest.fixture
def client_vendedor():
    # Simula a un usuario sin nivel gerencia: la dependencia responde 403
    def _no_gerencia():
        raise HTTPException(status_code=403, detail="Se requiere nivel de gerencia para esta acción")

    app = FastAPI()
    app.include_router(clientes.router)
    app.dependency_overrides[clientes.requerir_gerencia] = _no_gerencia
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
    assert "WHERE eliminado = 0 ORDER BY id DESC" in db.ejecutados[0][0]


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
    assert q.endswith("WHERE id = %s AND eliminado = 0") and p[-1] == 42
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


# ---------- Baja lógica: DELETE /clientes/{id}, restaurar y listado de eliminados ----------

def test_eliminar_marca_baja_logica_y_registra(client, db):
    db.respuestas_one = [{"id": 7, "nombre": "Cliente X", "empresa": "X SA"}]
    r = client.delete("/clientes/7")
    assert r.status_code == 200 and r.json()["id"] == 7
    (q, p), = db.updates()
    assert "eliminado = 1" in q and "eliminado = 0" in q.split("WHERE")[1]
    assert p == ("gerencia", 7)
    assert db.commits == 1
    assert not any(q.startswith("DELETE") for q, _ in db.ejecutados)


def test_eliminar_inexistente_o_ya_eliminado_da_404(client, db):
    r = client.delete("/clientes/999")
    assert r.status_code == 404
    assert db.updates() == [] and db.commits == 0


def test_eliminar_con_error_de_mysql_da_500_con_rollback(client, db):
    db.respuestas_one = [{"id": 7, "nombre": "X", "empresa": "X"}]
    db.error_en = "UPDATE clientes SET"
    db.error = mysql.connector.Error("boom")
    assert client.delete("/clientes/7").status_code == 500
    assert db.rollbacks == 1 and db.commits == 0


def test_eliminar_y_restaurar_exigen_gerencia(client_vendedor, db):
    assert client_vendedor.delete("/clientes/7").status_code == 403
    assert client_vendedor.post("/clientes/7/restaurar").status_code == 403
    assert client_vendedor.get("/clientes-eliminados").status_code == 403
    assert db.ejecutados == []


def test_restaurar_limpia_los_campos_de_baja(client, db):
    db.respuestas_one = [{"id": 7, "nombre": "Cliente X"}, None]  # existe eliminado, sin duplicado activo
    r = client.post("/clientes/7/restaurar")
    assert r.status_code == 200
    (q, p), = db.updates()
    assert "eliminado = 0" in q and "eliminado_por = NULL" in q and p == (7,)
    assert db.commits == 1


def test_restaurar_cliente_no_eliminado_da_404(client, db):
    assert client.post("/clientes/7/restaurar").status_code == 404
    assert db.updates() == []


def test_restaurar_con_nombre_duplicado_activo_da_409(client, db):
    db.respuestas_one = [{"id": 7, "nombre": "Cliente X"}, {"id": 9}]
    r = client.post("/clientes/7/restaurar")
    assert r.status_code == 409
    assert db.updates() == []


def test_listado_de_eliminados(client, db):
    db.filas = [{"id": 7, "nombre": "X"}]
    r = client.get("/clientes-eliminados")
    assert r.status_code == 200 and r.json() == db.filas
    assert "eliminado = 1" in db.selects[0]


def test_listado_de_eliminados_vacio_devuelve_lista_vacia(client, db):
    r = client.get("/clientes-eliminados")
    assert r.status_code == 200 and r.json() == []
