# Tests de la cola de pendientes por usuario (routers/pendientes.py), migrada de api_java:
# orden, visibilidad, alcance por usuario/gerencia, atender, terminar con recurrencia y estadísticas.
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from permisos import usuario_autenticado
from routers import pendientes

HOY = date(2026, 10, 19)


def fila(id, usuario="ana", prioridad="media", estado="pendiente", actividad=None, fecha=date(2026, 10, 1),
         promesa=None, recurrencia=None, visible=None, plantilla=None, obs=None):
    return {"id": id, "fecha": fecha, "usuario": usuario, "actividad": actividad or f"tarea {id}", "prioridad": prioridad,
            "estado": estado, "observaciones": obs, "fecha_promesa": promesa, "recurrencia": recurrencia,
            "fecha_visible": visible, "plantilla_id": plantilla}


class FakeCursor:
    def __init__(self, db):
        self.db = db
        self._res = []
        self.lastrowid = None

    def _visible(self, f):
        return f["fecha_visible"] is None or f["fecha_visible"] <= HOY

    def _de(self, usuario):
        return [f for f in self.db.filas if usuario is None or f["usuario"].lower() == usuario.lower()]

    def execute(self, query, params=None):
        q = " ".join(query.split())
        db = self.db
        db.ejecutados.append((q, params))
        self._res = []
        usuario = params[0] if params and "LOWER(usuario) = LOWER(%s)" in q else None
        if q.startswith("SELECT COUNT(*) AS n FROM pendientes WHERE estado = 'pendiente'"):
            n = sum(1 for f in self._de(usuario) if f["estado"] == "pendiente" and self._visible(f))
            self._res = [{"n": n}]
        elif q.startswith("SELECT id, fecha, usuario") and "AS visible FROM pendientes WHERE id = %s FOR UPDATE" in q:
            f = next((f for f in db.filas if f["id"] == params[0]), None)
            self._res = [dict(f, visible=self._visible(f))] if f else []
        elif q.startswith("SELECT id, fecha, usuario") and "WHERE estado = 'pendiente'" in q:
            self._res = [dict(f) for f in self._de(usuario) if f["estado"] == "pendiente" and self._visible(f)]
        elif q.startswith("SELECT id, fecha, usuario") and "WHERE estado = 'en proceso'" in q:
            self._res = [dict(f) for f in self._de(usuario) if f["estado"] == "en proceso"]
        elif q.startswith("SELECT id, fecha, usuario") and "FROM pendientes WHERE id = %s" in q:
            f = next((f for f in db.filas if f["id"] == params[0]), None)
            self._res = [dict(f)] if f else []
        elif q.startswith("SELECT LOWER(prioridad) AS prioridad, COUNT(*) AS n"):
            cuenta = {}
            for f in self._de(usuario):
                if f["estado"] == "pendiente" and self._visible(f):
                    cuenta[f["prioridad"].lower()] = cuenta.get(f["prioridad"].lower(), 0) + 1
            self._res = [{"prioridad": k, "n": v} for k, v in cuenta.items()]
        elif q.startswith("SELECT estado, COUNT(*) AS n FROM pendientes WHERE estado IN"):
            cuenta = {}
            for f in self._de(usuario):
                if f["estado"] in ("en proceso", "terminado"):
                    cuenta[f["estado"]] = cuenta.get(f["estado"], 0) + 1
            self._res = [{"estado": k, "n": v} for k, v in cuenta.items()]
        elif q.startswith("UPDATE pendientes SET estado = 'en proceso'"):
            next(f for f in db.filas if f["id"] == params[0])["estado"] = "en proceso"
        elif q.startswith("UPDATE pendientes SET estado = 'terminado'"):
            next(f for f in db.filas if f["id"] == params[0])["estado"] = "terminado"
        elif q.startswith("UPDATE pendientes SET recurrencia"):
            next(f for f in db.filas if f["id"] == params[1])["recurrencia"] = params[0]
        elif q.startswith("INSERT INTO pendientes") and "plantilla_id" in q:
            usuario_, actividad, prioridad, obs, promesa, recurrencia, visible, plantilla = params
            self.lastrowid = db.nuevo_id()
            db.filas.append(fila(self.lastrowid, usuario_, prioridad, "pendiente", actividad, HOY, promesa, recurrencia, visible, plantilla, obs))
        elif q.startswith("INSERT INTO pendientes"):
            usuario_, actividad, prioridad, obs, promesa, recurrencia = params
            self.lastrowid = db.nuevo_id()
            db.filas.append(fila(self.lastrowid, usuario_, prioridad, "pendiente", actividad, HOY, promesa, recurrencia, None, None, obs))
        else:
            raise AssertionError(f"consulta no esperada: {q}")

    def fetchone(self):
        return self._res[0] if self._res else None

    def fetchall(self):
        return list(self._res)

    def close(self):
        pass


class FakeDB:
    def __init__(self, filas):
        self.filas = filas
        self.ejecutados = []
        self.commits = 0
        self.rollbacks = 0
        self.movimientos = []

    def nuevo_id(self):
        return max([f["id"] for f in self.filas] + [0]) + 1

    def cursor(self, dictionary=False):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def montar(monkeypatch, filas, usuario):
    db = FakeDB(filas)
    monkeypatch.setattr(pendientes, "get_db_connection", lambda: db)
    monkeypatch.setattr(pendientes.mov_reg, "registrar_movimiento", lambda *a, **k: db.movimientos.append(a))
    app = FastAPI()
    app.include_router(pendientes.router)
    app.dependency_overrides[usuario_autenticado] = lambda: usuario
    return db, TestClient(app)


# ---------- lógica pura ----------

def test_prioridad_numerica_alta_media_baja_y_lo_demas_es_baja():
    assert [pendientes.prioridad_numerica(p) for p in ("alta", "Media", " BAJA ", "x", None)] == [1, 2, 3, 3, 3]


def test_proxima_fecha_por_recurrencia_y_mensual_recorta_fin_de_mes():
    f = pendientes.proxima_fecha
    assert f(date(2026, 10, 19), "diaria") == date(2026, 10, 20)
    assert f(date(2026, 10, 19), "semanal") == date(2026, 10, 26)
    assert f(date(2026, 10, 19), "quincenal") == date(2026, 11, 3)
    assert f(date(2026, 1, 31), "mensual") == date(2026, 2, 28)
    assert f(date(2026, 12, 15), "MENSUAL") == date(2027, 1, 15)


# ---------- lecturas ----------

@pytest.fixture
def mp(monkeypatch):
    return monkeypatch


def test_cola_ordena_y_oculta(mp):
    filas = [
        fila(1, prioridad="baja"),
        fila(2, prioridad="alta", promesa=date(2026, 11, 1)),
        fila(3, prioridad="alta", promesa=date(2026, 10, 25)),
        fila(4, prioridad="alta"),                                         # sin promesa: después de las que sí tienen
        fila(5, prioridad="media", fecha=date(2026, 9, 1)),
        fila(6, prioridad="media", fecha=date(2026, 8, 1)),
        fila(7, prioridad="alta", visible=date(2026, 10, 25)),            # aún oculta
        fila(8, prioridad="alta", estado="en proceso"),                    # no es cola
        fila(9, prioridad="Alta", visible=date(2026, 10, 19)),             # visible justo hoy, mayúscula mezclada
    ]
    _, c = montar(mp, filas, "gerencia")
    r = c.get("/pendientes")
    assert r.status_code == 200
    assert [p["id"] for p in r.json()] == [3, 2, 4, 9, 6, 5, 1]   # 4 y 9 empatan en todo: desempata el id
    assert r.json()[0]["prioridadNumerica"] == 1 and r.json()[0]["fechaPromesa"] == "2026-10-25"


def test_json_con_los_mismos_campos_que_api_java(mp):
    _, c = montar(mp, [fila(1, obs="nota", recurrencia="semanal", plantilla=5)], "ana")
    p = c.get("/pendientes").json()[0]
    assert sorted(p) == sorted(["id", "fecha", "usuario", "actividad", "prioridad", "prioridadNumerica", "estado",
                                "observaciones", "fechaPromesa", "recurrencia", "fechaVisible", "plantillaId"])
    assert p["fecha"] == "2026-10-01" and p["plantillaId"] == 5 and p["recurrencia"] == "semanal"


def test_quien_no_es_gerencia_solo_ve_las_suyas_y_no_puede_pedir_las_de_otro(mp):
    filas = [fila(1, "ana"), fila(2, "beto"), fila(3, "ANA")]
    _, c = montar(mp, filas, "ana")
    assert [p["id"] for p in c.get("/pendientes").json()] == [1, 3]                  # sin ?usuario= => las propias
    assert [p["id"] for p in c.get("/pendientes?usuario=Ana").json()] == [1, 3]      # mismo usuario sin importar mayúsculas
    assert c.get("/pendientes?usuario=beto").status_code == 403
    assert c.get("/pendientes/estadisticas?usuario=beto").status_code == 403
    assert c.get("/pendientes/en-proceso?usuario=beto").status_code == 403


def test_gerencia_ve_todas_o_filtra_por_usuario(mp):
    filas = [fila(1, "ana"), fila(2, "beto")]
    _, c = montar(mp, filas, "gerencia")
    assert [p["id"] for p in c.get("/pendientes").json()] == [1, 2]
    assert [p["id"] for p in c.get("/pendientes?usuario=beto").json()] == [2]


def test_en_proceso_puede_haber_varias_y_siguiente_da_404_con_cola_vacia(mp):
    filas = [fila(1, estado="en proceso", prioridad="baja"), fila(2, estado="en proceso", prioridad="alta"), fila(3, "beto")]
    _, c = montar(mp, filas, "ana")
    assert [p["id"] for p in c.get("/pendientes/en-proceso").json()] == [2, 1]
    r = c.get("/pendientes/siguiente")           # la ruta fija no la captura /{id}
    assert r.status_code == 404 and r.json()["detail"] == "Cola vacía"
    filas.append(fila(4, prioridad="alta"))
    assert c.get("/pendientes/siguiente").json()["id"] == 4


def test_detalle_en_cualquier_estado_pero_ajeno_es_404_salvo_gerencia(mp):
    filas = [fila(1, "ana", estado="terminado"), fila(2, "beto")]
    _, c = montar(mp, filas, "ana")
    assert c.get("/pendientes/1").json()["estado"] == "terminado"
    assert c.get("/pendientes/2").status_code == 404
    assert c.get("/pendientes/99").status_code == 404
    _, g = montar(mp, filas, "gerencia")
    assert g.get("/pendientes/2").status_code == 200


def test_estadisticas_desde_la_bd(mp):
    filas = [fila(1, prioridad="alta"), fila(2, prioridad="Alta"), fila(3, prioridad="baja"),
             fila(4, estado="en proceso"), fila(5, estado="terminado"), fila(6, estado="terminado"),
             fila(7, prioridad="alta", visible=date(2027, 1, 1)), fila(8, "beto", prioridad="media")]
    _, c = montar(mp, filas, "gerencia")
    g = c.get("/pendientes/estadisticas").json()
    assert g["enCola"] == 4 and g["atendidos"] == 3
    assert g["porPrioridad"] == {"alta": 2, "baja": 1, "media": 1}
    assert g["porEstado"] == {"pendiente": 4, "en proceso": 1, "terminado": 2}
    ana = c.get("/pendientes/estadisticas?usuario=ana").json()
    assert ana["usuario"] == "ana" and ana["enCola"] == 3 and "media" not in ana["porPrioridad"]


# ---------- atender ----------

def test_atender_por_id_pasa_a_en_proceso_y_cuenta_lo_que_queda(mp):
    filas = [fila(1, prioridad="baja"), fila(2, prioridad="alta"), fila(3, "beto")]
    db, c = montar(mp, filas, "ana")
    r = c.post("/pendientes/1/atender")           # elige la baja aunque haya una alta (sin restricción de nivel)
    assert r.status_code == 200
    assert r.json()["atendido"]["estado"] == "en proceso" and r.json()["enCola"] == 1
    assert filas[0]["estado"] == "en proceso" and db.commits == 1
    assert c.post("/pendientes/2/atender").status_code == 200   # puede tener varias abiertas
    assert sorted(p["id"] for p in c.get("/pendientes/en-proceso").json()) == [1, 2]


def test_atender_rechaza_ajena_ya_tomada_inexistente_y_oculta(mp):
    filas = [fila(1, "beto"), fila(2, "ana", estado="en proceso"), fila(3, "ana", visible=date(2026, 10, 25)), fila(4, "ana")]
    db, c = montar(mp, filas, "ana")
    for id_ in (1, 2, 3, 99):
        r = c.post(f"/pendientes/{id_}/atender")
        assert r.status_code == 404 and r.json()["detail"] == "No encontrado en cola", id_
    assert filas[0]["estado"] == "pendiente" and filas[2]["estado"] == "pendiente"
    assert db.commits == 0 and db.rollbacks == 4
    # La gerencia tampoco atiende tareas ajenas: atender es de quien las tiene asignadas.
    _, g = montar(mp, filas, "gerencia")
    assert g.post("/pendientes/4/atender").status_code == 404


def test_atender_dos_veces_la_misma_no_la_toma_de_nuevo(mp):
    _, c = montar(mp, [fila(1)], "ana")
    assert c.post("/pendientes/1/atender").status_code == 200
    assert c.post("/pendientes/1/atender").status_code == 404


def test_atender_siguiente_toma_la_primera_de_mi_cola(mp):
    filas = [fila(1, "ana", prioridad="baja"), fila(2, "ana", prioridad="alta"), fila(3, "beto", prioridad="alta")]
    _, c = montar(mp, filas, "ana")
    r = c.post("/pendientes/atender")
    assert r.status_code == 200 and r.json()["atendido"]["id"] == 2 and r.json()["enCola"] == 1
    assert filas[2]["estado"] == "pendiente"
    c.post("/pendientes/atender")
    assert c.post("/pendientes/atender").status_code == 404   # ya no queda nada propio


# ---------- terminar ----------

def test_terminar_marca_terminado_sin_clonar_si_no_hay_recurrencia(mp):
    filas = [fila(1, estado="en proceso")]
    db, c = montar(mp, filas, "ana")
    r = c.post("/pendientes/1/terminar")
    assert r.status_code == 200 and r.json() == {"terminado": True, "id": 1}
    assert filas[0]["estado"] == "terminado" and len(filas) == 1 and db.commits == 1


def test_terminar_ajena_o_inexistente_es_404_y_no_toca_nada(mp):
    filas = [fila(1, "beto", estado="en proceso")]
    db, c = montar(mp, filas, "ana")
    assert c.post("/pendientes/1/terminar").status_code == 404
    assert c.post("/pendientes/9/terminar").status_code == 404
    assert filas[0]["estado"] == "en proceso" and db.commits == 0


def test_terminar_con_recurrencia_clona_la_siguiente_oculta_hasta_su_fecha(mp):
    filas = [fila(1, estado="en proceso", prioridad="alta", recurrencia="semanal", promesa=date(2026, 10, 20), obs="x")]
    _, c = montar(mp, filas, "ana")
    r = c.post("/pendientes/1/terminar")
    assert r.status_code == 200 and r.json()["siguiente"] == 2
    clon = filas[1]
    assert (clon["estado"], clon["prioridad"], clon["recurrencia"], clon["observaciones"]) == ("pendiente", "alta", "semanal", "x")
    assert clon["fecha_promesa"] == date(2026, 10, 27) and clon["fecha_visible"] == date(2026, 10, 27)
    assert clon["plantilla_id"] == 1                              # la primera instancia es su propia plantilla
    assert [p["id"] for p in c.get("/pendientes").json()] == []    # el clon no aparece en la cola hasta su fecha


def test_terminar_la_instancia_clonada_conserva_la_plantilla_y_ancla_en_su_promesa(mp):
    filas = [fila(7, estado="en proceso", recurrencia="mensual", promesa=date(2026, 1, 31), plantilla=3)]
    _, c = montar(mp, filas, "ana")
    c.post("/pendientes/7/terminar")
    assert filas[1]["plantilla_id"] == 3 and filas[1]["fecha_promesa"] == date(2026, 2, 28)


def test_terminar_sin_promesa_ancla_en_la_fecha_de_alta(mp):
    filas = [fila(1, estado="en proceso", recurrencia="diaria", fecha=date(2026, 10, 10))]
    _, c = montar(mp, filas, "ana")
    c.post("/pendientes/1/terminar")
    assert filas[1]["fecha_promesa"] == date(2026, 10, 11)


def test_terminar_dos_veces_no_clona_dos_veces(mp):
    filas = [fila(1, estado="en proceso", recurrencia="diaria", promesa=date(2026, 10, 19))]
    db, c = montar(mp, filas, "ana")
    assert c.post("/pendientes/1/terminar").status_code == 200
    r = c.post("/pendientes/1/terminar")
    assert r.status_code == 200 and r.json() == {"terminado": True, "id": 1}
    assert len(filas) == 2


# ---------- recurrencia ----------

def test_cambiar_y_quitar_recurrencia_dueno_o_gerencia(mp):
    filas = [fila(1, "ana"), fila(2, "beto")]
    _, c = montar(mp, filas, "ana")
    assert c.post("/pendientes/1/recurrencia", json={"valor": "Quincenal"}).json()["recurrencia"] == "quincenal"
    assert filas[0]["recurrencia"] == "quincenal"
    assert c.post("/pendientes/1/recurrencia", json={"valor": ""}).json()["recurrencia"] is None
    assert c.post("/pendientes/1/recurrencia", json={}).json()["recurrencia"] is None
    assert c.post("/pendientes/2/recurrencia", json={"valor": "diaria"}).status_code == 404   # ajena
    _, g = montar(mp, filas, "gerencia")
    assert g.post("/pendientes/2/recurrencia", json={"valor": "diaria"}).status_code == 200


def test_recurrencia_invalida_da_422_y_no_escribe(mp):
    filas = [fila(1)]
    db, c = montar(mp, filas, "ana")
    assert c.post("/pendientes/1/recurrencia", json={"valor": "cada rato"}).status_code == 422
    assert filas[0]["recurrencia"] is None and db.commits == 0


# ---------- nueva ----------

def test_nueva_para_mi_con_prioridad_y_recurrencia_y_bitacora(mp):
    filas = []
    db, c = montar(mp, filas, "ana")
    r = c.post("/pendientes/nueva", json={"actividad": "  Cobrar a X ", "prioridad": "Alta", "observaciones": "  ",
                                            "fecha_promesa": "2026-10-30", "recurrencia": "MENSUAL"})
    assert r.status_code == 200
    p = r.json()
    assert (p["usuario"], p["actividad"], p["prioridad"], p["estado"], p["observaciones"], p["recurrencia"]) == \
           ("ana", "Cobrar a X", "Alta", "pendiente", None, "mensual")
    assert p["fechaPromesa"] == "2026-10-30" and p["fechaVisible"] is None
    assert db.commits == 1 and db.movimientos and db.movimientos[0][0] == "ana" and db.movimientos[0][2] == "Pendientes"
    assert [x["id"] for x in c.get("/pendientes").json()] == [p["id"]]   # visible de inmediato


def test_nueva_para_otro_usuario_solo_gerencia(mp):
    filas = []
    _, c = montar(mp, filas, "ana")
    assert c.post("/pendientes/nueva", json={"actividad": "x", "prioridad": "alta", "usuario": "beto"}).status_code == 403
    assert c.post("/pendientes/nueva", json={"actividad": "x", "prioridad": "alta", "usuario": "ANA"}).status_code == 200
    _, g = montar(mp, filas, "gerencia")
    r = g.post("/pendientes/nueva", json={"actividad": "x", "prioridad": "baja", "usuario": "beto"})
    assert r.status_code == 200 and r.json()["usuario"] == "beto"


@pytest.mark.parametrize("cuerpo", [
    {"actividad": "", "prioridad": "alta"},
    {"actividad": "x", "prioridad": "urgentisima"},
    {"actividad": "x", "prioridad": "alta", "recurrencia": "a veces"},
    {"prioridad": "alta"},
])
def test_nueva_invalida_da_422_sin_escribir(mp, cuerpo):
    filas = []
    db, c = montar(mp, filas, "ana")
    assert c.post("/pendientes/nueva", json=cuerpo).status_code == 422
    assert filas == [] and db.commits == 0


def test_la_bitacora_que_falla_no_tumba_la_creacion(mp):
    filas = []
    db, c = montar(mp, filas, "ana")
    mp.setattr(pendientes.mov_reg, "registrar_movimiento", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bitácora caída")))
    assert c.post("/pendientes/nueva", json={"actividad": "x", "prioridad": "alta"}).status_code == 200
    assert len(filas) == 1


# ---------- autenticación ----------

def test_sin_sesion_nada_responde():
    app = FastAPI()
    app.include_router(pendientes.router)
    c = TestClient(app)
    for metodo, ruta in (("get", "/pendientes"), ("post", "/pendientes/1/atender"), ("post", "/pendientes/1/terminar"),
                         ("get", "/pendientes/estadisticas")):
        assert getattr(c, metodo)(ruta).status_code in (401, 403), ruta
