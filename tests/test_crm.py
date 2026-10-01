# Tests del CRM (routers/crm.py + crm_metricas.py): validaciones de Pydantic,
# permisos por rol (vendedor vs gerencia), reglas de cartera/seguimientos/etapas
# y agregaciones de reportes. MySQL y bitácora van simulados; la app de prueba
# monta solo crm.router (el usuario sale del token vía permisos.usuario_autenticado).
from datetime import date, datetime

import mysql.connector
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import crm_metricas
import permisos
from routers import crm

AHORA = datetime(2026, 10, 1, 12, 0, 0)
HOY = AHORA.date()


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._resultado = []
        self.rowcount = 0
        self.lastrowid = None

    def execute(self, query, params=None):
        q = " ".join(query.split())
        db = self.db
        db.ejecutados.append((q, params))
        if db.error_en and db.error_en in q:
            raise db.error
        self._resultado, self.rowcount = [], 0

        if q.startswith("SELECT nombre_usuario FROM usuarios WHERE token"):
            u = db.tokens.get(params[0])
            self._resultado = [{"nombre_usuario": u}] if u else []
        elif q.startswith("SELECT nombre_usuario FROM usuarios WHERE nombre_usuario"):
            self._resultado = [{"nombre_usuario": params[0]}] if params[0] in db.usuarios else []
        elif q.startswith("SELECT c.id, c.nombre, c.empresa, c.telefono, c.email, c.usuario AS registrado_por"):
            c = db.clientes.get(params[0])
            if c:
                cc = db.cartera.get(c["id"])
                self._resultado = [{
                    "id": c["id"], "nombre": c["nombre"], "empresa": c.get("empresa"), "telefono": None,
                    "email": None, "registrado_por": c.get("usuario"),
                    "en_cartera": c["id"] if cc else None,
                    "vendedor": cc["vendedor"] if cc else None,
                    "etapa": cc["etapa"] if cc else None,
                    "motivo_perdida": cc.get("motivo_perdida") if cc else None,
                    "etapa_actualizada": None,
                }]
        elif q.startswith("INSERT INTO crm_cartera"):
            db.cartera[params[0]] = {"vendedor": params[1], "etapa": "contacto_inicial"}
            self.rowcount = 1
        elif q.startswith("UPDATE crm_cartera SET etapa"):
            db.cartera[params[3]].update({"etapa": params[0], "motivo_perdida": params[1]})
            self.rowcount = 1
        elif q.startswith("UPDATE crm_cartera SET vendedor"):
            db.cartera[params[1]]["vendedor"] = params[0]
            self.rowcount = 1
        elif q.startswith("INSERT INTO crm_etapas_historial"):
            db.historial.append(params)
            self.rowcount = 1
        elif q.startswith("UPDATE crm_interacciones SET seguimiento_cerrado = 1 WHERE cliente_id"):
            self.rowcount = db.abiertos.get(params[0], 0)
        elif q.startswith("INSERT INTO crm_interacciones"):
            self.lastrowid = 501
            self.rowcount = 1
        elif q.startswith("SELECT id, vendedor FROM crm_interacciones WHERE id"):
            i = db.interacciones.get(params[0])
            self._resultado = [{"id": params[0], "vendedor": i["vendedor"]}] if i else []
        elif q.startswith("UPDATE crm_interacciones SET eliminado = 1"):
            self.rowcount = 1 if params[0] in db.interacciones else 0
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
        # 1: sin dueño · 2: registrado por luis (sin CRM) · 3: cartera de ana · 4: cartera de luis
        self.clientes = {
            1: {"id": 1, "nombre": "Sin Dueño SA", "usuario": ""},
            2: {"id": 2, "nombre": "Alta de Luis", "usuario": "luis"},
            3: {"id": 3, "nombre": "Cliente Ana", "usuario": "gerencia"},
            4: {"id": 4, "nombre": "Cliente Luis", "usuario": "ana"},
        }
        self.cartera = {
            3: {"vendedor": "ana", "etapa": "en_seguimiento"},
            4: {"vendedor": "luis", "etapa": "cotizado"},
        }
        self.interacciones = {10: {"vendedor": "ana"}, 11: {"vendedor": "luis"}}
        self.abiertos = {3: 2}  # seguimientos abiertos por cliente
        self.historial = []
        self.respuestas = []    # (fragmento de SQL, filas) para consultas de lectura
        self.ejecutados = []
        self.commits = 0
        self.rollbacks = 0
        self.error_en = None
        self.error = None

    def cursor(self, dictionary=False):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass

    def consultas(self, inicio):
        return [(q, p) for q, p in self.ejecutados if q.startswith(inicio)]


@pytest.fixture
def db(monkeypatch):
    base = FakeDB()
    monkeypatch.setattr(crm, "get_db_connection", lambda: base)
    monkeypatch.setattr(permisos, "get_db_connection", lambda: base)
    monkeypatch.setattr(crm, "ahora_mx", lambda: AHORA)
    monkeypatch.setattr(crm, "hoy_mx", lambda: HOY)
    monkeypatch.setenv("USUARIOS_GERENCIA", "gerencia")
    movimientos = []
    monkeypatch.setattr(crm.mov_reg, "registrar_movimiento", lambda *a, **k: movimientos.append(a))
    base.movimientos = movimientos
    return base


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(crm.router)
    return TestClient(app)


def auth(token):
    return {"Authorization": f"Bearer {token}"}


ANA, LUIS, GER = auth("tok-ana"), auth("tok-luis"), auth("tok-ger")


# ---------- Validaciones ----------

def test_registro_minimo_solo_cliente_y_tipo(client, db):
    r = client.post("/crm/interacciones", json={"cliente_id": 3, "tipo": "llamada"}, headers=ANA)
    assert r.status_code == 200
    assert r.json()["id"] == 501
    (_, p), = db.consultas("INSERT INTO crm_interacciones")
    # cliente, vendedor (del token), tipo, fecha=ahora, resto vacío
    assert p == (3, "ana", "llamada", AHORA, None, None, None, None)
    assert db.commits == 1


@pytest.mark.parametrize("payload", [
    {"cliente_id": 3},                                   # falta tipo
    {"cliente_id": 3, "tipo": "fax"},                    # tipo inválido
    {"tipo": "llamada"},                                 # falta cliente
    {"cliente_id": 0, "tipo": "llamada"},                # id no positivo
    {"cliente_id": 3, "tipo": "correo", "resultado": "quizas"},
    {"cliente_id": 3, "tipo": "correo", "notas": "x" * 2001},
    {"cliente_id": 3, "tipo": "correo", "proxima_fecha": "2026-09-30"},       # compromiso en el pasado
    {"cliente_id": 3, "tipo": "correo", "fecha": "2026-10-01T13:00:00"},      # interacción en el futuro
    {"cliente_id": 3, "tipo": "correo", "etapa": "negociando"},
])
def test_payload_invalido_da_422_y_no_escribe(client, db, payload):
    r = client.post("/crm/interacciones", json=payload, headers=ANA)
    assert r.status_code == 422
    assert db.consultas("INSERT") == [] and db.commits == 0


def test_proxima_fecha_hoy_es_valida_y_textos_se_recortan(client, db):
    r = client.post("/crm/interacciones", json={
        "cliente_id": 3, "tipo": "whatsapp", "notas": "  pidió precios  ", "proxima_accion": "   ",
        "proxima_fecha": HOY.isoformat(), "fecha": "2026-10-01T09:30:00",
    }, headers=ANA)
    assert r.status_code == 200
    (_, p), = db.consultas("INSERT INTO crm_interacciones")
    assert p[3] == datetime(2026, 10, 1, 9, 30) and p[5] == "pidió precios" and p[6] is None and p[7] == HOY


def test_cliente_inexistente_da_404(client, db):
    r = client.post("/crm/interacciones", json={"cliente_id": 999, "tipo": "llamada"}, headers=ANA)
    assert r.status_code == 404


@pytest.mark.parametrize("ruta", ["/crm/metricas/resumen", "/crm/metricas/embudo", "/crm/interacciones"])
def test_rango_de_fechas_invertido_o_enorme_da_422(client, db, ruta):
    assert client.get(ruta, params={"desde": "2026-10-02", "hasta": "2026-10-01"}, headers=GER).status_code == 422
    assert client.get(ruta, params={"desde": "2025-01-01", "hasta": "2026-10-01"}, headers=GER).status_code == 422


def test_editar_sin_campos_da_422(client, db):
    assert client.patch("/crm/interacciones/10", json={}, headers=ANA).status_code == 422


def test_cambio_de_etapa_invalida_da_422(client, db):
    assert client.patch("/crm/clientes/3/etapa", json={"etapa": "ganadisimo"}, headers=ANA).status_code == 422


# ---------- Autenticación y permisos ----------

def test_sin_token_o_token_invalido_rechaza(client, db):
    assert client.get("/crm/seguimientos").status_code in (401, 403)
    assert client.get("/crm/seguimientos", headers=auth("nope")).status_code == 401


@pytest.mark.parametrize("cliente_id", [4, 2])  # 4: cartera de luis · 2: lo dio de alta luis
def test_vendedor_no_gestiona_cliente_de_otro(client, db, cliente_id):
    r = client.post("/crm/interacciones", json={"cliente_id": cliente_id, "tipo": "llamada"}, headers=ANA)
    assert r.status_code == 403
    assert client.patch(f"/crm/clientes/{cliente_id}/etapa", json={"etapa": "ganado"}, headers=ANA).status_code == 403
    assert client.get(f"/crm/clientes/{cliente_id}", headers=ANA).status_code == 403
    assert db.consultas("INSERT") == [] and db.consultas("UPDATE") == []


def test_dueno_provisional_es_quien_dio_de_alta(client, db):
    r = client.post("/crm/interacciones", json={"cliente_id": 2, "tipo": "correo"}, headers=LUIS)
    assert r.status_code == 200
    assert db.cartera[2] == {"vendedor": "luis", "etapa": "contacto_inicial"}


def test_cliente_sin_dueno_lo_toma_quien_registra(client, db):
    r = client.post("/crm/interacciones", json={"cliente_id": 1, "tipo": "llamada"}, headers=ANA)
    assert r.status_code == 200
    assert db.cartera[1]["vendedor"] == "ana"


def test_gerencia_registra_en_cualquier_cliente_sin_quitar_al_dueno(client, db):
    r = client.post("/crm/interacciones", json={"cliente_id": 2, "tipo": "reunion"}, headers=GER)
    assert r.status_code == 200
    assert db.cartera[2]["vendedor"] == "luis"
    assert db.consultas("INSERT INTO crm_interacciones")[0][1][1] == "gerencia"


def test_bitacora_de_vendedor_se_limita_a_lo_suyo_aunque_pida_otro(client, db):
    r = client.get("/crm/interacciones", params={"vendedor": "luis"}, headers=ANA)
    assert r.status_code == 200
    q, p = db.consultas("SELECT COUNT(*)")[0]
    assert "i.vendedor = %s" in q and "i.vendedor IN" not in q
    assert "ana" in p and "luis" not in p


def test_bitacora_de_gerencia_filtra_por_varios_vendedores_y_tipos(client, db):
    db.respuestas.append(("SELECT COUNT(*)", [{"total": 2}]))
    r = client.get("/crm/interacciones", params=[("vendedor", "ana"), ("vendedor", "luis"), ("tipo", "llamada")], headers=GER)
    assert r.status_code == 200 and r.json()["total"] == 2
    q, p = db.consultas("SELECT COUNT(*)")[0]
    assert "i.vendedor IN (%s, %s)" in q and "i.tipo IN (%s)" in q
    assert p[2:] == ("ana", "luis", "llamada")


def test_bitacora_por_cliente_ajeno_da_403(client, db):
    assert client.get("/crm/interacciones", params={"cliente_id": 4}, headers=ANA).status_code == 403


def test_buscador_de_vendedor_incluye_sus_clientes_y_los_sin_dueno(client, db):
    client.get("/crm/clientes", params={"q": "sa"}, headers=ANA)
    q, p = db.ejecutados[-1]
    assert "IS NULL" in q and p[-2] == "ana" and p[-1] == 20
    client.get("/crm/clientes", params={"solo_mios": True}, headers=ANA)
    q, p = db.ejecutados[-1]
    assert "IS NULL" not in q and p == ("ana", 20)


def test_buscador_de_gerencia_ve_todo_o_filtra_por_vendedor(client, db):
    client.get("/crm/clientes", headers=GER)
    q, p = db.ejecutados[-1]
    assert p == (20,)
    client.get("/crm/clientes", params={"vendedor": "luis", "solo_mios": True}, headers=GER)
    assert db.ejecutados[-1][1] == ("luis", 20)


def test_seguimientos_de_vendedor_son_los_suyos_y_gerencia_ve_todos(client, db):
    assert client.get("/crm/seguimientos", params={"vendedor": "luis"}, headers=ANA).status_code == 200
    q, p = db.ejecutados[-1]
    assert p == (date(2026, 10, 8), "ana")
    assert client.get("/crm/seguimientos", headers=GER).status_code == 200
    assert db.ejecutados[-1][1] == (date(2026, 10, 8),)
    client.get("/crm/seguimientos", params={"vendedor": "luis"}, headers=GER)
    assert db.ejecutados[-1][1] == (date(2026, 10, 8), "luis")


@pytest.mark.parametrize("ruta", ["/crm/metricas/resumen", "/crm/metricas/embudo", "/crm/vendedores"])
def test_metricas_solo_gerencia(client, db, ruta):
    assert client.get(ruta, headers=ANA).status_code == 403
    assert client.get(ruta, headers=GER).status_code == 200


def test_eliminar_interaccion_solo_gerencia(client, db):
    assert client.delete("/crm/interacciones/10", headers=ANA).status_code == 403  # aunque sea la autora
    assert db.consultas("UPDATE") == []
    r = client.delete("/crm/interacciones/10", headers=GER)
    assert r.status_code == 200 and r.json()["eliminado"] is True
    assert client.delete("/crm/interacciones/999", headers=GER).status_code == 404


def test_editar_interaccion_autor_o_gerencia(client, db):
    assert client.patch("/crm/interacciones/11", json={"notas": "x"}, headers=ANA).status_code == 403
    r = client.patch("/crm/interacciones/10", json={"seguimiento_cerrado": True, "notas": " ok "}, headers=ANA)
    assert r.status_code == 200
    q, p = db.consultas("UPDATE crm_interacciones SET")[0]
    assert q == "UPDATE crm_interacciones SET notas = %s, seguimiento_cerrado = %s WHERE id = %s"
    assert p == ("ok", 1, 10)
    assert client.patch("/crm/interacciones/11", json={"notas": "y"}, headers=GER).status_code == 200
    assert client.patch("/crm/interacciones/999", json={"notas": "y"}, headers=GER).status_code == 404


def test_asignar_vendedor_solo_gerencia_y_usuario_existente(client, db):
    assert client.put("/crm/clientes/4/vendedor", json={"vendedor": "ana"}, headers=ANA).status_code == 403
    assert client.put("/crm/clientes/4/vendedor", json={"vendedor": "fantasma"}, headers=GER).status_code == 422
    assert client.put("/crm/clientes/999/vendedor", json={"vendedor": "ana"}, headers=GER).status_code == 404
    r = client.put("/crm/clientes/4/vendedor", json={"vendedor": "ana"}, headers=GER)
    assert r.status_code == 200 and r.json()["vendedor_anterior"] == "luis"
    assert db.cartera[4]["vendedor"] == "ana"
    # cliente que aún no estaba en el CRM: entra con la etapa inicial
    assert client.put("/crm/clientes/1/vendedor", json={"vendedor": "luis"}, headers=GER).status_code == 200
    assert db.cartera[1] == {"vendedor": "luis", "etapa": "contacto_inicial"}


# ---------- Reglas de negocio ----------

def test_primera_interaccion_mete_al_cliente_al_crm_con_historial(client, db):
    r = client.post("/crm/interacciones", json={"cliente_id": 1, "tipo": "llamada"}, headers=ANA)
    assert r.json()["etapa"] == "contacto_inicial"
    assert db.historial == [(1, "ana")]  # (cliente, usuario); etapa NULL → contacto_inicial va en el SQL


def test_nueva_interaccion_cierra_seguimientos_abiertos_antes_de_insertar(client, db):
    r = client.post("/crm/interacciones", json={"cliente_id": 3, "tipo": "llamada"}, headers=ANA)
    assert r.json()["seguimientos_cerrados"] == 2
    orden = [q.split(" WHERE")[0] for q, _ in db.ejecutados if q.startswith(("UPDATE crm_interacciones", "INSERT INTO crm_interacciones"))]
    assert orden == ["UPDATE crm_interacciones SET seguimiento_cerrado = 1", "INSERT INTO crm_interacciones (cliente_id, vendedor, tipo, fecha, resultado, notas, proxima_accion, proxima_fecha) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"]


def test_cambio_de_etapa_en_la_misma_captura(client, db):
    r = client.post("/crm/interacciones", json={"cliente_id": 3, "tipo": "correo", "etapa": "cotizado"}, headers=ANA)
    assert r.json()["etapa"] == "cotizado"
    assert db.cartera[3]["etapa"] == "cotizado"
    assert db.historial == [(3, "en_seguimiento", "cotizado", "ana")]
    assert db.commits == 1


def test_perdido_guarda_motivo_y_otras_etapas_lo_limpian(client, db):
    r = client.patch("/crm/clientes/3/etapa", json={"etapa": "perdido", "motivo_perdida": " precio "}, headers=ANA)
    assert r.status_code == 200 and r.json()["cambio"] is True
    assert db.cartera[3]["motivo_perdida"] == "precio"
    client.patch("/crm/clientes/3/etapa", json={"etapa": "en_seguimiento", "motivo_perdida": "ignorado"}, headers=ANA)
    assert db.cartera[3]["motivo_perdida"] is None
    assert [h[2] for h in db.historial] == ["perdido", "en_seguimiento"]


def test_misma_etapa_no_genera_historial(client, db):
    r = client.patch("/crm/clientes/3/etapa", json={"etapa": "en_seguimiento"}, headers=ANA)
    assert r.json()["cambio"] is False
    assert db.historial == [] and db.movimientos == []


def test_error_de_mysql_da_500_con_rollback(client, db):
    db.error_en = "INSERT INTO crm_interacciones"
    db.error = mysql.connector.Error("boom")
    r = client.post("/crm/interacciones", json={"cliente_id": 3, "tipo": "llamada"}, headers=ANA)
    assert r.status_code == 500
    assert db.rollbacks == 1 and db.commits == 0


def test_resumen_endpoint_arma_reporte_con_filas_de_mysql(client, db):
    db.respuestas += [
        ("GROUP BY i.vendedor, i.tipo, DATE(i.fecha)", [
            {"vendedor": "ana", "tipo": "llamada", "dia": date(2026, 9, 30), "total": 3},
            {"vendedor": "luis", "tipo": "whatsapp", "dia": date(2026, 10, 1), "total": 2},
        ]),
        ("COUNT(DISTINCT i.cliente_id)", [{"vendedor": "ana", "clientes": 2}]),
        ("AS vencidos", [{"vendedor": "luis", "vencidos": 4}]),
        ("FROM crm_etapas_historial", [{"vendedor": "ana", "etapa": "ganado", "total": 1}]),
    ]
    r = client.get("/crm/metricas/resumen", params={"desde": "2026-09-29", "hasta": "2026-10-01"}, headers=GER)
    assert r.status_code == 200
    body = r.json()
    assert body["totales"] == {"interacciones": 5, "por_tipo": {"llamada": 3, "correo": 0, "whatsapp": 2, "reunion": 0}, "vencidos": 4, "ganados": 1}
    assert [d["total"] for d in body["serie"]] == [0, 3, 2]
    assert body["por_vendedor"][0]["vendedor"] == "ana"


# ---------- Lógica de reportes (pura) ----------

def test_agrupar_seguimientos_por_vencimiento():
    filas = [
        {"id": 1, "proxima_fecha": date(2026, 9, 28)},
        {"id": 2, "proxima_fecha": date(2026, 9, 30)},
        {"id": 3, "proxima_fecha": HOY},
        {"id": 4, "proxima_fecha": "2026-10-05"},
        {"id": 5, "proxima_fecha": datetime(2026, 10, 3, 0, 0)},
        {"id": 6, "proxima_fecha": None},
    ]
    g = crm_metricas.agrupar_seguimientos(filas, HOY)
    assert [x["id"] for x in g["vencidos"]] == [1, 2]
    assert [x["dias_atraso"] for x in g["vencidos"]] == [3, 1]
    assert [x["id"] for x in g["hoy"]] == [3]
    assert [x["id"] for x in g["proximos"]] == [5, 4]
    assert g["totales"] == {"vencidos": 2, "hoy": 1, "proximos": 2}


def test_armar_resumen_suma_por_tipo_vendedor_y_rellena_dias():
    r = crm_metricas.armar_resumen(
        conteos=[
            {"vendedor": "ana", "tipo": "llamada", "dia": date(2026, 9, 1), "total": 2},
            {"vendedor": "ana", "tipo": "correo", "dia": date(2026, 9, 3), "total": 1},
            {"vendedor": "luis", "tipo": "llamada", "dia": date(2026, 9, 3), "total": 4},
            {"vendedor": "luis", "tipo": "desconocido", "dia": date(2026, 9, 3), "total": 9},
        ],
        clientes=[{"vendedor": "ana", "clientes": 2}, {"vendedor": "luis", "clientes": 1}],
        vencidos=[{"vendedor": "pepe", "vencidos": 3}],
        movimientos=[{"vendedor": "luis", "etapa": "ganado", "total": 2}, {"vendedor": "ana", "etapa": "perdido", "total": 1}],
        desde=date(2026, 9, 1), hasta=date(2026, 9, 4),
    )
    assert r["totales"]["por_tipo"] == {"llamada": 6, "correo": 1, "whatsapp": 0, "reunion": 0}
    assert r["totales"]["interacciones"] == 7 and r["totales"]["vencidos"] == 3 and r["totales"]["ganados"] == 2
    assert [d["dia"] for d in r["serie"]] == ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
    assert [d["total"] for d in r["serie"]] == [2, 0, 14, 0]
    por = {v["vendedor"]: v for v in r["por_vendedor"]}
    assert por["luis"]["total"] == 13 and por["luis"]["llamada"] == 4 and por["luis"]["ganado_periodo"] == 2
    assert por["ana"]["clientes"] == 2 and por["ana"]["perdido_periodo"] == 1
    assert por["pepe"]["vencidos"] == 3 and por["pepe"]["total"] == 0  # vendedor solo con vencidos
    assert r["por_vendedor"][0]["vendedor"] == "luis"  # orden por actividad


def test_armar_resumen_vacio():
    r = crm_metricas.armar_resumen([], [], [], [], date(2026, 10, 1), date(2026, 10, 1))
    assert r["totales"]["interacciones"] == 0 and r["por_vendedor"] == []
    assert r["serie"] == [{"dia": "2026-10-01", "total": 0}]


def test_armar_embudo_ordena_etapas_rellena_ceros_y_calcula_cierre():
    e = crm_metricas.armar_embudo(
        actual=[
            {"vendedor": "ana", "etapa": "contacto_inicial", "total": 5},
            {"vendedor": "ana", "etapa": "ganado", "total": 2},
            {"vendedor": "luis", "etapa": "cotizado", "total": 3},
            {"vendedor": "luis", "etapa": "rara", "total": 7},
        ],
        movimientos=[{"etapa": "ganado", "total": 3}, {"etapa": "perdido", "total": 1}],
    )
    assert [x["etapa"] for x in e["etapas"]] == list(crm_metricas.ETAPAS)
    assert [x["actual"] for x in e["etapas"]] == [5, 0, 3, 2, 0]
    assert e["etapas"][3]["entradas_periodo"] == 3 and e["etapas"][3]["label"] == "Cerrado / Ganado"
    assert e["total"] == 10
    assert e["tasa_cierre"] == 0.75
    assert e["por_vendedor"][0] == {"vendedor": "ana", "total": 7, "contacto_inicial": 5, "en_seguimiento": 0, "cotizado": 0, "ganado": 2, "perdido": 0}


def test_armar_embudo_sin_cierres_no_inventa_tasa():
    e = crm_metricas.armar_embudo([], [])
    assert e["tasa_cierre"] is None and e["total"] == 0
    assert all(x["actual"] == 0 for x in e["etapas"])


# ---------- Permisos (puros) ----------

def test_reglas_de_dueno(monkeypatch):
    monkeypatch.setenv("USUARIOS_GERENCIA", "gerencia")
    assert crm.vendedor_efectivo({"vendedor": "ana", "registrado_por": "luis"}) == "ana"
    assert crm.vendedor_efectivo({"vendedor": None, "registrado_por": " luis "}) == "luis"
    assert crm.vendedor_efectivo({"vendedor": None, "registrado_por": ""}) is None
    assert crm.puede_gestionar("ana", "Ana") and crm.puede_gestionar("ana", None)
    assert not crm.puede_gestionar("ana", "luis")
    assert crm.puede_gestionar("gerencia", "luis")
