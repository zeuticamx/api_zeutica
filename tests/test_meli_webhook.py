# Tests del webhook MeLi: solo funciones puras (sin red ni DB).
import hashlib
import hmac

from routers import meli_webhook as wh


def test_normalizar_body_mercadopago():
    t, rid = wh.normalizar_evento({"action": "payment.created", "data": {"id": "123"}}, {})
    assert (t, rid) == ("payment", "123")


def test_normalizar_body_meli_clasico_y_resource_path():
    t, rid = wh.normalizar_evento({"topic": "orders", "resource": "/orders/999"}, {})
    assert (t, rid) == ("orders", "999")


def test_normalizar_query_ipn():
    t, rid = wh.normalizar_evento({}, {"topic": "payment", "id": "456"})
    assert (t, rid) == ("payment", "456")


def test_normalizar_desconocido():
    assert wh.normalizar_evento({"topic": "items"}, {}) == (None, None)
    assert wh.normalizar_evento({}, {}) == (None, None)


def test_firma_sin_secreto_acepta():
    assert wh.verificar_firma_mp("cualquiera", "1", "r") is True


def test_firma_valida_e_invalida(monkeypatch):
    monkeypatch.setenv("MELI_WEBHOOK_SECRET", "secreto")
    msg = "id:123;request-id:req-1;ts:111;"
    v1 = hmac.new(b"secreto", msg.encode(), hashlib.sha256).hexdigest()
    assert wh.verificar_firma_mp(f"ts=111,v1={v1}", "123", "req-1") is True
    assert wh.verificar_firma_mp("ts=111,v1=00", "123", "req-1") is False
    assert wh.verificar_firma_mp(None, "123", "req-1") is False


def test_clasificar_pago():
    assert wh.clasificar_pago({"status": "approved", "status_detail": "accredited"}) == "pagada"
    assert wh.clasificar_pago({"status": "cancelled"}) == "cancelada"
    assert wh.clasificar_pago({"status": "refunded"}) == "cancelada"
    assert wh.clasificar_pago({"status": "pending"}) == "ignorar"
    assert wh.clasificar_pago({"status": "in_process"}) == "ignorar"


def test_clasificar_orden():
    assert wh.clasificar_orden({"status": "paid"}) == "pagada"
    assert wh.clasificar_orden({"status": "cancelled"}) == "cancelada"
    assert wh.clasificar_orden({"status": "shipped"}) == "ignorar"
    assert wh.clasificar_orden({"status": "shipped", "cancel_detail": {"code": "x"}}) == "cancelada"


def test_clasificar_claim():
    assert wh.clasificar_claim({"status": "opened"}) == "abierta"
    assert wh.clasificar_claim({"status": "closed"}) == "ignorar"
    assert wh.clasificar_claim({}) == "ignorar"


def test_normalizar_orders_v2_y_claims():
    assert wh.normalizar_evento({"topic": "orders_v2", "resource": "/orders/7"}, {}) == ("orders", "7")
    assert wh.normalizar_evento({"topic": "claims", "resource": "/claims/9"}, {}) == ("claims", "9")


def test_ordenes_de_pago():
    assert wh.ordenes_de_pago({"order": {"id": 7}}) == ["7"]
    assert wh.ordenes_de_pago({}) == []


def test_dedup_aviso_solo_una_vez(monkeypatch):
    vistos = set()

    class Cur:
        rowcount = 0

        def execute(self, q, p=None):
            q = " ".join(q.split())
            if q.startswith("INSERT IGNORE"):
                if p in vistos:
                    self.rowcount = 0
                else:
                    vistos.add(p)
                    self.rowcount = 1
            elif q.startswith("SELECT 1"):
                self._uno = [("1",)] if p in vistos else []

        def fetchone(self):
            return self._uno[0] if self._uno else None

        def close(self):
            pass

    class DB:
        def cursor(self):
            return Cur()

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(wh, "_get_conn", lambda: DB())
    assert wh.ya_avisado("payment", "1", "pagada") is False
    wh.marcar_aviso("payment", "1", "pagada")
    assert wh.ya_avisado("payment", "1", "pagada") is True
    assert wh.ya_avisado("payment", "1", "cancelada") is False
