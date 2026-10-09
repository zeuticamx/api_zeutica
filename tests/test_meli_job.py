# Tests del job MeLi: solo funciones puras (sin red ni DB).
from datetime import datetime
from zoneinfo import ZoneInfo

from jobs import meli_ventas as job


def orden(id_, sku, qty=1, unit=100.0, fee=10.0, logistic="self_service"):
    return {
        "id": id_, "pack_id": None, "date_created": "2026-10-07T10:00:00.000-06:00",
        "buyer": {"nickname": "comp"}, "shipping": {"id": "S1"},
        "payments": [{"id": "P1", "status": "approved", "payment_type": "credit_card"}],
        "order_items": [{"quantity": qty, "unit_price": unit, "sale_fee": fee,
                         "item": {"seller_sku": sku, "title": "Prod"}}],
        "_logistic": logistic,
    }


def test_limpiar_agrupa_multiplicador_y_full():
    paginas = [{"results": [
        orden("1", "ABC_3", qty=2),
        {**orden("2", "XYZFULL"), "shipping": {"id": "S2"},
         "order_items": [{"quantity": 1, "unit_price": 50, "sale_fee": 5,
                          "item": {"seller_sku": "XYZFULL", "title": "X"}}]},
        orden("3", "500_ESCFANBLA", qty=1, unit=20, fee=2),
    ]}]
    out = {l["id_venta"]: l for l in job.limpiar_ordenes(paginas)}
    assert out["1"]["sku_mod"] == "ABC" and out["1"]["cantidad_final"] == 6
    assert out["2"]["es_full"] == 1 and out["2"]["sku_mod"] == "XYZ"
    assert out["3"]["cantidad_final"] == 5


def test_enriquecer_no_descuenta_fulfillment_aunque_sku_no_diga_full():
    lineas = [{"id_venta": "1", "sku_mod": "ABC", "shipping_id": "S1", "es_full": 0,
               "producto": "P", "cantidad": 1, "cantidad_final": 1, "codigo": "ABC"}]
    out = job.enriquecer_lineas(lineas, {"ABC": 10}, {"S1": "fulfillment"}, {})
    assert out[0]["a_descontar"] is False
    assert "NO se descontó" in (out[0]["alerta_logistica"] or "")


def test_enriquecer_si_descuenta_flex():
    lineas = [{"id_venta": "1", "sku_mod": "ABC", "shipping_id": "S1", "es_full": 0,
               "producto": "P", "cantidad": 1, "cantidad_final": 1, "codigo": "ABC"}]
    out = job.enriquecer_lineas(lineas, {"ABC": 10}, {"S1": "self_service"}, {})
    assert out[0]["canal"] == "Flex" and out[0]["a_descontar"] is True


def test_enriquecer_no_descuenta_full_por_sku():
    lineas = [{"id_venta": "1", "sku_mod": "XYZ", "shipping_id": "S1", "es_full": 1,
               "producto": "P", "cantidad": 1, "cantidad_final": 1, "codigo": "XYZFULL"}]
    out = job.enriquecer_lineas(lineas, {"XYZ": 10}, {"S1": "self_service"}, {})
    assert out[0]["a_descontar"] is False


def test_margen_reparte_neto_y_alerta_solo_nueva():
    lineas = [{"id_venta": "1", "sku_mod": "ABC", "bruto": 100.0, "costo": 10.0,
               "cantidad_final": 5, "es_nueva": True, "payment_id": "P1", "canal": "Flex"}]
    pagos = {"P1": {"transaction_details": {"net_received_amount": 40.0}}}
    (m,) = job.calcular_margenes(lineas, pagos)
    assert m["net_received_amount"] == 40.0 and m["alerta"] is True  # margen -20%
    (m2,) = job.calcular_margenes([{**lineas[0], "es_nueva": False}], pagos)
    assert m2["alerta"] is False


def test_procesar_cancelacion_marca_estatus_y_regresa_stock(monkeypatch):
    ejecutados = []

    class Cur:
        rowcount = 1

        def execute(self, q, p=None):
            ejecutados.append((" ".join(q.split()), p))

        def fetchall(self):
            return [{"sku": "ABC", "cantidad": 2}]

        def close(self):
            pass

    class DB:
        def cursor(self, dictionary=False):
            return Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(job, "get_db_connection", lambda: DB())
    import mov_reg
    monkeypatch.setattr(mov_reg, "registrar_movimiento", lambda *a, **k: None)
    r = job.procesar_cancelacion("777", "test")
    assert r == {"order_id": "777", "regresadas": 1, "piezas": 2}
    updates = [q for q, _ in ejecutados if q.startswith("UPDATE")]
    assert len(updates) == 1 and "estatus = 'cancelada'" in updates[0]
    assert "inventario_descontado = 0" in updates[0]


def test_procesar_cancelacion_sin_filas_no_toca_nada(monkeypatch):
    class Cur:
        def execute(self, q, p=None):
            pass

        def fetchall(self):
            return []

        def close(self):
            pass

    class DB:
        def cursor(self, dictionary=False):
            return Cur()

        def close(self):
            pass

    monkeypatch.setattr(job, "get_db_connection", lambda: DB())
    assert job.procesar_cancelacion("000", "test") == {"order_id": "000", "regresadas": 0, "piezas": 0}


def test_reporte_incluye_descuento_y_totales():
    ahora = datetime(2026, 10, 8, 13, 0, tzinfo=ZoneInfo("America/Mexico_City"))
    linea = {"id_venta": "1", "pack_id": None, "sku_mod": "ABC", "codigo": "ABC", "producto": "P",
             "cantidad": 1, "cantidad_final": 2, "multiplicador": 2, "bruto": 100.0, "precio": 90.0,
             "fecha": "2026-10-08", "hora": "10:00", "otros": "", "canal": "Flex", "es_full": 0,
             "es_flex": True, "sin_costo": False, "costo": 10, "es_nueva": True, "a_descontar": True,
             "alerta_logistica": None, "estado_amazon": None}
    chunks = job.build_reporte([linea], [], ahora)
    assert len(chunks) == 1 and "ABC" in chunks[0] and "−2 pza" in chunks[0]


def _linea_meli(id_venta, fecha):
    return {"id_venta": id_venta, "pack_id": None, "sku_mod": "ABC", "codigo": "ABC", "producto": "P",
            "cantidad": 1, "cantidad_final": 1, "multiplicador": 1, "bruto": 50.0, "precio": 45.0,
            "fecha": fecha, "hora": "10:00", "otros": "", "canal": "Flex", "es_full": 0,
            "es_flex": True, "sin_costo": False, "costo": 10, "es_nueva": True, "a_descontar": True,
            "alerta_logistica": None, "estado_amazon": None}


def test_reporte_ventas_solo_del_dia_atrasadas_en_inventario():
    ahora = datetime(2026, 10, 8, 13, 0, tzinfo=ZoneInfo("America/Mexico_City"))
    chunks = job.build_reporte([_linea_meli("DIA", "2026-10-08"), _linea_meli("VIEJA", "2026-10-05")], [], ahora)
    texto = "\n".join(chunks)
    assert "<code>DIA</code>" in texto and "<code>VIEJA</code>" not in texto
    assert "Atrasadas" in texto and "Totales del día" in texto
    assert "Corte:" in texto
