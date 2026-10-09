# Tests de la auditoría de inventario: endpoint unión + columna usuario en devoluciones.
from fastapi import FastAPI
from fastapi.testclient import TestClient

from permisos import usuario_autenticado
from routers import inventario
from routers import productos


class FakeCursor:
    def __init__(self, filas=None, columnas=None):
        self.filas = filas or []
        self.columnas = columnas or set()
        self.ejecutados = []

    def execute(self, query, params=None):
        q = " ".join(query.split())
        self.ejecutados.append((q, params))

    def fetchall(self):
        if self.ejecutados and self.ejecutados[-1][0].startswith("SELECT COLUMN_NAME"):
            return [(c,) for c in self.columnas]
        return list(self.filas)

    def fetchone(self):
        filas = self.fetchall()
        return filas[0] if filas else None

    def close(self):
        pass


class FakeDB:
    def __init__(self, filas=None, columnas=None):
        self.cursor_obj = FakeCursor(filas, columnas)
        self.commits = 0

    def cursor(self, dictionary=False):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def is_connected(self):
        return True

    def close(self):
        pass


def _app():
    app = FastAPI()
    app.include_router(inventario.router)
    app.dependency_overrides[usuario_autenticado] = lambda: "tester"
    return TestClient(app)


def test_movimientos_une_fuentes_y_filtra(monkeypatch):
    filas = [
        {"fecha": "2026-10-08T10:00:00", "tipo": "venta", "sku": "A-1",
         "cantidad": -2, "folio": "99", "usuario": "ana", "detalle": "Directo"},
        {"fecha": "2026-10-07T10:00:00", "tipo": "compra", "sku": "A-1",
         "cantidad": 10, "folio": "F-1", "usuario": "ger", "detalle": "Prov"},
    ]
    base = FakeDB(filas, {"almacen", "usuario"})
    monkeypatch.setattr(inventario, "get_db_connection", lambda: base)
    with _app() as client:
        r = client.get("/inventario/movimientos", params={"sku": "A-1", "limite": 50})
    assert r.status_code == 200, r.text
    assert r.json() == filas
    (query, params), = [(q, p) for q, p in base.cursor_obj.ejecutados if "UNION ALL" in q]
    assert "ventasRegistro" in query and "stock_actual" in query
    assert "compras" in query and "devoluciones" in query
    assert "costo" not in query.lower()
    assert params[0] == "%A-1%" and params[-1] == 50


def test_movimientos_tolera_esquema_viejo(monkeypatch):
    base = FakeDB([], set())
    monkeypatch.setattr(inventario, "get_db_connection", lambda: base)
    with _app() as client:
        r = client.get("/inventario/movimientos")
    assert r.status_code == 200, r.text
    (query, _), = [(q, p) for q, p in base.cursor_obj.ejecutados if "UNION ALL" in q]
    assert "almacen" not in query


def test_migracion_devolucion_agrega_usuario(monkeypatch):
    base = FakeDB(columnas={"sku", "producto"})
    monkeypatch.setattr(productos, "get_db_connection", lambda: base)
    productos.asegurar_columnas_devolucion()
    qs = [q for q, _ in base.cursor_obj.ejecutados]
    assert any("ADD COLUMN usuario" in q for q in qs)
    assert base.commits == 1


def test_indices_inventario_se_crean_si_faltan(monkeypatch):
    from routers import inventario as inv
    base = FakeDB(columnas=set())

    class IdxCursor(FakeCursor):
        def fetchall(self):
            if self.ejecutados and self.ejecutados[-1][0].startswith("SELECT INDEX_NAME"):
                return []
            return super().fetchall()

    base.cursor_obj = IdxCursor([], set())
    monkeypatch.setattr(inv, "get_db_connection", lambda: base)
    inv.asegurar_indices_inventario()
    qs = [q for q, _ in base.cursor_obj.ejecutados]
    assert sum("ADD INDEX" in q for q in qs) == 4
