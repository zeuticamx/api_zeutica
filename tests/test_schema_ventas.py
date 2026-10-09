# Tests de la migración de ventasRegistro (con DB falsa, sin MySQL real).
import mysql.connector

from jobs import schema_ventas


class FakeCursor:
    def __init__(self):
        self.ejecutados = []
        self.rowcount = 3
        self._colas = []

    def execute(self, query, params=None):
        q = " ".join(query.split())
        self.ejecutados.append((q, params))
        if q.startswith("SELECT COLUMN_NAME"):
            self._colas = [("id_ventas",), ("sku",)]
        elif q.startswith("SELECT INDEX_NAME"):
            self._colas = [("PRIMARY",)]

    def fetchall(self):
        return list(self._colas)

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        self.cursor_obj = FakeCursor()

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        pass

    def close(self):
        pass


def test_migracion_agrega_columnas_indice_y_backfill(monkeypatch):
    base = FakeDB()
    monkeypatch.setattr(schema_ventas, "get_db_connection", lambda: base)
    rep = schema_ventas.asegurar_columnas_ventas()
    qs = [q for q, _ in base.cursor_obj.ejecutados]
    assert rep["columnas_agregadas"] == ["inventario_descontado", "costo_unitario", "es_full", "estatus"]
    assert rep["indice_creado"] is True and rep["backfill"] == 3
    assert rep["canceladas_backfill"] == 3
    assert any("ADD UNIQUE KEY uq_venta_partida" in q for q in qs)
    assert any("SET inventario_descontado = 1" in q for q in qs)


def test_migracion_idempotente_si_todo_existe(monkeypatch):
    base = FakeDB()
    cols = [("id_ventas",), ("sku",), ("inventario_descontado",), ("costo_unitario",), ("es_full",), ("estatus",)]

    orig_execute = FakeCursor.execute

    def execute_con_todo(self, query, params=None):
        q = " ".join(query.split())
        self.ejecutados.append((q, params))
        if q.startswith("SELECT COLUMN_NAME"):
            self._colas = list(cols)
        elif q.startswith("SELECT INDEX_NAME"):
            self._colas = [("uq_venta_partida",), ("idx_ventasregistro_estatus",)]

    monkeypatch.setattr(FakeCursor, "execute", execute_con_todo)
    monkeypatch.setattr(schema_ventas, "get_db_connection", lambda: base)
    rep = schema_ventas.asegurar_columnas_ventas()
    qs = [q for q, _ in base.cursor_obj.ejecutados]
    assert rep["columnas_agregadas"] == [] and rep["indice_creado"] is False
    assert not any("ADD COLUMN" in q for q in qs) and not any("ADD UNIQUE KEY" in q for q in qs)


def test_verificar_esquema_falla_claro_si_falta_columna(monkeypatch):
    base = FakeDB()
    monkeypatch.setattr(schema_ventas, "get_db_connection", lambda: base)
    try:
        schema_ventas.verificar_esquema_job()
    except RuntimeError as err:
        assert "inventario_descontado" in str(err)
    else:
        raise AssertionError("debió fallar")
