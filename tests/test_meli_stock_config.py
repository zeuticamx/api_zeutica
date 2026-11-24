# Configuración editable del job de stock MeLi: validaciones puras, lectura de config en el job
# (interruptor, falla de BD) y endpoints de routers/meli_stock_config.py con BD falsa.
# El SQL real (CREATE/INSERT IGNORE/ON DUPLICATE KEY) no se ha probado contra un MySQL real.
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from servicios import mov_reg
from jobs import meli_stock as job
from servicios.permisos import usuario_autenticado
from routers import meli_stock_config as router_cfg


# ---------- Validaciones puras ----------

def test_normalizar_id_meli():
    assert job.normalizar_id_meli("  mlm1234567890 ") == "MLM1234567890"
    for malo in ["", "ABC123", "MLM12", "MLM", "MLA1234567", "MLM12 34567", None]:
        with pytest.raises(ValueError):
            job.normalizar_id_meli(malo)


@pytest.mark.parametrize("valor,esperado", [(2, 2), ("5", 5), (" 10 ", 10), (1000, 1000), (3.0, 3)])
def test_multiplicador_valido(valor, esperado):
    assert job.validar_multiplicador(valor) == esperado


@pytest.mark.parametrize("valor", [0, -1, 1001, 2.5, "abc", "", None, True, "1e3"])
def test_multiplicador_invalido(valor):
    with pytest.raises(ValueError):
        job.validar_multiplicador(valor)


@pytest.mark.parametrize("hora", ["20:00", "00:00", "23:59", " 09:05 "])
def test_hora_valida(hora):
    assert job.validar_hora(hora) == hora.strip()


@pytest.mark.parametrize("hora", ["24:00", "9:00", "20:60", "abc", "", None, "20:00:00"])
def test_hora_invalida(hora):
    with pytest.raises(ValueError):
        job.validar_hora(hora)


def test_la_semilla_no_deja_multiplicadores_de_ids_ignorados():
    assert not (set(job.SEMILLA_MULTIPLICADORES) & job.SEMILLA_IGNORAR)
    assert "MLM2505625865" in job.SEMILLA_IGNORAR and "MLM2505490861" in job.SEMILLA_IGNORAR


# ---------- Decisión con la configuración inyectada ----------

def _det(item_id):
    return {"id": item_id, "title": "P", "available_quantity": 0, "variations": [], "buying_mode": ""}


def test_decidir_usa_multiplicadores_inyectados_y_1_por_defecto():
    db = {"MLM1": {"sku": "ABC", "stock_bodega": 100}, "MLM2": {"sku": "DEF", "stock_bodega": 100}}
    ups, _ = job.decidir_actualizaciones([_det("MLM1"), _det("MLM2")], db, multiplicadores={"MLM1": 5})
    por_id = {u["item_id"]: u for u in ups}
    assert por_id["MLM1"]["mult"] == 5 and por_id["MLM1"]["nuevo"] == 3    # 20 paquetes * .15
    assert por_id["MLM2"]["mult"] == 1 and por_id["MLM2"]["nuevo"] == 15   # sin entrada = 1


def test_decidir_ignorado_gana_sobre_multiplicador():
    db = {"MLM1": {"sku": "ABC", "stock_bodega": 100}}
    ups, sin = job.decidir_actualizaciones([_det("MLM1")], db, ignorar={"MLM1"}, multiplicadores={"MLM1": 5})
    assert ups == [] and sin == []


def test_decidir_sin_configuracion_no_ignora_nada():
    db = {"MLM1": {"sku": "ABC", "stock_bodega": 100}}
    ups, _ = job.decidir_actualizaciones([_det("MLM1")], db)
    assert [u["item_id"] for u in ups] == ["MLM1"]


# ---------- run_job: interruptor y falla de configuración ----------

@pytest.fixture
def avisos(monkeypatch):
    from servicios.telegram import notificacion
    enviados = []

    async def tg(texto):
        enviados.append(texto)

    monkeypatch.setattr(notificacion, "send_telegram_alert", tg)
    monkeypatch.setattr(mov_reg, "registrar_movimiento", lambda *a, **k: None)
    return enviados


def _cfg(**extra):
    base = {"habilitado": True, "dry_run": True, "hora": "20:00", "version": 1,
            "ignorar": set(), "multiplicadores": {}}
    base.update(extra)
    return base


@pytest.mark.asyncio
async def test_scheduler_apagado_no_corre_ni_avisa(monkeypatch, avisos):
    monkeypatch.setattr(job, "cargar_config", lambda: _cfg(habilitado=False))

    async def no_debe_llamarse(*a, **k):
        raise AssertionError("no debe consultar MeLi con el job apagado")

    monkeypatch.setattr(job, "_ids_activos", no_debe_llamarse)
    r = await job.run_job(motivo="scheduler")
    assert r["estado"] == "deshabilitado"
    assert avisos == []


@pytest.mark.asyncio
async def test_manual_corre_aunque_este_apagado(monkeypatch, avisos):
    monkeypatch.setattr(job, "cargar_config", lambda: _cfg(habilitado=False))
    llamadas = []

    async def ids(client, token):
        llamadas.append(1)
        return []

    async def detalles(client, token, ids_):
        return []

    async def token(client):
        return "t"

    from jobs import meli_ventas
    monkeypatch.setattr(meli_ventas, "_meli_token", token)
    monkeypatch.setattr(job, "_ids_activos", ids)
    monkeypatch.setattr(job, "_detalles", detalles)
    monkeypatch.setattr(job, "mapa_db", lambda ids_: {})
    r = await job.run_job(motivo="manual por ana")
    assert llamadas and r["estado"] == "ok" and r["dry_run"] is True  # dry-run viene de la config


@pytest.mark.asyncio
async def test_si_falla_la_config_se_aborta_y_se_avisa(monkeypatch, avisos):
    def falla():
        raise job.MeliStockConfigError("BD caída")

    monkeypatch.setattr(job, "cargar_config", falla)

    async def no_debe_llamarse(*a, **k):
        raise AssertionError("no debe consultar MeLi sin configuración")

    monkeypatch.setattr(job, "_ids_activos", no_debe_llamarse)
    r = await job.run_job(motivo="scheduler")
    assert r["estado"] == "error"
    assert len(avisos) == 1 and "configuración" in avisos[0]


# ---------- Endpoints (BD falsa) ----------

class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._res = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        d = self.db
        s = " ".join(sql.split())
        d.ejecutadas.append((s, params))
        self.rowcount = 0
        if s.startswith("SELECT habilitado, dry_run, hora, version"):
            self._res = [dict(d.cfg)]
        elif s.startswith("SELECT hora FROM meli_stock_config"):
            self._res = [{"hora": d.cfg["hora"]}]
        elif s.startswith("SELECT i.id_meli"):
            self._res = [{"id_meli": i, "nota": n, "creado_por": "x", "creado_en": None, "sku": d.skus.get(i)}
                         for i, n in sorted(d.ignorados.items())]
        elif s.startswith("SELECT m.id_meli"):
            self._res = [{"id_meli": i, "multiplicador": m, "actualizado_por": "x", "actualizado_en": None,
                          "sku": d.skus.get(i)} for i, m in sorted(d.mults.items())]
        elif s.startswith("SELECT sku FROM publicaciones_meli"):
            self._res = [{"sku": d.skus[params[0]]}] if params[0] in d.skus else []
        elif s.startswith("INSERT IGNORE INTO meli_stock_ignorados"):
            if params[0] not in d.ignorados:
                d.ignorados[params[0]] = params[1]
                self.rowcount = 1
        elif s.startswith("DELETE FROM meli_stock_ignorados"):
            self.rowcount = 1 if params[0] in d.ignorados else 0
            d.ignorados.pop(params[0], None)
        elif s.startswith("INSERT INTO meli_stock_multiplicadores"):
            d.mults[params[0]] = params[1]
            self.rowcount = 1
        elif s.startswith("DELETE FROM meli_stock_multiplicadores"):
            self.rowcount = 1 if params[0] in d.mults else 0
            d.mults.pop(params[0], None)
        elif s.startswith("UPDATE meli_stock_config"):
            hab, dry, hora, usuario, version = params
            if version == d.cfg["version"]:
                d.cfg.update(habilitado=hab, dry_run=dry, hora=hora, version=version + 1, actualizado_por=usuario)
                self.rowcount = 1

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return list(self._res)

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        self.ejecutadas = []
        self.cfg = {"habilitado": 1, "dry_run": 0, "hora": "20:00", "version": 1,
                    "actualizado_por": None, "actualizado_en": None}
        self.ignorados = {"MLM1111111": "nota"}
        self.mults = {"MLM2222222": 5}
        self.skus = {"MLM2222222": "ABC"}

    def cursor(self, dictionary=False):
        return FakeCursor(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def db(monkeypatch):
    base = FakeDB()
    monkeypatch.setattr(router_cfg, "get_db_connection", lambda: base)
    bitacora = []
    monkeypatch.setattr(mov_reg, "registrar_movimiento", lambda *a, **k: bitacora.append(a))
    base.bitacora = bitacora
    return base


def _cliente(usuario="ana", scheduler=None):
    app = FastAPI()
    app.include_router(router_cfg.router)
    app.dependency_overrides[usuario_autenticado] = lambda: usuario
    if scheduler is not None:
        app.state.amazon_scheduler = scheduler
    return TestClient(app)


def test_get_config_la_ve_cualquier_sesion(db):
    r = _cliente("cualquiera").get("/meli-stock/config")
    assert r.status_code == 200
    body = r.json()
    assert body["habilitado"] is True and body["hora"] == "20:00" and body["version"] == 1
    assert [i["id_meli"] for i in body["ignorados"]] == ["MLM1111111"]
    assert body["multiplicadores"][0] == {"id_meli": "MLM2222222", "multiplicador": 5, "sku": "ABC",
                                          "actualizado_por": "x", "actualizado_en": None}
    assert body["en_ambas"] == []


def test_get_config_avisa_los_ids_en_ambas_listas(db):
    db.mults["MLM1111111"] = 2
    assert _cliente().get("/meli-stock/config").json()["en_ambas"] == ["MLM1111111"]


def test_agregar_ignorado_normaliza_y_registra(db):
    r = _cliente("ana").post("/meli-stock/ignorados", json={"id_meli": " mlm9999999 ", "nota": "  pausada "})
    assert r.status_code == 201
    assert "MLM9999999" in db.ignorados and db.ignorados["MLM9999999"] == "pausada"
    assert r.json()["advertencia"]  # sin SKU asignado: se permite con aviso
    assert db.bitacora[-1][0] == "ana" and "MLM9999999" in db.bitacora[-1][1]


def test_agregar_ignorado_duplicado_da_409_y_invalido_422(db):
    c = _cliente()
    assert c.post("/meli-stock/ignorados", json={"id_meli": "MLM1111111"}).status_code == 409
    assert c.post("/meli-stock/ignorados", json={"id_meli": "hola"}).status_code == 422


def test_quitar_ignorado(db):
    c = _cliente()
    assert c.delete("/meli-stock/ignorados/MLM1111111").status_code == 200
    assert "MLM1111111" not in db.ignorados
    assert c.delete("/meli-stock/ignorados/MLM1111111").status_code == 404


def test_multiplicador_alta_edicion_y_uno_equivale_a_quitar(db):
    c = _cliente()
    r = c.put("/meli-stock/multiplicadores/mlm3333333", json={"multiplicador": 10})
    assert r.status_code == 200 and db.mults["MLM3333333"] == 10
    c.put("/meli-stock/multiplicadores/MLM3333333", json={"multiplicador": "2"})
    assert db.mults["MLM3333333"] == 2
    c.put("/meli-stock/multiplicadores/MLM3333333", json={"multiplicador": 1})
    assert "MLM3333333" not in db.mults


@pytest.mark.parametrize("valor", [0, -3, 1001, "x", 2.5])
def test_multiplicador_invalido_da_422(db, valor):
    r = _cliente().put("/meli-stock/multiplicadores/MLM3333333", json={"multiplicador": valor})
    assert r.status_code == 422 and "MLM3333333" not in db.mults


def test_quitar_multiplicador(db):
    c = _cliente()
    assert c.delete("/meli-stock/multiplicadores/MLM2222222").status_code == 200
    assert c.delete("/meli-stock/multiplicadores/MLM2222222").status_code == 404


def test_guardar_config_apaga_el_job_y_sube_la_version(db):
    r = _cliente("ana").put("/meli-stock/config", json={"habilitado": False, "dry_run": True, "hora": "21:30", "version": 1})
    assert r.status_code == 200
    assert db.cfg["habilitado"] == 0 and db.cfg["dry_run"] == 1 and db.cfg["hora"] == "21:30" and db.cfg["version"] == 2
    assert r.json()["version"] == 2 and r.json()["habilitado"] is False
    assert db.bitacora[-1][0] == "ana"


def test_guardar_config_con_version_vieja_da_409(db):
    r = _cliente().put("/meli-stock/config", json={"habilitado": False, "dry_run": False, "hora": "20:00", "version": 0})
    assert r.status_code == 409 and db.cfg["habilitado"] == 1


def test_guardar_config_hora_invalida_da_422(db):
    r = _cliente().put("/meli-stock/config", json={"habilitado": True, "dry_run": False, "hora": "25:00", "version": 1})
    assert r.status_code == 422


def test_cambiar_la_hora_reprograma_el_scheduler(db, monkeypatch):
    llamadas = []

    class FakeSched:
        def reschedule_job(self, job_id, trigger=None):
            llamadas.append(job_id)

        def get_job(self, job_id):
            return None

    monkeypatch.setattr(job, "crear_trigger", lambda hora: ("trigger", hora))
    _cliente(scheduler=FakeSched()).put(
        "/meli-stock/config", json={"habilitado": True, "dry_run": False, "hora": "22:15", "version": 1})
    assert llamadas == ["meli_stock"]
