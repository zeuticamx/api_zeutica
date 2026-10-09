# Tests del job de Amazon: solo funciones puras (sin red ni DB).
from datetime import datetime
from zoneinfo import ZoneInfo

from jobs import amazon_ventas as job


def tsv(*filas, sep="\t"):
    return (sep.join(filas[0]) + "\n" + "\n".join(sep.join(r) for r in filas[1:])).encode("utf-8")


HEAD = ["amazon-order-id", "sku", "product-name", "quantity", "item-price",
        "purchase-date", "asin", "payment-method-details", "fulfillment-channel",
        "order-status", "item-status"]


def test_parse_clasifica_ok_pendiente_cancelada():
    raw = tsv(HEAD,
              ["111", "ABC", "Prod", "2", "100", "2026-10-07T10:00:00-06:00", "A1", "Visa", "Amazon", "Shipped", ""],
              ["222", "ABC", "Prod", "1", "50", "2026-10-07T10:00:00-06:00", "A1", "", "Amazon", "Pending", ""],
              ["333", "ABC", "Prod", "1", "50", "2026-10-07T10:00:00-06:00", "A1", "", "Amazon", "Cancelled", ""],
              ["444", "ABC", "Prod", "0", "50", "2026-10-07T10:00:00-06:00", "A1", "", "Amazon", "Shipped", ""])
    out = {l["id_venta"]: l["estado"] for l in job.parse_report_bytes(raw)}
    assert out == {"111": "ok", "222": "pendiente", "333": "cancelada", "444": "cancelada"}


def test_parse_gzip_y_pending_pickup_es_por_surtir_en_agrupado():
    import gzip
    raw = gzip.compress(tsv(HEAD,
                            ["555", "ABC", "Prod", "1", "80", "2026-10-07T10:00:00-06:00",
                             "A1", "", "Merchant", "Pending - Waiting for Pick Up", ""]))
    lineas = job.parse_report_bytes(raw)
    assert lineas[0]["estado"] == "pendiente"
    agrup = job.agrupar_lineas(lineas, {"ABC": 10}, set(), {}, set())
    assert agrup[0]["estado"] == "por_surtir" and agrup[0]["valida"] is True


def test_resolver_sku_multiplicador():
    assert job.resolver_sku("ABC", {"ABC": 5}) == ("ABC", 1)
    assert job.resolver_sku("ABC_3", {"ABC": 5}) == ("ABC", 3)
    assert job.resolver_sku("ZZZ_3", {"ABC": 5}) == ("ZZZ_3", 1)


def test_agrupar_no_duplica_y_regresa_solo_descontado():
    costos = {"ABC": 10}
    existentes = {"111|ABC"}
    registro = {"111|ABC": {"descontado": True, "cantidad": 2}}
    ordenes = {"111"}
    cancel = [{"id_venta": "111", "codigo": "ABC", "producto": "P", "cantidad": 2, "precio": 100,
               "fecha": "2026-10-07", "asin": "A", "otros": "", "es_full": 1,
               "estado": "cancelada", "estado_amazon": "Cancelled"}]
    agrup = job.agrupar_lineas(cancel, costos, existentes, registro, ordenes)
    assert agrup[0]["a_regresar"] is True and agrup[0]["cantidad_regresar"] == 2


def test_cancelacion_parcial_no_regresa_solo():
    costos = {"ABC": 10}
    lineas = [
        {"id_venta": "111", "codigo": "ABC", "producto": "P", "cantidad": 1, "precio": 50,
         "fecha": "2026-10-07", "asin": "A", "otros": "", "es_full": 1, "estado": "ok", "estado_amazon": "Shipped"},
        {"id_venta": "111", "codigo": "ABC", "producto": "P", "cantidad": 1, "precio": 50,
         "fecha": "2026-10-07", "asin": "A", "otros": "", "es_full": 1, "estado": "cancelada", "estado_amazon": "Cancelled"},
    ]
    agrup = job.agrupar_lineas(lineas, costos, set(), {"111|ABC": {"descontado": True, "cantidad": 2}}, {"111"})
    canc = [a for a in agrup if a["estado"] == "cancelada"][0]
    assert canc["a_regresar"] is False and canc["cancelacion_parcial"] is True


def test_resumen_parte_mensajes_largos():
    ahora = datetime(2026, 10, 8, 13, 0, tzinfo=ZoneInfo("America/Mexico_City"))
    linea = {"id_venta": "111", "sku": "ABC", "producto": "P", "cantidad": 1, "cantidad_final": 1,
             "multiplicador": 1, "precio": 10.0, "fecha": "2026-10-08", "asin": "A", "otros": "",
             "es_full": 1, "costo": 5, "sin_costo": False, "es_multiplo": False,
             "estado": "ok", "estado_amazon": "Shipped", "valida": True, "es_nueva": True,
             "registrada_antes": False, "registrada_linea": False, "a_regresar": False,
             "cantidad_regresar": 0, "cancelacion_parcial": False}
    chunks = job.build_resumen([linea], ahora)
    assert len(chunks) == 1 and "Resumen de ventas Amazon" in chunks[0]


def _linea_amazon(id_venta, fecha, estado="ok"):
    return {"id_venta": id_venta, "sku": "ABC", "producto": "Prod", "cantidad": 1, "cantidad_final": 1,
            "multiplicador": 1, "precio": 10.0, "fecha": fecha, "asin": "A", "otros": "",
            "es_full": 0, "costo": 5, "sin_costo": False, "es_multiplo": False,
            "estado": estado, "estado_amazon": "Shipped", "valida": True, "es_nueva": True,
            "registrada_antes": False, "registrada_linea": False, "a_regresar": False,
            "cantidad_regresar": 0, "cancelacion_parcial": False}


def test_regreso_amazon_marca_estatus_cancelada(monkeypatch):
    from jobs import amazon_ventas as amz
    ejecutados = []

    class Cur:
        rowcount = 1

        def execute(self, q, p=None):
            ejecutados.append(" ".join(q.split()))

        def fetchone(self):
            return None

        def close(self):
            pass

    class DB:
        def cursor(self, *a, **k):
            return Cur()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(amz, "get_db_connection", lambda: DB())
    agrupadas = [{"id_venta": "A1", "sku": "ABC", "valida": False, "es_nueva": False,
                  "a_regresar": True, "cantidad_regresar": 2}]
    stats = amz.aplicar_ventas(agrupadas, dry_run=False)
    assert stats["regresadas"] == 1
    ups = [q for q in ejecutados if "inventario_descontado = 0" in q]
    assert len(ups) == 1 and "estatus = 'cancelada'" in ups[0]


def test_resumen_ventas_solo_del_dia_atrasadas_solo_en_inventario():
    ahora = datetime(2026, 10, 8, 13, 0, tzinfo=ZoneInfo("America/Mexico_City"))
    chunks = job.build_resumen([_linea_amazon("DIA", "2026-10-08"), _linea_amazon("VIEJA", "2026-10-05")], ahora)
    texto = "\n".join(chunks)
    assert "<code>DIA</code>" in texto and "<code>VIEJA</code>" not in texto.replace("Atrasadas", "")
    assert "Atrasadas" in texto and "Totales del día" in texto
    assert texto.count("ABC: −") == 1 and "−2 pza" in texto.replace("(s)", " pza")
