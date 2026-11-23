"""Niveles de usuario: gerencia por entorno, developer (solo lectura de lo exclusivo) y ventas."""
import pytest
from fastapi import HTTPException
from starlette.requests import Request

import permisos
from routers import usuarios


def _req(metodo):
    return Request({"type": "http", "method": metodo, "headers": []})


def test_requerir_gerencia_deja_pasar_gerencia(monkeypatch):
    monkeypatch.setenv("USUARIOS_GERENCIA", "jefe")
    assert permisos.requerir_gerencia(_req("POST"), "jefe") == "jefe"


def test_developer_lee_pero_no_escribe(monkeypatch):
    monkeypatch.setenv("USUARIOS_GERENCIA", "jefe")
    monkeypatch.setattr(permisos, "nivel_usuario", lambda u: "developer")
    assert permisos.requerir_gerencia(_req("GET"), "dev") == "dev"
    for metodo in ("POST", "PUT", "PATCH", "DELETE"):
        with pytest.raises(HTTPException) as e:
            permisos.requerir_gerencia(_req(metodo), "dev")
        assert e.value.status_code == 403


def test_ventas_no_pasa_ni_para_leer(monkeypatch):
    monkeypatch.setenv("USUARIOS_GERENCIA", "jefe")
    monkeypatch.setattr(permisos, "nivel_usuario", lambda u: "ventas")
    with pytest.raises(HTTPException) as e:
        permisos.requerir_gerencia(_req("GET"), "vendedor")
    assert e.value.status_code == 403


def test_nivel_usuario_falla_cerrado(monkeypatch):
    monkeypatch.setenv("USUARIOS_GERENCIA", "jefe")
    monkeypatch.setattr(permisos, "get_db_connection", lambda: (_ for _ in ()).throw(RuntimeError("bd caída")))
    assert permisos.nivel_usuario("jefe") == "gerencia"
    assert permisos.nivel_usuario("otro") == "ventas"


def _nuevo(**kw):
    base = dict(nombre_usuario="ana.lopez", password="Clave1234", nivel="developer")
    base.update(kw)
    return usuarios.UsuarioNuevo(**base)


def test_validar_ok_normaliza():
    assert usuarios.validar_usuario_nuevo(_nuevo(nivel=" Developer ")) == ("ana.lopez", "developer")


@pytest.mark.parametrize("cambio", [
    {"nombre_usuario": "ab"},
    {"nombre_usuario": "con espacio"},
    {"password": "corta"},
    {"nivel": "gerencia"},      # gerencia solo sale del entorno
    {"nivel": "root"},
])
def test_validar_rechaza(cambio, monkeypatch):
    monkeypatch.setenv("USUARIOS_GERENCIA", "jefe")
    with pytest.raises(ValueError):
        usuarios.validar_usuario_nuevo(_nuevo(**cambio))


def test_nombre_reservado_de_gerencia(monkeypatch):
    monkeypatch.setenv("USUARIOS_GERENCIA", "jefe")
    with pytest.raises(ValueError):
        usuarios.validar_usuario_nuevo(_nuevo(nombre_usuario="Jefe"))


# ---------- developer lee los datos de todo el equipo ----------

def test_puede_ver_todo(monkeypatch):
    monkeypatch.setenv("USUARIOS_GERENCIA", "jefe")
    monkeypatch.setattr(permisos, "nivel_usuario", lambda u: {"dev": "developer"}.get(u, "ventas"))
    assert permisos.puede_ver_todo("jefe")
    assert permisos.puede_ver_todo("dev")
    assert not permisos.puede_ver_todo("vendedor")


def test_pendientes_alcance_developer_ve_todos_ventas_no(monkeypatch):
    from routers import pendientes
    monkeypatch.setenv("USUARIOS_GERENCIA", "jefe")
    monkeypatch.setattr(permisos, "nivel_usuario", lambda u: "developer" if u == "dev" else "ventas")
    assert pendientes._alcance("dev", None) is None          # sin ?usuario= = todos
    assert pendientes._alcance("dev", "ana") == "ana"
    assert pendientes._alcance("vendedor", None) == "vendedor"
    with pytest.raises(HTTPException) as e:
        pendientes._alcance("vendedor", "ana")
    assert e.value.status_code == 403


class _Cur:
    def __init__(self, fila): self.fila = fila
    def execute(self, *a, **k): pass
    def fetchone(self): return self.fila


def test_crm_cliente_lectura_developer_escritura_no(monkeypatch):
    from routers import crm
    monkeypatch.setenv("USUARIOS_GERENCIA", "jefe")
    monkeypatch.setattr(permisos, "nivel_usuario", lambda u: "developer")
    monkeypatch.setattr(crm, "_fila_cliente", lambda c, i: {"vendedor": "ana", "en_cartera": 1})
    monkeypatch.setattr(crm, "vendedor_efectivo", lambda f: f["vendedor"])
    assert crm._cliente_autorizado(None, 1, "dev", lectura=True)["vendedor"] == "ana"
    with pytest.raises(HTTPException) as e:
        crm._cliente_autorizado(None, 1, "dev")              # escribir: sigue siendo del dueño o gerencia
    assert e.value.status_code == 403


def test_prospecto_ficha_lectura_developer_escritura_no(monkeypatch):
    from routers import prospectos
    monkeypatch.setenv("USUARIOS_GERENCIA", "jefe")
    monkeypatch.setattr(permisos, "nivel_usuario", lambda u: "developer")
    monkeypatch.setattr(prospectos, "_fila", lambda c, i: {"vendedor": "ana"})
    assert prospectos._autorizado(None, 1, "dev", lectura=True)["vendedor"] == "ana"
    with pytest.raises(HTTPException) as e:
        prospectos._autorizado(None, 1, "dev")
    assert e.value.status_code == 403
