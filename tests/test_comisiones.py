# Tests de comisiones (comisiones_calc.py + routers/comisiones.py): exclusión de
# canales, fórmula exacta, permisos (solo gerencia edita la matriz) y vínculo
# venta ↔ seguimiento del CRM. MySQL y bitácora van simulados.
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import comisiones_calc as calc
import permisos
from routers import comisiones, crm

D = Decimal


# ---------- BD simulada ----------

class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._resultado = []
        self.rowcount = 0

    def execute(self, query, params=None):
        q = " ".join(query.split())
        db = self.db
        db.ejecutados.append((q, params))
        self._resultado, self.rowcount = [], 0

        if q.startswith("SELECT nombre_usuario FROM usuarios WHERE token"):
            u = db.tokens.get(params[0])
            self._resultado = [{"nombre_usuario": u}] if u else []
        elif q.startswith("SELECT nombre_usuario FROM usuarios WHERE nombre_usuario"):
            self._resultado = [{"nombre_usuario": params[0]}] if params[0] in db.usuarios else []
        elif q.startswith("SELECT sku, porcentaje FROM comisiones_config WHERE vendedor"):
            self._resultado = [{"sku": s, "porcentaje": p} for s, p in db.tasas.get(params[0], {}).items()]
        elif q.startswith("INSERT IGNORE INTO comisiones_ventas"):
            self.rowcount = 1
        elif q.startswith("INSERT INTO comisiones_config"):
            self.rowcount = 1
        elif q.startswith("DELETE FROM comisiones_config"):
            self.rowcount = 1
        elif q.startswith("SELECT id FROM clientes WHERE TRIM(nombre)"):
            cid = db.clientes.get(params[0])
            self._resultado = [{"id": cid}] if cid else []
        elif q.startswith("SELECT id FROM crm_interacciones WHERE cliente_id"):
            ids = db.abiertos.get((params[0], params[1]), [])
            self._resultado = [{"id": ids[0]}] if ids else []
        elif q.startswith("SELECT c.id, c.nombre, c.empresa"):
            self._resultado = [{
                "id": params[0], "nombre": "Cliente", "empresa": None, "telefono": None, "email": None,
                "registrado_por": "ana", "en_cartera": params[0], "vendedor": "ana", "etapa": "en_seguimiento",
                "motivo_perdida": None, "etapa_actualizada": None,
            }]
        elif q.startswith("SELECT vendedor, comprador FROM comisiones_ventas WHERE id_ventas"):
            v = db.ventas.get(params[0])
            self._resultado = [v] if v else []
        elif q.startswith("SELECT id, cliente_id, vendedor FROM crm_interacciones WHERE id"):
            s = db.seguimientos.get(params[0])
            self._resultado = [{"id": params[0], **s}] if s else []
        elif q.startswith("SELECT codigo_cotizacion, usuario FROM cotizaciones WHERE codigo_cotizacion"):
            u = db.cotizaciones.get(params[0])
            self._resultado = [{"codigo_cotizacion": params[0], "usuario": u}] if u else []
        elif q.startswith("DELETE FROM venta_seguimiento"):
            self.rowcount = 1
        else:
            for marca, filas in db.respuestas:
                if marca in q:
                    self._resultado = [dict(f) for f in filas]
                    break

    def fetchone(self):
        return self._resultado[0] if self._resultado else None

    def fetchall(self):
        return list(self._resultado)

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        self.tokens = {"tok-ger": "gerencia", "tok-ana": "ana", "tok-luis": "luis"}
        self.usuarios = {"gerencia", "ana", "luis"}
        self.tasas = {}            # vendedor -> {sku: porcentaje}
        self.clientes = {}         # nombre -> id
        self.abiertos = {}         # (cliente_id, vendedor) -> [ids de seguimientos abiertos, recientes primero]
        self.ventas = {}           # id_ventas -> {vendedor, comprador}
        self.seguimientos = {}     # id -> {cliente_id, vendedor}
        self.cotizaciones = {}     # folio -> usuario que la hizo
        self.respuestas = []
        self.ejecutados = []
        self.commits = 0

    def cursor(self, dictionary=False):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass

    def consultas(self, inicio):
        return [(q, p) for q, p in self.ejecutados if q.startswith(inicio)]


@pytest.fixture
def db(monkeypatch):
    base = FakeDB()
    monkeypatch.setattr(comisiones, "get_db_connection", lambda: base)
    monkeypatch.setattr(permisos, "get_db_connection", lambda: base)
    monkeypatch.setenv("USUARIOS_GERENCIA", "gerencia")
    movimientos = []
    monkeypatch.setattr(comisiones.mov_reg, "registrar_movimiento", lambda *a, **k: movimientos.append(a))
    base.movimientos = movimientos
    return base


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(comisiones.router)
    return TestClient(app)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


ANA, LUIS, GER = auth("tok-ana"), auth("tok-luis"), auth("tok-ger")


def registrar(db, items, vendedor="ana", plataforma="Directo", comprador="Cliente SA", **extra):
    cursor = db.cursor()
    return comisiones.registrar_comisiones(cursor, 1001, vendedor, comprador, plataforma, "2026-10-01", items, **extra)


# ---------- Exclusión de canales ----------

@pytest.mark.parametrize("plataforma, comprador, esperado", [
    ("Directo", "Cliente SA", False),
    ("Local", "Cliente SA", False),
    ("BODEGA", "Cliente SA", False),
    ("Amazon", "Cliente SA", True),
    ("AMAZON FBA", "Cliente SA", True),
    ("Mercado Libre", "Cliente SA", True),
    ("MERCADO LIBRE FULL", "Cliente SA", True),
    ("meli", "Cliente SA", True),
    ("Cleanest", "Cliente SA", True),
    ("SISTEMA ZEUTICA", "CLEANEST CHOICE", True),   # así registra el panel las ventas de Cleanest
    ("Directo", "Cleanest Choice", True),
    ("SISTEMA ZEUTICA", "Cliente SA", False),
    ("Directo", "Amazon Servicios SA", False),       # un cliente llamado Amazon que compra directo sí comisiona
])
def test_canal_excluido(plataforma, comprador, esperado):
    assert calc.canal_excluido(plataforma, comprador) is esperado


def test_ventas_con_llave_de_marketplace_estan_excluidas():
    assert calc.canal_excluido("Directo", "X", meli_key="MLM123") is True
    assert calc.canal_excluido("Directo", "X", amazon_key="114-1") is True


@pytest.mark.parametrize("plataforma, comprador", [
    ("Amazon", "Cliente SA"), ("Mercado Libre", "Cliente SA"), ("Directo", "CLEANEST CHOICE"),
])
def test_venta_excluida_no_genera_comision_ni_vinculo(db, plataforma, comprador):
    db.tasas["ana"] = {"*": D("5")}
    r = registrar(db, [{"sku": "A", "producto": "A", "cantidad": 1, "precio": 116}], plataforma=plataforma, comprador=comprador)
    assert r["excluida"] is True
    assert db.consultas("INSERT") == []
    assert db.consultas("SELECT sku, porcentaje") == []


def test_venta_directa_si_genera_comision(db):
    db.tasas["ana"] = {"*": D("5")}
    r = registrar(db, [{"sku": "A", "producto": "A", "cantidad": 1, "precio": 116}])
    assert r["excluida"] is False and r["partidas"] == 1
    assert len(db.consultas("INSERT IGNORE INTO comisiones_ventas")) == 1


# ---------- Fórmula exacta ----------

@pytest.mark.parametrize("precio, cantidad, pct, neto, base, comision", [
    (116, 1, 5, "116.00", "100.00", "5.00"),
    (58, 3, "2.5", "174.00", "150.00", "3.75"),
    (232, 1, "12.5", "232.00", "200.00", "25.00"),
    (116, 10, 10, "1160.00", "1000.00", "100.00"),
    (116, 1, 0, "116.00", "100.00", "0.00"),
    ("99.99", 1, 10, "99.99", "86.20", "8.62"),        # 86.1982758… × 10% = 8.6198… → 8.62
    ("10.00", 3, "3.33", "30.00", "25.86", "0.86"),    # 25.8620689… × 3.33% = 0.8612… → 0.86
])
def test_formula_comision(precio, cantidad, pct, neto, base, comision):
    r = calc.calcular_partida(precio, cantidad, pct)
    assert r["precio_neto"] == D(neto)
    assert r["base_sin_iva"] == D(base)
    assert r["comision"] == D(comision)


def test_la_comision_sale_de_la_base_sin_redondear():
    # Con base redondeada (86.20) daría 8.62 también; este caso las separa:
    # 1.17/1.16 = 1.00862… → base 1.01; 1.00862… × 50% = 0.5043 → 0.50 (con 1.01 daría 0.505 → 0.51)
    r = calc.calcular_partida("1.17", 1, 50)
    assert r["base_sin_iva"] == D("1.01")
    assert r["comision"] == D("0.50")


def test_tasa_por_sku_base_y_sin_tasa(db):
    db.tasas["ana"] = {"A": D("5"), "*": D("2")}
    items = [
        {"sku": "A", "producto": "A", "cantidad": 1, "precio": 116},   # tasa propia 5%
        {"sku": "B", "producto": "B", "cantidad": 1, "precio": 116},   # tasa base 2%
    ]
    registrar(db, items)
    filas = db.consultas("INSERT IGNORE INTO comisiones_ventas")
    # (id_ventas, sku, producto, cantidad, vendedor, comprador, plataforma, fecha, neto, base, pct, comision, origen)
    assert [(p[1], p[10], p[11], p[12]) for _, p in filas] == [
        ("A", D("5"), D("5.00"), "sku"),
        ("B", D("2"), D("2.00"), "base"),
    ]


def test_sku_sin_porcentaje_ni_base_se_marca_sin_tasa(db):
    db.tasas["luis"] = {"A": D("5")}
    registrar(db, [{"sku": "Z", "producto": "Z", "cantidad": 2, "precio": 116}], vendedor="luis")
    (_, p), = db.consultas("INSERT IGNORE INTO comisiones_ventas")
    assert (p[10], p[11], p[12]) == (D("0"), D("0.00"), "sin_tasa")


def test_la_tasa_es_del_vendedor_que_vendio(db):
    db.tasas = {"ana": {"A": D("5")}, "luis": {"A": D("1")}}
    registrar(db, [{"sku": "A", "producto": "A", "cantidad": 1, "precio": 116}], vendedor="luis")
    (_, p), = db.consultas("INSERT IGNORE INTO comisiones_ventas")
    assert p[4] == "luis" and p[11] == D("1.00")


# ---------- Reporte ----------

def _fila(id_ventas, sku, vendedor, base, com, pct, origen="sku", seg=None):
    return {"id_ventas": id_ventas, "sku": sku, "producto": sku, "cantidad": 1, "vendedor": vendedor,
            "comprador": "Cliente SA", "fecha_venta": date(2026, 10, 1), "precio_neto": D(base) * D("1.16"),
            "base_sin_iva": D(base), "porcentaje": D(pct), "comision": D(com), "origen_tasa": origen,
            "seguimiento_id": seg, "modo": "auto" if seg else None}


def test_armar_reporte_acumula_por_venta_sku_y_vendedor():
    filas = [
        _fila("1", "A", "ana", "100.00", "5.00", "5", seg=9),
        _fila("1", "B", "ana", "200.00", "4.00", "2", "base", seg=9),
        _fila("2", "A", "luis", "100.00", "1.00", "1"),
    ]
    rep = calc.armar_reporte(filas)
    assert rep["resumen"]["ventas"] == 2
    assert rep["resumen"]["base_sin_iva"] == D("400.00")
    assert rep["resumen"]["comision"] == D("10.00")
    assert rep["resumen"]["sin_vinculo"] == 1
    venta1 = next(v for v in rep["ventas"] if v["id_ventas"] == "1")
    assert venta1["base_sin_iva"] == D("300.00") and venta1["comision"] == D("9.00") and len(venta1["partidas"]) == 2
    sku_a = next(s for s in rep["por_sku"] if s["sku"] == "A")
    assert sku_a["cantidad"] == 2 and sku_a["comision"] == D("6.00")
    assert {w["vendedor"]: w["comision"] for w in rep["por_vendedor"]} == {"ana": D("9.00"), "luis": D("1.00")}


def test_vendedor_solo_ve_sus_comisiones_en_el_reporte(client, db):
    db.respuestas = [("FROM comisiones_ventas cv", [_fila("1", "A", "ana", "100.00", "5.00", "5")])]
    r = client.get("/comisiones/reporte?desde=2026-10-01&hasta=2026-10-31&vendedor=luis", headers=ANA)
    assert r.status_code == 200
    (q, p), = db.consultas("SELECT cv.id_ventas")
    assert "cv.vendedor = %s" in q and p[-1] == "ana"   # ignora el vendedor ajeno que pidió
    assert r.json()["resumen"]["comision"] == 5.0


def test_gerencia_ve_todos_en_el_reporte(client, db):
    db.respuestas = [("FROM comisiones_ventas cv", [])]
    assert client.get("/comisiones/reporte?desde=2026-10-01&hasta=2026-10-31", headers=GER).status_code == 200
    (q, _), = db.consultas("SELECT cv.id_ventas")
    assert "cv.vendedor = %s" not in q


def test_reporte_rechaza_rango_invertido(client, db):
    assert client.get("/comisiones/reporte?desde=2026-10-31&hasta=2026-10-01", headers=GER).status_code == 422


# ---------- Permisos de la matriz ----------

CUERPO = {"vendedor": "ana", "items": [{"sku": "A", "porcentaje": 5}, {"sku": "*", "porcentaje": 2}]}


def test_vendedor_no_puede_editar_la_matriz(client, db):
    assert client.put("/comisiones/config", json=CUERPO, headers=ANA).status_code == 403
    assert db.consultas("INSERT INTO comisiones_config") == []
    assert db.commits == 0


def test_vendedor_no_puede_editar_su_propia_matriz_ni_la_de_otro(client, db):
    assert client.put("/comisiones/config", json={**CUERPO, "vendedor": "luis"}, headers=ANA).status_code == 403


def test_sin_token_no_puede_editar(client, db):
    assert client.put("/comisiones/config", json=CUERPO).status_code in (401, 403)


def test_gerencia_edita_la_matriz(client, db):
    r = client.put("/comisiones/config", json=CUERPO, headers=GER)
    assert r.status_code == 200 and r.json() == {"vendedor": "ana", "guardados": 2, "borrados": 0}
    inserts = db.consultas("INSERT INTO comisiones_config")
    assert [p for _, p in inserts] == [("ana", "A", D("5"), "gerencia"), ("ana", "*", D("2"), "gerencia")]
    assert db.commits == 1


def test_porcentaje_null_borra_la_tasa(client, db):
    r = client.put("/comisiones/config", json={"vendedor": "ana", "items": [{"sku": "A", "porcentaje": None}]}, headers=GER)
    assert r.status_code == 200 and r.json()["borrados"] == 1
    assert db.consultas("INSERT INTO comisiones_config") == []


def test_matriz_rechaza_vendedor_inexistente(client, db):
    assert client.put("/comisiones/config", json={**CUERPO, "vendedor": "fantasma"}, headers=GER).status_code == 404


@pytest.mark.parametrize("pct", [-1, 100.5, 101, 5.555])
def test_matriz_rechaza_porcentajes_invalidos(client, db, pct):
    r = client.put("/comisiones/config", json={"vendedor": "ana", "items": [{"sku": "A", "porcentaje": pct}]}, headers=GER)
    assert r.status_code == 422


def test_vendedor_solo_lee_su_matriz(client, db):
    db.respuestas = [("FROM comisiones_config", [])]
    client.get("/comisiones/config?vendedor=luis", headers=ANA)
    (_, p), = db.consultas("SELECT vendedor, sku, porcentaje")
    assert p == ("ana",)


def test_recalcular_es_solo_gerencia(client, db):
    r = client.post("/comisiones/recalcular", json={"desde": "2026-10-01", "hasta": "2026-10-31"}, headers=ANA)
    assert r.status_code == 403


# ---------- Vinculación venta ↔ seguimiento ----------

ITEM = {"sku": "A", "producto": "A", "cantidad": 1, "precio": 116}


def test_venta_se_vincula_sola_al_seguimiento_abierto_mas_reciente(db):
    db.clientes["Cliente SA"] = 3
    db.abiertos[(3, "ana")] = [77, 40]   # 77 es el más reciente
    r = registrar(db, [ITEM])
    assert r["seguimiento_id"] == 77
    (_, p), = db.consultas("INSERT INTO venta_seguimiento")
    assert p == ("1001", 3, 77, None, "ana", "auto")
    # cierra el seguimiento y pasa al cliente a 'ganado'
    assert db.consultas("UPDATE crm_interacciones SET seguimiento_cerrado = 1 WHERE cliente_id")
    (_, e), = db.consultas("UPDATE crm_cartera SET etapa")
    assert e[0] == "ganado"


def test_sin_seguimiento_abierto_no_hay_vinculo(db):
    db.clientes["Cliente SA"] = 3
    r = registrar(db, [ITEM])
    assert r["seguimiento_id"] is None
    assert db.consultas("INSERT INTO venta_seguimiento") == []
    assert db.consultas("UPDATE crm_cartera") == []


def test_seguimiento_de_otro_vendedor_no_se_vincula(db):
    db.clientes["Cliente SA"] = 3
    db.abiertos[(3, "luis")] = [55]
    assert registrar(db, [ITEM], vendedor="ana")["seguimiento_id"] is None


def test_comprador_desconocido_no_se_vincula(db):
    db.abiertos[(3, "ana")] = [77]
    assert registrar(db, [ITEM], comprador="Nadie")["seguimiento_id"] is None


def test_venta_excluida_no_se_vincula(db):
    db.clientes["Cliente SA"] = 3
    db.abiertos[(3, "ana")] = [77]
    registrar(db, [ITEM], plataforma="Mercado Libre")
    assert db.consultas("INSERT INTO venta_seguimiento") == []


def test_recalcular_historico_no_vincula(db):
    db.clientes["Cliente SA"] = 3
    db.abiertos[(3, "ana")] = [77]
    registrar(db, [ITEM], auto_vincular=False)
    assert db.consultas("INSERT INTO venta_seguimiento") == []


def test_vinculo_manual_del_vendedor_dueno(client, db):
    db.ventas["1001"] = {"vendedor": "ana", "comprador": "Cliente SA"}
    db.seguimientos[88] = {"cliente_id": 3, "vendedor": "ana"}
    r = client.post("/comisiones/vinculos", json={"id_ventas": "1001", "seguimiento_id": 88}, headers=ANA)
    assert r.status_code == 200 and r.json()["modo"] == "manual"
    (_, p), = db.consultas("INSERT INTO venta_seguimiento")
    assert p == ("1001", 3, 88, None, "ana", "manual")
    assert db.commits == 1


def test_vendedor_no_vincula_venta_de_otro(client, db):
    db.ventas["1001"] = {"vendedor": "luis", "comprador": "Cliente SA"}
    db.seguimientos[88] = {"cliente_id": 3, "vendedor": "ana"}
    r = client.post("/comisiones/vinculos", json={"id_ventas": "1001", "seguimiento_id": 88}, headers=ANA)
    assert r.status_code == 403
    assert db.consultas("INSERT INTO venta_seguimiento") == []


def test_vendedor_no_vincula_seguimiento_de_otro(client, db):
    db.ventas["1001"] = {"vendedor": "ana", "comprador": "Cliente SA"}
    db.seguimientos[99] = {"cliente_id": 3, "vendedor": "luis"}
    r = client.post("/comisiones/vinculos", json={"id_ventas": "1001", "seguimiento_id": 99}, headers=ANA)
    assert r.status_code == 403


def test_gerencia_puede_vincular_cualquier_venta(client, db):
    db.ventas["1001"] = {"vendedor": "luis", "comprador": "Cliente SA"}
    db.seguimientos[99] = {"cliente_id": 3, "vendedor": "luis"}
    r = client.post("/comisiones/vinculos", json={"id_ventas": "1001", "seguimiento_id": 99}, headers=GER)
    assert r.status_code == 200
    (_, p), = db.consultas("INSERT INTO venta_seguimiento")
    assert p == ("1001", 3, 99, None, "luis", "manual")   # el vínculo queda a nombre del vendedor de la venta


def test_vinculo_con_venta_inexistente_o_excluida_da_404(client, db):
    db.seguimientos[88] = {"cliente_id": 3, "vendedor": "ana"}
    r = client.post("/comisiones/vinculos", json={"id_ventas": "404", "seguimiento_id": 88}, headers=ANA)
    assert r.status_code == 404


def test_vinculo_con_seguimiento_inexistente_da_404(client, db):
    db.ventas["1001"] = {"vendedor": "ana", "comprador": "Cliente SA"}
    r = client.post("/comisiones/vinculos", json={"id_ventas": "1001", "seguimiento_id": 12345}, headers=ANA)
    assert r.status_code == 404


def test_desvincular_respeta_dueno(client, db):
    db.ventas["1001"] = {"vendedor": "luis", "comprador": "Cliente SA"}
    assert client.delete("/comisiones/vinculos/1001", headers=ANA).status_code == 403
    assert client.delete("/comisiones/vinculos/1001", headers=LUIS).status_code == 200


# ---------- Vinculación por folio de cotización ----------

def test_venta_desde_cotizacion_queda_ligada_al_folio(db):
    db.cotizaciones["COT-0042"] = "ana"
    r = registrar(db, [ITEM], cotizacion="COT-0042")
    assert r["cotizacion"] == "COT-0042"
    (_, p), = db.consultas("INSERT INTO venta_seguimiento")
    assert p == ("1001", None, None, "COT-0042", "ana", "manual")


def test_folio_inexistente_en_la_venta_se_ignora(db):
    r = registrar(db, [ITEM], cotizacion="COT-9999")
    assert r["cotizacion"] is None
    assert db.consultas("INSERT INTO venta_seguimiento") == []


def test_folio_y_seguimiento_automatico_conviven(db):
    db.clientes["Cliente SA"] = 3
    db.abiertos[(3, "ana")] = [77]
    db.cotizaciones["COT-0042"] = "ana"
    registrar(db, [ITEM], cotizacion="COT-0042")
    vinculos = [p for _, p in db.consultas("INSERT INTO venta_seguimiento")]
    assert ("1001", 3, 77, None, "ana", "auto") in vinculos
    assert ("1001", None, None, "COT-0042", "ana", "manual") in vinculos


def test_venta_excluida_ignora_el_folio(db):
    db.cotizaciones["COT-0042"] = "ana"
    registrar(db, [ITEM], plataforma="Amazon", cotizacion="COT-0042")
    assert db.consultas("INSERT INTO venta_seguimiento") == []


def test_vinculo_manual_solo_por_folio_no_toca_el_crm(client, db):
    db.ventas["1001"] = {"vendedor": "ana", "comprador": "Cliente SA"}
    db.cotizaciones["COT-0042"] = "ana"
    r = client.post("/comisiones/vinculos", json={"id_ventas": "1001", "cotizacion": " COT-0042 "}, headers=ANA)
    assert r.status_code == 200 and r.json()["cotizacion"] == "COT-0042" and r.json()["seguimiento_id"] is None
    (_, p), = db.consultas("INSERT INTO venta_seguimiento")
    assert p == ("1001", None, None, "COT-0042", "ana", "manual")
    assert db.consultas("UPDATE crm_cartera") == [] and db.consultas("UPDATE crm_interacciones") == []


def test_vinculo_manual_con_folio_y_seguimiento(client, db):
    db.ventas["1001"] = {"vendedor": "ana", "comprador": "Cliente SA"}
    db.seguimientos[88] = {"cliente_id": 3, "vendedor": "ana"}
    db.cotizaciones["COT-0042"] = "ana"
    r = client.post("/comisiones/vinculos", json={"id_ventas": "1001", "seguimiento_id": 88, "cotizacion": "COT-0042"}, headers=ANA)
    assert r.status_code == 200
    (_, p), = db.consultas("INSERT INTO venta_seguimiento")
    assert p == ("1001", 3, 88, "COT-0042", "ana", "manual")


def test_vinculo_exige_seguimiento_o_folio(client, db):
    db.ventas["1001"] = {"vendedor": "ana", "comprador": "Cliente SA"}
    assert client.post("/comisiones/vinculos", json={"id_ventas": "1001"}, headers=ANA).status_code == 422
    assert client.post("/comisiones/vinculos", json={"id_ventas": "1001", "cotizacion": "  "}, headers=ANA).status_code == 422


def test_folio_inexistente_da_404(client, db):
    db.ventas["1001"] = {"vendedor": "ana", "comprador": "Cliente SA"}
    r = client.post("/comisiones/vinculos", json={"id_ventas": "1001", "cotizacion": "COT-9999"}, headers=ANA)
    assert r.status_code == 404
    assert db.consultas("INSERT INTO venta_seguimiento") == []


def test_vendedor_no_vincula_cotizacion_de_otro(client, db):
    db.ventas["1001"] = {"vendedor": "ana", "comprador": "Cliente SA"}
    db.cotizaciones["COT-0050"] = "luis"
    r = client.post("/comisiones/vinculos", json={"id_ventas": "1001", "cotizacion": "COT-0050"}, headers=ANA)
    assert r.status_code == 403
    assert db.consultas("INSERT INTO venta_seguimiento") == []


def test_vendedor_no_vincula_folio_a_venta_de_otro(client, db):
    db.ventas["1001"] = {"vendedor": "luis", "comprador": "Cliente SA"}
    db.cotizaciones["COT-0042"] = "ana"
    assert client.post("/comisiones/vinculos", json={"id_ventas": "1001", "cotizacion": "COT-0042"}, headers=ANA).status_code == 403


def test_gerencia_vincula_cualquier_folio(client, db):
    db.ventas["1001"] = {"vendedor": "luis", "comprador": "Cliente SA"}
    db.cotizaciones["COT-0050"] = "luis"
    assert client.post("/comisiones/vinculos", json={"id_ventas": "1001", "cotizacion": "COT-0050"}, headers=GER).status_code == 200


def test_candidatos_incluyen_cotizaciones_del_cliente_y_vendedor(client, db):
    db.ventas["1001"] = {"vendedor": "ana", "comprador": "Cliente SA"}
    db.respuestas = [("FROM cotizaciones", [{"codigo_cotizacion": "COT-0042", "empresa": "Cliente SA", "fecha": "2026-09-30", "total": D("116"), "vendido": 0}])]
    r = client.get("/comisiones/candidatos/1001", headers=ANA)
    assert r.status_code == 200
    assert [c["codigo_cotizacion"] for c in r.json()["cotizaciones"]] == ["COT-0042"]
    assert r.json()["seguimientos"] == []     # el cliente no está en Clientes: sin seguimientos que proponer
    (_, p), = db.consultas("SELECT codigo_cotizacion, empresa")
    assert p == ("Cliente SA", "ana")


def test_reporte_cuenta_como_vinculada_la_venta_con_folio():
    filas = [dict(_fila("1", "A", "ana", "100.00", "5.00", "5"), cotizacion="COT-0042"),
             _fila("2", "A", "ana", "100.00", "5.00", "5")]
    rep = calc.armar_reporte(filas)
    assert rep["resumen"]["sin_vinculo"] == 1
    assert next(v for v in rep["ventas"] if v["id_ventas"] == "1")["cotizacion"] == "COT-0042"


# ---------- Hook desde ventas ----------

def test_hook_nunca_propaga_errores(monkeypatch):
    def falla():
        raise RuntimeError("sin base de datos")
    monkeypatch.setattr(comisiones, "get_db_connection", falla)
    assert comisiones.registrar_comisiones_seguro(1, "ana", "X", "Directo", "2026-10-01", []) is None


def test_hook_hace_commit_en_exito(db):
    db.tasas["ana"] = {"*": D("5")}
    r = comisiones.registrar_comisiones_seguro(1001, "ana", "Cliente SA", "Directo", "2026-10-01", [ITEM])
    assert r["partidas"] == 1 and db.commits == 1
