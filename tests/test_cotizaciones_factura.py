# Vinculación factura-cotización: avisa por vínculo con factura (Telegram + tabla).
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import cotizacionesBack


class FakeCursor:
    def __init__(self, facturas_previas=None):
        self.ejecutados = []
        self.rowcount = 0
        self.facturas_previas = facturas_previas or {}  # codigo_cotizacion -> relacion_factura en BD
        self._filas = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        q = " ".join(query.split())
        self.ejecutados.append((q, params))
        self._filas = []
        if q.startswith("SELECT relacion_factura FROM cotizaciones"):
            self._filas = [(self.facturas_previas.get(params[0]),)]
        # Dos partidas de venta salieron de ZTC-1.
        self.rowcount = 2 if q.startswith("UPDATE ventasRegistro") and "ZTC-1" in params else 0

    def fetchall(self):
        return list(self._filas)


class FakeDB:
    def __init__(self, facturas_previas=None):
        self.cursor_obj = FakeCursor(facturas_previas)
        self.commits = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


def _app():
    app = FastAPI()
    app.include_router(cotizacionesBack.router)
    return TestClient(app)


def test_vincular_factura_avisa_solo_con_factura(monkeypatch):
    base = FakeDB()
    tg = AsyncMock()
    notif = AsyncMock()
    monkeypatch.setattr(cotizacionesBack, "get_db_connection", lambda: base)
    monkeypatch.setattr(cotizacionesBack, "send_telegram_alert", tg)
    monkeypatch.setattr(cotizacionesBack.mov_reg, "registrar_movimiento", lambda *a, **k: None)
    import notificaciones_service
    monkeypatch.setattr(notificaciones_service, "crear_y_notificar_todos", notif)

    with _app() as client:
        r = client.post("/relacionFactura", json=[
            {"id": 1, "codigo_cotizacion": "ZTC-1", "relacion_factura": "F-100",
             "metodo_pago": "Transferencia", "fecha_pago": "2026-10-08", "usuario": "ana"},
            {"id": 2, "codigo_cotizacion": "ZTC-2", "relacion_factura": None,
             "metodo_pago": None, "fecha_pago": None, "usuario": "ana"},
        ])
    assert r.status_code == 200, r.text
    # Por cotización: SELECT previa + UPDATE cotizaciones; solo ZTC-1 (con factura) toca ventas.
    assert len(base.cursor_obj.ejecutados) == 5 and base.commits == 1
    assert tg.await_count == 1
    assert "F-100" in tg.await_args.args[0] and "ZTC-1" in tg.await_args.args[0]
    assert "marcadas facturadas:</b> 2" in tg.await_args.args[0]
    assert notif.await_count == 1


def _ventas_actualizadas(monkeypatch, vinculos, facturas_previas=None):
    base = FakeDB(facturas_previas)
    monkeypatch.setattr(cotizacionesBack, "get_db_connection", lambda: base)
    monkeypatch.setattr(cotizacionesBack, "send_telegram_alert", AsyncMock())
    monkeypatch.setattr(cotizacionesBack.mov_reg, "registrar_movimiento", lambda *a, **k: None)
    import notificaciones_service
    monkeypatch.setattr(notificaciones_service, "crear_y_notificar_todos", AsyncMock())
    with _app() as client:
        r = client.post("/relacionFactura", json=vinculos)
    assert r.status_code == 200, r.text
    return [(q, p) for q, p in base.cursor_obj.ejecutados if q.startswith("UPDATE ventasRegistro")]


def test_vincular_factura_marca_facturadas_las_ventas_de_la_cotizacion(monkeypatch):
    upd = _ventas_actualizadas(monkeypatch, [
        {"id": 1, "codigo_cotizacion": "ZTC-1", "relacion_factura": " F-100 ", "usuario": "ana"}])
    assert upd == [("UPDATE ventasRegistro SET facturada = 1, factura = %s WHERE cotizacion = %s", ("F-100", "ZTC-1"))]


def test_quitar_la_factura_regresa_a_pendientes_solo_las_de_esa_factura(monkeypatch):
    upd = _ventas_actualizadas(monkeypatch, [
        {"id": 2, "codigo_cotizacion": "ZTC-2", "relacion_factura": "", "usuario": "ana"}],
        facturas_previas={"ZTC-2": "F-50"})
    assert upd == [("UPDATE ventasRegistro SET facturada = 0, factura = NULL WHERE cotizacion = %s AND factura = %s",
                     ("ZTC-2", "F-50"))]


def test_cambiar_solo_pago_sin_factura_no_toca_ventas(monkeypatch):
    upd = _ventas_actualizadas(monkeypatch, [
        {"id": 3, "codigo_cotizacion": "ZTC-3", "relacion_factura": None, "fecha_pago": "2026-10-20",
         "usuario": "ana"}])
    assert upd == []
