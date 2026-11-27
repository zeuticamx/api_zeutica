# pendientes-agregar y pendientes-actualizar (Monitor Gerencia) validan que el usuario asignado exista en `usuarios`.
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from servicios.permisos import usuario_autenticado
from routers import pendientes

CUERPO = {"usuario": "beto", "actividad": "Llamar", "prioridad": "Alta", "estado": "Pendiente",
          "observaciones": None, "fecha_promesa": None}


class Cursor:
    def __init__(self, db):
        self.db = db
        self._res = []

    def execute(self, query, params=None):
        q = " ".join(query.split())
        self.db.ejecutados.append((q, params))
        self._res = []
        if q.startswith("SELECT nombre_usuario FROM usuarios WHERE LOWER(nombre_usuario) = LOWER(%s)"):
            self._res = [{"nombre_usuario": u} for u in self.db.usuarios if u.lower() == params[0].strip().lower()]
        elif q.startswith("SELECT usuario FROM pendientes WHERE id = %s"):
            dueno = self.db.pendientes.get(params[0])
            self._res = [{"usuario": dueno}] if dueno is not None else []
        elif q.startswith("INSERT INTO pendientes") or q.startswith("UPDATE pendientes"):
            self.db.escrituras.append((q, params))
        else:
            raise AssertionError(f"consulta no esperada: {q}")

    def fetchone(self):
        return self._res[0] if self._res else None

    def close(self):
        pass


class DB:
    def __init__(self, usuarios, pendientes=None):
        self.usuarios = usuarios
        self.pendientes = pendientes or {}  # id -> usuario dueño actual
        self.ejecutados = []
        self.escrituras = []
        self.commits = 0

    def cursor(self, dictionary=False):
        return Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def montar(monkeypatch):
    def _montar(usuarios, pendientes_bd=None):
        db = DB(usuarios, pendientes_bd)
        monkeypatch.setattr(pendientes, "get_db_connection", lambda: db)
        monkeypatch.setattr(pendientes.mov_reg, "registrar_movimiento", lambda *a, **k: None)
        monkeypatch.setattr(pendientes, "_aviso_pendiente", lambda *a, **k: None)
        app = FastAPI()
        app.include_router(pendientes.router)
        app.dependency_overrides[usuario_autenticado] = lambda: "gerencia"
        return db, TestClient(app)
    return _montar


def test_agregar_a_usuario_existente(montar):
    db, c = montar(["beto"])
    assert c.post("/pendientes-agregar", json=CUERPO).status_code == 200
    assert len(db.escrituras) == 1 and db.commits == 1


def test_agregar_a_usuario_inexistente_da_422_sin_escribir(montar):
    db, c = montar(["ana"])
    r = c.post("/pendientes-agregar", json=CUERPO)
    assert r.status_code == 422 and "beto" in r.json()["detail"]
    assert db.escrituras == [] and db.commits == 0


def test_actualizar_reasignando_a_usuario_inexistente_da_422(montar):
    db, c = montar(["ana"], {7: "ana"})
    r = c.patch("/pendientes-actualizar/7", json=CUERPO)
    assert r.status_code == 422
    assert db.escrituras == [] and db.commits == 0


def test_actualizar_reasignando_a_usuario_existente(montar):
    db, c = montar(["ana", "beto"], {7: "ana"})
    assert c.patch("/pendientes-actualizar/7", json=CUERPO).status_code == 200
    assert len(db.escrituras) == 1


def test_actualizar_sin_cambiar_dueno_que_ya_no_existe_se_permite(montar):
    # El dueño fue eliminado de `usuarios`, pero su tarea se puede seguir editando sin reasignarla.
    db, c = montar(["ana"], {7: "beto"})
    assert c.patch("/pendientes-actualizar/7", json=CUERPO).status_code == 200
    assert not any("FROM usuarios" in q for q, _ in db.ejecutados)
