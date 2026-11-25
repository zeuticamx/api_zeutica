# T1: columna cotizaciones.cliente_id y rellenado único. BD falsa en memoria, sin MySQL.
import logging

import pytest

from servicios import cotizacion_service as cs


def test_normalizar_nombre():
    assert cs.normalizar_nombre("  ÁLVARO   Pérez  ") == "alvaro perez"
    assert cs.normalizar_nombre("Ñandú\tSA") == "nandu sa"
    assert cs.normalizar_nombre(None) == ""
    assert cs.normalizar_nombre("   ") == ""


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._filas = []

    def execute(self, query, params=None):
        q = " ".join(query.split())
        self.db.ejecutados.append((q, params))
        for clave in self.db.fallar_en:
            if clave in q:
                raise RuntimeError("fallo simulado")
        self._filas = []
        if "information_schema.COLUMNS" in q:
            self._filas = [(c,) for c in self.db.columnas]
        elif q.startswith("SELECT id, empresa FROM cotizaciones"):
            self._filas = [(i, e) for i, (e, cid) in self.db.cotizaciones.items() if cid is None]
        elif q.startswith("SELECT id, nombre FROM clientes"):
            assert "eliminado = 0" in q
            self._filas = [(i, n) for i, n in self.db.clientes]
        elif q.startswith("UPDATE cotizaciones SET cliente_id"):
            cid, cot_id = params
            e, actual = self.db.cotizaciones[cot_id]
            if actual is None:  # no sobrescribe
                self.db.cotizaciones[cot_id] = (e, cid)

    def fetchall(self):
        return list(self._filas)

    def close(self):
        pass


class FakeDB:
    def __init__(self, columnas=("id", "empresa"), cotizaciones=None, clientes=None, fallar_en=()):
        self.columnas = list(columnas)
        self.cotizaciones = cotizaciones or {}
        self.clientes = clientes or []
        self.fallar_en = fallar_en
        self.ejecutados = []
        self.commits = 0

    def cursor(self, *a, **k):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture(autouse=True)
def reset_indicador():
    cs._COLUMNA_CLIENTE_ID = False
    yield
    cs._COLUMNA_CLIENTE_ID = False


def usar(monkeypatch, db):
    monkeypatch.setattr(cs, "get_db_connection", lambda: db)


def test_crea_columna_indice_sin_fk_y_rellena(monkeypatch, caplog):
    db = FakeDB(
        cotizaciones={1: ("ACME, S.A.", None), 2: ("Sin Match", None), 3: ("dup", None),
                      4: ("", None), 5: ("acme, s.a.", 99), 6: (None, None)},
        clientes=[(10, "Acme, S.A."), (11, "Dup"), (12, "DUP"), (13, "Otro")],
    )
    usar(monkeypatch, db)
    with caplog.at_level(logging.INFO):
        cs.asegurar_columna_cliente_id()
    sql = " ".join(q for q, _ in db.ejecutados)
    assert "ADD COLUMN cliente_id" in sql
    assert "INDEX" in sql.upper()
    assert "FOREIGN KEY" not in sql.upper()
    assert cs.columna_cliente_id_existe() is True
    assert db.cotizaciones[1][1] == 10
    assert db.cotizaciones[2][1] is None   # sin coincidencia
    assert db.cotizaciones[3][1] is None   # ambigua
    assert db.cotizaciones[4][1] is None   # vacía
    assert db.cotizaciones[5][1] == 99     # no sobrescribe
    assert db.cotizaciones[6][1] is None
    assert any("1" in r.getMessage() and "ligadas" in r.getMessage() for r in caplog.records)


def test_si_la_columna_ya_existe_no_rellena(monkeypatch):
    db = FakeDB(columnas=("id", "empresa", "cliente_id"),
                cotizaciones={1: ("Acme", None)}, clientes=[(10, "Acme")])
    usar(monkeypatch, db)
    cs.asegurar_columna_cliente_id()
    cs.asegurar_columna_cliente_id()
    assert db.cotizaciones[1][1] is None
    assert not any(q.startswith("ALTER") or q.startswith("UPDATE") for q, _ in db.ejecutados)
    assert cs.columna_cliente_id_existe() is True


def test_error_en_rellenado_no_propaga_y_deja_columna(monkeypatch, caplog):
    db = FakeDB(cotizaciones={1: ("Acme", None)}, clientes=[(10, "Acme")],
                fallar_en=("SELECT id, nombre FROM clientes",))
    usar(monkeypatch, db)
    with caplog.at_level(logging.ERROR):
        cs.asegurar_columna_cliente_id()
    assert cs.columna_cliente_id_existe() is True
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_si_el_alter_falla_no_cae_e_indicador_falso(monkeypatch, caplog):
    db = FakeDB(fallar_en=("ALTER TABLE",))
    usar(monkeypatch, db)
    with caplog.at_level(logging.ERROR):
        cs.asegurar_columna_cliente_id()
    assert cs.columna_cliente_id_existe() is False
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_si_no_hay_conexion_no_cae(monkeypatch):
    def boom():
        raise RuntimeError("sin bd")
    monkeypatch.setattr(cs, "get_db_connection", boom)
    cs.asegurar_columna_cliente_id()
    assert cs.columna_cliente_id_existe() is False


def test_error_del_indice_no_impide_columna(monkeypatch):
    db = FakeDB(cotizaciones={1: ("Acme", None)}, clientes=[(10, "Acme")],
                fallar_en=("ADD INDEX",))
    usar(monkeypatch, db)
    cs.asegurar_columna_cliente_id()
    assert cs.columna_cliente_id_existe() is True
    assert db.cotizaciones[1][1] == 10


# ---------------------------------------------------------------- T2: altas con cliente_id
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from routers import cotizacionesBack


class _CurAlta:
    def __init__(self, db):
        self.db = db
        self.lastrowid = 11

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        q = " ".join(query.split())
        self.db.sql.append((q, params))
        self._fila = None
        if q.startswith("SELECT id FROM clientes"):
            assert "eliminado = 0" in q
            self._fila = (params[0],) if params[0] in self.db.clientes_validos else None

    def executemany(self, query, data):
        self.db.sql.append((" ".join(query.split()), data))

    def fetchone(self):
        return self._fila

    def close(self):
        pass


class DBAlta:
    def __init__(self, clientes_validos=(5,)):
        self.clientes_validos = set(clientes_validos)
        self.sql = []
        self.commits = 0

    def cursor(self, *a, **k):
        return _CurAlta(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass

    def insert_maestro(self):
        return [x for x in self.sql if x[0].startswith("INSERT INTO cotizaciones")][0]


def _cot(**extra):
    base = dict(codigo_cotizacion="ZTC-1", empresa="Otra SA", atencion="a", email="e", domicilio="d",
                telefono="1", subtotal=1, iva=0.16, total=1.16, costo_envio=0, forma_pago="03",
                comentarios="c", usuario="u", pdf="x", items=[])
    base.update(extra)
    return SimpleNamespace(**base)


def test_validar_cliente_id_none_no_consulta():
    db = DBAlta()
    cs.validar_cliente_id(db, None)
    assert db.sql == []


def test_validar_cliente_id_existente_pasa_e_inexistente_422():
    db = DBAlta(clientes_validos=(5,))
    cs.validar_cliente_id(db, 5)
    with pytest.raises(HTTPException) as e:
        cs.validar_cliente_id(db, 99)  # inexistente o eliminado: el SELECT filtra eliminado = 0
    assert e.value.status_code == 422


def test_guardar_con_columna_guarda_cliente_id_y_gana_sobre_empresa():
    cs._COLUMNA_CLIENTE_ID = True
    db = DBAlta()
    cs.guardar_cotizacion_db(db, _cot(cliente_id=5))
    q, vals = db.insert_maestro()
    assert "cliente_id" in q and vals[-1] == 5 and vals[1] == "Otra SA"


def test_guardar_sin_atributo_o_none_guarda_null():
    cs._COLUMNA_CLIENTE_ID = True
    for cot in (_cot(), _cot(cliente_id=None)):  # el primero no tiene el atributo (getattr)
        db = DBAlta()
        cs.guardar_cotizacion_db(db, cot)
        assert db.insert_maestro()[1][-1] is None


def test_guardar_con_indicador_falso_usa_insert_anterior():
    cs._COLUMNA_CLIENTE_ID = False
    db = DBAlta()
    cs.guardar_cotizacion_db(db, _cot(cliente_id=5))
    q, vals = db.insert_maestro()
    assert "cliente_id" not in q and len(vals) == 14


def _payload_guardar(**extra):
    d = dict(codigo_cotizacion="ZTC-1", empresa="E", atencion="a", email="e", domicilio="d", telefono="1",
             subtotal=1, iva=0.16, total=1.16, costo_envio=0, forma_pago="03", comentarios="c",
             usuario="u", pdf="x", items=[])
    d.update(extra)
    return d


@pytest.fixture
def cliente_guardar(monkeypatch):
    db = DBAlta(clientes_validos=(5,))
    cs._COLUMNA_CLIENTE_ID = True
    monkeypatch.setattr(cotizacionesBack, "get_db_connection", lambda: db)
    monkeypatch.setattr(cotizacionesBack.mov_reg, "registrar_movimiento", lambda *a, **k: None)
    async def _tg(*a, **k):
        return None
    monkeypatch.setattr(cotizacionesBack, "send_telegram_alert", _tg)
    app = FastAPI()
    app.include_router(cotizacionesBack.router)
    return TestClient(app), db


def test_guardar_ruta_cliente_valido(cliente_guardar):
    c, db = cliente_guardar
    r = c.post("/cotizaciones/guardar", json=_payload_guardar(cliente_id=5))
    assert r.status_code == 200
    assert db.insert_maestro()[1][-1] == 5


def test_guardar_ruta_sin_cliente_id_guarda_null(cliente_guardar):
    c, db = cliente_guardar
    assert c.post("/cotizaciones/guardar", json=_payload_guardar()).status_code == 200
    assert db.insert_maestro()[1][-1] is None


@pytest.mark.parametrize("valor", [0, -3, "abc", 2.5, "5", True])
def test_guardar_ruta_cliente_id_invalido_422(cliente_guardar, valor):
    c, db = cliente_guardar
    r = c.post("/cotizaciones/guardar", json=_payload_guardar(cliente_id=valor))
    assert r.status_code == 422
    assert not any(x[0].startswith("INSERT") for x in db.sql)


def test_guardar_ruta_cliente_inexistente_422_sin_insertar(cliente_guardar):
    c, db = cliente_guardar
    r = c.post("/cotizaciones/guardar", json=_payload_guardar(cliente_id=99))
    assert r.status_code == 422
    assert not any(x[0].startswith("INSERT") for x in db.sql)
    assert db.commits == 0


# ---------- T3: GET /cotizaciones/por-cliente/{cliente_id} ----------
import datetime
from decimal import Decimal


class _CurPorCliente:
    def __init__(self, db):
        self.db = db
        self._uno = None
        self._filas = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, query, params=None):
        q = " ".join(query.split())
        self.db.sql.append((q, params))
        if self.db.error:
            raise RuntimeError("fallo simulado")
        if q.startswith("SELECT id FROM clientes"):
            assert "eliminado = 0" in q
            self._uno = {"id": params[0]} if params[0] in self.db.clientes else None
        elif "FROM cotizaciones" in q:
            assert "cliente_id = %s" in q and "ORDER BY c.fecha DESC, c.id DESC" in q
            assert "c.pdf," not in q and "pdf AS" not in q  # nunca se selecciona el contenido
            self._filas = list(self.db.cotizaciones.get(params[0], []))

    def fetchone(self):
        return self._uno

    def fetchall(self):
        return self._filas


class DBPorCliente:
    def __init__(self, clientes=(5,), cotizaciones=None, error=False):
        self.clientes = clientes
        self.cotizaciones = cotizaciones or {}
        self.error = error
        self.sql = []
        self.cerrada = False

    def cursor(self, *a, **k):
        return _CurPorCliente(self)

    def close(self):
        self.cerrada = True


def _fila(id=1, **extra):
    d = dict(id=id, codigo_cotizacion=f"ZTC-{id}", fecha=datetime.date(2026, 5, 1),
             subtotal=Decimal("100.00"), total=Decimal("116.00"), usuario="ana", vendido=0,
             cancelada=0, fecha_cancelada=None, cancelada_por=None, motivo_cancelacion=None,
             fecha_vencimiento=datetime.date(2026, 6, 1), relacion_factura=None,
             seguimiento="ESPERANDO", seguimiento_otros=None, tiene_pdf=1)
    d.update(extra)
    return d


def _cliente_pc(monkeypatch, db, usuario="dev"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import cotizacionesBack
    from servicios.permisos import usuario_autenticado
    monkeypatch.setattr(cotizacionesBack, "get_db_connection", lambda: db)
    app = FastAPI()
    app.include_router(cotizacionesBack.router)
    app.dependency_overrides[usuario_autenticado] = lambda: usuario
    return TestClient(app)


def test_por_cliente_devuelve_campos_en_todos_los_estados(monkeypatch):
    filas = [_fila(3, cancelada=1, fecha_cancelada=datetime.datetime(2026, 5, 2, 10, 0),
                   cancelada_por="ana", motivo_cancelacion="x"),
             _fila(2, vendido=1, tiene_pdf=0), _fila(1, tiene_pdf=None)]
    db = DBPorCliente(cotizaciones={5: filas})
    r = _cliente_pc(monkeypatch, db).get("/cotizaciones/por-cliente/5")
    assert r.status_code == 200
    lista = r.json()["cotizaciones"]
    assert [c["id"] for c in lista] == [3, 2, 1]
    c = lista[0]
    assert c["codigo_cotizacion"] == "ZTC-3" and c["fecha"] == "2026-05-01"
    assert c["subtotal"] == "100.00" and c["total"] == "116.00" and c["usuario"] == "ana"
    assert c["cancelada"] == 1 and c["cancelada_por"] == "ana" and c["motivo_cancelacion"] == "x"
    assert c["fecha_cancelada"] == "2026-05-02 10:00:00" and c["fecha_vencimiento"] == "2026-06-01"
    assert c["seguimiento"] == "ESPERANDO" and c["seguimiento_otros"] is None and c["relacion_factura"] is None
    assert lista[1]["vendido"] == 1
    assert [x["tiene_pdf"] for x in lista] == [True, False, False]
    assert all(isinstance(x["tiene_pdf"], bool) for x in lista)
    assert all("pdf" not in x for x in lista)
    assert db.cerrada


def test_por_cliente_sin_cotizaciones_200_vacio(monkeypatch):
    r = _cliente_pc(monkeypatch, DBPorCliente()).get("/cotizaciones/por-cliente/5")
    assert r.status_code == 200 and r.json() == {"cotizaciones": []}


def test_por_cliente_inexistente_o_eliminado_404(monkeypatch):
    db = DBPorCliente(clientes=(5,))
    r = _cliente_pc(monkeypatch, db).get("/cotizaciones/por-cliente/99")
    assert r.status_code == 404
    assert not any("FROM cotizaciones" in s[0] for s in db.sql)


def test_por_cliente_cualquier_sesion_incluido_developer(monkeypatch):
    db = DBPorCliente(cotizaciones={5: [_fila()]})
    for usuario in ("developer1", "ventas1"):
        assert _cliente_pc(monkeypatch, db, usuario).get("/cotizaciones/por-cliente/5").status_code == 200


def test_por_cliente_error_de_bd_500_generico(monkeypatch):
    r = _cliente_pc(monkeypatch, DBPorCliente(error=True)).get("/cotizaciones/por-cliente/5")
    assert r.status_code == 500 and "fallo simulado" not in r.text


def test_por_cliente_enrutamiento(monkeypatch):
    db = DBPorCliente(cotizaciones={5: [_fila()]})
    c = _cliente_pc(monkeypatch, db)
    assert c.get("/cotizaciones/por-cliente/5").status_code == 200
    assert c.get("/cotizaciones/por-cliente/abc").status_code == 422
    # no captura ni es capturada por las rutas vecinas
    from routers import cotizacionesBack
    llamado = []
    class _C(_CurPorCliente):
        def execute(self, query, params=None):
            llamado.append(params)
            self._uno = {"pdf": "JVBER"}
    class _D(DBPorCliente):
        def cursor(self, *a, **k):
            return _C(self)
    monkeypatch.setattr(cotizacionesBack, "get_db_connection", lambda: _D())
    r = c.get("/cotizaciones/base64/ZTC-X")
    assert r.status_code == 200 and r.json() == {"pdf_base64": "JVBER"} and llamado == [("ZTC-X",)]
