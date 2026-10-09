# Tests de promociones MeLi: solo función pura resumir_ofertas (sin red ni DB).
from datetime import datetime, timezone

from jobs import meli_promos as promos


def _base(**extra):
    d = {"id": "P1", "name": "Campaña", "status": "candidate", "price": 90,
         "original_price": 100, "meli_percentage": 5, "seller_percentage": 10,
         "suggested_discounted_price": 90}
    d.update(extra)
    return d


def test_resumen_y_proximas_a_vencer():
    ahora = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    items = [_base(id="A", finish_date="2026-10-10T00:00:00.000Z"),
             _base(id="B", finish_date="2026-12-01T00:00:00.000Z"),
             _base(id="C")]
    resumen, proximas = promos.resumir_ofertas(items, ahora)
    assert len(resumen) == 3
    assert [p["id"] for p in proximas] == ["A"]
    assert resumen[0]["meli_percentage"] == 5


def test_porcentajes_none_a_cero_y_fecha_invalida():
    ahora = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    resumen, proximas = promos.resumir_ofertas(
        [_base(meli_percentage=None, seller_percentage=None, finish_date="no-fecha")], ahora)
    assert resumen[0]["meli_percentage"] == 0 and resumen[0]["seller_percentage"] == 0
    assert proximas == []


def test_obtener_promos_usa_cache(monkeypatch):
    promos._CACHE["ts"] = 9999999999.0
    promos._CACHE["data"] = {"message": "cache", "ofertas_meli": []}
    import asyncio
    assert asyncio.run(promos.obtener_promos()) == {"message": "cache", "ofertas_meli": []}
    promos._CACHE["ts"] = 0.0
    promos._CACHE["data"] = None
