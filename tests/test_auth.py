# Tests de autenticación (main.py + permisos.py): login sin filtraciones, token con vigencia,
# cambio de contraseña con sesión, límite de intentos (slowapi) y CORS restringido.
import bcrypt
import pytest
from fastapi.testclient import TestClient

import main
import permisos


def _hash(plano):
    return bcrypt.hashpw(plano.encode(), bcrypt.gensalt(rounds=4)).decode()


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._res = []

    def execute(self, query, params=None):
        q = " ".join(query.split())
        db = self.db
        db.ejecutados.append((q, params))
        self._res = []
        if q.startswith("SELECT u.password_hash, u.id, e.estatus"):
            u = db.usuarios.get(params[0])
            self._res = [{"password_hash": u["hash"], "id": u["id"], "estatus": u["estatus"]}] if u else []
        elif q.startswith("SELECT nombre_usuario FROM usuarios WHERE token = %s AND token_expira > NOW()"):
            ok = [n for n, u in db.usuarios.items() if u.get("token") == params[0] and u.get("vigente", True)]
            self._res = [{"nombre_usuario": ok[0]}] if ok else []
        elif q.startswith("SELECT password_hash FROM usuarios WHERE nombre_usuario"):
            u = db.usuarios.get(params[0])
            self._res = [{"password_hash": u["hash"]}] if u else []
        elif q.startswith("SELECT nombre_usuario FROM usuarios WHERE nombre_usuario"):
            self._res = [{"nombre_usuario": params[0]}] if params[0] in db.usuarios else []
        elif q.startswith("UPDATE usuarios SET token"):
            db.usuarios[params[2]]["token"] = params[0]
            db.horas_token = params[1]
        elif q.startswith("UPDATE usuarios SET password_hash"):
            db.usuarios[params[1]]["hash"] = params[0]
        elif q.startswith("INSERT INTO registro_login"):
            db.logins.append(params)

    def fetchone(self):
        return self._res[0] if self._res else None

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        self.ejecutados = []
        self.logins = []
        self.horas_token = None
        self.usuarios = {
            "ana": {"id": 1, "hash": _hash("Secreta1"), "estatus": 1, "token": "tok-ana"},
            "baja": {"id": 2, "hash": _hash("Secreta1"), "estatus": 0},
            "gerencia": {"id": 3, "hash": _hash("Jefe1234"), "estatus": 1, "token": "tok-gerencia"},
            "vence": {"id": 4, "hash": _hash("Secreta1"), "estatus": 1, "token": "tok-vence", "vigente": False},
        }

    def cursor(self, dictionary=False):
        return FakeCursor(self)

    def commit(self):
        pass

    def is_connected(self):
        return True

    def close(self):
        pass


@pytest.fixture
def db(monkeypatch):
    base = FakeDB()
    monkeypatch.setattr(main, "get_db_connection", lambda: base)
    monkeypatch.setattr(permisos, "get_db_connection", lambda: base)
    main.limiter.reset()
    yield base
    main.limiter.reset()


@pytest.fixture
def client(db):
    return TestClient(main.app)


def login(client, usuario, password, ip="10.0.0.1"):
    return client.post("/login", json={"usuario": usuario, "password": password}, headers={"X-Forwarded-For": ip})


def con_token(token):
    return {"Authorization": f"Bearer {token}"}


# ---------- login ----------

def test_login_ok_emite_token_con_vigencia_y_registra_el_ingreso(client, db):
    r = login(client, "ana", "Secreta1")
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["access_token"] and cuerpo["expira_en_horas"] == permisos.token_horas()
    assert db.usuarios["ana"]["token"] == cuerpo["access_token"]
    assert db.horas_token == permisos.token_horas()
    assert len(db.logins) == 1


def test_login_con_password_mala_no_registra_ingreso_ni_cambia_el_token(client, db):
    r = login(client, "ana", "mala")
    assert r.status_code == 401 and r.json()["detail"] == "Usuario o contraseña incorrectos"
    assert db.logins == [] and db.usuarios["ana"]["token"] == "tok-ana"


def test_usuario_inexistente_da_el_mismo_rechazo_y_corre_bcrypt(client, db, monkeypatch):
    llamadas = []
    original = main.verify_password
    monkeypatch.setattr(main, "verify_password", lambda p, h: llamadas.append(h) or original(p, h))
    r = login(client, "nadie", "Secreta1")
    assert r.status_code == 401 and r.json()["detail"] == "Usuario o contraseña incorrectos"
    assert llamadas == [main._HASH_FALSO]  # se verificó contra el hash de relleno


def test_usuario_inactivo_solo_se_revela_con_la_password_correcta(client, db):
    assert login(client, "baja", "mala").status_code == 401
    r = login(client, "baja", "Secreta1")
    assert r.status_code == 403 and "inactivo" in r.json()["detail"]
    assert db.logins == [] and "token" not in db.usuarios["baja"]


def test_hash_invalido_en_bd_es_rechazo_no_error_500(client, db):
    db.usuarios["ana"]["hash"] = "no-es-un-hash-bcrypt"
    assert login(client, "ana", "Secreta1").status_code == 401


# ---------- token con vigencia ----------

def test_ruta_protegida_acepta_token_vigente_y_rechaza_vencido_o_ausente(client, db):
    assert client.get("/productos/ubicaciones/X", headers=con_token("tok-vence")).status_code == 401
    assert client.get("/productos/ubicaciones/X", headers=con_token("inventado")).status_code == 401
    assert client.get("/productos/ubicaciones/X").status_code in (401, 403)
    # la validación usa la columna de vigencia
    assert any("token_expira > NOW()" in q for q, _ in db.ejecutados)


def test_main_y_routers_validan_el_token_con_la_misma_dependencia():
    assert main.obtener_usuario_actual is permisos.usuario_autenticado


# ---------- cambio de contraseña ----------

def cambio(client, token, usuario, actual, nueva="Nueva1234"):
    return client.put("/cambio-passw", json={"usuario": usuario, "password_actual": actual, "password_nueva": nueva},
                      headers={**con_token(token), "X-Forwarded-For": "10.0.0.9"})


def test_cambio_passw_exige_sesion(client, db):
    r = client.put("/cambio-passw", json={"usuario": "gerencia", "password_actual": "x", "password_nueva": "Nueva1234"})
    assert r.status_code in (401, 403)
    assert db.usuarios["gerencia"]["hash"] != "x"


def test_no_se_puede_cambiar_la_password_de_otro_sin_ser_gerencia(client, db):
    hash_antes = db.usuarios["gerencia"]["hash"]
    r = cambio(client, "tok-ana", "gerencia", "Secreta1")
    assert r.status_code == 403
    assert db.usuarios["gerencia"]["hash"] == hash_antes


def test_cambio_propio_exige_la_password_actual(client, db):
    hash_antes = db.usuarios["ana"]["hash"]
    assert cambio(client, "tok-ana", "ana", "equivocada").status_code == 400  # no 401: no debe cerrar la sesión del panel
    assert db.usuarios["ana"]["hash"] == hash_antes
    r = cambio(client, "tok-ana", "ana", "Secreta1")
    assert r.status_code == 200
    assert bcrypt.checkpw(b"Nueva1234", db.usuarios["ana"]["hash"].encode())


def test_gerencia_puede_cambiar_la_de_otro_dando_su_propia_password(client, db):
    assert cambio(client, "tok-gerencia", "ana", "Secreta1").status_code == 400  # la de ana no vale como "actual"
    r = cambio(client, "tok-gerencia", "ana", "Jefe1234")
    assert r.status_code == 200
    assert bcrypt.checkpw(b"Nueva1234", db.usuarios["ana"]["hash"].encode())


# ---------- límite de intentos ----------

def test_login_se_limita_por_ip_y_responde_429_en_espanol(client, db):
    for _ in range(10):
        assert login(client, "ana", "mala", ip="203.0.113.7").status_code == 401
    r = login(client, "ana", "Secreta1", ip="203.0.113.7")
    assert r.status_code == 429
    assert "Demasiados intentos" in r.json()["detail"] and r.headers["Retry-After"] == "60"
    # otra IP tiene su propio contador
    assert login(client, "ana", "Secreta1", ip="203.0.113.8").status_code == 200


def test_la_ip_se_toma_del_final_de_x_forwarded_for_no_del_principio(client, db):
    # El cliente inventa el primer valor; el proxy agrega el real al final.
    for i in range(10):
        login(client, "ana", "mala", ip=f"1.1.1.{i}, 198.51.100.4")
    assert login(client, "ana", "Secreta1", ip="9.9.9.9, 198.51.100.4").status_code == 429


# ---------- CORS ----------

def test_cors_solo_permite_el_panel_y_localhost(client):
    panel = main.ORIGENES_CORS[0]
    ok = client.options("/login", headers={"Origin": panel, "Access-Control-Request-Method": "POST",
                                           "Access-Control-Request-Headers": "authorization,content-type"})
    assert ok.headers.get("access-control-allow-origin") == panel
    local = client.options("/login", headers={"Origin": "http://localhost:8765", "Access-Control-Request-Method": "POST"})
    assert local.headers.get("access-control-allow-origin") == "http://localhost:8765"
    ajeno = client.options("/login", headers={"Origin": "https://sitio-malicioso.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in ajeno.headers
    http_ajeno = client.options("/login", headers={"Origin": "http://evil.localhost.example.com", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in http_ajeno.headers
