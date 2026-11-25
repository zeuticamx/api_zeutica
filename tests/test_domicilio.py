# Tests de servicios/domicilio_logica.py: validación del domicilio estructurado, composición de
# `direccion`, catálogo de estados/municipios y migración de `cp` (INT -> texto). Sin BD real.
import pytest

from servicios import domicilio_logica as d


def completo(**extra):
    base = {"calle": "Av. Juárez", "numero": "100", "interior": "4", "colonia": "Centro",
            "cp": "44100", "municipio": "Guadalajara", "estado": "Jalisco"}
    base.update(extra)
    return base


# ---------- Catálogo ----------

def test_catalogo_trae_los_32_estados_y_sus_municipios():
    assert len(d.CATALOGO) == 32
    assert sum(len(v) for v in d.CATALOGO.values()) > 2400
    assert len(d.CATALOGO["Jalisco"]) == 125 and len(d.CATALOGO["Ciudad de México"]) == 16
    assert "Guadalajara" in d.CATALOGO["Jalisco"]


def test_estado_y_municipio_se_comparan_sin_acentos_ni_mayusculas():
    assert d.estado_canonico("  yucatan ") == "Yucatán"
    assert d.municipio_canonico("jalisco", "ZAPOPAN") == "Zapopan"
    assert d.municipio_canonico("Ciudad de Mexico", "alvaro obregon") == "Álvaro Obregón"
    assert d.estado_canonico("Narnia") is None
    assert d.municipio_canonico("Jalisco", "Mérida") is None  # es de Yucatán


# ---------- Validación ----------

def test_domicilio_vacio_es_valido_y_no_compone_direccion():
    r = d.validar_domicilio({})
    assert r["direccion"] is None and r["cp"] is None and r["calle"] is None
    assert d.validar_domicilio({"calle": "  ", "cp": 0})["direccion"] is None


def test_domicilio_completo_se_normaliza_y_compone_direccion():
    r = d.validar_domicilio(completo(estado="jalisco", municipio="guadalajara", calle="  Av.   Juárez "))
    assert (r["estado"], r["municipio"], r["calle"]) == ("Jalisco", "Guadalajara", "Av. Juárez")
    assert r["direccion"] == "Av. Juárez 100 Int. 4, Col. Centro, CP 44100, Guadalajara, Jalisco"


def test_interior_es_opcional():
    r = d.validar_domicilio(completo(interior=None))
    assert r["interior"] is None
    assert r["direccion"] == "Av. Juárez 100, Col. Centro, CP 44100, Guadalajara, Jalisco"


def test_numero_acepta_sin_numero():
    assert d.validar_domicilio(completo(numero="S/N"))["numero"] == "S/N"


@pytest.mark.parametrize("campo,etiqueta", [
    ("calle", "calle"), ("numero", "número"), ("colonia", "colonia"),
    ("cp", "código postal"), ("municipio", "municipio"), ("estado", "estado"),
])
def test_con_algo_capturado_faltar_un_obligatorio_da_error(campo, etiqueta):
    with pytest.raises(ValueError) as e:
        d.validar_domicilio(completo(**{campo: ""}))
    assert etiqueta in str(e.value) and "Completa el domicilio" in str(e.value)


def test_un_solo_campo_capturado_exige_el_resto():
    with pytest.raises(ValueError) as e:
        d.validar_domicilio({"colonia": "Centro"})
    assert "calle" in str(e.value) and "municipio" in str(e.value)


def test_cp_solo_no_exige_el_resto_del_domicilio():
    # Cliente viejo con CP fiscal pero sin domicilio nuevo: editarlo no debe bloquearse.
    assert d.validar_domicilio({"cp": "44100"})["cp"] == "44100"


@pytest.mark.parametrize("cp", ["1234", "441000", "44A00", "abcde"])
def test_cp_invalido_da_error(cp):
    with pytest.raises(ValueError, match="5 dígitos"):
        d.validar_domicilio({"cp": cp})


def test_estado_o_municipio_fuera_del_catalogo_da_error():
    with pytest.raises(ValueError, match="no es válido"):
        d.validar_domicilio(completo(estado="Narnia"))
    with pytest.raises(ValueError, match="no pertenece a Jalisco"):
        d.validar_domicilio(completo(municipio="Mérida"))


def test_limites_de_longitud():
    with pytest.raises(ValueError, match="calle"):
        d.validar_domicilio(completo(calle="x" * 151))
    with pytest.raises(ValueError, match="numero"):
        d.validar_domicilio(completo(numero="1" * 21))


# ---------- CP como texto ----------

@pytest.mark.parametrize("entrada,esperado", [
    (None, None), ("", None), (0, None), ("0", None), (True, None),
    (44100, "44100"), (1000, "01000"), ("01000", "01000"), (" 44100 ", "44100"),
])
def test_cp_a_texto(entrada, esperado):
    assert d.cp_a_texto(entrada) == esperado


def test_un_cp_entero_viejo_recupera_su_cero_inicial_al_validar():
    assert d.validar_domicilio({"cp": 1000})["cp"] == "01000"


# ---------- Migración ----------

class CursorFalso:
    def __init__(self, columnas):
        self.columnas, self.sql = columnas, []

    def execute(self, q, params=None):
        self.sql.append(" ".join(q.split()))

    def fetchall(self):
        return self.columnas


def test_migracion_agrega_columnas_y_convierte_cp_numerico():
    cur = CursorFalso([("id", "int"), ("cp", "int"), ("direccion", "varchar")])
    d.asegurar_columnas_domicilio(cur, "clientes")
    altas = [s for s in cur.sql if "ADD COLUMN" in s]
    assert len(altas) == 6 and all(s.startswith("ALTER TABLE clientes ADD COLUMN") for s in altas)
    assert "ALTER TABLE clientes MODIFY cp VARCHAR(10) NULL" in cur.sql
    assert "UPDATE clientes SET cp = NULL WHERE cp IN ('0', '')" in cur.sql
    assert "UPDATE clientes SET cp = LPAD(cp, 5, '0') WHERE cp REGEXP '^[0-9]{1,4}$'" in cur.sql


def test_migracion_es_idempotente_si_ya_esta_aplicada():
    cols = [("id", "int"), ("cp", "varchar")] + [(c, "varchar") for c in d.COLUMNAS_DOMICILIO]
    cur = CursorFalso(cols)
    d.asegurar_columnas_domicilio(cur, "prospectos")
    assert [s for s in cur.sql if s.startswith(("ALTER", "UPDATE"))] == []
