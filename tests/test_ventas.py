# Tests del router de ventas: consulta de reportes por fecha_registro y
# validación de id_ventas duplicado al registrar ventas.
#
# No se toca MySQL, Telegram ni la bitácora: get_db_connection devuelve una BD
# falsa en memoria que interpreta las pocas consultas que usa el router.
# La app de prueba monta solo ventas.router, sin la dependencia de token de
# main.py (esa se prueba en el login, no aquí).
from datetime import date
from unittest.mock import AsyncMock

import mysql.connector
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mysql.connector import errorcode

from routers import ventas


class FakeCursor:
    """Cursor que responde según la consulta y registra todo lo ejecutado."""

    def __init__(self, db):
        self.db = db
        self._resultado = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        q = " ".join(query.split())
        self.db.ejecutados.append((q, params))
        if self.db.error_en and self.db.error_en in q:
            raise self.db.error
        self._resultado, self.rowcount = [], 0

        if q.startswith("SELECT * FROM ventasRegistro"):
            self._resultado = list(self.db.ventas)
        elif q.startswith("SELECT id FROM ventasRegistro WHERE id_ventas"):
            self._resultado = [{"id": 1} for v in self.db.registradas if v["id_ventas"] == params[0]][:1]
        elif q.startswith("SELECT stock_bodega FROM productos"):
            sku = params[0]
            if sku in self.db.stock:
                self._resultado = [{"stock_bodega": self.db.stock[sku]}]
        elif q.startswith("INSERT INTO ventasRegistro"):
            self.db.registradas.append({"id_ventas": str(params[0]), "sku": params[1], "saldo_pendiente": params[11]})
            self.rowcount = 1
        elif q.startswith("UPDATE productos SET stock_bodega"):
            cantidad, sku, _ = params
            if self.db.stock.get(sku, 0) >= cantidad and not self.db.update_falla:
                self.db.stock[sku] -= cantidad
                self.rowcount = 1

    def fetchone(self):
        return self._resultado[0] if self._resultado else None

    def fetchall(self):
        return list(self._resultado)

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        self.ventas = []          # filas que devuelve la consulta de reportes
        self.registradas = []     # filas insertadas en ventasRegistro
        self.stock = {}
        self.ejecutados = []
        self.commits = 0
        self.rollbacks = 0
        self.error_en = None      # fragmento de consulta que debe lanzar self.error
        self.error = None
        self.update_falla = False

    def cursor(self, dictionary=False, buffered=False):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def is_connected(self):
        return True

    def close(self):
        pass

    def consultas(self, prefijo):
        return [(q, p) for q, p in self.ejecutados if q.startswith(prefijo)]


@pytest.fixture
def db(monkeypatch):
    base = FakeDB()
    monkeypatch.setattr(ventas, "get_db_connection", lambda: base)
    monkeypatch.setattr(ventas.mov_reg, "registrar_movimiento", lambda *a, **k: None)
    monkeypatch.setattr(ventas, "send_telegram_alert", AsyncMock())
    # El cálculo de comisiones tiene sus propios tests (test_comisiones.py)
    monkeypatch.setattr(ventas.comisiones, "registrar_comisiones_seguro", lambda *a, **k: None)
    return base


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ventas.router)
    return TestClient(app)


def venta_completa(**extra):
    datos = {
        "id_venta": 1234567890,
        "fecha": "2026-09-29",
        "nombreComprador": "Cliente Prueba",
        "otros": "CONTADO",
        "plataforma": "Directo",
        "usuario": "tester",
        "condicion_pago": "CONTADO",
        "items": [
            {"sku": "B-002", "producto": "Guante", "cantidad": 2, "precio": 116.0},
            {"sku": "A-001", "producto": "Cubrebocas", "cantidad": 1, "precio": 58.0},
        ],
    }
    datos.update(extra)
    return datos


def venta_simple(**extra):
    datos = {
        "id_venta": 555,
        "sku": "A-001",
        "producto": "Cubrebocas",
        "stock_bodega": 3,
        "precio": 10.0,
        "fecha": "2026-09-29",
        "nombreComprador": "USO DE BODEGA",
        "otros": "",
        "plataforma": "BODEGA",
        "usuario": "tester",
        "condicion_pago": "N/A",
    }
    datos.update(extra)
    return datos


def error_mysql(errno):
    return mysql.connector.Error(msg="simulado", errno=errno)


# ---------- GET /ventas/{f1}/{f2} (reportes por fecha_registro) ----------

def test_reportes_filtra_por_fecha_registro_incluyendo_el_dia_final(client, db):
    db.ventas = [{"id_ventas": "1", "sku": "A-001", "fecha_registro": "2026-09-15T10:00:00"}]

    r = client.get("/ventas/2026-09-01/2026-09-30")

    assert r.status_code == 200
    assert r.json() == db.ventas
    (query, params), = db.consultas("SELECT * FROM ventasRegistro")
    assert "fecha_registro >= %s AND fecha_registro < %s" in query
    assert "ORDER BY fecha_registro DESC" in query
    assert params == (date(2026, 9, 1), date(2026, 10, 1))


def test_reportes_rango_sin_ventas_devuelve_lista_vacia(client, db):
    r = client.get("/ventas/2026-01-01/2026-01-31")
    assert r.status_code == 200
    assert r.json() == []


def test_reportes_un_solo_dia(client, db):
    client.get("/ventas/2026-09-29/2026-09-29")
    (_, params), = db.consultas("SELECT * FROM ventasRegistro")
    assert params == (date(2026, 9, 29), date(2026, 9, 30))


@pytest.mark.parametrize("ruta", ["/ventas/2026-13-01/2026-09-30", "/ventas/ayer/hoy", "/ventas/2026-09-01/2026-02-30"])
def test_reportes_fecha_invalida_devuelve_422_sin_consultar(client, db, ruta):
    r = client.get(ruta)
    assert r.status_code == 422
    assert db.ejecutados == []


def test_reportes_rango_invertido_devuelve_422(client, db):
    r = client.get("/ventas/2026-09-30/2026-09-01")
    assert r.status_code == 422
    assert "posterior" in r.json()["detail"]
    assert db.ejecutados == []


def test_reportes_error_de_bd_devuelve_500(client, db):
    db.error_en, db.error = "SELECT * FROM ventasRegistro", error_mysql(2013)
    r = client.get("/ventas/2026-09-01/2026-09-30")
    assert r.status_code == 500
    assert "Error de base de datos" in r.json()["detail"]


# ---------- existe_id_venta ----------

def test_existe_id_venta_falso_si_no_hay_partidas():
    base = FakeDB()
    assert ventas.existe_id_venta(base.cursor(), 42) is False


def test_existe_id_venta_verdadero_y_consulta_como_texto_con_bloqueo():
    base = FakeDB()
    base.registradas = [{"id_ventas": "42", "sku": "A-001", "saldo_pendiente": 0}]
    assert ventas.existe_id_venta(base.cursor(), 42) is True
    (query, params), = base.consultas("SELECT id FROM ventasRegistro")
    assert query.endswith("FOR UPDATE")
    assert params == ("42",)


@pytest.mark.parametrize("errno,esperado", [
    (errorcode.ER_DUP_ENTRY, True),
    (errorcode.ER_LOCK_DEADLOCK, True),
    (errorcode.ER_LOCK_WAIT_TIMEOUT, False),
    (2013, False),
])
def test_es_conflicto_de_venta(errno, esperado):
    assert ventas.es_conflicto_de_venta(error_mysql(errno)) is esperado


# ---------- POST /ventas/registrar (venta completa del portal) ----------

def test_registrar_venta_completa_inserta_todas_las_partidas(client, db):
    db.stock = {"A-001": 10, "B-002": 5}

    r = client.post("/ventas/registrar", json=venta_completa())

    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["partidas"] == 2
    assert cuerpo["total"] == pytest.approx(2 * 116.0 + 58.0)
    assert cuerpo["saldo_pendiente"] == 0
    assert cuerpo["nuevo_stock"] == {"B-002": 3, "A-001": 9}
    assert db.stock == {"A-001": 9, "B-002": 3}
    assert [(v["id_ventas"], v["sku"]) for v in db.registradas] == [("1234567890", "B-002"), ("1234567890", "A-001")]
    assert db.commits == 1
    ventas.send_telegram_alert.assert_called_once()


def test_registrar_venta_completa_valida_id_antes_que_nada(client, db):
    db.stock = {"A-001": 10, "B-002": 5}
    client.post("/ventas/registrar", json=venta_completa())
    primera, *_ = db.ejecutados
    assert primera[0].startswith("SELECT id FROM ventasRegistro WHERE id_ventas")


def test_registrar_venta_completa_bloquea_skus_en_orden(client, db):
    db.stock = {"A-001": 10, "B-002": 5}
    client.post("/ventas/registrar", json=venta_completa())
    bloqueos = [p[0] for _, p in db.consultas("SELECT stock_bodega FROM productos")]
    assert bloqueos == ["A-001", "B-002"]


def test_registrar_venta_completa_id_duplicado_devuelve_409_sin_registrar(client, db):
    db.stock = {"A-001": 10, "B-002": 5}
    db.registradas = [{"id_ventas": "1234567890", "sku": "X", "saldo_pendiente": 0}]

    r = client.post("/ventas/registrar", json=venta_completa())

    assert r.status_code == 409
    assert r.json()["detail"] == "La venta '1234567890' ya fue registrada previamente"
    assert db.consultas("INSERT") == []
    assert db.consultas("UPDATE") == []
    assert db.consultas("SELECT stock_bodega") == []
    assert db.stock == {"A-001": 10, "B-002": 5}
    assert db.commits == 0 and db.rollbacks == 1
    ventas.send_telegram_alert.assert_not_called()


def test_registrar_venta_completa_dos_veces_la_segunda_es_409(client, db):
    db.stock = {"A-001": 10, "B-002": 5}
    assert client.post("/ventas/registrar", json=venta_completa()).status_code == 200
    r = client.post("/ventas/registrar", json=venta_completa())
    assert r.status_code == 409
    assert db.stock == {"A-001": 9, "B-002": 3}
    assert len(db.registradas) == 2


def test_registrar_venta_completa_credito_guarda_saldo_por_partida(client, db):
    db.stock = {"A-001": 10, "B-002": 5}
    r = client.post("/ventas/registrar", json=venta_completa(condicion_pago="CREDITO", otros="CREDITO"))
    assert r.status_code == 200
    assert r.json()["saldo_pendiente"] == pytest.approx(290.0)
    assert [v["saldo_pendiente"] for v in db.registradas] == [pytest.approx(232.0), pytest.approx(58.0)]


def test_registrar_venta_completa_stock_insuficiente_devuelve_400_sin_registrar(client, db):
    db.stock = {"A-001": 10, "B-002": 1}
    r = client.post("/ventas/registrar", json=venta_completa())
    assert r.status_code == 400
    assert "B-002" in r.json()["detail"]
    assert db.registradas == []
    assert db.stock == {"A-001": 10, "B-002": 1}


def test_registrar_venta_completa_sku_inexistente_devuelve_404(client, db):
    db.stock = {"B-002": 5}
    r = client.post("/ventas/registrar", json=venta_completa())
    assert r.status_code == 404
    assert "A-001" in r.json()["detail"]
    assert db.registradas == []


def test_registrar_venta_completa_carrera_de_stock_devuelve_409(client, db):
    db.stock = {"A-001": 10, "B-002": 5}
    db.update_falla = True
    r = client.post("/ventas/registrar", json=venta_completa())
    assert r.status_code == 409
    assert db.commits == 0 and db.rollbacks == 1


@pytest.mark.parametrize("cambio", [
    {"items": []},
    {"items": [{"sku": "A-001", "producto": "x", "cantidad": 0, "precio": 1}]},
    {"items": [{"sku": "A-001", "producto": "x", "cantidad": 1, "precio": -1}]},
    {"items": [{"sku": "A-001", "producto": "x", "cantidad": 1, "precio": 1},
               {"sku": "A-001", "producto": "x", "cantidad": 2, "precio": 1}]},
    {"id_venta": None},
])
def test_registrar_venta_completa_payload_invalido_devuelve_422(client, db, cambio):
    r = client.post("/ventas/registrar", json=venta_completa(**cambio))
    assert r.status_code == 422
    assert db.ejecutados == []


@pytest.mark.parametrize("errno", [errorcode.ER_DUP_ENTRY, errorcode.ER_LOCK_DEADLOCK])
def test_registrar_venta_completa_conflicto_en_bd_devuelve_409(client, db, errno):
    db.stock = {"A-001": 10, "B-002": 5}
    db.error_en, db.error = "INSERT INTO ventasRegistro", error_mysql(errno)
    r = client.post("/ventas/registrar", json=venta_completa())
    assert r.status_code == 409
    assert db.commits == 0 and db.rollbacks == 1


def test_registrar_venta_completa_otro_error_de_bd_devuelve_500(client, db):
    db.stock = {"A-001": 10, "B-002": 5}
    db.error_en, db.error = "INSERT INTO ventasRegistro", error_mysql(errorcode.ER_DATA_TOO_LONG)
    r = client.post("/ventas/registrar", json=venta_completa())
    assert r.status_code == 500
    assert db.commits == 0


def test_registrar_venta_completa_falla_de_bitacora_no_revierte_la_venta(client, db, monkeypatch):
    db.stock = {"A-001": 10, "B-002": 5}

    def bitacora_caida(*a, **k):
        raise error_mysql(2013)

    monkeypatch.setattr(ventas.mov_reg, "registrar_movimiento", bitacora_caida)
    r = client.post("/ventas/registrar", json=venta_completa())
    assert r.status_code == 200
    assert db.commits == 1


# ---------- POST /producto/venta (una partida; lo usa gastos) ----------

def test_producto_venta_registra_y_descuenta(client, db):
    db.stock = {"A-001": 10}
    r = client.post("/producto/venta", json=venta_simple())
    assert r.status_code == 200, r.text
    assert r.json()["nuevo_stock"] == 7
    assert db.stock == {"A-001": 7}
    assert db.commits == 1
    (query, _), = db.consultas("INSERT")
    assert "IGNORE" not in query


def test_producto_venta_id_duplicado_devuelve_409_sin_registrar(client, db):
    db.stock = {"A-001": 10}
    db.registradas = [{"id_ventas": "555", "sku": "A-001", "saldo_pendiente": 0}]
    r = client.post("/producto/venta", json=venta_simple())
    assert r.status_code == 409
    assert r.json()["detail"] == "La venta '555' ya fue registrada previamente"
    assert db.consultas("INSERT") == []
    assert db.consultas("SELECT stock_bodega") == []
    assert db.stock == {"A-001": 10}


def test_producto_venta_conflicto_de_llave_unica_devuelve_409(client, db):
    db.stock = {"A-001": 10}
    db.error_en, db.error = "INSERT INTO ventasRegistro", error_mysql(errorcode.ER_DUP_ENTRY)
    r = client.post("/producto/venta", json=venta_simple())
    assert r.status_code == 409
    assert db.stock == {"A-001": 10}


def test_producto_venta_cantidad_cero_devuelve_400(client, db):
    r = client.post("/producto/venta", json=venta_simple(stock_bodega=0))
    assert r.status_code == 400


def test_producto_venta_stock_insuficiente_devuelve_400(client, db):
    db.stock = {"A-001": 1}
    r = client.post("/producto/venta", json=venta_simple())
    assert r.status_code == 400
    assert db.registradas == []
