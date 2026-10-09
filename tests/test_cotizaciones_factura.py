# Vinculación factura-cotización: avisa por vínculo con factura (Telegram + tabla).
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import cotizacionesBack


class FakeCursor:
    def __init__(self):
        self.ejecutados = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.ejecutados.append((" ".join(query.split()), params))


class FakeDB:
    def __init__(self):
        self.cursor_obj = FakeCursor()
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
    assert len(base.cursor_obj.ejecutados) == 2 and base.commits == 1
    assert tg.await_count == 1
    assert "F-100" in tg.await_args.args[0] and "ZTC-1" in tg.await_args.args[0]
    assert notif.await_count == 1
