# Cancelar / reactivar cotizaciones (borrado lógico) y guards de las rutas que la usan.
# Sin MySQL: una BD falsa en memoria interpreta las pocas consultas del router.
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import cotizacion_cancelacion as cc
from permisos import usuario_autenticado
from routers import cotizacionesBack


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._fila = None
        self._filas = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        q = " ".join(query.split())
        self.db.ejecutados.append((q, params))
        self._fila, self._filas, self.rowcount = None, [], 0
        if q.startswith("SELECT usuario, vendido, relacion_factura") or q.startswith("SELECT usuario, COALESCE(cancelada"):
            self._fila = self.db.cotizaciones.get(params[0])
        elif q.startswith("SELECT COALESCE(cancelada, 0) AS cancelada FROM cotizaciones"):
            fila = self.db.cotizaciones.get(params[0])
            self._filas = [{"cancelada": fila["cancelada"]}] if fila else []
        elif q.startswith("UPDATE cotizaciones SET cancelada"):
            self.rowcount = 1

    def fetchone(self):
        return self._fila

    def fetchall(self):
        return list(self._filas)


class FakeDB:
    def __init__(self):
        self.cotizaciones = {}
        self.ejecutados = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, dictionary=False, buffered=False):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass

    def updates(self):
        return [(q, p) for q, p in self.ejecutados if q.startswith("UPDATE")]


def cot(usuario="ana", vendido=0, relacion_factura=None, cancelada=0):
    return {"usuario": usuario, "vendido": vendido, "relacion_factura": relacion_factura, "cancelada": cancelada}


@pytest.fixture
def db(monkeypatch):
    base = FakeDB()
    monkeypatch.setattr(cotizacionesBack, "get_db_connection", lambda: base)
    monkeypatch.setattr(cotizacionesBack.mov_reg, "registrar_movimiento", lambda *a, **k: bitacora.append(a))
    monkeypatch.setattr(cotizacionesBack.skydropx_envios, "listar_envios", lambda codigo: base.guias)
    base.guias = []
    bitacora.clear()
    return base


bitacora = []


def cliente(usuario="ana"):
    app = FastAPI()
    app.include_router(cotizacionesBack.router)
    app.dependency_overrides[usuario_autenticado] = lambda: usuario
    return TestClient(app)


def test_cancelar_marca_la_cotizacion_y_deja_bitacora(db):
    db.cotizaciones = {"ZTC-1": cot()}
    r = cliente("ana").post("/cotizaciones/ZTC-1/cancelar", json={"motivo": "  Cliente desistió "})
    assert r.status_code == 200, r.text
    assert r.json()["guias_activas"] == 0 and "aviso" not in r.json()
    (q, p), = db.updates()
    assert "cancelada = 1" in q and p[1] == "ana" and p[2] == "Cliente desistió" and p[3] == "ZTC-1"
    assert db.commits == 1
    assert bitacora and "ZTC-1" in bitacora[0][1] and "Cliente desistió" in bitacora[0][1]


def test_cancelar_sin_cuerpo_y_motivo_opcional(db):
    db.cotizaciones = {"ZTC-1": cot()}
    r = cliente("ana").post("/cotizaciones/ZTC-1/cancelar")
    assert r.status_code == 200, r.text
    assert db.updates()[0][1][2] is None


def test_gerencia_puede_cancelar_una_ajena(db):
    db.cotizaciones = {"ZTC-1": cot(usuario="ana")}
    assert cliente("gerencia").post("/cotizaciones/ZTC-1/cancelar").status_code == 200


def test_otro_vendedor_no_puede_cancelar(db):
    db.cotizaciones = {"ZTC-1": cot(usuario="ana")}
    r = cliente("beto").post("/cotizaciones/ZTC-1/cancelar")
    assert r.status_code == 403 and db.updates() == [] and db.commits == 0


def test_cancelar_inexistente_da_404(db):
    assert cliente().post("/cotizaciones/NOPE/cancelar").status_code == 404


@pytest.mark.parametrize("fila", [
    cot(cancelada=1),
    cot(vendido=1),
    cot(relacion_factura="F-100"),
])
def test_cancelar_cancelada_vendida_o_facturada_da_409(db, fila):
    db.cotizaciones = {"ZTC-1": fila}
    r = cliente("ana").post("/cotizaciones/ZTC-1/cancelar")
    assert r.status_code == 409 and db.updates() == [] and db.commits == 0


def test_cancelar_con_guias_se_permite_y_avisa(db):
    db.cotizaciones = {"ZTC-1": cot()}
    db.guias = [{"tracking_number": "T1"}, {"tracking_number": "T2"}]
    r = cliente("ana").post("/cotizaciones/ZTC-1/cancelar")
    assert r.status_code == 200
    assert r.json()["guias_activas"] == 2 and "Skydropx" in r.json()["aviso"]


def test_motivo_demasiado_largo_da_422(db):
    db.cotizaciones = {"ZTC-1": cot()}
    r = cliente("ana").post("/cotizaciones/ZTC-1/cancelar", json={"motivo": "x" * 501})
    assert r.status_code == 422 and db.updates() == []


def test_reactivar_limpia_la_cancelacion(db):
    db.cotizaciones = {"ZTC-1": cot(cancelada=1)}
    r = cliente("ana").post("/cotizaciones/ZTC-1/reactivar")
    assert r.status_code == 200, r.text
    (q, _), = db.updates()
    assert "cancelada = 0" in q and "fecha_cancelada = NULL" in q
    assert bitacora and "Reactivó" in bitacora[0][1]


def test_reactivar_una_no_cancelada_da_409_y_ajena_da_403(db):
    db.cotizaciones = {"ZTC-1": cot(cancelada=0), "ZTC-2": cot(usuario="ana", cancelada=1)}
    assert cliente("ana").post("/cotizaciones/ZTC-1/reactivar").status_code == 409
    assert cliente("beto").post("/cotizaciones/ZTC-2/reactivar").status_code == 403
    assert db.updates() == []


# ---- helpers y guards ----

def test_exigir_no_cancelada_solo_bloquea_las_canceladas():
    base = FakeDB()
    base.cotizaciones = {"A": {"cancelada": 1}, "B": {"cancelada": 0}}
    cur = FakeCursor(base)
    with pytest.raises(HTTPException) as err:
        cc.exigir_no_cancelada(cur, "A")
    assert err.value.status_code == 409
    cc.exigir_no_cancelada(cur, "B")          # no cancelada
    cc.exigir_no_cancelada(cur, "NO-EXISTE")  # texto libre: no bloquea
    cc.exigir_no_cancelada(cur, "  ")         # vacío: ni consulta
    assert len(base.ejecutados) == 3


@pytest.mark.parametrize("ruta,cuerpo", [
    ("/complemento-pago", {"codigo_cotizacion": "ZTC-1", "complemento": "C1", "pago": 10}),
    ("/firma-ventas", {"codigo_cotizacion": "ZTC-1", "firma_base64": "x", "usuario": "ana", "fecha_firma": "hoy"}),
    ("/cotizaciones/vendido", {"codigo_cotizacion": "ZTC-1", "vendido": 1}),
])
def test_rutas_de_una_cotizacion_cancelada_dan_409(db, ruta, cuerpo):
    db.cotizaciones = {"ZTC-1": cot(cancelada=1)}
    r = cliente().post(ruta, json=cuerpo)
    assert r.status_code == 409 and "cancelada" in r.json()["detail"]
    assert db.updates() == [] and db.commits == 0


def test_desmarcar_vendido_de_una_cancelada_si_se_permite(db):
    db.cotizaciones = {"ZTC-1": cot(cancelada=1)}
    r = cliente().post("/cotizaciones/vendido", json={"codigo_cotizacion": "ZTC-1", "vendido": 0})
    assert r.status_code == 200, r.text


def test_normalizar_motivo_y_permiso():
    assert cc.normalizar_motivo("   ") is None and cc.normalizar_motivo(None) is None
    assert cc.normalizar_motivo(" hola ") == "hola"
    assert cc.es_autor_o_gerencia("Ana", "ana", lambda u: False)
    assert cc.es_autor_o_gerencia("jefe", "ana", lambda u: True)
    assert not cc.es_autor_o_gerencia("beto", "ana", lambda u: False)
    assert not cc.es_autor_o_gerencia("beto", None, lambda u: False)
