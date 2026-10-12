# Tests de clientes_potenciales (dedupe + llave única) y lock compartido de jobs.
import asyncio

from jobs import schema_potenciales as mig
from jobs import _ejecucion


class FakeCursor:
    def __init__(self, tablas=True, columnas=None, unicos=None):
        self.tablas = tablas
        self.columnas = columnas if columnas is not None else set(mig.COLUMNAS_POTENCIALES)
        self.unicos = unicos if unicos is not None else set()
        self.ejecutados = []
        self.rowcount = 0

    def execute(self, query, params=None):
        q = " ".join(query.split())
        self.ejecutados.append((q, params))
        if q.startswith("SHOW TABLES"):
            self._uno = [("clientes_potenciales",)] if self.tablas else []
        elif q.startswith("SELECT COLUMN_NAME"):
            self._uno = [(c,) for c in self.columnas]
        elif q.startswith("SELECT INDEX_NAME"):
            self._uno = [(u,) for u in self.unicos]
        elif q.startswith("DELETE"):
            self.rowcount = 2
            self._uno = []

    def fetchone(self):
        return self._uno[0] if self._uno else None

    def fetchall(self):
        return list(self._uno)

    def close(self):
        pass


class FakeDB:
    def __init__(self, **kw):
        self.cursor_obj = FakeCursor(**kw)
        self.commits = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def close(self):
        pass


def test_migracion_deduplica_y_crea_llave(monkeypatch):
    base = FakeDB()
    monkeypatch.setattr(mig, "get_db_connection", lambda: base)
    rep = mig.asegurar_tabla_potenciales()
    qs = [q for q, _ in base.cursor_obj.ejecutados]
    assert rep == {"tabla_creada": False, "duplicados_eliminados": 2, "indice_creado": True}
    assert any(q.startswith("DELETE t1 FROM") for q in qs)
    assert any("ADD UNIQUE KEY uq_potencial_venta_sku" in q for q in qs)


def test_migracion_idempotente(monkeypatch):
    base = FakeDB(unicos={"uq_potencial_venta_sku"})
    monkeypatch.setattr(mig, "get_db_connection", lambda: base)
    rep = mig.asegurar_tabla_potenciales()
    assert rep == {"tabla_creada": False, "duplicados_eliminados": 2, "indice_creado": False}
    qs = [q for q, _ in base.cursor_obj.ejecutados]
    assert not any("ADD UNIQUE KEY" in q for q in qs)


def test_migracion_crea_tabla_si_falta(monkeypatch):
    base = FakeDB(tablas=False, columnas=set())
    monkeypatch.setattr(mig, "get_db_connection", lambda: base)
    rep = mig.asegurar_tabla_potenciales()
    qs = [q for q, _ in base.cursor_obj.ejecutados]
    assert rep["tabla_creada"] is True
    assert any(q.startswith("CREATE TABLE clientes_potenciales") for q in qs)


def test_lock_segunda_corrida_da_ocupado():
    liberado = asyncio.Event()

    class Mod:
        __name__ = "mod_prueba"
        LAST_RUN = {}

        @staticmethod
        async def run_job(dry_run=None, motivo="", telegram=True):
            await liberado.wait()
            return {"estado": "ok"}

    async def main():
        t1 = asyncio.ensure_future(_ejecucion.ejecutar(Mod, motivo="a"))
        await asyncio.sleep(0.05)
        assert _ejecucion.en_curso(Mod) is True
        r2 = await _ejecucion.ejecutar(Mod, motivo="b")
        assert r2["estado"] == "ocupado"
        liberado.set()
        r1 = await t1
        assert r1["estado"] == "ok"
        assert _ejecucion.en_curso(Mod) is False

    asyncio.run(main())
    _ejecucion._LOCKS.pop("mod_prueba", None)
