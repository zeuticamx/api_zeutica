# Tests del job vencimientos de cotizaciones: solo funciones puras.
from datetime import date

from jobs import cotizaciones_vencimiento as job


def _c(cod, empresa, fecha, factura=None):
    return {"codigo_cotizacion": cod, "empresa": empresa,
            "fecha_vencimiento": fecha, "relacion_factura": factura}


def test_solo_fecha_exacta_hoy_mas_3():
    hoy = date(2026, 10, 8)
    filas = job.proximas_3dias(
        [_c("A", "E1", "2026-10-11"), _c("B", "E2", "2026-10-09"),
         _c("C", "E3", "2026-10-05"), _c("D", "E4", "2026-10-11", factura="F-1"),
         _c("E", "E5", None)], hoy)
    assert [o["codigo_cotizacion"] for o in filas] == ["A"]


def test_mensaje_agrupado():
    filas = [_c("A", "E1", "2026-10-11"), _c("B", "E2", "2026-10-11")]
    texto = "\n".join(job.build_mensaje(filas))
    assert "vencen en 3 d" in texto and "(2)" in texto
    assert "<code>A</code>" in texto and "11/10" in texto
