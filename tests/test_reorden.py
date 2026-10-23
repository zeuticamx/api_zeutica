# Tests del punto de reorden: lógica pura (reorden_logica.py), endpoints de
# routers/reorden.py con una BD falsa y el job de Telegram (jobs/reorden_compras.py).
import math
from datetime import date, datetime
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import reorden_logica as rl
from jobs import reorden_compras as job
from permisos import usuario_autenticado
from routers import reorden

CDMX = ZoneInfo("America/Mexico_City")


# ---------- Ventana de meses ----------

def test_ventana_va_del_mes_de_la_primera_venta_al_mes_pasado():
    etiquetas, desde, hasta = rl.ventana_meses(date(2026, 10, 20), date(2025, 10, 17))
    assert len(etiquetas) == 12
    assert etiquetas[0] == "2025-10" and etiquetas[-1] == "2026-09"
    assert desde == date(2025, 10, 1) and hasta == date(2026, 10, 1)


def test_ventana_en_enero_cruza_de_anio():
    etiquetas, desde, hasta = rl.ventana_meses(date(2026, 1, 5), date(2025, 10, 9))
    assert etiquetas == ["2025-10", "2025-11", "2025-12"]
    assert desde == date(2025, 10, 1) and hasta == date(2026, 1, 1)


def test_ventana_vacia_si_la_primera_venta_es_de_este_mes_o_posterior():
    assert rl.ventana_meses(date(2026, 10, 20), date(2026, 10, 1)) == ([], date(2026, 10, 1), date(2026, 10, 1))
    assert rl.ventana_meses(date(2026, 10, 20), date(2026, 12, 1))[0] == []


def test_serie_rellena_con_cero_los_meses_sin_venta():
    assert rl.serie_consumo({"2026-02": 5}, ["2026-01", "2026-02", "2026-03"]) == [0.0, 5.0, 0.0]


# ---------- Cálculo por SKU ----------

def test_formulas_con_desviacion_muestral_y_techo():
    consumos = [100, 120, 80, 110, 90, 100]
    r = rl.calcular_sku(consumos, existencia=150, en_transito=50, lead_time=2, z=1.65)
    import statistics
    desv = statistics.stdev(consumos)  # n-1
    ss = math.ceil(1.65 * desv * math.sqrt(2))
    assert r["total_mercancia"] == 200
    assert r["promedio"] == 100
    assert r["stock_seguridad"] == ss
    assert r["punto_reorden"] == math.ceil(100 * 2 + ss)
    assert r["meses_stock"] == 2.0
    assert r["decision"] == rl.COMPRAR      # 200 <= ROP
    assert r["volatilidad"] == "NORMAL"


def test_total_igual_al_punto_de_reorden_es_comprar():
    # Sin variación: SS = 0 y ROP = promedio * LT = 30.
    r = rl.calcular_sku([10, 10, 10], existencia=30, en_transito=0, lead_time=3, z=2.33)
    assert r["stock_seguridad"] == 0 and r["punto_reorden"] == 30
    assert r["decision"] == rl.COMPRAR
    r2 = rl.calcular_sku([10, 10, 10], existencia=31, en_transito=0, lead_time=3, z=2.33)
    assert r2["decision"] == rl.OK


def test_techo_ignora_ruido_de_coma_flotante():
    # 0.1 * 3 = 0.30000000000000004 → con 10 de promedio da 3.0000000000000004, no debe subir a 4.
    r = rl.calcular_sku([0.1, 0.1, 0.1], existencia=0, en_transito=0, lead_time=30, z=1.65)
    assert r["punto_reorden"] == 3


def test_volatil_cuando_cv_supera_0_5():
    r = rl.calcular_sku([0, 0, 0, 0, 0, 60], existencia=1000, en_transito=0)
    assert r["cv"] > 0.5 and r["volatilidad"] == "VOLATIL"
    assert r["historial_corto"] is True  # un solo mes con venta


def test_sin_demanda_no_decide_compra_ni_divide_entre_cero():
    r = rl.calcular_sku([0] * 12, existencia=40, en_transito=0)
    assert r["decision"] == rl.SIN_DEMANDA
    assert r["cv"] is None and r["meses_stock"] is None and r["punto_reorden"] == 0


def test_evaluar_usa_parametros_propios_o_defaults_y_ordena():
    etiquetas = ["2026-07", "2026-08", "2026-09"]
    productos = [
        {"sku": "A", "nombre": "a", "categoria": "x", "existencia": 1000},
        {"sku": "B", "nombre": "b", "categoria": "x", "existencia": 5},
        {"sku": "C", "nombre": "c", "categoria": "x", "existencia": 0},
    ]
    consumo = {"a": {m: 10 for m in etiquetas}, " B ": {m: 10 for m in etiquetas}}  # llaves sin normalizar
    transito = {"B": 2}
    parametros = {"B": {"lead_time_meses": 1, "nivel_servicio_z": 2.33}}
    filas = rl.evaluar(productos, consumo, transito, parametros, etiquetas)
    assert [f["sku"] for f in filas] == ["B", "A", "C"]  # COMPRAR, OK, SIN_DEMANDA
    b = filas[0]
    assert b["en_transito"] == 2 and b["total_mercancia"] == 7
    assert b["lead_time_meses"] == 1 and b["nivel_servicio_z"] == 2.33 and b["parametros_propios"]
    a = filas[1]
    assert a["lead_time_meses"] == rl.LEAD_TIME_DEFAULT and a["nivel_servicio_z"] == rl.Z_DEFAULT
    assert not a["parametros_propios"]


# ---------- SQL ----------

def test_sql_consumo_solo_activas_por_anio_y_mes_numericos():
    q = " ".join(reorden.SQL_CONSUMO.split())
    assert "estatus = 'activa'" in q and "GROUP BY sku, anio, mes" in q
    assert "YEAR(fecha_registro)" in q and "MONTH(fecha_registro)" in q
    assert "DATE_FORMAT" not in q  # podía devolver bytearray y dejar todo el consumo en 0
    assert "fecha_registro >= %s AND fecha_registro < %s" in q


def test_armar_consumo_arma_etiqueta_y_tolera_bytes():
    filas = [
        {"sku": "GUA-M", "anio": 2026, "mes": 9, "cantidad": 30},
        {"sku": bytearray(b" GUA-M "), "anio": 2026, "mes": 9, "cantidad": 5},   # bytes y espacios
        {"sku": b"GUA-L", "anio": 2025, "mes": 12, "cantidad": None},
        {"sku": "X", "anio": None, "mes": None, "cantidad": 9},                  # fecha nula: se omite
    ]
    assert reorden.armar_consumo(filas) == {"GUA-M": {"2026-09": 35.0}, "GUA-L": {"2025-12": 0.0}}


def test_sql_transito_solo_embarques_sin_recibir_y_existencia_es_stock_total():
    assert "fecha_de_recibido IS NULL" in reorden.SQL_TRANSITO
    q = " ".join(reorden.SQL_PRODUCTOS.split())
    assert "stock_bodega" in q and "stock_fba" in q and "stock_clean" in q


# ---------- Endpoints (BD falsa) ----------

class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._res = []

    def execute(self, sql, params=None):
        self.db.ejecutadas.append((sql, params))
        if "MIN(fecha_registro)" in sql:
            self._res = [{"primera": self.db.primera_venta}]
        elif "FROM productos ORDER BY" in sql:
            self._res = self.db.productos
        elif "FROM ventasRegistro" in sql:
            self._res = self.db.consumo
        elif "FROM embarque_items" in sql:
            self._res = self.db.transito
        elif "FROM reorden_parametros" in sql:
            self._res = self.db.parametros
        elif "SELECT sku FROM productos WHERE sku" in sql:
            self._res = [(params[0],)] if params[0] in self.db.skus else []
        else:
            self._res = []

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
        self.skus = {"GUANITRL-M"}
        self.productos = [{"sku": "GUANITRL-M", "nombre": "Guante", "categoria": "GUANTES", "existencia": 20}]
        self.consumo = [{"sku": "GUANITRL-M", "anio": 2026, "mes": 9, "cantidad": 30}]
        self.transito = [{"sku": "GUANITRL-M", "qty": 5}]
        self.parametros = []
        self.primera_venta = datetime(2025, 10, 17, 9, 30)  # 12 meses completos antes del 2026-10

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
    monkeypatch.setattr(reorden, "get_db_connection", lambda: base)
    import mov_reg
    monkeypatch.setattr(mov_reg, "registrar_movimiento", lambda *a, **k: None)
    return base


def _cliente(usuario):
    app = FastAPI()
    app.include_router(reorden.router)
    app.dependency_overrides[usuario_autenticado] = lambda: usuario
    return TestClient(app)


def test_get_reorden_solo_gerencia(db):
    assert _cliente("ana").get("/reorden").status_code == 403


def test_get_reorden_devuelve_filas_evaluadas(db):
    r = _cliente("gerencia").get("/reorden")
    assert r.status_code == 200
    body = r.json()
    assert body["meses"][0] == "2025-10" and body["desde"] == "2025-10-01"
    assert body["defaults"] == {"lead_time_meses": 3.0, "nivel_servicio_z": 1.65}
    assert body["ventas_en_ventana"] == 30 and body["skus_vendidos_sin_producto"] == []
    f = body["filas"][0]
    assert f["sku"] == "GUANITRL-M" and f["en_transito"] == 5 and f["total_mercancia"] == 25
    assert f["decision"] in ("COMPRAR", "OK")


def test_consumo_con_sku_en_bytes_llega_a_la_fila_y_sku_huerfano_se_reporta(db):
    # Último mes completo de la ventana: el consumo debe aparecer en la fila del producto.
    ultimo = rl.ventana_meses(datetime.now(CDMX).date(), date(2025, 10, 17))[0][-1]
    anio, mes = int(ultimo[:4]), int(ultimo[5:])
    db.consumo = [{"sku": bytearray(b"GUANITRL-M"), "anio": anio, "mes": mes, "cantidad": 7},
                  {"sku": "NOEXISTE", "anio": anio, "mes": mes, "cantidad": 3}]
    body = _cliente("gerencia").get("/reorden").json()
    assert body["ventas_en_ventana"] == 10 and body["skus_vendidos_sin_producto"] == ["NOEXISTE"]
    assert body["filas"][0]["consumos"][-1] == 7 and body["filas"][0]["decision"] != "SIN_DEMANDA"


def test_put_parametros_guarda_con_usuario_del_token(db):
    r = _cliente("gerencia").put("/reorden/parametros/GUANITRL-M", json={"lead_time_meses": 2, "nivel_servicio_z": 2.33})
    assert r.status_code == 200 and r.json()["nivel_servicio_z"] == 2.33
    sql, params = db.ejecutadas[-1]
    assert "INSERT INTO reorden_parametros" in sql and params == ("GUANITRL-M", 2.0, 2.33, "gerencia")
    assert db.commits == 1


@pytest.mark.parametrize("cuerpo,codigo", [
    ({"lead_time_meses": 2, "nivel_servicio_z": 3.1}, 422),   # Z fuera del catálogo
    ({"lead_time_meses": 0, "nivel_servicio_z": 1.65}, 422),  # lead time debe ser > 0
    ({"lead_time_meses": 30, "nivel_servicio_z": 1.65}, 422), # máximo 24 meses
])
def test_put_parametros_valida(db, cuerpo, codigo):
    assert _cliente("gerencia").put("/reorden/parametros/GUANITRL-M", json=cuerpo).status_code == codigo


def test_put_parametros_sku_inexistente_404_y_no_gerencia_403(db):
    ok = {"lead_time_meses": 2, "nivel_servicio_z": 1.65}
    assert _cliente("gerencia").put("/reorden/parametros/NOEXISTE", json=ok).status_code == 404
    assert _cliente("ana").put("/reorden/parametros/GUANITRL-M", json=ok).status_code == 403


# ---------- Job de Telegram ----------

def _fila(**extra):
    f = {"sku": "SKU<1>", "nombre": "Guante & co", "total_mercancia": 7, "punto_reorden": 31,
         "meses_stock": 0.7, "volatilidad": "VOLATIL", "historial_corto": False, "decision": rl.COMPRAR}
    f.update(extra)
    return f


def test_mensaje_escapa_html_y_trae_datos():
    texto = "\n".join(job.build_mensaje([_fila()], "2025-10", "2026-09"))
    assert "1 SKU por comprar" in texto
    assert "<code>SKU&lt;1&gt;</code>" in texto and "Guante &amp; co" in texto
    assert "total 7 / ROP 31" in texto and "0.7 meses de stock" in texto and "volátil" in texto


def test_mensaje_se_parte_bajo_el_limite_de_telegram():
    chunks = job.build_mensaje([_fila(sku=f"S{i}") for i in range(200)], "2025-10", "2026-09")
    assert len(chunks) > 1 and all(len(c) <= 3800 for c in chunks)
    assert sum(c.count("🔺") for c in chunks) == 200


def test_trigger_semanal_lunes_9_cdmx():
    nxt = job.crear_trigger().get_next_fire_time(None, datetime(2026, 10, 20, 12, 0, tzinfo=CDMX))  # martes
    assert nxt.astimezone(CDMX).replace(tzinfo=None) == datetime(2026, 10, 26, 9, 0)


@pytest.fixture
def avisos(monkeypatch):
    import mov_reg
    from routers import sofi_notificaciones
    from servicios.telegram import notificacion
    tg, ws = AsyncMock(), AsyncMock()
    monkeypatch.setattr(notificacion, "send_telegram_alert", tg)
    monkeypatch.setattr(sofi_notificaciones.manager, "broadcast", ws)
    monkeypatch.setattr(mov_reg, "registrar_movimiento", lambda *a, **k: None)
    return tg, ws


def _datos(filas):
    return {"meses": ["2025-10", "2026-09"], "filas": filas}


@pytest.mark.asyncio
async def test_run_job_avisa_solo_los_de_comprar(monkeypatch, avisos):
    tg, ws = avisos
    monkeypatch.setattr(job, "cargar_reorden", lambda: _datos([_fila(), _fila(sku="X", decision=rl.OK)]))
    r = await job.run_job(motivo="scheduler")
    assert r["estado"] == "ok" and r["stats"] == {"comprar": 1, "evaluados": 2}
    assert tg.await_count == 1 and "SKU&lt;1&gt;" in tg.await_args.args[0] and "<code>X</code>" not in tg.await_args.args[0]
    assert ws.await_args.args[0]["job"] == "reorden"


@pytest.mark.asyncio
async def test_run_job_sin_compras_no_manda_telegram(monkeypatch, avisos):
    tg, _ = avisos
    monkeypatch.setattr(job, "cargar_reorden", lambda: _datos([_fila(decision=rl.OK)]))
    r = await job.run_job()
    assert r["stats"] == {"comprar": 0, "evaluados": 1, "sin_compras": True}
    tg.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_job_error_queda_en_estado(monkeypatch, avisos):
    tg, _ = avisos

    def caida():
        raise RuntimeError("sin conexión")

    monkeypatch.setattr(job, "cargar_reorden", caida)
    r = await job.run_job()
    assert r["estado"] == "error" and "sin conexión" in r["error"]
    assert "falló" in tg.await_args.args[0]


def test_sin_ventas_registradas_la_ventana_queda_vacia_y_no_truena(db):
    db.primera_venta = None
    db.consumo = []
    r = _cliente("gerencia").get("/reorden")
    assert r.status_code == 200
    body = r.json()
    assert body["meses"] == [] and body["ventas_en_ventana"] == 0
