# Tests del job vencimientos de cotizaciones: solo funciones puras.
from datetime import date

from jobs import cotizaciones_vencimiento as job


def _c(cod, empresa, fecha, factura=None):
    return {"codigo_cotizacion": cod, "empresa": empresa,
            "fecha_vencimiento": fecha, "relacion_factura": factura}


def test_parte_vencidas_proximas_y_facturadas_fuera():
    hoy = date(2026, 10, 8)
    v, p = job.partir(
        [_c("A", "E1", "2026-10-05"), _c("B", "E2", "2026-10-10"),
         _c("C", "E3", "2026-12-01"), _c("D", "E4", "2026-10-05", factura="F-1")], hoy)
    assert [o["codigo_cotizacion"] for o in v] == ["A"]
    assert [o["codigo_cotizacion"] for o in p] == ["B"]


def test_mensaje_agrupado():
    chunks = job.build_mensaje([_c("A", "E1", "2026-10-05")], [_c("B", "E2", "2026-10-09")])
    texto = "\n".join(chunks)
    assert "Vencidas sin factura (1)" in texto and "Vencen en 3 días (1)" in texto
    assert "<code>A</code>" in texto and "05/10" in texto


def test_mensaje_sin_pendientes():
    assert "Sin cotizaciones" in "\n".join(job.build_mensaje([], []))
