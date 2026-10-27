# Tests del Motor Zentor (Mercado Ads): lógica pura (zentor_logica.py), endpoints de
# routers/mercado_ads.py con BD y cliente de MeLi falsos, y el job del piloto (jobs/zentor_piloto.py).
from datetime import date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import zentor_logica as zl
import zentor_service
from jobs import zentor_piloto as job
from permisos import usuario_autenticado
from routers import mercado_ads

CDMX = ZoneInfo("America/Mexico_City")
REGLAS = {"target_acos": 20.0, "max_clicks_without_sale": 30, "min_spend_threshold": 100.0}


def _pub(**extra):
    p = {"item_id": "MLM1", "titulo": "Guante", "imagen": "", "estado": "active", "campana_id": 1,
         "impresiones": 1000, "clics": 50, "unidades": 5, "inversion": Decimal("200"), "ventas": Decimal("2000")}
    p.update(extra)
    return p


def _crudo(item_id="MLM1", status="active", clics=50, costo=200, ventas=2000, unidades=5):
    return {"item_id": item_id, "title": f"Anuncio {item_id}", "status": status, "thumbnail": "http://img",
            "metrics": {"clicks": clics, "prints": 1000, "cost": costo, "total_amount": ventas, "units_quantity": unidades}}


# ---------- Rango de fechas ----------

def test_rango_por_defecto_son_7_dias_terminando_hoy():
    assert zl.rango_fechas(None, None, date(2026, 10, 27)) == (date(2026, 10, 21), date(2026, 10, 27))


@pytest.mark.parametrize("desde,hasta", [
    (date(2026, 10, 10), date(2026, 10, 1)),   # invertido
    (date(2026, 10, 1), date(2026, 11, 1)),    # futuro
    (date(2026, 6, 1), date(2026, 10, 1)),     # más de 90 días
])
def test_rango_invalido(desde, hasta):
    with pytest.raises(ValueError):
        zl.rango_fechas(desde, hasta, date(2026, 10, 27))


# ---------- Reglas ----------

def _regla(**extra):
    r = {"target_acos": 20, "max_clicks_without_sale": 30, "min_spend_threshold": 100, "auto_pilot": False}
    r.update(extra)
    return r


def test_reglas_validas_se_redondean():
    assert zl.validar_reglas(_regla(target_acos=17.456)) == {
        "target_acos": 17.46, "max_clicks_without_sale": 30, "min_spend_threshold": 100.0, "auto_pilot": False}


@pytest.mark.parametrize("cambio", [
    {"target_acos": 0}, {"target_acos": 101}, {"target_acos": -5}, {"target_acos": "abc"},
    {"max_clicks_without_sale": 0}, {"max_clicks_without_sale": 2.5}, {"max_clicks_without_sale": True},
    {"min_spend_threshold": -1}, {"min_spend_threshold": 2_000_000},
    {"auto_pilot": "si"},
])
def test_reglas_invalidas(cambio):
    with pytest.raises(ValueError):
        zl.validar_reglas(_regla(**cambio))


def test_estado_efectivo_la_pausa_manda_sobre_el_piloto():
    assert zl.estado_efectivo("pausa", True) == "pausa"
    assert zl.estado_efectivo("activo", True) == "piloto"
    assert zl.estado_efectivo("activo", False) == "activo"


# ---------- Diagnóstico ----------

def test_acos_sobre_el_objetivo_baja_puja():
    d = zl.diagnosticar(_pub(inversion=Decimal("500"), ventas=Decimal("2000")), REGLAS)  # ACOS 25 %
    assert (d["accion"], d["semaforo"], d["aplicable"]) == (zl.BAJAR_PUJA, zl.ROJO, False)


def test_acos_holgado_sugiere_subir_puja():
    d = zl.diagnosticar(_pub(inversion=Decimal("200"), ventas=Decimal("2000")), REGLAS)  # ACOS 10 % <= 14 %
    assert (d["accion"], d["semaforo"]) == (zl.SUBIR_PUJA, zl.VERDE)


def test_acos_dentro_del_objetivo_mantiene():
    d = zl.diagnosticar(_pub(inversion=Decimal("360"), ventas=Decimal("2000")), REGLAS)  # ACOS 18 %
    assert (d["accion"], d["semaforo"]) == (zl.MANTENER, zl.AMARILLO)


def test_acos_justo_en_el_objetivo_no_baja_puja():
    d = zl.diagnosticar(_pub(inversion=Decimal("400"), ventas=Decimal("2000")), REGLAS)  # ACOS 20 %
    assert d["accion"] == zl.MANTENER


def test_clics_sin_ventas_pausan_y_es_aplicable():
    d = zl.diagnosticar(_pub(clics=30, unidades=0, ventas=Decimal(0)), REGLAS)
    assert (d["accion"], d["semaforo"], d["aplicable"]) == (zl.PAUSAR, zl.ROJO, True)


def test_pocos_clics_sin_ventas_no_pausa():
    d = zl.diagnosticar(_pub(clics=29, unidades=0, ventas=Decimal(0)), REGLAS)
    assert d["accion"] == zl.MANTENER and "sin datos" in d["motivo"].lower()


def test_sin_ventas_ni_inversion_no_es_pausar():
    d = zl.diagnosticar(_pub(clics=0, unidades=0, ventas=Decimal(0), inversion=Decimal(0)), REGLAS)
    assert d["accion"] == zl.MANTENER


def test_inversion_menor_al_minimo_mantiene_aunque_el_acos_sea_malo():
    d = zl.diagnosticar(_pub(inversion=Decimal("50"), ventas=Decimal("100")), REGLAS)  # ACOS 50 % pero $50 < $100
    assert d["accion"] == zl.MANTENER


def test_pausado_nunca_recibe_pausar_y_solo_se_reactiva_si_rindio():
    mal = zl.diagnosticar(_pub(estado="paused", clics=80, unidades=0, ventas=Decimal(0)), REGLAS)
    assert mal["accion"] == zl.MANTENER
    bien = zl.diagnosticar(_pub(estado="paused", inversion=Decimal("200"), ventas=Decimal("2000")), REGLAS)
    assert (bien["accion"], bien["aplicable"]) == (zl.ACTIVAR, True)


def test_activo_nunca_recibe_activar():
    for ventas in (Decimal(0), Decimal("500"), Decimal("5000")):
        assert zl.diagnosticar(_pub(ventas=ventas, unidades=0 if ventas == 0 else 3), REGLAS)["accion"] != zl.ACTIVAR


def test_estado_desconocido_no_hace_nada():
    d = zl.diagnosticar(_pub(estado="under_review", clics=500, unidades=0, ventas=Decimal(0)), REGLAS)
    assert d["accion"] == zl.MANTENER


# ---------- Normalización y KPIs ----------

def test_normalizar_acepta_ventas_directas_mas_indirectas():
    p = zl.normalizar_publicacion({"item_id": "MLM9", "status": "ACTIVE", "metrics": {
        "clicks": 10, "cost": "12.5", "direct_amount": 100, "indirect_amount": 50, "direct_units_quantity": 2}})
    assert p["ventas"] == Decimal("150") and p["unidades"] == 2 and p["inversion"] == Decimal("12.5")
    assert p["estado"] == "active"


def test_normalizar_tolera_metricas_ausentes():
    p = zl.normalizar_publicacion({"item_id": "MLM9"})
    assert p["clics"] == 0 and p["inversion"] == 0 and p["ventas"] == 0


def test_evaluar_descarta_filas_sin_item_id():
    pubs, evaluadas = zl.evaluar([_crudo(), {"title": "sin id"}], REGLAS)
    assert len(pubs) == 1 and evaluadas[0]["item_id"] == "MLM1"


def test_kpis_agregados():
    pubs, evaluadas = zl.evaluar([_crudo("A", costo=200, ventas=2000, clics=50, unidades=5),
                                  _crudo("B", costo=100, ventas=0, clics=40, unidades=0)], REGLAS)
    k = zl.agregar_kpis(pubs, evaluadas)
    assert k["inversion"] == 300 and k["ventas"] == 2000 and k["acos"] == 15.0
    assert k["roas"] == 6.67 and k["clics"] == 90 and k["cvr"] == 5.56
    assert k["en_alerta"] == 1 and k["por_pausar"] == 1 and k["publicaciones"] == 2


def test_kpis_sin_ventas_ni_clics_dan_null_no_cero():
    pubs, evaluadas = zl.evaluar([_crudo(costo=0, ventas=0, clics=0, unidades=0)], REGLAS)
    k = zl.agregar_kpis(pubs, evaluadas)
    assert k["acos"] is None and k["roas"] is None and k["cvr"] is None
    k0 = zl.agregar_kpis([], [])
    assert k0["acos"] is None and k0["publicaciones"] == 0


# ---------- Piloto: selección y tope ----------

def test_pausas_a_aplicar_respeta_el_tope_y_prioriza_por_clics():
    crudos = [_crudo(f"M{i}", clics=30 + i, ventas=0, unidades=0, costo=10) for i in range(5)]
    _, evaluadas = zl.evaluar(crudos, REGLAS)
    pausar, omitidas = zl.pausas_a_aplicar(evaluadas, 3)
    assert [e["item_id"] for e in pausar] == ["M4", "M3", "M2"]
    assert [e["item_id"] for e in omitidas] == ["M1", "M0"]


def test_pausas_a_aplicar_tope_cero_no_pausa_nada():
    _, evaluadas = zl.evaluar([_crudo(clics=99, ventas=0, unidades=0)], REGLAS)
    assert zl.pausas_a_aplicar(evaluadas, 0)[0] == []


# ---------- Endpoints (BD y MeLi falsos) ----------

class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._res = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.db.ejecutadas.append((sql, params))
        if sql.startswith("SELECT target_acos"):
            self._res = [dict(self.db.fila)]
        elif sql.startswith("UPDATE zentor_reglas SET target_acos"):
            *valores, usuario, version = params
            if version == self.db.fila["version"]:
                f = self.db.fila
                f.update(target_acos=valores[0], max_clicks_without_sale=valores[1], min_spend_threshold=valores[2],
                         auto_pilot=valores[3], version=f["version"] + 1, actualizado_por=usuario)
                self.rowcount = 1
            else:
                self.rowcount = 0
        elif sql.startswith("UPDATE zentor_reglas SET estado_motor"):
            self.db.fila["estado_motor"] = params[0]
            self.rowcount = 1

    def fetchone(self):
        return self._res[0] if self._res else None

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        self.ejecutadas = []
        self.commits = 0
        self.fila = {"target_acos": Decimal("20.00"), "max_clicks_without_sale": 30,
                     "min_spend_threshold": Decimal("100.00"), "auto_pilot": 0, "estado_motor": "activo",
                     "version": 1, "actualizado_por": None, "actualizado_en": None}

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
    monkeypatch.setattr(mercado_ads, "get_db_connection", lambda: base)
    import mov_reg
    bitacora = []
    monkeypatch.setattr(mov_reg, "registrar_movimiento", lambda *a, **k: bitacora.append(a))
    base.bitacora = bitacora
    return base


@pytest.fixture
def ads(monkeypatch):
    """Cliente de MeLi falso: tres anuncios y registro de los cambios de estado."""
    crudos = [_crudo("PAUSAR", clics=60, ventas=0, unidades=0, costo=80),
              _crudo("BAJAR", costo=500, ventas=2000),
              _crudo("OK", costo=360, ventas=2000)]
    cambios = []

    async def listar(desde, hasta):
        return crudos

    async def cambiar(item_id, estado):
        cambios.append((item_id, estado))

    monkeypatch.setattr(zentor_service, "listar_anuncios", listar)
    monkeypatch.setattr(zentor_service, "cambiar_estado", cambiar)
    return cambios


def _cliente(usuario):
    app = FastAPI()
    app.include_router(mercado_ads.router)
    app.dependency_overrides[usuario_autenticado] = lambda: usuario
    return TestClient(app)


def test_metricas_las_ve_cualquier_sesion(db, ads):
    r = _cliente("ana").get("/mercado-ads/metricas")
    assert r.status_code == 200
    body = r.json()
    assert body["kpis"]["publicaciones"] == 3 and body["kpis"]["en_alerta"] == 2
    acciones = {p["item_id"]: p["diagnostico"]["accion"] for p in body["publicaciones"]}
    assert acciones == {"PAUSAR": "pausar", "BAJAR": "bajar_puja", "OK": "mantener"}
    assert body["reglas"]["estado"] == "activo" and body["reglas"]["target_acos"] == 20.0


def test_metricas_rango_invalido_422(db, ads):
    assert _cliente("ana").get("/mercado-ads/metricas?desde=2026-10-10&hasta=2026-10-01").status_code == 422


@pytest.mark.parametrize("sin_acceso,codigo", [(True, 503), (False, 502)])
def test_metricas_errores_de_mercado_ads(db, monkeypatch, sin_acceso, codigo):
    async def caida(desde, hasta):
        raise zentor_service.MercadoAdsError("sin scope", sin_acceso=sin_acceso)
    monkeypatch.setattr(zentor_service, "listar_anuncios", caida)
    r = _cliente("ana").get("/mercado-ads/metricas")
    assert r.status_code == codigo
    assert ("Sin conexión con Mercado Ads" in r.json()["detail"]) == sin_acceso


def test_reglas_las_ve_cualquiera_y_traen_ultima_corrida(db):
    r = _cliente("ana").get("/mercado-ads/reglas")
    assert r.status_code == 200 and "ultima_corrida" in r.json() and r.json()["version"] == 1


CUERPO = {"target_acos": 18, "max_clicks_without_sale": 25, "min_spend_threshold": 150, "auto_pilot": True, "version": 1}


def test_guardar_reglas_solo_gerencia(db):
    assert _cliente("ana").put("/mercado-ads/reglas", json=CUERPO).status_code == 403
    assert db.commits == 0


def test_guardar_reglas_sube_version_y_registra_bitacora(db):
    r = _cliente("gerencia").put("/mercado-ads/reglas", json=CUERPO)
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 2 and body["target_acos"] == 18.0 and body["auto_pilot"] is True and body["estado"] == "piloto"
    assert db.fila["actualizado_por"] == "gerencia" and db.commits == 1
    assert db.bitacora and db.bitacora[0][0] == "gerencia" and db.bitacora[0][2] == "Mercado Ads"


def test_guardar_reglas_con_version_vieja_da_409(db):
    db.fila["version"] = 5
    r = _cliente("gerencia").put("/mercado-ads/reglas", json=CUERPO)
    assert r.status_code == 409 and db.commits == 0


@pytest.mark.parametrize("cambio", [{"target_acos": 0}, {"target_acos": 150}, {"max_clicks_without_sale": 0},
                                    {"min_spend_threshold": -1}])
def test_guardar_reglas_invalidas_422(db, cambio):
    assert _cliente("gerencia").put("/mercado-ads/reglas", json={**CUERPO, **cambio}).status_code == 422


def test_motor_solo_gerencia_y_valida_el_valor(db):
    assert _cliente("ana").put("/mercado-ads/motor", json={"estado_motor": "pausa"}).status_code == 403
    assert _cliente("gerencia").put("/mercado-ads/motor", json={"estado_motor": "otro"}).status_code == 422
    r = _cliente("gerencia").put("/mercado-ads/motor", json={"estado_motor": "pausa"})
    assert r.status_code == 200 and r.json()["estado"] == "pausa"


def test_aplicar_pausa_un_anuncio_y_queda_en_bitacora(db, ads):
    r = _cliente("gerencia").post("/mercado-ads/acciones/aplicar", json={"item_id": "PAUSAR", "accion": "pausar"})
    assert r.status_code == 200 and r.json()["estado"] == "paused"
    assert ads == [("PAUSAR", "paused")]
    assert "PAUSAR" in db.bitacora[-1][1]


def test_aplicar_solo_gerencia(db, ads):
    r = _cliente("ana").post("/mercado-ads/acciones/aplicar", json={"item_id": "PAUSAR", "accion": "pausar"})
    assert r.status_code == 403 and ads == []


def test_aplicar_con_motor_en_pausa_da_409_y_no_toca_mercado_ads(db, ads):
    db.fila["estado_motor"] = "pausa"
    r = _cliente("gerencia").post("/mercado-ads/acciones/aplicar", json={"item_id": "PAUSAR", "accion": "pausar"})
    assert r.status_code == 409 and ads == []


def test_aplicar_con_recomendacion_vencida_da_409(db, ads):
    # «OK» hoy es «mantener»: pedir pausarlo (recomendación que ya no existe) no debe tocar MeLi.
    r = _cliente("gerencia").post("/mercado-ads/acciones/aplicar", json={"item_id": "OK", "accion": "pausar"})
    assert r.status_code == 409 and "cambió" in r.json()["detail"] and ads == []


def test_aplicar_anuncio_inexistente_404(db, ads):
    r = _cliente("gerencia").post("/mercado-ads/acciones/aplicar", json={"item_id": "NOEXISTE", "accion": "pausar"})
    assert r.status_code == 404 and ads == []


def test_aplicar_accion_no_aplicable_422(db, ads):
    r = _cliente("gerencia").post("/mercado-ads/acciones/aplicar", json={"item_id": "BAJAR", "accion": "bajar_puja"})
    assert r.status_code == 422 and ads == []


def test_aplicar_doble_clic_el_segundo_da_409(db, monkeypatch):
    estado = {"status": "active"}

    async def listar(desde, hasta):
        return [_crudo("X", status=estado["status"], clics=60, ventas=0, unidades=0, costo=80)]

    async def cambiar(item_id, nuevo):
        estado["status"] = "paused"

    monkeypatch.setattr(zentor_service, "listar_anuncios", listar)
    monkeypatch.setattr(zentor_service, "cambiar_estado", cambiar)
    c = _cliente("gerencia")
    cuerpo = {"item_id": "X", "accion": "pausar"}
    assert c.post("/mercado-ads/acciones/aplicar", json=cuerpo).status_code == 200
    assert c.post("/mercado-ads/acciones/aplicar", json=cuerpo).status_code == 409


def test_aplicar_sin_acceso_a_mercado_ads_da_503(db, monkeypatch):
    async def caida(desde, hasta):
        raise zentor_service.MercadoAdsError("falta scope", sin_acceso=True)
    monkeypatch.setattr(zentor_service, "listar_anuncios", caida)
    r = _cliente("gerencia").post("/mercado-ads/acciones/aplicar", json={"item_id": "X", "accion": "pausar"})
    assert r.status_code == 503


def test_evaluar_solo_gerencia_y_con_motor_en_pausa_409(db, ads):
    assert _cliente("ana").post("/mercado-ads/evaluar").status_code == 403
    db.fila["estado_motor"] = "pausa"
    assert _cliente("gerencia").post("/mercado-ads/evaluar").status_code == 409


# ---------- Job del piloto ----------

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


def _reglas(**extra):
    r = {**REGLAS, "auto_pilot": True, "estado_motor": "activo", "version": 1}
    r.update(extra)
    return r


def _anuncios(monkeypatch, crudos, fallan=()):
    cambios = []

    async def listar(desde, hasta):
        return crudos

    async def cambiar(item_id, estado):
        if item_id in fallan:
            raise zentor_service.MercadoAdsError("HTTP 500")
        cambios.append((item_id, estado))

    monkeypatch.setattr(zentor_service, "listar_anuncios", listar)
    monkeypatch.setattr(zentor_service, "cambiar_estado", cambiar)
    return cambios


def _sangrados(n):
    return [_crudo(f"M{i}", clics=40 + i, ventas=0, unidades=0, costo=20) for i in range(n)]


@pytest.mark.asyncio
async def test_job_con_piloto_pausa_con_tope_y_avisa(monkeypatch, avisos):
    tg, ws = avisos
    monkeypatch.setattr(mercado_ads, "leer_reglas", lambda: _reglas())
    monkeypatch.setenv("ZENTOR_MAX_PAUSAS", "2")
    cambios = _anuncios(monkeypatch, _sangrados(4))
    r = await job.run_job(motivo="scheduler")
    assert r["estado"] == "ok" and r["stats"]["pausados"] == 2 and r["stats"]["omitidos"] == 2
    assert cambios == [("M3", "paused"), ("M2", "paused")]
    assert tg.await_count == 1 and "tope" in tg.await_args.args[0]
    assert ws.await_args.args[0]["job"] == "zentor"


@pytest.mark.asyncio
async def test_job_sin_piloto_solo_evalua(monkeypatch, avisos):
    tg, _ = avisos
    monkeypatch.setattr(mercado_ads, "leer_reglas", lambda: _reglas(auto_pilot=False))
    cambios = _anuncios(monkeypatch, _sangrados(3))
    r = await job.run_job(motivo="manual por gerencia")
    assert r["estado"] == "ok" and r["stats"]["por_pausar"] == 3 and r["stats"]["pausados"] == 0
    assert cambios == [] and tg.await_count == 0


@pytest.mark.asyncio
async def test_job_con_motor_en_pausa_no_hace_nada(monkeypatch, avisos):
    monkeypatch.setattr(mercado_ads, "leer_reglas", lambda: _reglas(estado_motor="pausa"))
    cambios = _anuncios(monkeypatch, _sangrados(3))
    r = await job.run_job(motivo="scheduler")
    assert r["estado"] == "omitido" and cambios == []


@pytest.mark.asyncio
async def test_job_un_fallo_no_aborta_el_lote(monkeypatch, avisos):
    tg, _ = avisos
    monkeypatch.setattr(mercado_ads, "leer_reglas", lambda: _reglas())
    cambios = _anuncios(monkeypatch, _sangrados(3), fallan={"M2"})
    r = await job.run_job(motivo="scheduler")
    assert r["stats"]["pausados"] == 2 and r["stats"]["fallidos"] == 1
    assert {c[0] for c in cambios} == {"M1", "M0"} and "M2" in tg.await_args.args[0]


@pytest.mark.asyncio
async def test_job_dry_run_no_pausa(monkeypatch, avisos):
    monkeypatch.setattr(mercado_ads, "leer_reglas", lambda: _reglas())
    cambios = _anuncios(monkeypatch, _sangrados(2))
    r = await job.run_job(dry_run=True, motivo="manual")
    assert r["stats"]["pausados"] == 2 and cambios == []


@pytest.mark.asyncio
async def test_job_sin_acceso_queda_en_error_sin_spam_de_telegram(monkeypatch, avisos):
    tg, _ = avisos
    monkeypatch.setattr(mercado_ads, "leer_reglas", lambda: _reglas())

    async def caida(desde, hasta):
        raise zentor_service.MercadoAdsError("falta scope", sin_acceso=True)
    monkeypatch.setattr(zentor_service, "listar_anuncios", caida)
    r = await job.run_job(motivo="scheduler")
    assert r["estado"] == "error" and r["sin_acceso"] is True and tg.await_count == 0


def test_mensaje_escapa_html():
    pub = {"item_id": "MLM<1>", "titulo": "Guante & co", "diagnostico": {"motivo": "50 clics <sin> venta"}}
    texto = job.build_mensaje([pub], [], [])
    assert "<code>MLM&lt;1&gt;</code>" in texto and "Guante &amp; co" in texto and "&lt;sin&gt;" in texto


def test_trigger_diario_13_cdmx():
    nxt = job.crear_trigger("13:00").get_next_fire_time(None, datetime(2026, 10, 27, 14, 0, tzinfo=CDMX))
    assert nxt.astimezone(CDMX).replace(tzinfo=None) == datetime(2026, 10, 28, 13, 0)
