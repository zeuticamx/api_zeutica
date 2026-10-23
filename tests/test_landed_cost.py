# Tests del costeo de importación (Landed Cost): lógica pura contra la fila real de la
# plantilla «PRECIOS PRODUCTOS FELIPE FER.xlsx», rutas (solo gerencia, 409 con costeo
# cerrado, invoice del embarque) y cierre/reapertura desde la etapa HL_LIQUIDADA.
import json
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import landed_cost_logica as lc
import mov_reg
from permisos import usuario_autenticado
from routers import embarques, landed_cost
from routers.embarques import EtapaTipo, EtapaUpdate

GERENCIA = "Gerencia"

# Embarque 12, invoice YH1377 (fila 3 del Excel, con valores reales).
ITEM_EXCEL = {
    "id": 1, "sku": "COFPLINGR", "invoice": "YH1377", "fecha_compra": "2024-07-24",
    "paquete_venta": 100, "unit_price_fob_usd": 0.017, "qty": 103000, "ctns": 103,
    "cbm": 2.065, "arancel_porc": 0.35, "tipo_cambio_china": 20,
}
LIQ_EXCEL = {
    "total_cbm_manual": 6.775, "no_hoja_liquidacion": "698", "tipo_cambio_log": 18.1,
    "incrementales_usd": 844.56, "igi_total_embarque": 31174.54, "iva_total_aduana": 21684.99,
    "total_hoja_liquidacion": 88407.44, "total_consolidado_porc": 0.1064,
}
# Valores que calculó Excel para esa fila (columna de la plantilla → campo).
ESPERADO_EXCEL = {
    "total_paquetes_venta": 1030,                          # J
    "total_usd_sin_igi": 1751.0000000000002,               # N
    "costo_paq_venta_china_usd": 1.7000000000000004,       # O
    "costo_paq_venta_china_mxn_sin_igi": 34.00000000000001,  # P
    "incremental_proporcional": 257.41939483394833,        # X
    "valor_mercancia_aduana_total": 2008.4193948339484,    # Y
    "igi_correspondiente_item": 12723.336866273065,        # Z
    "igi_por_paquete": 12.352754239100063,                 # AA
    "iva_item_aduana": 7852.116466042806,                  # AB
    "iva_item_por_paquete": 7.623414044701753,             # AC
    "piezas_m2": 103000,                                   # AD
    "porc_cbm_en_barco": 0.3047970479704797,               # AE
    "costo_envio_item_sin_iva_igi": 10834.898029520296,    # AI
    "costo_envio_paquete_sin_iva_igi": 10.519318475262423,  # AJ
    "igi_mas_iva_por_paquete": 19.976168283801815,         # AK
    "costo_envio_paq_venta_c_iva": 30.49548675906424,      # AL
    "comprobacion": 31410.351361836165,                    # AM
    "costo_envio_paq_venta_sin_iva": 22.872072714362485,   # AN
    "relacion_costo_material_vs_envio": 0.896926081148948,  # AP
    "costo_por_paquete_c_iva_bodega": 64.49548675906425,   # AQ
    "costo_por_paquete_sin_iva_bodega": 56.87207271436249,  # AR
    "qty_paq_primario": 1030,                              # AT
    "zeutica_paga_total": 66430.35136183618,               # AU
}


# ---- Lógica pura ----

def test_fila_real_del_excel_da_los_mismos_valores():
    r = lc.calcular_embarque([ITEM_EXCEL], LIQ_EXCEL, ["YH1377"])
    calc = r["items"][0]["calculado"]
    for campo, esperado in ESPERADO_EXCEL.items():
        assert calc[campo] == pytest.approx(esperado, abs=1e-6), campo
    assert r["estado"] == lc.COMPLETO
    assert r["resumen"]["costo_envio_total_sin_iva_igi"] == pytest.approx(35547.91)  # AH
    assert r["items"][0]["advertencias"] == []


def test_igi_es_valor_aduana_por_tc_log_por_arancel_no_la_formula_vieja():
    """Regresión: la fórmula por defecto de la tabla en Excel era `#REF! + P × arancel`."""
    calc = lc.calcular_embarque([ITEM_EXCEL], LIQ_EXCEL)["items"][0]["calculado"]
    assert calc["igi_correspondiente_item"] == pytest.approx(2008.4193948339484 * 18.1 * 0.35)
    assert calc["igi_correspondiente_item"] != pytest.approx(34.0 * 0.35)


def test_una_fila_de_un_embarque_de_varias_no_cuadra_y_avisa_el_cbm_manual():
    r = lc.calcular_embarque([ITEM_EXCEL], LIQ_EXCEL)
    res = r["resumen"]
    assert res["origen_total_cbm"] == "manual"
    assert res["total_cbm_usado"] == pytest.approx(6.775)
    assert res["total_cbm_items"] == pytest.approx(2.065)
    assert res["cuadra"] is False
    assert res["diferencia_comprobacion"] == pytest.approx(31410.351361836165 - 88407.44)
    assert any("CBM total capturado" in a for a in r["advertencias"])
    assert any("no cuadra" in a for a in r["advertencias"])


def test_cuadra_cuando_el_igi_y_iva_reales_coinciden_con_los_estimados():
    otro = {**ITEM_EXCEL, "id": 2, "sku": "OTRO", "cbm": 4.71, "qty": 5000, "paquete_venta": 10,
            "unit_price_fob_usd": 1.2, "arancel_porc": 0.15}
    liq = {**LIQ_EXCEL, "total_cbm_manual": None}
    previo = lc.calcular_embarque([ITEM_EXCEL, otro], liq)["resumen"]
    liq["igi_total_embarque"] = previo["igi_estimado_total"]
    liq["iva_total_aduana"] = previo["iva_estimado_total"]
    res = lc.calcular_embarque([ITEM_EXCEL, otro], liq)["resumen"]
    assert res["origen_total_cbm"] == "items"
    assert res["total_cbm_usado"] == pytest.approx(6.775)
    assert res["suma_porc_cbm"] == pytest.approx(1)
    assert res["suma_comprobacion"] == pytest.approx(88407.44, abs=0.01)
    assert res["cuadra"] is True


def test_paquete_venta_cero_da_none_con_advertencia_nunca_cero():
    item = {**ITEM_EXCEL, "paquete_venta": 0}
    fila = lc.calcular_embarque([item], LIQ_EXCEL)["items"][0]
    calc = fila["calculado"]
    assert calc["total_paquetes_venta"] is None
    assert calc["costo_paq_venta_china_mxn_sin_igi"] is None
    assert calc["costo_por_paquete_c_iva_bodega"] is None
    assert calc["zeutica_paga_total"] is None
    assert calc["total_usd_sin_igi"] == pytest.approx(1751)  # lo que no depende del paquete sí sale
    assert any("Paquete de venta en 0" in a for a in fila["advertencias"])


def test_total_cbm_cero_o_items_sin_cbm_no_dividen_entre_cero():
    item = {**ITEM_EXCEL, "cbm": None}
    r = lc.calcular_embarque([item], {**LIQ_EXCEL, "total_cbm_manual": None})
    calc = r["items"][0]["calculado"]
    assert calc["porc_cbm_en_barco"] is None
    assert calc["costo_envio_paq_venta_c_iva"] is None
    assert calc["costo_paq_venta_china_mxn_sin_igi"] == pytest.approx(34)
    assert r["resumen"]["total_cbm_usado"] is None
    assert any("Sin CBM total" in a for a in r["advertencias"])
    assert "Falta CBM" in r["items"][0]["advertencias"]


def test_sin_liquidacion_es_estimado_y_solo_sale_el_paso_1():
    r = lc.calcular_embarque([ITEM_EXCEL], None)
    calc = r["items"][0]["calculado"]
    assert r["estado"] == lc.ESTIMADO
    assert calc["costo_paq_venta_china_mxn_sin_igi"] == pytest.approx(34)
    assert calc["igi_correspondiente_item"] is None
    assert calc["costo_por_paquete_c_iva_bodega"] is None
    assert r["resumen"]["cuadra"] is None
    assert any("hoja de liquidación" in a for a in r["advertencias"])


def test_cantidad_no_multiplo_del_paquete_avisa_paquetes_fraccionarios():
    fila = lc.calcular_embarque([{**ITEM_EXCEL, "qty": 103050}], LIQ_EXCEL)["items"][0]
    assert fila["calculado"]["total_paquetes_venta"] == pytest.approx(1030.5)
    assert any("múltiplo" in a for a in fila["advertencias"])


def test_hoja_menor_que_igi_mas_iva_avisa_envio_negativo():
    r = lc.calcular_embarque([ITEM_EXCEL], {**LIQ_EXCEL, "total_hoja_liquidacion": 1000})
    assert r["resumen"]["costo_envio_total_sin_iva_igi"] < 0
    assert any("menor que el IGI" in a for a in r["advertencias"])


def test_invoice_que_ya_no_esta_en_el_embarque_avisa():
    fila = lc.calcular_embarque([ITEM_EXCEL], LIQ_EXCEL, ["OTRA"])["items"][0]
    assert any("YH1377" in a for a in fila["advertencias"])


def test_conciliacion_de_pagos_con_china_y_asx():
    liq = {**LIQ_EXCEL, "pagado_china_mxn": 35000}
    res = lc.calcular_embarque([ITEM_EXCEL], liq)["resumen"]
    assert res["asx"] == pytest.approx(88407.44)
    assert res["total_pagado_real"] == pytest.approx(123407.44)
    assert res["diferencia_pagado"] == pytest.approx(66430.35136183618 - 123407.44)


def test_sin_items_no_truena():
    r = lc.calcular_embarque([], LIQ_EXCEL)
    assert r["items"] == []
    assert r["resumen"]["suma_comprobacion"] is None


# ---- Rutas ----

class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._res = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        db = self.db
        db.ejecutadas.append((sql, params))
        self._res, self.rowcount = [], 0
        if "FROM embarques WHERE id" in sql:
            self._res = [db.embarque] if db.embarque and params[0] == db.embarque["id"] else []
        elif "SELECT 1 FROM embarque_invoices" in sql:
            self._res = [{"1": 1}] if params[1] in db.invoices else []
        elif "FROM embarque_invoices" in sql:
            self._res = [{"numero": n} for n in db.invoices]
        elif "SELECT id FROM embarque_items WHERE id" in sql:
            self._res = [{"id": params[0]}] if any(i["id"] == params[0] for i in db.items) else []
        elif "FROM embarque_items" in sql:
            self._res = [dict(i) for i in db.items]
        elif "FROM embarque_liquidacion" in sql:
            self._res = [dict(db.liquidacion)] if db.liquidacion else []
        elif sql.startswith("SELECT resultado") and "embarque_costeo_cierre" in sql:
            self._res = [dict(db.cierre)] if db.cierre else []
        elif sql.startswith("INSERT INTO embarque_liquidacion"):
            db.liquidacion = dict(zip(lc.CAMPOS_LIQUIDACION, params[1:-1]))
            db.liquidacion_por = params[-1]
        elif sql.startswith("UPDATE embarque_items SET"):
            columnas = [c.split(" = ")[0] for c in sql.split("SET ")[1].split(" WHERE")[0].split(", ")]
            item = next(i for i in db.items if i["id"] == params[-2])
            item.update(zip(columnas, params[:len(columnas)]))
        elif sql.startswith("INSERT INTO embarque_costeo_cierre"):
            db.cierre = {"resultado": params[1], "cerrado_por": params[2], "cerrado_en": "2026-10-23T12:00:00"}
        elif sql.startswith("DELETE FROM embarque_costeo_cierre"):
            self.rowcount = 1 if db.cierre else 0
            db.cierre = None
        elif "FROM embarque_etapas" in sql and "nota" in sql and sql.startswith("SELECT id, nota"):
            self._res = [{"id": 30, "nota": None, "tipo_cambio_fecha": None, "tipo_cambio_valor": None,
                          "tipo_cambio_fecha_dato": None}]
        elif "FROM embarque_etapas WHERE id" in sql:
            self._res = [{"id": 30, "embarque_id": 12, "tipo": "HL_LIQUIDADA", "completado": True, "nota": None}]

    def fetchall(self):
        return self._res

    def fetchone(self):
        return self._res[0] if self._res else None

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        self.ejecutadas = []
        self.commits = 0
        self.embarque = {"id": 12, "numero_contenedor": "MSKU1", "llegada_manzanillo_tentativa": None,
                         "fecha_de_recibido": None}
        self.invoices = ["YH1377", "YH1400"]
        self.items = [{**ITEM_EXCEL, "id": 1, "qty": 103000, "cbm": Decimal("2.065")}]
        self.liquidacion = dict(LIQ_EXCEL)
        self.liquidacion_por = None
        self.cierre = None

    def cursor(self, dictionary=False):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def db(monkeypatch):
    base = FakeDB()
    monkeypatch.setattr(landed_cost, "get_db_connection", lambda: base)
    monkeypatch.setattr(embarques, "get_db_connection", lambda: base)
    base.movimientos = []
    monkeypatch.setattr(mov_reg, "registrar_movimiento", lambda *a, **k: base.movimientos.append(a))
    return base


def _cliente(usuario):
    app = FastAPI()
    app.include_router(landed_cost.router)
    app.dependency_overrides[usuario_autenticado] = lambda: usuario
    return TestClient(app)


def test_todas_las_rutas_son_solo_gerencia(db):
    c = _cliente("vendedor1")
    assert c.get("/embarques/12/landed-cost").status_code == 403
    assert c.put("/embarques/12/landed-cost/liquidacion", json={}).status_code == 403
    assert c.patch("/embarques/12/items/1/costos", json={}).status_code == 403
    assert c.post("/landed-cost/simular", json={}).status_code == 403


def test_get_devuelve_entradas_calculo_y_resumen(db):
    r = _cliente(GERENCIA).get("/embarques/12/landed-cost")
    assert r.status_code == 200
    d = r.json()
    assert d["cerrado"] is False
    assert d["embarque"]["invoices"] == ["YH1377", "YH1400"]
    assert d["liquidacion"]["tipo_cambio_log"] == 18.1
    assert d["items"][0]["calculado"]["costo_por_paquete_sin_iva_bodega"] == pytest.approx(56.87207271436249)
    assert d["items"][0]["entrada"]["invoice"] == "YH1377"


def test_get_embarque_inexistente_404(db):
    assert _cliente(GERENCIA).get("/embarques/99/landed-cost").status_code == 404


def test_put_liquidacion_guarda_con_usuario_del_token_e_ignora_calculados(db):
    cuerpo = {**LIQ_EXCEL, "pagado_china_mxn": 35000, "usuario": "otro",
              "costo_por_paquete_c_iva_bodega": 1, "total_paquetes_venta": 5}
    r = _cliente(GERENCIA).put("/embarques/12/landed-cost/liquidacion", json=cuerpo)
    assert r.status_code == 200
    assert db.liquidacion_por == GERENCIA
    assert db.liquidacion["pagado_china_mxn"] == Decimal("35000")
    sql = next(s for s, _ in db.ejecutadas if s.startswith("INSERT INTO embarque_liquidacion"))
    assert "costo_por_paquete" not in sql and "total_paquetes_venta" not in sql
    assert r.json()["resumen"]["total_pagado_real"] == pytest.approx(123407.44)
    assert db.movimientos and db.movimientos[0][0] == GERENCIA


@pytest.mark.parametrize("cuerpo", [
    {"arancel_porc": 15},             # el arancel va como fracción (0.15), no como 15
    {"tipo_cambio_log": -1},
])
def test_valida_rangos(db, cuerpo):
    c = _cliente(GERENCIA)
    if "arancel_porc" in cuerpo:
        assert c.patch("/embarques/12/items/1/costos", json=cuerpo).status_code == 422
    else:
        assert c.put("/embarques/12/landed-cost/liquidacion", json=cuerpo).status_code == 422


def test_patch_item_guarda_solo_lo_enviado(db):
    r = _cliente(GERENCIA).patch("/embarques/12/items/1/costos", json={"tipo_cambio_china": 19.5, "zeutica_paga_total": 1})
    assert r.status_code == 200
    _, params = next((s, p) for s, p in db.ejecutadas if s.startswith("UPDATE embarque_items"))
    sql = next(s for s, _ in db.ejecutadas if s.startswith("UPDATE embarque_items"))
    assert "tipo_cambio_china = %s" in sql and "paquete_venta" not in sql
    assert params == (Decimal("19.5"), 1, 12)
    assert r.json()["items"][0]["calculado"]["costo_paq_venta_china_mxn_sin_igi"] == pytest.approx(1.7 * 19.5)


def test_patch_item_invoice_debe_ser_del_embarque(db):
    c = _cliente(GERENCIA)
    assert c.patch("/embarques/12/items/1/costos", json={"invoice": "NO-EXISTE"}).status_code == 422
    assert c.patch("/embarques/12/items/1/costos", json={"invoice": " YH1400 "}).status_code == 200
    assert db.items[0]["invoice"] == "YH1400"


def test_patch_item_ajeno_404(db):
    assert _cliente(GERENCIA).patch("/embarques/12/items/77/costos", json={"ctns": 3}).status_code == 404


def test_simular_calcula_sin_guardar(db):
    cuerpo = {"liquidacion": LIQ_EXCEL, "items": [ITEM_EXCEL], "invoices": ["YH1377"]}
    r = _cliente(GERENCIA).post("/landed-cost/simular", json=cuerpo)
    assert r.status_code == 200
    assert r.json()["items"][0]["calculado"]["zeutica_paga_total"] == pytest.approx(66430.35136183618)
    assert db.ejecutadas == []


# ---- Cierre desde la etapa HL_LIQUIDADA ----

@pytest.mark.asyncio
async def test_completar_hl_cierra_el_costeo_y_bloquea_la_edicion(db):
    await embarques.marcar_etapa(12, EtapaTipo.HL_LIQUIDADA, EtapaUpdate(completado=True, usuario="ana"))
    assert db.cierre is not None and db.cierre["cerrado_por"] == "ana"
    foto = json.loads(db.cierre["resultado"])
    assert foto["items"][0]["calculado"]["zeutica_paga_total"] == pytest.approx(66430.35136183618)

    c = _cliente(GERENCIA)
    assert c.put("/embarques/12/landed-cost/liquidacion", json=LIQ_EXCEL).status_code == 409
    assert c.patch("/embarques/12/items/1/costos", json={"ctns": 1}).status_code == 409

    # Lo que cambie después en los items no altera la foto cerrada.
    db.items[0]["unit_price_fob_usd"] = 99
    d = c.get("/embarques/12/landed-cost").json()
    assert d["cerrado"] is True and d["cerrado_por"] == "ana"
    assert d["items"][0]["calculado"]["zeutica_paga_total"] == pytest.approx(66430.35136183618)


@pytest.mark.asyncio
async def test_volver_a_marcar_hl_no_reescribe_la_foto(db):
    db.cierre = {"resultado": json.dumps({"items": []}), "cerrado_por": "ana", "cerrado_en": "x"}
    await embarques.marcar_etapa(12, EtapaTipo.HL_LIQUIDADA, EtapaUpdate(completado=True, nota="100", usuario="beto"))
    assert db.cierre["cerrado_por"] == "ana"
    assert not any(s.startswith("INSERT INTO embarque_costeo_cierre") for s, _ in db.ejecutadas)


@pytest.mark.asyncio
async def test_desmarcar_hl_reabre_el_costeo(db):
    db.cierre = {"resultado": json.dumps({"items": []}), "cerrado_por": "ana", "cerrado_en": "x"}
    await embarques.marcar_etapa(12, EtapaTipo.HL_LIQUIDADA, EtapaUpdate(completado=False, usuario="ana"))
    assert db.cierre is None
    assert any("reabrió el costeo" in m[1] for m in db.movimientos)
    assert _cliente(GERENCIA).patch("/embarques/12/items/1/costos", json={"ctns": 1}).status_code == 200


@pytest.mark.asyncio
async def test_otras_etapas_no_tocan_el_costeo(db):
    await embarques.marcar_etapa(12, EtapaTipo.LIQUIDADO_CHINA, EtapaUpdate(completado=True, usuario="ana"))
    assert db.cierre is None
    assert not any("embarque_costeo_cierre" in s for s, _ in db.ejecutadas)
