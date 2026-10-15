# Tests del plano de bodega (routers/anaqueles.py) y de mover tarjetas entre
# celdas (routers/productos.py). El ID de celda = warehouse_id de las tarjetas.
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import anaqueles, productos


def anaquel(id, codigo, x=0, y=0, columnas=2, niveles=1, celdas=(), orientacion="H"):
    return {"id": id, "codigo": codigo, "x": x, "y": y, "columnas": columnas, "niveles": niveles,
            "orientacion": orientacion,
            "celdas": [{"nivel": n, "columna": c, "codigo": cod} for n, c, cod in celdas]}


# ---------- planear_disposicion (lógica pura) ----------

def test_plan_sin_cambios_no_tiene_errores_ni_renombres():
    actual = [anaquel(1, "A", celdas=[(0, 0, "10"), (0, 1, "11")])]
    plan = anaqueles.planear_disposicion(actual, actual, {"10": 2})
    assert plan == {"errores": [], "renombres": [], "desasignadas": []}


def test_plan_detecta_encimados_fuera_de_plano_y_limites():
    nueva = [
        anaquel(None, "A", x=0, y=0, columnas=4),
        anaquel(None, "B", x=3, y=0, columnas=2),           # se encima con A
        anaquel(None, "C", x=23, y=1, columnas=2),          # sale del plano (24 de ancho)
        anaquel(None, "D", x=0, y=2, columnas=11),          # excede 10 columnas
    ]
    errores = anaqueles.planear_disposicion([], nueva, {})["errores"]
    assert any("A y B se enciman" in e for e in errores)
    assert any("C sale del plano" in e for e in errores)
    assert any("D excede 10 columnas" in e for e in errores)


def test_plan_ids_de_celda_repetidos_sin_importar_mayusculas():
    nueva = [anaquel(None, "A", celdas=[(0, 0, "x1")]), anaquel(None, "B", y=1, celdas=[(0, 0, " X1 ")])]
    errores = anaqueles.planear_disposicion([], nueva, {})["errores"]
    assert any("X1 está repetido" in e for e in errores)


def test_plan_anaquel_vertical_ocupa_1_ancho_por_columnas_de_fondo():
    # Vertical en x=3: ocupa (3, 0..3); uno horizontal en la fila 2 que cruce x=3 choca.
    vertical = anaquel(None, "V", x=3, y=0, columnas=4, orientacion="V")
    choca = anaquel(None, "H", x=2, y=2, columnas=3)
    libre = anaquel(None, "L", x=4, y=2, columnas=3)
    assert any("V y H se enciman" in e for e in anaqueles.planear_disposicion([], [vertical, choca], {})["errores"])
    assert anaqueles.planear_disposicion([], [vertical, libre], {})["errores"] == []
    # Dos verticales en columnas distintas conviven; en la misma columna y filas cruzadas, no.
    v2 = anaquel(None, "W", x=4, y=0, columnas=4, orientacion="V")
    assert anaqueles.planear_disposicion([], [vertical, v2], {})["errores"] == []
    v3 = anaquel(None, "X", x=3, y=3, columnas=2, orientacion="V")
    assert any("se enciman" in e for e in anaqueles.planear_disposicion([], [vertical, v3], {})["errores"])


def test_plan_vertical_se_valida_contra_el_alto_del_plano():
    # 12 filas de alto: 6 columnas verticales desde la fila 8 llegan a 14.
    fuera = anaquel(None, "V", x=0, y=8, columnas=6, orientacion="V")
    assert any("V sale del plano" in e for e in anaqueles.planear_disposicion([], [fuera], {})["errores"])
    # El mismo anaquel horizontal en y=8 sí cabe (ocupa una sola fila).
    assert anaqueles.planear_disposicion([], [dict(fuera, orientacion="H")], {})["errores"] == []


def test_plan_celda_fuera_del_tamano_es_error():
    nueva = [anaquel(None, "A", columnas=2, niveles=1, celdas=[(1, 0, "Z")])]
    assert any("fuera del anaquel" in e for e in anaqueles.planear_disposicion([], nueva, {})["errores"])


def test_plan_renombrar_celda_arrastra_tarjetas():
    actual = [anaquel(1, "A", celdas=[(0, 0, "10")])]
    nueva = [anaquel(1, "A", celdas=[(0, 0, "A-10")])]
    plan = anaqueles.planear_disposicion(actual, nueva, {"10": 3})
    assert plan["errores"] == []
    assert plan["renombres"] == [{"de": "10", "a": "A-10", "tarjetas": 3}]


def test_plan_mover_el_id_a_otra_celda_dejaria_cajas_sin_id():
    # Las cajas siguen físicamente en (0,0): quitarle el ID con tarjetas se bloquea.
    actual = [anaquel(1, "A", celdas=[(0, 0, "10")])]
    nueva = [anaquel(1, "A", celdas=[(0, 1, "10")])]
    plan = anaqueles.planear_disposicion(actual, nueva, {"10": 3})
    assert any("10 tiene 3 tarjeta" in e for e in plan["errores"])
    assert anaqueles.planear_disposicion(actual, nueva, {})["errores"] == []


def test_plan_solo_cambio_de_mayusculas_no_es_renombre():
    actual = [anaquel(1, "A", celdas=[(0, 0, "a1")])]
    nueva = [anaquel(1, "A", celdas=[(0, 0, "A1")])]
    assert anaqueles.planear_disposicion(actual, nueva, {"a1": 1})["renombres"] == []


def test_plan_borrar_anaquel_cuyo_id_se_reusa_se_bloquea():
    actual = [anaquel(1, "A", celdas=[(0, 0, "10")]), anaquel(2, "B", y=1, celdas=[(0, 0, "20")])]
    nueva = [anaquel(1, "A", celdas=[(0, 0, "10"), (0, 1, "20")])]
    plan = anaqueles.planear_disposicion(actual, nueva, {"20": 1})
    assert any("aparecerían en otra celda" in e for e in plan["errores"])


def test_plan_bloquea_encoger_con_tarjetas_pero_no_sin_ellas():
    actual = [anaquel(1, "A", columnas=2, celdas=[(0, 0, "10"), (0, 1, "11")])]
    encogido = [anaquel(1, "A", columnas=1, celdas=[(0, 0, "10")])]
    plan = anaqueles.planear_disposicion(actual, encogido, {"11": 1})
    assert any("11 tiene 1 tarjeta" in e for e in plan["errores"])
    assert anaqueles.planear_disposicion(actual, encogido, {})["errores"] == []


def test_plan_borrar_anaquel_desasigna_sus_tarjetas():
    actual = [anaquel(1, "A", celdas=[(0, 0, "10")]), anaquel(2, "B", y=1, celdas=[(0, 0, "20")])]
    nueva = [anaquel(1, "A", celdas=[(0, 0, "10")])]
    plan = anaqueles.planear_disposicion(actual, nueva, {"20": 4})
    assert plan["errores"] == []
    assert plan["desasignadas"] == [{"codigo": "20", "tarjetas": 4}]


def test_plan_anaquel_inexistente_pide_recargar():
    errores = anaqueles.planear_disposicion([], [anaquel(99, "A")], {})["errores"]
    assert any("#99 ya no existe" in e for e in errores)


# ---------- Endpoints con BD falsa ----------

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
        self._resultado, self.rowcount = [], 0
        if q.startswith("CREATE TABLE") or q.startswith("INSERT IGNORE INTO bodega_disposicion"):
            return
        if q.startswith("ALTER TABLE anaqueles ADD COLUMN orientacion"):
            db.alters += 1
        elif q.startswith("SELECT COUNT(*) AS n FROM information_schema.COLUMNS"):
            self._resultado = [{"n": 1 if db.tiene_orientacion else 0}]
        elif q.startswith("SELECT version FROM bodega_disposicion"):
            self._resultado = [{"version": db.version}]
        elif q.startswith("SELECT id, codigo, x, y, columnas, niveles, orientacion FROM anaqueles"):
            self._resultado = [{k: a[k] for k in ("id", "codigo", "x", "y", "columnas", "niveles", "orientacion")} for a in db.anaqueles.values()]
        elif q.startswith("SELECT anaquel_id, nivel, columna, codigo FROM anaqueles_celdas"):
            self._resultado = [dict(c) for c in db.celdas]
        elif q.startswith("SELECT LOWER(TRIM(warehouse_id)) AS clave"):
            conteo = {}
            for t in db.tarjetas.values():
                clave = t["warehouse_id"].strip().lower()
                f = conteo.setdefault(clave, {"clave": clave, "tarjetas": 0, "skus": set()})
                f["tarjetas"] += 1
                f["skus"].add(t["sku"])
            self._resultado = [dict(f, skus=len(f["skus"])) for f in conteo.values()]
        elif q.startswith("DELETE FROM anaqueles WHERE id"):
            db.anaqueles.pop(params[0], None)
        elif q.startswith("DELETE FROM anaqueles_celdas"):
            db.celdas = []
        elif q.startswith("INSERT INTO anaqueles ("):
            db.siguiente_id += 1
            self.lastrowid = db.siguiente_id
            db.anaqueles[self.lastrowid] = dict(zip(("codigo", "x", "y", "columnas", "niveles", "orientacion"), params), id=self.lastrowid)
        elif q.startswith("UPDATE anaqueles SET"):
            db.anaqueles[params[6]].update(dict(zip(("codigo", "x", "y", "columnas", "niveles", "orientacion"), params[:6])))
        elif q.startswith("INSERT INTO anaqueles_celdas"):
            db.celdas.append(dict(zip(("anaquel_id", "nivel", "columna", "codigo"), params)))
        elif q.startswith("UPDATE stock_ubicacion SET warehouse_id = %s WHERE LOWER(TRIM(warehouse_id))"):
            for t in db.tarjetas.values():
                if t["warehouse_id"].strip().lower() == params[1]:
                    t["warehouse_id"] = params[0]
        elif q.startswith("UPDATE stock_ubicacion SET warehouse_id = %s WHERE warehouse_id"):
            for t in db.tarjetas.values():
                if t["warehouse_id"] == params[1]:
                    t["warehouse_id"] = params[0]
        elif q.startswith("UPDATE bodega_disposicion SET version"):
            db.version = params[0]
        elif q.startswith("SELECT id, sku, warehouse_id, cama, cantidad FROM stock_ubicacion WHERE id"):
            t = db.tarjetas.get(params[0])
            self._resultado = [dict(t)] if t else []
        elif q.startswith("UPDATE stock_ubicacion SET warehouse_id = %s WHERE id"):
            db.tarjetas[params[1]]["warehouse_id"] = params[0]
            self.rowcount = 1

    def fetchone(self):
        return self._resultado[0] if self._resultado else None

    def fetchall(self):
        return list(self._resultado)

    def close(self):
        pass


class FakeDB:
    def __init__(self):
        self.version = 3
        self.siguiente_id = 2
        self.tiene_orientacion = True
        self.alters = 0
        self.anaqueles = {
            1: {"id": 1, "codigo": "A", "x": 0, "y": 0, "columnas": 2, "niveles": 1, "orientacion": "H"},
            2: {"id": 2, "codigo": "B", "x": 0, "y": 1, "columnas": 2, "niveles": 1, "orientacion": "H"},
        }
        self.celdas = [
            {"anaquel_id": 1, "nivel": 0, "columna": 0, "codigo": "10"},
            {"anaquel_id": 1, "nivel": 0, "columna": 1, "codigo": "11"},
            {"anaquel_id": 2, "nivel": 0, "columna": 0, "codigo": "20"},
        ]
        # Dos SKUs comparten la celda 10.
        self.tarjetas = {
            1: {"id": 1, "sku": "BOLECO17", "warehouse_id": "10", "cama": 2, "cantidad": 5},
            2: {"id": 2, "sku": "ESPFARBLA", "warehouse_id": "10 ", "cama": 0, "cantidad": 7},
            3: {"id": 3, "sku": "BOLECO17", "warehouse_id": "11", "cama": 0, "cantidad": 1},
            4: {"id": 4, "sku": "BOLECO17", "warehouse_id": "20", "cama": 0, "cantidad": 3},
        }
        self.ejecutados = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self, dictionary=False):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


@pytest.fixture
def db(monkeypatch):
    base = FakeDB()
    movimientos = []
    for modulo in (anaqueles, productos):
        monkeypatch.setattr(modulo, "get_db_connection", lambda: base)
        monkeypatch.setattr(modulo.mov_reg, "registrar_movimiento", lambda *a, **k: movimientos.append(a))
    monkeypatch.setattr(productos.mov_reg, "registrar_edicionUbi", lambda *a, **k: None)
    base.movimientos = movimientos
    return base


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(anaqueles.router)
    app.include_router(productos.router)
    return TestClient(app)


def disposicion_actual(db, **cambios):
    anq = []
    for a in db.anaqueles.values():
        celdas = [{"nivel": c["nivel"], "columna": c["columna"], "codigo": c["codigo"]}
                  for c in db.celdas if c["anaquel_id"] == a["id"]]
        anq.append(dict(a, celdas=celdas))
    return {"version": db.version, "usuario": "ana", "anaqueles": anq, **cambios}


def test_get_devuelve_plano_y_ocupacion_compartida(client, db):
    r = client.get("/anaqueles")
    assert r.status_code == 200
    body = r.json()
    assert body["version"] == 3
    assert [a["codigo"] for a in body["anaqueles"]] == ["A", "B"]
    assert body["anaqueles"][0]["celdas"][0] == {"nivel": 0, "columna": 0, "codigo": "10"}
    assert body["ocupacion"]["10"] == {"tarjetas": 2, "skus": 2}


def test_guardar_con_version_vieja_da_409_sin_escribir(client, db):
    r = client.put("/anaqueles/disposicion", json=disposicion_actual(db, version=2))
    assert r.status_code == 409
    assert db.version == 3 and len(db.celdas) == 3


def test_guardar_renombra_tarjetas_de_todos_los_skus_y_sube_version(client, db):
    p = disposicion_actual(db)
    p["anaqueles"][0]["celdas"][0]["codigo"] = "A-10"
    r = client.put("/anaqueles/disposicion", json=p)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 4 and db.version == 4
    assert db.tarjetas[1]["warehouse_id"] == "A-10" and db.tarjetas[2]["warehouse_id"] == "A-10"
    assert db.tarjetas[3]["warehouse_id"] == "11"
    assert any("10→A-10" in m[1] for m in db.movimientos)


def test_intercambiar_ids_no_mezcla_tarjetas(client, db):
    # Las cajas no se mueven: cada celda conserva sus tarjetas con el ID nuevo.
    p = disposicion_actual(db)
    celdas = p["anaqueles"][0]["celdas"]
    celdas[0]["codigo"], celdas[1]["codigo"] = "11", "10"
    r = client.put("/anaqueles/disposicion", json=p)
    assert r.status_code == 200, r.text
    assert db.tarjetas[1]["warehouse_id"] == "11" and db.tarjetas[2]["warehouse_id"] == "11"
    assert db.tarjetas[3]["warehouse_id"] == "10"


def test_renombre_en_cadena_usa_dos_fases(client, db):
    # 10→11 y 11→12 a la vez: sin la fase temporal, las de 10 acabarían en 12.
    p = disposicion_actual(db)
    celdas = p["anaqueles"][0]["celdas"]
    celdas[0]["codigo"], celdas[1]["codigo"] = "11", "12"
    r = client.put("/anaqueles/disposicion", json=p)
    assert r.status_code == 200, r.text
    assert db.tarjetas[1]["warehouse_id"] == "11" and db.tarjetas[3]["warehouse_id"] == "12"


def test_encoger_con_tarjetas_da_422(client, db):
    p = disposicion_actual(db)
    p["anaqueles"][0]["columnas"] = 1
    p["anaqueles"][0]["celdas"] = [c for c in p["anaqueles"][0]["celdas"] if c["columna"] == 0]
    r = client.put("/anaqueles/disposicion", json=p)
    assert r.status_code == 422 and "11 tiene 1 tarjeta" in r.json()["detail"]
    assert db.version == 3


def test_borrar_anaquel_desasigna_y_conserva_tarjetas(client, db):
    p = disposicion_actual(db)
    p["anaqueles"] = [a for a in p["anaqueles"] if a["codigo"] != "B"]
    r = client.put("/anaqueles/disposicion", json=p)
    assert r.status_code == 200, r.text
    assert 2 not in db.anaqueles
    assert r.json()["desasignadas"] == [{"codigo": "20", "tarjetas": 1}]
    assert db.tarjetas[4]["warehouse_id"] == "20"


def test_anaquel_nuevo_se_inserta_con_sus_celdas(client, db):
    p = disposicion_actual(db)
    p["anaqueles"].append({"codigo": "C", "x": 5, "y": 0, "columnas": 1, "niveles": 2,
                           "celdas": [{"nivel": 1, "columna": 0, "codigo": "C-21"}]})
    r = client.put("/anaqueles/disposicion", json=p)
    assert r.status_code == 200, r.text
    assert db.anaqueles[3]["codigo"] == "C"
    assert {"anaquel_id": 3, "nivel": 1, "columna": 0, "codigo": "C-21"} in db.celdas


def test_girar_a_vertical_se_guarda_y_se_lee(client, db):
    p = disposicion_actual(db)
    p["anaqueles"][1].update({"x": 6, "y": 0, "orientacion": "V"})  # B: 2 columnas, 1×2 en el plano
    r = client.put("/anaqueles/disposicion", json=p)
    assert r.status_code == 200, r.text
    assert db.anaqueles[2]["orientacion"] == "V"
    leido = client.get("/anaqueles").json()["anaqueles"]
    assert {a["codigo"]: a["orientacion"] for a in leido} == {"A": "H", "B": "V"}


def test_girar_que_se_encima_o_sale_del_plano_da_422(client, db):
    p = disposicion_actual(db)
    p["anaqueles"][0].update({"columnas": 10, "orientacion": "V", "y": 6})  # 6+10 > 12 filas
    r = client.put("/anaqueles/disposicion", json=p)
    assert r.status_code == 422 and "sale del plano" in r.json()["detail"]
    assert db.version == 3


def test_orientacion_invalida_da_422(client, db):
    p = disposicion_actual(db)
    p["anaqueles"][0]["orientacion"] = "X"
    assert client.put("/anaqueles/disposicion", json=p).status_code == 422


def test_sin_orientacion_en_el_payload_es_horizontal(client, db):
    p = disposicion_actual(db)
    del p["anaqueles"][0]["orientacion"]
    assert client.put("/anaqueles/disposicion", json=p).status_code == 200
    assert db.anaqueles[1]["orientacion"] == "H"


def test_tabla_vieja_sin_orientacion_se_migra_sola(client, db):
    db.tiene_orientacion = False
    assert client.get("/anaqueles").status_code == 200
    assert db.alters == 1


@pytest.mark.parametrize("cambio", [{"columnas": 11}, {"niveles": 5}, {"niveles": 0}, {"x": -1}])
def test_limites_de_tamano_dan_422(client, db, cambio):
    p = disposicion_actual(db)
    p["anaqueles"][0].update(cambio)
    assert client.put("/anaqueles/disposicion", json=p).status_code == 422


# ---------- Mover tarjeta ----------

def test_mover_cambia_solo_warehouse_id(client, db):
    r = client.put("/ubicaciones/mover/3", json={"sku": "BOLECO17", "warehouse_id": " 20 ", "usuario": "ana"})
    assert r.status_code == 200, r.text
    assert r.json()["origen"] == "11" and r.json()["destino"] == "20"
    assert db.tarjetas[3] == {"id": 3, "sku": "BOLECO17", "warehouse_id": "20", "cama": 0, "cantidad": 1}
    assert any("Movió la tarjeta #3" in m[1] for m in db.movimientos)


def test_mover_no_falla_si_la_bitacora_truena_despues_del_commit(client, db, monkeypatch):
    def truena(*a, **k):
        raise RuntimeError("1146: Table 'ubicaciones_editadas' doesn't exist")
    monkeypatch.setattr(productos.mov_reg, "registrar_edicionUbi", truena)
    r = client.put("/ubicaciones/mover/3", json={"sku": "BOLECO17", "warehouse_id": "20", "usuario": "ana"})
    assert r.status_code == 200
    assert db.tarjetas[3]["warehouse_id"] == "20" and db.commits == 1


def test_registrar_edicion_crea_la_tabla_antes_de_insertar(monkeypatch):
    import mov_reg
    consultas = []

    class Cur:
        def execute(self, q, p=None): consultas.append(" ".join(q.split()))
        def close(self): pass

    class Con:
        def cursor(self): return Cur()
        def commit(self): consultas.append("COMMIT")
        def close(self): pass

    monkeypatch.setattr(mov_reg, "get_db_connection", lambda: Con())
    mov_reg.registrar_edicionUbi("BOLECO17", "20", 0, 3)
    assert consultas[0].startswith("CREATE TABLE IF NOT EXISTS ubicaciones_editadas")
    assert consultas[1].startswith("INSERT INTO ubicaciones_editadas") and consultas[-1] == "COMMIT"


def test_mover_a_la_misma_celda_otro_sku_o_inexistente(client, db):
    misma = client.put("/ubicaciones/mover/1", json={"sku": "BOLECO17", "warehouse_id": "10", "usuario": "ana"})
    assert misma.status_code == 422
    otro = client.put("/ubicaciones/mover/2", json={"sku": "BOLECO17", "warehouse_id": "20", "usuario": "ana"})
    assert otro.status_code == 422
    assert client.put("/ubicaciones/mover/99", json={"sku": "BOLECO17", "warehouse_id": "20", "usuario": "ana"}).status_code == 404
    assert db.tarjetas[1]["warehouse_id"] == "10" and db.tarjetas[2]["warehouse_id"] == "10 "
