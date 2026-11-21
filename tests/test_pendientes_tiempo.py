# Cronómetro de tareas de pendientes (fparra) y «Intercambiado» de gerencia.
# Lógica pura (pendientes_tiempo_logica) y rutas de routers/pendientes.py con BD falsa
# (el SQL real no se ha probado contra un MySQL).
from datetime import date, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import pendientes_tiempo_logica as t
from permisos import requerir_gerencia, usuario_autenticado
from routers import pendientes

T0 = datetime(2026, 11, 20, 18, 0, 0)
T1 = datetime(2026, 11, 21, 9, 30, 0)
AHORA = datetime(2026, 11, 21, 12, 0, 0)


# ---------- lógica pura ----------

def test_se_cronometra_solo_fparra():
    assert t.se_cronometra("fparra") and t.se_cronometra(" FParra ")
    assert not t.se_cronometra("ana") and not t.se_cronometra(None)


def test_duracion_corrida_cruza_noches_y_abierta_cuenta_hasta_ahora():
    assert t.duracion_segundos(T0, T1) == 15 * 3600 + 30 * 60
    assert t.duracion_segundos(T0, None, AHORA) == 18 * 3600


def test_duracion_dato_faltante_o_incoherente_es_none_no_cero():
    assert t.duracion_segundos(None, T1) is None
    assert t.duracion_segundos(T0, None, None) is None
    assert t.duracion_segundos(T1, T0) is None


def _fila(id, inicio, fin, intercambiado=0):
    return {"id": id, "usuario": "fparra", "actividad": f"t{id}", "prioridad": "alta", "estado": "terminado",
            "inicio_atencion": inicio, "fin_atencion": fin, "intercambiado": intercambiado,
            "intercambiado_por": None, "fecha_intercambiado": None}


def test_kpi_suma_solo_terminadas_sin_intercambiar():
    filas = [
        t.tarea_tiempo_json(_fila(1, T0, T1), AHORA),                 # 15.5 h, cuenta
        t.tarea_tiempo_json(_fila(2, T0, T1, 1), AHORA),              # intercambiada: no
        t.tarea_tiempo_json(_fila(3, T0, None), AHORA),               # en curso: aparte
        t.tarea_tiempo_json(_fila(4, T1, T0), AHORA),                 # fin < inicio: sin dato
    ]
    k = t.resumen_kpi(filas)
    assert k == {"segundos_sin_intercambiar": 15 * 3600 + 1800, "tareas_sin_intercambiar": 2,
                 "tareas_sin_dato": 1, "tareas_en_curso": 1}


# ---------- rutas ----------

class Cur:
    def __init__(self, db):
        self.db, self._res, self.lastrowid = db, [], None

    def execute(self, query, params=None):
        q = " ".join(query.split())
        self.db.ejecutados.append((q, params))
        self._res = []
        if "AS visible FROM pendientes WHERE id = %s FOR UPDATE" in q:
            f = next((f for f in self.db.filas if f["id"] == params[0]), None)
            self._res = [dict(f, visible=True)] if f else []
        elif q.startswith("SELECT COUNT(*) AS n FROM pendientes WHERE estado = 'pendiente'"):
            self._res = [{"n": sum(1 for f in self.db.filas if f["estado"] == "pendiente")}]
        elif q.startswith("SELECT COUNT(*) AS n FROM pendientes WHERE LOWER(usuario)"):
            n = sum(1 for f in self.db.filas if f["usuario"].lower() == params[0].lower()
                    and f["estado"].lower() == "en proceso" and f["id"] != params[1])
            self._res = [{"n": n}]
        elif q.startswith("SELECT id, usuario, actividad, prioridad, estado, inicio_atencion"):
            self._res = [dict(f) for f in self.db.filas if f["usuario"].lower() in params and f.get("inicio_atencion")]
        elif q.startswith("SELECT id, usuario, actividad, inicio_atencion, fin_atencion, intercambiado"):
            f = next((f for f in self.db.filas if f["id"] == params[0]), None)
            self._res = [dict(f)] if f else []
        elif q.startswith("UPDATE pendientes SET estado = 'en proceso'"):
            next(f for f in self.db.filas if f["id"] == params[0])["estado"] = "en proceso"
        elif q.startswith("UPDATE pendientes SET inicio_atencion"):
            f = next(f for f in self.db.filas if f["id"] == params[1])
            f["inicio_atencion"], f["fin_atencion"] = params[0], None
        elif q.startswith("UPDATE pendientes SET estado = 'terminado'"):
            next(f for f in self.db.filas if f["id"] == params[0])["estado"] = "terminado"
        elif q.startswith("UPDATE pendientes SET fin_atencion"):
            f = next(f for f in self.db.filas if f["id"] == params[1])
            if f.get("fin_atencion") is None:
                f["fin_atencion"] = params[0]
        elif q.startswith("UPDATE pendientes SET intercambiado"):
            f = next(f for f in self.db.filas if f["id"] == params[3])
            f["intercambiado"], f["intercambiado_por"] = params[0], params[1]
        else:
            raise AssertionError(f"consulta no esperada: {q}")

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return list(self._res)

    def close(self):
        pass


class DB:
    def __init__(self, filas):
        self.filas, self.ejecutados, self.movimientos = filas, [], []

    def cursor(self, dictionary=False):
        return Cur(self)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def fila(id, usuario="fparra", estado="pendiente", inicio=None, fin=None, intercambiado=0):
    return {"id": id, "fecha": date(2026, 11, 1), "usuario": usuario, "actividad": f"tarea {id}", "prioridad": "media",
            "estado": estado, "observaciones": None, "fecha_promesa": None, "recurrencia": None,
            "fecha_visible": None, "plantilla_id": None, "inicio_atencion": inicio, "fin_atencion": fin,
            "intercambiado": intercambiado, "intercambiado_por": None, "fecha_intercambiado": None}


def montar(monkeypatch, filas, usuario):
    db = DB(filas)
    monkeypatch.setattr(pendientes, "get_db_connection", lambda: db)
    monkeypatch.setattr(pendientes.mov_reg, "registrar_movimiento", lambda *a, **k: db.movimientos.append(a))
    monkeypatch.setattr(t, "ahora_mx", lambda: AHORA)
    app = FastAPI()
    app.include_router(pendientes.router)
    app.dependency_overrides[usuario_autenticado] = lambda: usuario
    from permisos import es_gerencia
    if es_gerencia(usuario):
        app.dependency_overrides[requerir_gerencia] = lambda: usuario
    return db, TestClient(app)


def test_atender_inicia_el_cronometro_de_fparra(monkeypatch):
    db, c = montar(monkeypatch, [fila(1)], "fparra")
    r = c.post("/pendientes/1/atender")
    assert r.status_code == 200
    assert db.filas[0]["inicio_atencion"] == AHORA
    assert r.json()["atendido"]["segundosTranscurridos"] == 0


def test_atender_otro_usuario_no_cronometra(monkeypatch):
    db, c = montar(monkeypatch, [fila(1, usuario="ana")], "ana")
    r = c.post("/pendientes/1/atender")
    assert r.status_code == 200
    assert db.filas[0]["inicio_atencion"] is None
    assert "segundosTranscurridos" not in r.json()["atendido"]


def test_fparra_solo_una_tarea_abierta(monkeypatch):
    db, c = montar(monkeypatch, [fila(1, estado="en proceso", inicio=T0), fila(2)], "fparra")
    r = c.post("/pendientes/2/atender")
    assert r.status_code == 409
    assert db.filas[1]["estado"] == "pendiente" and db.filas[1]["inicio_atencion"] is None


def test_terminar_sella_el_fin_una_sola_vez(monkeypatch):
    db, c = montar(monkeypatch, [fila(1, estado="en proceso", inicio=T0)], "fparra")
    assert c.post("/pendientes/1/terminar").status_code == 200
    assert db.filas[0]["fin_atencion"] == AHORA
    db.filas[0]["fin_atencion"] = T1                       # una repetición no lo pisa
    assert c.post("/pendientes/1/terminar").json()["terminado"] is True
    assert db.filas[0]["fin_atencion"] == T1


def test_tiempos_es_solo_gerencia(monkeypatch):
    _, c = montar(monkeypatch, [fila(1, estado="terminado", inicio=T0, fin=T1)], "ana")
    # sin override de requerir_gerencia, la dependencia real rechaza a un usuario que no es gerencia
    r = c.get("/pendientes/tiempos")
    assert r.status_code in (401, 403)


def test_tiempos_kpi_y_filtro_de_intercambiadas(monkeypatch):
    filas = [fila(1, estado="terminado", inicio=T0, fin=T1), fila(2, estado="terminado", inicio=T0, fin=T1, intercambiado=1),
             fila(3, estado="en proceso", inicio=T0), fila(4, usuario="ana", estado="terminado", inicio=T0, fin=T1)]
    _, c = montar(monkeypatch, filas, "gerencia")
    r = c.get("/pendientes/tiempos").json()
    assert [x["id"] for x in r["tareas"]] == [1, 3]                       # sin la intercambiada ni la de ana
    assert r["kpi"]["segundos_sin_intercambiar"] == 15 * 3600 + 1800
    assert r["kpi"]["tareas_en_curso"] == 1
    todas = c.get("/pendientes/tiempos?incluir_intercambiadas=true").json()
    assert sorted(x["id"] for x in todas["tareas"]) == [1, 2, 3]
    assert todas["kpi"] == r["kpi"]                                       # el KPI no depende del filtro


def test_intercambiar_y_deshacer(monkeypatch):
    db, c = montar(monkeypatch, [fila(1, estado="terminado", inicio=T0, fin=T1)], "gerencia")
    assert c.post("/pendientes/1/intercambiar").json() == {"id": 1, "intercambiado": True}
    assert db.filas[0]["intercambiado"] == 1 and db.filas[0]["intercambiado_por"] == "gerencia"
    assert db.movimientos and db.movimientos[0][1] == "intercambiar"
    assert c.post("/pendientes/1/intercambiar").status_code == 409        # doble clic
    assert c.post("/pendientes/1/intercambiar", json={"valor": False}).json()["intercambiado"] is False
    assert db.filas[0]["intercambiado"] == 0


def test_fparra_no_revisa_sus_propios_tiempos(monkeypatch):
    # fparra está en USUARIOS_GERENCIA: pasa requerir_gerencia, pero no puede revisarse a sí mismo
    db, c = montar(monkeypatch, [fila(1, estado="terminado", inicio=T0, fin=T1)], "gerencia")
    c.app.dependency_overrides[requerir_gerencia] = lambda: "fparra"
    assert c.get("/pendientes/tiempos").status_code == 403
    assert c.post("/pendientes/1/intercambiar").status_code == 403
    assert db.filas[0]["intercambiado"] == 0


def test_intercambiar_rechaza_abierta_inexistente_y_no_cronometrada(monkeypatch):
    _, c = montar(monkeypatch, [fila(1, estado="en proceso", inicio=T0), fila(2, usuario="ana", estado="terminado")], "gerencia")
    assert c.post("/pendientes/1/intercambiar").status_code == 409
    assert c.post("/pendientes/2/intercambiar").status_code == 404
    assert c.post("/pendientes/99/intercambiar").status_code == 404
