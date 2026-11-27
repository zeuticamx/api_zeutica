"""Renombrar una cuenta (PUT /usuarios/{nombre}/nombre): lógica pura y ruta con BD falsa."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from servicios import usuario_renombre_logica as R
from servicios.permisos import requerir_gerencia
from routers import usuarios


# ---------- lógica pura ----------

@pytest.mark.parametrize("col", ["usuario", "Vendedor", "autor", "nombre_usuario", "editado_por", "eliminado_por",
                                 "usuario_registro", "usuario_original", "vendedor_anterior", "actualizado_por"])
def test_columnas_que_guardan_usuario(col):
    assert R.columna_guarda_usuario(col)


@pytest.mark.parametrize("col", ["", None, "nombre", "cliente", "sku", "usuarios_total", "id", "porcentaje", "importe"])
def test_columnas_que_no(col):
    assert not R.columna_guarda_usuario(col)


def test_citar_escapa_backticks():
    assert R.citar("a`b") == "`a``b`"


def test_validar_nuevo_nombre(monkeypatch):
    nunca = lambda n: False
    assert R.validar_nuevo_nombre("ana", " cecilia ", nunca) == "cecilia"
    for malo in ("ab", "con espacio", "", "a" * 41, "ñandú"):
        with pytest.raises(ValueError):
            R.validar_nuevo_nombre("ana", malo, nunca)
    with pytest.raises(ValueError):  # no se renombra una cuenta de gerencia
        R.validar_nuevo_nombre("jefe", "jefa", lambda n: n == "jefe")
    with pytest.raises(ValueError):  # ni se toma un nombre reservado
        R.validar_nuevo_nombre("ana", "jefe", lambda n: n == "jefe")


def test_resumir():
    a = [{"tabla": "p", "columna": "usuario", "filas": 3}, {"tabla": "p", "columna": "editado_por", "filas": 1},
         {"tabla": "v", "columna": "usuario", "filas": 0}]
    assert R.resumir(a) == {"total": 4, "tablas": 1}


# ---------- ruta ----------

class Cursor:
    def __init__(self, db):
        self.db = db
        self._res = []
        self.rowcount = 0

    def execute(self, query, params=None):
        q = " ".join(query.split())
        db = self.db
        db.ejecutados.append((q, params))
        self._res = []
        if q.startswith("SELECT id, nombre_usuario FROM usuarios WHERE nombre_usuario = %s FOR UPDATE"):
            self._res = [{"id": 1, "nombre_usuario": db.cuenta}] if params[0] == db.cuenta else []
        elif q.startswith("SELECT 1 FROM usuarios WHERE nombre_usuario = %s AND id <> %s"):
            self._res = [{"x": 1}] if params[0] in db.otras else []
        elif q.startswith("SELECT c.TABLE_NAME"):
            self._res = [{"tabla": t, "columna": c} for t, c in db.columnas]
        elif q.startswith("SELECT COUNT(*) AS n FROM"):
            self._res = [{"n": db.filas.get(q.split("FROM ")[1].split(" WHERE")[0], 0)}]
        elif q.startswith("UPDATE usuarios SET nombre_usuario"):
            db.cuenta = params[0]
        elif q.startswith("UPDATE `"):
            self.rowcount = db.filas.get(q.split("UPDATE ")[1].split(" SET")[0], 0)
        elif q.startswith("SET FOREIGN_KEY_CHECKS"):
            db.fk.append(q[-1])
        else:
            raise AssertionError(f"consulta no esperada: {q}")

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return list(self._res)

    def close(self):
        pass


class DB:
    def __init__(self):
        self.cuenta = "cecilia_vieja"
        self.otras = {"ocupado"}
        self.columnas = [("pendientes", "usuario"), ("pendientes", "intercambiado_por"), ("empleados", "usuario"),
                         ("ventas", "cliente"), ("usuarios", "nombre_usuario")]
        self.filas = {"`pendientes`": 4, "`empleados`": 1}
        self.ejecutados = []
        self.fk = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, dictionary=False):
        return Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


@pytest.fixture
def montar(monkeypatch):
    monkeypatch.setenv("USUARIOS_GERENCIA", "jefe")
    bit = []

    def _montar(quien="jefe"):
        db = DB()
        monkeypatch.setattr(usuarios, "get_db_connection", lambda: db)
        monkeypatch.setattr(usuarios.mov_reg, "registrar_movimiento", lambda *a, **k: bit.append(a))
        app = FastAPI()
        app.include_router(usuarios.router)
        app.dependency_overrides[requerir_gerencia] = lambda: quien
        return db, TestClient(app), bit
    return _montar


def _put(c, nombre="cecilia_vieja", **cuerpo):
    return c.put(f"/usuarios/{nombre}/nombre", json={"nuevo_nombre": "cecilia", **cuerpo})


def test_renombra_en_la_cuenta_y_en_las_tablas_con_columnas_de_usuario(montar):
    db, c, bit = montar()
    r = _put(c)
    assert r.status_code == 200
    j = r.json()
    assert j["status"] == "success" and j["nombre_anterior"] == "cecilia_vieja" and j["nombre_usuario"] == "cecilia"
    assert j["total"] == 9 and j["tablas"] == 2
    assert {(a["tabla"], a["columna"]) for a in j["afectados"]} == {("pendientes", "usuario"), ("pendientes", "intercambiado_por"), ("empleados", "usuario")}
    assert db.cuenta == "cecilia" and db.commits == 1 and db.rollbacks == 0
    assert db.fk == ["0", "1"]  # llaves apagadas durante el cambio y restauradas al final
    assert bit and bit[0][0] == "jefe" and bit[0][2] == "Usuarios"
    # la cuenta se actualiza una sola vez (se excluye del barrido) y su sesión se cierra
    upd = [q for q, _ in db.ejecutados if q.startswith("UPDATE usuarios")]
    assert len(upd) == 1 and "token = NULL" in upd[0]


def test_simular_cuenta_sin_escribir(montar):
    db, c, bit = montar()
    r = _put(c, simular=True)
    assert r.status_code == 200 and r.json()["status"] == "simulacion" and r.json()["total"] == 9
    assert db.cuenta == "cecilia_vieja" and db.commits == 0 and db.rollbacks == 1 and bit == []
    assert not any(q.startswith("UPDATE") for q, _ in db.ejecutados)


def test_nombre_ocupado_da_409_sin_escribir(montar):
    db, c, _ = montar()
    r = c.put("/usuarios/cecilia_vieja/nombre", json={"nuevo_nombre": "ocupado"})
    assert r.status_code == 409 and db.commits == 0 and not any(q.startswith("UPDATE") for q, _ in db.ejecutados)


def test_cuenta_inexistente_da_404(montar):
    _, c, _ = montar()
    assert _put(c, nombre="fantasma").status_code == 404


def test_mismo_nombre_es_sin_cambios(montar):
    db, c, _ = montar()
    r = c.put("/usuarios/cecilia_vieja/nombre", json={"nuevo_nombre": "cecilia_vieja"})
    assert r.status_code == 200 and r.json()["status"] == "sin_cambios" and db.commits == 0


@pytest.mark.parametrize("nuevo", ["ab", "con espacio", "jefe"])
def test_nombre_invalido_o_reservado_da_422(montar, nuevo):
    _, c, _ = montar()
    assert c.put("/usuarios/cecilia_vieja/nombre", json={"nuevo_nombre": nuevo}).status_code == 422


def test_no_se_renombra_una_cuenta_de_gerencia(montar):
    _, c, _ = montar(quien="otro")
    assert c.put("/usuarios/jefe/nombre", json={"nuevo_nombre": "jefa"}).status_code == 422


def test_no_se_renombra_la_propia_cuenta(montar):
    _, c, _ = montar(quien="cecilia_vieja")
    assert _put(c).status_code == 409
