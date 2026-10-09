# Tests del job stock MeLi: solo funciones puras (sin red ni DB).
from jobs import meli_stock as job


def test_multiplicador_y_15_porciento():
    # 100 uds, mult 5 -> 20 paquetes -> 15% = 3
    nuevo, paq = job.calcular_nuevo_stock("ABC", "buy_it_now", 100, 5)
    assert (nuevo, paq) == (3, 20)


def test_tap_usa_8_porciento():
    nuevo, paq = job.calcular_nuevo_stock("TAPETE1", "1", 100, 1)
    assert nuevo == 8 and paq == 100  # floor(100 * .08)


def test_minimo_2_si_hay_paquetes():
    nuevo, paq = job.calcular_nuevo_stock("ABC", "buy_it_now", 3, 1)
    assert nuevo == 2 and paq == 3  # floor(3*.15)=0 -> mínimo 2


def test_umbral_50_porciento():
    ok, motivo, _ = job.debe_actualizar(3, 10)
    assert ok and motivo == "Cambio > 50%"
    ok, _, _ = job.debe_actualizar(9, 10)
    assert not ok
    ok, motivo, _ = job.debe_actualizar(5, 0)
    assert ok and motivo == "Reabastecimiento"
    ok, motivo, _ = job.debe_actualizar(0, 4)
    assert ok and motivo == "Agotado"


def test_variante_color_unica():
    variantes = [
        {"id": "V1", "available_quantity": 5,
         "attribute_combinations": [{"name": "Color", "value_name": "Rojo"}]},
        {"id": "V2", "available_quantity": 7,
         "attribute_combinations": [{"name": "Color", "value_name": "Azul"}]},
    ]
    var = job.encontrar_variante_color(variantes, "GUANTE ROJ")
    assert var and var["id"] == "V1"


def test_variante_ambigua_no_match():
    variantes = [
        {"id": "V1", "available_quantity": 5,
         "attribute_combinations": [{"name": "Color", "value_name": "Rojo"}]},
        {"id": "V2", "available_quantity": 7,
         "attribute_combinations": [{"name": "Color", "value_name": "Rojo oscuro"}]},
    ]
    assert job.encontrar_variante_color(variantes, "GUANTE ROJ") is None


def test_excepcion_pur_violeta():
    variantes = [{"id": "V9", "available_quantity": 2,
                  "attribute_combinations": [{"name": "Color", "value_name": "Violeta"}]}]
    var = job.encontrar_variante_color(variantes, "FUNDA PUR")
    assert var and var["id"] == "V9"


def test_decidir_respeta_ignorar_y_sin_match():
    dets = [{"id": "MLM2505625865", "title": "X", "available_quantity": 1, "variations": [], "buying_mode": ""},
            {"id": "MLM1", "title": "Prod", "available_quantity": 0, "variations": [], "buying_mode": "",
             }]
    db = {"MLM1": {"sku": "ABC", "stock_bodega": 100}}
    ups, sin = job.decidir_actualizaciones(dets, db)
    assert [u["item_id"] for u in ups] == ["MLM1"]
    assert ups[0]["nuevo"] == 15
    dets2 = [{"id": "MLM2", "title": "Y", "available_quantity": 1, "variations": [], "buying_mode": ""}]
    ups2, sin2 = job.decidir_actualizaciones(dets2, {})
    assert ups2 == [] and sin2[0]["motivo"] == "sin registro en DB"


def test_resumen_compacto():
    chunks = job.build_resumen(
        [{"item_id": "MLM1", "sku": "ABC", "titulo": "Producto largo de prueba para recorte",
          "anterior": 1, "nuevo": 15, "motivo": "Cambio > 50%"}],
        {}, [{"item_id": "MLM9", "sku_db": "ZZZ", "motivo": "sin registro en DB"}])
    texto = "\n".join(chunks)
    assert "<code>MLM1</code>" in texto and "1→15" in texto
    assert "<code>MLM9</code>" in texto
