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


def test_ordenes_de_pago():
    assert wh.ordenes_de_pago({"order": {"id": 7}}) == ["7"]
    assert wh.ordenes_de_pago({}) == []
