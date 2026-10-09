# Tests del job cotizaciones por vender: solo funciones puras.
from jobs import cotizaciones_por_vender as job


def test_mensaje_agrupado():
    filas = [
        {"codigo_cotizacion": "ZTC-1", "empresa": "E1", "total": 1500.5, "relacion_factura": "F-1"},
        {"codigo_cotizacion": "ZTC-2", "empresa": "E2", "total": 200, "relacion_factura": "F-2"},
    ]
    texto = "\n".join(job.build_mensaje(filas))
    assert "Registra la venta (2)" in texto
    assert "<code>ZTC-1</code>" in texto and "$1,500.50" in texto
    assert "F-2" in texto
