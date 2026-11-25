# T5: búsqueda y filtros de /crm/seguimientos y /crm/interacciones (BD falsa de test_crm).
from datetime import date

import pytest

from servicios import crm_busqueda
from tests.test_crm import ANA, GER, client, db  # noqa: F401  (fixtures)

HOY = date(2026, 10, 1)
BS = chr(92)
ESCAPE_SQL = "ESCAPE '" + BS * 2 + "'"  # lo que ve MySQL: ESCAPE '\'


def test_escapar_like_escapa_barra_porcentaje_y_guion_bajo():
    assert crm_busqueda.escapar_like(r"a\b%c_d") == r"a\\b\%c\_d"


def test_normalizar_q_ignora_vacio_y_espacios():
    assert crm_busqueda.normalizar_q(None) is None
    assert crm_busqueda.normalizar_q("   ") is None
    assert crm_busqueda.normalizar_q("  hola ") == "hola"


@pytest.mark.parametrize("ruta", ["/crm/seguimientos", "/crm/interacciones"])
def test_q_de_101_caracteres_da_422(client, db, ruta):
    assert client.get(ruta, params={"q": "a" * 101}, headers=ANA).status_code == 422
    assert client.get(ruta, params={"q": "a" * 100}, headers=ANA).status_code == 200


def test_seguimientos_q_va_como_parametro_con_escape_y_nunca_en_el_sql(client, db):
    malo = "o'brien%_" + BS + " \"; DROP"
    assert client.get("/crm/seguimientos", params={"q": malo}, headers=ANA).status_code == 200
    q, p = db.consultas("SELECT i.id, i.cliente_id")[-1]
    assert ESCAPE_SQL in q and "o'brien" not in q and "DROP" not in q
    patron = "%" + crm_busqueda.escapar_like(malo) + "%"
    assert p.count(patron) == 6  # nombre, empresa, próxima acción, notas, teléfono, email


def _fila(id_, dias, tipo="llamada"):
    from datetime import timedelta
    return {"id": id_, "cliente_id": 1, "tipo": tipo, "proxima_fecha": HOY + timedelta(days=dias)}


def test_seguimientos_tipo_en_sql_como_lista(client, db):
    client.get("/crm/seguimientos", params=[("tipo", "llamada"), ("tipo", "correo")], headers=ANA)
    q, p = db.consultas("SELECT i.id, i.cliente_id")[-1]
    assert "i.tipo IN (%s, %s)" in q and p[-2:] == ("llamada", "correo")
    assert client.get("/crm/seguimientos", params={"tipo": "fax"}, headers=ANA).status_code == 422


def test_seguimientos_estado_invalido_da_422(client, db):
    assert client.get("/crm/seguimientos", params={"estado": "ayer"}, headers=ANA).status_code == 422


def _con_filas(db):
    db.respuestas.append(("FROM crm_interacciones i", [_fila(1, -2), _fila(2, 0), _fila(3, 5), _fila(4, 7)]))


def test_seguimientos_sin_parametros_devuelve_todo_igual_que_antes(client, db):
    _con_filas(db)
    j = client.get("/crm/seguimientos", headers=ANA).json()
    assert j["totales"] == {"vencidos": 1, "hoy": 1, "proximos": 2}
    assert [x["id"] for x in j["vencidos"] + j["hoy"] + j["proximos"]] == [1, 2, 3, 4]


def test_seguimientos_estado_filtra_en_python_y_totales_lo_reflejan(client, db):
    _con_filas(db)
    j = client.get("/crm/seguimientos", params=[("estado", "vencidos"), ("estado", "proximos")], headers=ANA).json()
    assert j["totales"] == {"vencidos": 1, "hoy": 0, "proximos": 2}
    assert j["hoy"] == [] and len(j["proximos"]) == 2
    j = client.get("/crm/seguimientos", params={"estado": "hoy"}, headers=ANA).json()
    assert j["totales"] == {"vencidos": 0, "hoy": 1, "proximos": 0}


def test_seguimientos_sin_filtros_nuevos_el_sql_no_cambia(client, db):
    client.get("/crm/seguimientos", headers=ANA)
    q, p = db.consultas("SELECT i.id, i.cliente_id")[-1]
    assert " LIKE " not in q and "i.tipo IN" not in q and p == (date(2026, 10, 8),)


def test_bitacora_q_y_resultado(client, db):
    r = client.get("/crm/interacciones", params=[("q", "50%_"), ("resultado", "contesto"), ("resultado", "interesado")], headers=ANA)
    assert r.status_code == 200
    q, p = db.consultas("SELECT COUNT(*)")[0]
    assert ESCAPE_SQL in q and "50%_" not in q and "i.resultado IN (%s, %s)" in q
    assert ("%50" + BS + "%" + BS + "_%") in p and p[2:4] == ("contesto", "interesado")
    q2, p2 = db.consultas("SELECT i.id, i.cliente_id")[0]
    assert p2[:-2] == p  # el mismo filtro en el listado
    assert p.count("%50" + BS + "%" + BS + "_%") == 4


def test_bitacora_resultado_invalido_y_rango(client, db):
    assert client.get("/crm/interacciones", params={"resultado": "quiza"}, headers=ANA).status_code == 422
    assert client.get("/crm/interacciones", params={"desde": "2026-10-02", "hasta": "2026-10-01"}, headers=ANA).status_code == 422
    assert client.get("/crm/interacciones", params={"desde": "2025-01-01", "hasta": "2026-10-01"}, headers=ANA).status_code == 422


def test_bitacora_sin_parametros_nuevos_el_sql_no_cambia(client, db):
    client.get("/crm/interacciones", headers=ANA)
    q, p = db.consultas("SELECT COUNT(*)")[0]
    assert " LIKE " not in q and "i.resultado IN" not in q and len(p) == 2


def test_exportar_no_usa_la_busqueda_nueva(client, db):
    client.get("/crm/interacciones/exportar", params={"q": "x", "resultado": "contesto"}, headers=GER)
    q, _ = db.consultas("SELECT i.fecha, i.vendedor")[-1]
    assert " LIKE " not in q and "resultado IN" not in q
