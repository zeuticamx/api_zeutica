# Tests de routers/prospectos.py: editar (campos, uso_cfdi, reasignación), eliminar prospecto
# y borrado lógico de seguimientos. MySQL y bitácora van simulados; la app de prueba monta solo
# prospectos.router (el usuario sale del token vía permisos.usuario_autenticado).
import mysql.connector
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import permisos
from routers import prospectos


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._resultado = []
        self.rowcount = 0
        self.lastrowid = None

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
        elif q.startswith("SELECT nombre_usuario FROM usuarios WHERE nombre_usuario"):
            self._resultado = [{"nombre_usuario": params[0]}] if params[0] in db.usuarios else []
        elif q.startswith("SELECT * FROM prospectos WHERE id"):
            p = db.prospectos.get(params[0])
            self._resultado = [dict(p)] if p else []
        elif q.startswith("SELECT id, vendedor, tipo FROM prospectos_seguimientos"):
            seg = db.seguimientos.get(params[0])
            self._resultado = [dict(seg)] if seg else []
        elif q.startswith("SELECT id FROM clientes WHERE LOWER"):
            self._resultado = []
        elif q.startswith("INSERT INTO clientes"):
            self.lastrowid = 900
        elif q.startswith("SELECT vendedor, tipo, fecha"):
            self._resultado = [dict(f) for f in db.historial]
        elif q.startswith(("UPDATE", "INSERT")):
            self.rowcount = 1
        else:
            for marca, filas in db.respuestas:
                if marca in q:
                    self._resultado = [dict(f) for f in filas]
                    break

    def fetchone(self):
        return self._resultado[0] if self._resultado else None

    def fetchall(self):
        return list(self._resultado)

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        self.tokens = {"tok-ger": "gerencia", "tok-ana": "ana", "tok-luis": "luis"}
        self.usuarios = {"gerencia", "ana", "luis"}
        # 1: de ana · 2: de luis · 3: de ana, ya convertido
        self.prospectos = {
            1: {"id": 1, "nombre": "Prospecto Ana", "vendedor": "ana", "etapa": "nuevo"},
            2: {"id": 2, "nombre": "Prospecto Luis", "vendedor": "luis", "etapa": "contactado"},
            3: {"id": 3, "nombre": "Ya Cliente", "vendedor": "ana", "etapa": "convertido", "cliente_id": 50},
        }
        # seguimientos: 10 de ana (prospecto 1) · 11 de gerencia (prospecto 1)
        self.seguimientos = {10: {"id": 10, "vendedor": "ana", "tipo": "llamada"},
                             11: {"id": 11, "vendedor": "gerencia", "tipo": "correo"}}
        self.historial = []
        self.respuestas = []
        self.ejecutados = []
        self.commits = 0
        self.error_en = None
        self.error = None

    def cursor(self, dictionary=False):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass

    def consultas(self, inicio):
        return [(q, p) for q, p in self.ejecutados if q.startswith(inicio)]


@pytest.fixture
def db(monkeypatch):
    base = FakeDB()
    monkeypatch.setattr(prospectos, "get_db_connection", lambda: base)
    monkeypatch.setattr(permisos, "get_db_connection", lambda: base)
    monkeypatch.setenv("USUARIOS_GERENCIA", "gerencia")
    movimientos = []
    monkeypatch.setattr(prospectos.mov_reg, "registrar_movimiento", lambda *a, **k: movimientos.append(a))
    base.movimientos = movimientos

    async def _telegram(*a, **k):
        return None

    monkeypatch.setattr(prospectos, "send_telegram_alert", _telegram)
    return base


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(prospectos.router)
    return TestClient(app)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


ANA, LUIS, GER = auth("tok-ana"), auth("tok-luis"), auth("tok-ger")


# ---------- Editar prospecto ----------


def test_editar_actualiza_solo_lo_enviado_y_recorta_el_nombre(client, db):
    r = client.patch("/prospectos/1", json={"nombre": "  Nuevo Nombre  ", "telefono": 3312345678, "credito": True}, headers=ANA)
    assert r.status_code == 200
    q, p = db.consultas("UPDATE prospectos SET")[0]
    # El orden de las columnas sale de un set: se compara columna -> valor, no la cadena.
    asignaciones = q.split(" SET ")[1].split(" WHERE ")[0].split(", ")
    columnas = [a.replace(" = %s", "") for a in asignaciones]
    assert dict(zip(columnas, p[:-1])) == {"nombre": "Nuevo Nombre", "telefono": 3312345678, "credito": 1}
    assert q.endswith("WHERE id = %s AND eliminado = 0") and p[-1] == 1
    assert db.movimientos and "Editó el prospecto Prospecto Ana" in db.movimientos[0][1]


def test_editar_uso_cfdi_del_panel_se_guarda_en_usocfdi(client, db):
    # Regresión: el filtro de columnas descartaba 'uso_cfdi' y nunca se guardaba.
    r = client.patch("/prospectos/1", json={"uso_cfdi": "G03"}, headers=ANA)
    assert r.status_code == 200 and r.json()["actualizados"] == ["usocfdi"]
    q, p = db.consultas("UPDATE prospectos SET")[0]
    assert "usocfdi = %s" in q and p[0] == "G03"


def test_editar_nombre_vacio_da_422_y_sin_campos_da_422(client, db):
    assert client.patch("/prospectos/1", json={"nombre": "   "}, headers=ANA).status_code == 422
    assert client.patch("/prospectos/1", json={}, headers=ANA).status_code == 422
    assert db.consultas("UPDATE prospectos SET") == []


def test_editar_prospecto_ajeno_da_403_y_gerencia_si_puede(client, db):
    assert client.patch("/prospectos/2", json={"empresa": "X"}, headers=ANA).status_code == 403
    assert client.patch("/prospectos/2", json={"empresa": "X"}, headers=GER).status_code == 200


def test_editar_inexistente_da_404(client, db):
    assert client.patch("/prospectos/999", json={"empresa": "X"}, headers=GER).status_code == 404


def test_editar_convertido_da_409(client, db):
    r = client.patch("/prospectos/3", json={"empresa": "X"}, headers=ANA)
    assert r.status_code == 409
    assert db.consultas("UPDATE prospectos SET") == []


def test_reasignar_vendedor_solo_gerencia(client, db):
    assert client.patch("/prospectos/1", json={"vendedor": "luis"}, headers=ANA).status_code == 403
    r = client.patch("/prospectos/1", json={"vendedor": "luis"}, headers=GER)
    assert r.status_code == 200 and r.json()["actualizados"] == ["vendedor"]
    assert db.consultas("UPDATE prospectos SET")[0][1] == ("luis", 1)
    assert "reasignado a luis" in db.movimientos[-1][1]


def test_reasignar_a_usuario_inexistente_da_422(client, db):
    assert client.patch("/prospectos/1", json={"vendedor": "nadie"}, headers=GER).status_code == 422
    assert db.consultas("UPDATE prospectos SET") == []


def test_vendedor_igual_al_actual_no_cambia_nada_ni_pide_gerencia(client, db):
    r = client.patch("/prospectos/1", json={"vendedor": "ANA", "empresa": "Acme"}, headers=ANA)
    assert r.status_code == 200 and r.json()["actualizados"] == ["empresa"]


def test_editar_error_de_bd_da_500_sin_filtrar_el_texto(client, db):
    db.error_en = "UPDATE prospectos SET"
    db.error = mysql.connector.Error("secreto interno")
    r = client.patch("/prospectos/1", json={"empresa": "X"}, headers=ANA)
    assert r.status_code == 500 and "secreto" not in r.text


# ---------- Eliminar prospecto ----------


def test_eliminar_prospecto_propio_y_ajeno(client, db):
    assert client.delete("/prospectos/2", headers=ANA).status_code == 403
    r = client.delete("/prospectos/1", headers=ANA)
    assert r.status_code == 200 and r.json()["eliminado"] is True
    assert db.consultas("UPDATE prospectos SET eliminado = 1")


def test_eliminar_prospecto_convertido_da_409(client, db):
    assert client.delete("/prospectos/3", headers=ANA).status_code == 409
    assert db.consultas("UPDATE prospectos SET eliminado = 1") == []


# ---------- Eliminar seguimiento (borrado lógico) ----------


def test_autor_elimina_su_seguimiento_con_borrado_logico(client, db):
    r = client.delete("/prospectos/1/seguimientos/10", headers=ANA)
    assert r.status_code == 200 and r.json()["eliminado"] is True
    q, p = db.consultas("UPDATE prospectos_seguimientos SET eliminado = 1")[0]
    assert p == (10,)
    assert not db.consultas("DELETE")
    assert db.movimientos and "Eliminó un seguimiento (llamada)" in db.movimientos[0][1]


def test_seguimiento_de_otro_autor_da_403_pero_gerencia_si_puede(client, db):
    assert client.delete("/prospectos/1/seguimientos/11", headers=ANA).status_code == 403
    assert db.consultas("UPDATE prospectos_seguimientos SET eliminado = 1") == []
    assert client.delete("/prospectos/1/seguimientos/10", headers=GER).status_code == 200


def test_seguimiento_inexistente_o_de_otro_prospecto_da_404(client, db):
    assert client.delete("/prospectos/1/seguimientos/999", headers=ANA).status_code == 404
    # Quien no es dueño del prospecto ni siquiera llega al seguimiento.
    assert client.delete("/prospectos/2/seguimientos/10", headers=ANA).status_code == 403


def test_seguimiento_de_prospecto_convertido_da_409(client, db):
    assert client.delete("/prospectos/3/seguimientos/10", headers=ANA).status_code == 409


def test_seguimiento_busca_solo_dentro_del_prospecto_y_no_eliminados(client, db):
    client.delete("/prospectos/1/seguimientos/10", headers=ANA)
    q, p = db.consultas("SELECT id, vendedor, tipo FROM prospectos_seguimientos")[0]
    assert "prospecto_id = %s" in q and "eliminado = 0" in q and p == (10, 1)


# ---------- Los eliminados no cuentan ----------


def test_listado_ficha_y_conversion_ignoran_seguimientos_eliminados(client, db):
    client.get("/prospectos", headers=ANA)
    client.get("/prospectos/1", headers=ANA)
    client.post("/prospectos/1/convertir", headers=ANA)
    listado = db.consultas("SELECT p.*")[0][0]
    assert listado.count("s.eliminado = 0") == 2
    assert "eliminado = 0" in db.consultas("SELECT id, vendedor, tipo, fecha, resultado")[0][0]
    assert "eliminado = 0" in db.consultas("SELECT vendedor, tipo, fecha")[0][0]
