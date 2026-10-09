# Tests del job alerta stock Full: solo funciones puras (sin red ni DB).
from jobs import meli_full_stock as job


def _det(id_, qty, logistic="fulfillment", sku="ABC", price=100.0):
    return {"id": id_, "seller_sku": sku, "title": "Producto de prueba",
            "available_quantity": qty, "price": price,
            "permalink": "https://x", "shipping": {"logistic_type": logistic}}


def test_evaluar_solo_full_bajo():
    alertas, salidas = job.evaluar(
        [_det("A", 5), _det("B", 50), _det("C", 3, logistic="self_service")], 20)
    assert [a["id"] for a in alertas] == ["A"]
    assert [s["id"] for s in salidas] == ["C"]


def test_config_lee_env(monkeypatch):
    monkeypatch.setenv("MELI_FULL_IDS", "A, B ,")
    monkeypatch.setenv("MELI_FULL_UMBRAL", "7")
    ids, umbral = job.config()
    assert ids == ["A", "B"] and umbral == 7


def test_mensaje_compacto_con_link_y_dias():
    chunks = job.build_mensaje(
        [{"id": "A", "sku": "ABC", "titulo": "Prod", "stock": 5, "precio": 100.0,
          "url": "https://x", "bodega": "fulfillment"}], [], {"A": 3})
    texto = "\n".join(chunks)
    assert "<code>A</code>" in texto and "×5" in texto
    assert "3 días en bajo" in texto and "Ver publicación" in texto


def test_mensaje_sin_bajos():
    assert "Sin publicaciones" in "\n".join(job.build_mensaje([], [], {}))
