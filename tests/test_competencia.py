# Tests del Análisis de Competencia ML: lógica pura (competencia_logica.py) y endpoints de
# routers/competencia.py con el acceso a datos y los clientes de Context.dev / Anthropic falsos.
# No prueban el SQL contra un MySQL real (cupo, candado GET_LOCK, caché): eso se verifica a mano.
import json
from datetime import datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import competencia_logica as cl
import competencia_service as svc
from permisos import usuario_autenticado
from routers import competencia

D = Decimal


# ---------- URL ----------

@pytest.mark.parametrize("url", [
    "https://listado.mercadolibre.com.mx/taladro-inalambrico",
    "https://articulo.mercadolibre.com.mx/MLM-123-taladro-_JM",
    "https://www.mercadolibre.com.mx/p/MLM123",
    "https://mercadolibre.com.mx/",
    "  https://LISTADO.mercadolibre.com.mx/x#frag  ",
])
def test_url_valida(url):
    assert cl.validar_url(url).startswith("https://") and "#" not in cl.validar_url(url)


@pytest.mark.parametrize("url", [
    "", None, "   ", 123,
    "http://listado.mercadolibre.com.mx/x",
    "https://listado.mercadolibre.com/x",
    "https://mercadolibre.com.mx.evil.com/x",
    "https://evil.com/mercadolibre.com.mx",
    "https://evilmercadolibre.com.mx/x",
    "https://mercadolibre.com.mx@evil.com/x",
    "https://user:pw@listado.mercadolibre.com.mx/x",
    "https://listado.mercadolibre.com.mx:8080/x",
    "https://listado.mercadolibre.com.mx/a b",
    "https://listado.mercadolibre.com.mx/" + "a" * 600,
    "ftp://listado.mercadolibre.com.mx/x",
])
def test_url_invalida(url):
    with pytest.raises(ValueError):
        cl.validar_url(url)


# ---------- Semana ----------

def test_semana_va_de_lunes_a_lunes():
    ini, fin = cl.semana_actual(datetime(2026, 10, 28, 15, 30))   # miércoles
    assert ini == datetime(2026, 10, 26) and fin == datetime(2026, 11, 2)


def test_semana_domingo_noche_aun_es_la_semana_anterior():
    ini, _ = cl.semana_actual(datetime(2026, 11, 1, 23, 59))
    assert ini == datetime(2026, 10, 26)


def test_semana_lunes_a_medianoche_abre_semana_nueva():
    ini, fin = cl.semana_actual(datetime(2026, 11, 2, 0, 0))
    assert ini == datetime(2026, 11, 2) and fin == datetime(2026, 11, 9)


# ---------- Context.dev / texto ----------

@pytest.mark.parametrize("cuerpo,esperado", [
    ({"markdown": "# hola"}, "# hola"),
    ({"markdown": {"requested": True, "success": True, "data": " # real "}}, "# real"),
    ({"markdown": {"success": False, "data": "x", "error_code": "E"}}, ""),
    ({"markdown": {"success": True}}, ""),
    ({"data": {"markdown": "# hola"}}, "# hola"),
    ({"content": " texto "}, "texto"),
    ({"result": {"data": {"content": "x"}}}, "x"),
    ("crudo", "crudo"),
    ({"markdown": ""}, ""),
    ({}, ""), (None, ""), ([1], ""),
])
def test_extraer_markdown(cuerpo, esperado):
    assert cl.extraer_markdown(cuerpo) == esperado


def test_parece_bloqueado():
    assert cl.parece_bloqueado("Por favor resuelve el captcha")
    assert not cl.parece_bloqueado("captcha " + "x" * 2000)   # página larga: contenido real


def test_truncar():
    assert cl.truncar("abc", 10) == ("abc", False)
    texto, cortado = cl.truncar("linea1\nlinea2\nlinea3\n" * 100, 500)
    assert cortado and len(texto) <= 500


# ---------- JSON del LLM ----------

def test_extraer_json_tolera_bloque_y_texto_alrededor():
    assert cl.extraer_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert cl.extraer_json('Claro: {"a": 1} listo') == {"a": 1}


@pytest.mark.parametrize("texto", ["", None, "sin json", "{roto", "[1, 2]"])
def test_extraer_json_invalido(texto):
    with pytest.raises(ValueError):
        cl.extraer_json(texto)


def test_validar_resultado_normaliza_y_descarta():
    r = cl.validar_resultado({
        "resumen": "  Mercado competido ",
        "nivel_oferta": "altisima",
        "competidores": [
            {"titulo": "A", "precio": "$1,299.50", "calificacion": 4.5, "envio_gratis": True},
            {"titulo": "B", "precio": -5, "calificacion": 9, "envio_gratis": "si"},
            {"titulo": "", "precio": 100},
            "basura",
            {"titulo": "C", "precio": True},
        ],
        "fortalezas": ["f", "", None, 3], "debilidades": "no es lista", "insights": ["i"],
    })
    assert r["resumen"] == "Mercado competido" and r["nivel_oferta"] is None
    assert [c["titulo"] for c in r["competidores"]] == ["A", "B", "C"]
    a, b, c = r["competidores"]
    assert a["precio"] == D("1299.50") and a["calificacion"] == D("4.5") and a["envio_gratis"] is True
    assert b["precio"] is None and b["calificacion"] is None and b["envio_gratis"] is None
    assert c["precio"] is None            # bool no es precio
    assert r["fortalezas"] == ["f", "3"] and r["debilidades"] == []


def test_validar_resultado_vacio_falla():
    with pytest.raises(ValueError):
        cl.validar_resultado({"competidores": [], "resumen": ""})


def test_validar_resultado_topa_competidores():
    r = cl.validar_resultado({"competidores": [{"titulo": f"p{i}", "precio": 10} for i in range(100)]})
    assert len(r["competidores"]) == cl.MAX_COMPETIDORES


# ---------- Estadísticas y precio sugerido ----------

def _c(*precios):
    return [{"titulo": f"p{i}", "precio": p} for i, p in enumerate(precios)]


def test_estadisticas_ignoran_nulos_y_no_positivos():
    e = cl.estadisticas_precios(_c(D(100), D(200), None, D(0), D(600)))
    assert e == {"n": 3, "promedio": D("300.00"), "mediana": D("200.00"), "minimo": D("100.00"), "maximo": D("600.00")}


def test_estadisticas_sin_precios_es_none_no_cero():
    assert cl.estadisticas_precios(_c(None, None)) is None
    assert cl.estadisticas_precios([]) is None


def test_precio_piso_alcanza_el_margen_pedido():
    s = cl.precio_sugerido(D(100), 30, 0, None)
    assert s["precio_piso"] == D("165.71") and s["precio_sugerido"] == D("165.71")
    assert abs(s["margen_real_sugerido"] - D(30)) < D("0.05")
    assert s["viable"] is None and "No se encontraron precios" in s["aviso"]


def test_comision_sube_el_piso():
    sin = cl.precio_sugerido(D(100), 30, 0, None)["precio_piso"]
    con = cl.precio_sugerido(D(100), 30, 15, None)["precio_piso"]
    assert con > sin


def test_piso_bajo_la_mediana_sugiere_la_mediana():
    est = cl.estadisticas_precios(_c(D(180), D(200), D(220)))
    s = cl.precio_sugerido(D(100), 30, 0, est)
    assert s["viable"] is True and s["precio_sugerido"] == D("200.00")
    assert s["margen_real_sugerido"] > D(30) and s["aviso"] is None


def test_piso_sobre_la_mediana_no_es_viable_y_avisa_el_margen_a_mediana():
    est = cl.estadisticas_precios(_c(D(140), D(150), D(160)))
    s = cl.precio_sugerido(D(100), 30, 0, est)
    assert s["viable"] is False and s["precio_sugerido"] == s["precio_piso"] == D("165.71")
    assert s["margen_a_mediana"] < D(30) and "por encima de la mediana" in s["aviso"]


@pytest.mark.parametrize("costo", [None, 0, D(0), -5, "abc"])
def test_sin_costo_no_hay_sugerencia_ni_cero(costo):
    s = cl.precio_sugerido(costo, 30, 0, None)
    assert s["precio_sugerido"] is None and s["precio_piso"] is None and "costo" in s["aviso"]


@pytest.mark.parametrize("margen,comision", [(0, 0), (-1, 0), (96, 0), (None, 0), (30, 61), (30, -1), (30, "x"), (60, 35)])
def test_margen_y_comision_fuera_de_rango(margen, comision):
    with pytest.raises(ValueError):
        cl.validar_margen_comision(margen, comision)


def test_comision_vacia_cuenta_como_cero():
    assert cl.validar_margen_comision(30, None) == (D(30), D(0))
    assert cl.validar_margen_comision(30, 0) == (D(30), D(0))


# ---------- Endpoints ----------

BUENA = {
    "resumen": "Mercado competido",
    "nivel_oferta": "alta",
    "competidores": [{"titulo": "A", "precio": 180}, {"titulo": "B", "precio": 200}, {"titulo": "C", "precio": 220},
                     {"titulo": "D", "precio": None}],
    "fortalezas": ["Envío full"], "debilidades": ["Sin garantía"], "insights": ["Bajar precio"],
}
URL = "https://listado.mercadolibre.com.mx/taladro"


class Escenario:
    """Datos y llamadas falsos: registra qué se pidió y guarda lo que 'escribiría' en la tabla."""
    def __init__(self):
        self.llamadas, self.filas, self.errores, self.bitacora = [], {}, [], []
        self.cache, self.costo, self.cupo_agotado = None, D("100"), False
        self.markdown_error = self.llm_error = None
        self.texto_llm = json.dumps(BUENA)
        self.historial_pedido = None


def _fila(id_, usuario, resultado, **extra):
    f = {"id": id_, "usuario": usuario, "url": URL, "enfoque": "completo", "sku": None, "margen_objetivo": None,
         "comision_pct": None, "estado": "ok", "precio_sugerido": None, "precio_promedio": None, "truncado": 0,
         "origen_cache_id": None, "error_origen": None, "error_detalle": None,
         "creado_en": datetime(2026, 10, 28, 10), "finalizado_en": datetime(2026, 10, 28, 10, 1),
         "resultado": json.dumps(resultado) if resultado is not None else None}
    f.update(extra)
    return f


@pytest.fixture
def esc(monkeypatch):
    e = Escenario()

    def costo_de_sku(sku):
        e.llamadas.append(("costo", sku))
        if sku == "NOEXISTE":
            raise HTTPException(status_code=404, detail="El SKU no existe")
        return e.costo

    def buscar_cache(usuario, url, enfoque, ahora):
        e.llamadas.append(("cache", url))
        return e.cache

    def reservar_cupo(usuario, url, enfoque, sku, margen, comision, ahora):
        e.llamadas.append(("reservar", usuario))
        if e.cupo_agotado:
            raise HTTPException(status_code=429, detail="Ya usaste tu análisis de esta semana")
        return 7

    def guardar_cache(usuario, url, enfoque, sku, margen, comision, origen, resultado, truncado, ahora):
        e.llamadas.append(("guardar_cache", origen))
        e.filas[8] = _fila(8, usuario, resultado, sku=sku, origen_cache_id=origen, margen_objetivo=margen)
        return 8

    def finalizar_ok(id_, resultado, truncado, ahora):
        e.llamadas.append(("ok", id_))
        e.filas[id_] = _fila(id_, "ana", resultado, truncado=int(truncado), precio_sugerido=competencia._precio_sugerido(resultado),
                             precio_promedio=competencia._precio_promedio(resultado))

    def finalizar_error(id_, origen, detalle):
        e.errores.append((id_, origen))

    async def extraer(url):
        e.llamadas.append(("context", url))
        if e.markdown_error:
            raise e.markdown_error
        return "# resultados\n" * 10

    async def evaluar(system, usuario):
        e.llamadas.append(("anthropic", None))
        if e.llm_error:
            raise e.llm_error
        return e.texto_llm

    monkeypatch.setattr(competencia, "costo_de_sku", costo_de_sku)
    monkeypatch.setattr(competencia, "buscar_cache", buscar_cache)
    monkeypatch.setattr(competencia, "reservar_cupo", reservar_cupo)
    monkeypatch.setattr(competencia, "guardar_cache", guardar_cache)
    monkeypatch.setattr(competencia, "finalizar_ok", finalizar_ok)
    monkeypatch.setattr(competencia, "finalizar_error", finalizar_error)
    monkeypatch.setattr(competencia, "leer_analisis", lambda id_: e.filas.get(id_))
    monkeypatch.setattr(competencia, "obtener_cupo", lambda u, a: {"limite": 1, "usados": 1, "restantes": 0, "ilimitado": False})
    monkeypatch.setattr(competencia, "listar_historial", lambda u, l: (setattr(e, "historial_pedido", u), [])[1])
    monkeypatch.setattr(competencia.mov_reg, "registrar_movimiento", lambda *a, **k: e.bitacora.append(a))
    monkeypatch.setattr(svc, "extraer_markdown", extraer)
    monkeypatch.setattr(svc, "evaluar", evaluar)
    return e


def _cliente(usuario="ana"):
    app = FastAPI()
    app.include_router(competencia.router)
    app.dependency_overrides[usuario_autenticado] = lambda: usuario
    return TestClient(app)


def _post(c, **extra):
    return c.post("/competencia/analizar", json={"url": URL, **extra})


def _no_llamo_apis(e):
    return not any(l[0] in ("context", "anthropic", "reservar") for l in e.llamadas)


def test_url_invalida_da_422_y_no_gasta_nada(esc):
    r = _post(_cliente(), url="https://evil.com/x")
    assert r.status_code == 422 and "mercadolibre.com.mx" in r.json()["detail"]
    assert esc.llamadas == []


def test_sku_sin_margen_y_margen_sin_sku_dan_422(esc):
    assert _post(_cliente(), sku="TAL-1").status_code == 422
    assert _post(_cliente(), margen_objetivo=30).status_code == 422
    assert _no_llamo_apis(esc)


def test_margen_fuera_de_rango_422_y_sku_inexistente_404(esc):
    assert _post(_cliente(), sku="TAL-1", margen_objetivo=99).status_code == 422
    assert _post(_cliente(), sku="NOEXISTE", margen_objetivo=30).status_code == 404
    assert _no_llamo_apis(esc)


def test_analisis_exitoso_calcula_los_numeros_en_python(esc):
    r = _post(_cliente(), sku="TAL-1", margen_objetivo=30, comision_pct=0)
    assert r.status_code == 200
    body = r.json()
    res = body["resultado"]
    assert res["estadisticas"] == {"n": 3, "promedio": 200.0, "mediana": 200.0, "minimo": 180.0, "maximo": 220.0}
    assert res["sugerencia"]["precio_sugerido"] == 200.0 and res["sugerencia"]["viable"] is True
    assert res["costo_base"] == 100.0 and body["reutilizado"] is False and body["precio_promedio"] == 200.0
    assert [l[0] for l in esc.llamadas if l[0] in ("context", "anthropic", "ok")] == ["context", "anthropic", "ok"]
    assert esc.errores == [] and len(esc.bitacora) == 1 and esc.bitacora[0][2] == "Análisis de Competencia"


def test_sin_sku_no_hay_sugerencia(esc):
    res = _post(_cliente()).json()["resultado"]
    assert res["sugerencia"] is None and res["costo_base"] is None and res["estadisticas"]["promedio"] == 200.0


def test_sku_sin_costo_registrado_avisa_sin_precio(esc):
    esc.costo = None
    res = _post(_cliente(), sku="TAL-1", margen_objetivo=30).json()["resultado"]
    assert res["sugerencia"]["precio_sugerido"] is None and "costo" in res["sugerencia"]["aviso"]


def test_falla_de_context_da_502_y_no_gasta_cupo(esc):
    esc.markdown_error = svc.CompetenciaError("context", "Context.dev no pudo leer la página", 502)
    r = _post(_cliente())
    assert r.status_code == 502 and "Context.dev" in r.json()["detail"]
    assert esc.errores == [(7, "context")] and not any(l[0] in ("anthropic", "ok") for l in esc.llamadas)


def test_limite_de_anthropic_da_429_con_retry_after(esc):
    esc.llm_error = svc.CompetenciaError("anthropic", "La IA alcanzó su límite de uso", 429, retry_after=30)
    r = _post(_cliente())
    assert r.status_code == 429 and r.headers["retry-after"] == "30"
    assert esc.errores == [(7, "anthropic")]


def test_json_invalido_del_modelo_da_502(esc):
    esc.texto_llm = "no soy json"
    r = _post(_cliente())
    assert r.status_code == 502 and esc.errores == [(7, "anthropic")]
    assert "tracebac" not in r.text.lower()


def test_cupo_agotado_da_429_y_no_llama_a_las_apis(esc):
    esc.cupo_agotado = True
    r = _post(_cliente())
    assert r.status_code == 429 and "semana" in r.json()["detail"]
    assert not any(l[0] in ("context", "anthropic") for l in esc.llamadas)


def test_cache_reutiliza_sin_apis_ni_cupo_y_recalcula_el_precio(esc):
    esc.cache = {"origen": 3, "resultado": {**BUENA, "competidores": [{"titulo": "A", "precio": 150.0}, {"titulo": "B", "precio": 160.0}]},
                 "truncado": False}
    r = _post(_cliente(), sku="TAL-1", margen_objetivo=30)
    body = r.json()
    assert r.status_code == 200 and body["reutilizado"] is True
    assert not any(l[0] in ("context", "anthropic", "reservar") for l in esc.llamadas)
    assert ("guardar_cache", 3) in esc.llamadas
    assert body["resultado"]["sugerencia"]["viable"] is False       # piso 165.71 > mediana 155


def test_gerencia_ve_todo_el_historial_y_el_resto_solo_lo_suyo(esc):
    assert _cliente("gerencia").get("/competencia/historial").status_code == 200
    assert esc.historial_pedido is None
    _cliente("gerencia").get("/competencia/historial?usuario_filtro=ana")
    assert esc.historial_pedido == "ana"
    _cliente("ana").get("/competencia/historial")
    assert esc.historial_pedido == "ana"
    assert _cliente("ana").get("/competencia/historial?usuario_filtro=beto").status_code == 403


def test_detalle_dueno_gerencia_ajeno_e_inexistente(esc):
    esc.filas[5] = _fila(5, "ana", {"resumen": "x"})
    assert _cliente("ana").get("/competencia/5").json()["resultado"] == {"resumen": "x"}
    assert _cliente("gerencia").get("/competencia/5").status_code == 200
    assert _cliente("beto").get("/competencia/5").status_code == 403
    assert _cliente("ana").get("/competencia/99").status_code == 404


def test_cupo_es_una_ruta_fija_no_un_id(esc):
    r = _cliente().get("/competencia/cupo")
    assert r.status_code == 200 and r.json()["restantes"] == 0


# ---------- Servicio (httpx falso) ----------

import asyncio
import httpx


class _Resp:
    def __init__(self, status=200, cuerpo=None, headers=None):
        self.status_code, self._cuerpo, self.headers = status, cuerpo, headers or {}
        self.text = json.dumps(cuerpo) if cuerpo is not None else ""

    def json(self):
        if self._cuerpo is None:
            raise ValueError("sin json")
        return self._cuerpo


def _httpx_falso(monkeypatch, respuesta=None, excepcion=None, capturar=None):
    class Cliente:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None):
            if capturar is not None:
                capturar.update(url=url, headers=headers, json=json)
            if excepcion:
                raise excepcion
            return respuesta
    monkeypatch.setattr(svc.httpx, "AsyncClient", Cliente)


def _correr(coro):
    return asyncio.run(coro)


def _falla(coro):
    with pytest.raises(svc.CompetenciaError) as e:
        _correr(coro)
    return e.value


def test_servicio_sin_keys_da_503(monkeypatch):
    monkeypatch.delenv("CONTEXT_DEV_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _falla(svc.extraer_markdown(URL)).status == 503
    assert _falla(svc.evaluar("s", "u")).status == 503


def test_context_manda_url_y_formato_markdown(monkeypatch):
    monkeypatch.setenv("CONTEXT_DEV_API_KEY", "ctx-key")
    for var in ("CONTEXT_DEV_SCRAPE_URL", "CONTEXT_DEV_AUTH_HEADER", "CONTEXT_DEV_AUTH_SCHEME", "CONTEXT_DEV_HEADERS_JSON",
                "CONTEXT_DEV_MAX_AGE_MS", "CONTEXT_DEV_TIMEOUT_MS"):
        monkeypatch.delenv(var, raising=False)   # el .env local puede traer valores propios
    visto = {}
    _httpx_falso(monkeypatch, _Resp(200, {"markdown": {"requested": True, "success": True, "data": "# ok"}}), capturar=visto)
    assert _correr(svc.extraer_markdown(URL)) == "# ok"
    assert visto["url"] == "https://api.context.dev/v1/web/scrape"
    assert visto["json"] == cl.cuerpo_scrape(URL)
    assert visto["headers"]["Authorization"] == "Bearer ctx-key"


@pytest.mark.parametrize("resp,exc,status", [
    (_Resp(429, {}, {"retry-after": "45"}), None, 429),
    (_Resp(401, {}), None, 503),
    (_Resp(500, {}), None, 502),
    (_Resp(200, {"otra": "cosa"}), None, 502),
    (_Resp(200, {"markdown": {"success": False, "error_code": "BLOCKED", "message": "x"}}), None, 502),
    (_Resp(404, {"error_code": "ENDPOINT_NOT_FOUND"}), None, 502),
    (_Resp(200, {"markdown": {"success": False, "error_code": "WEBSITE_BLOCKED", "message": "blocked"}}), None, 502),
    (_Resp(200, {"markdown": "Por favor resuelve el captcha"}), None, 502),
    (None, httpx.ReadTimeout("t"), 504),
    (None, httpx.ConnectError("c"), 502),
])
def test_context_mapea_errores(monkeypatch, resp, exc, status):
    monkeypatch.setenv("CONTEXT_DEV_API_KEY", "k")
    _httpx_falso(monkeypatch, resp, exc)
    err = _falla(svc.extraer_markdown(URL))
    assert err.status == status and "traceback" not in str(err).lower()


def test_anthropic_arma_la_peticion_y_lee_el_texto(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "ant-key")
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    visto = {}
    _httpx_falso(monkeypatch, _Resp(200, {"stop_reason": "end_turn", "content": [{"type": "text", "text": "{"}, {"type": "text", "text": "}"}]}),
                 capturar=visto)
    assert _correr(svc.evaluar("sistema", "usuario")) == "{}"
    assert visto["url"] == "https://api.anthropic.com/v1/messages"
    assert visto["headers"]["x-api-key"] == "ant-key" and visto["headers"]["anthropic-version"] == "2023-06-01"
    assert visto["json"]["model"] == "claude-sonnet-5-5" and visto["json"]["system"] == "sistema"
    assert visto["json"]["messages"] == [{"role": "user", "content": "usuario"}]


@pytest.mark.parametrize("resp,exc,status,retry", [
    (_Resp(429, {}, {"retry-after": "30"}), None, 429, 30),
    (_Resp(529, {}), None, 503, None),
    (_Resp(401, {}), None, 503, None),
    (_Resp(400, {}), None, 502, None),
    (_Resp(200, {"stop_reason": "max_tokens", "content": [{"type": "text", "text": "{"}]}), None, 502, None),
    (None, httpx.ReadTimeout("t"), 504, None),
])
def test_anthropic_mapea_errores(monkeypatch, resp, exc, status, retry):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    _httpx_falso(monkeypatch, resp, exc)
    err = _falla(svc.evaluar("s", "u"))
    assert err.status == status and err.retry_after == retry


# ---------- Cuerpo de /v1/web/scrape ----------

PERMITIDAS = {"url", "formats", "sharedParams", "maxAgeMs", "timeoutOpts"}   # ScrapeRequest no admite extras


def test_cuerpo_scrape_solo_trae_propiedades_validas_y_pide_mexico():
    c = cl.cuerpo_scrape("https://listado.mercadolibre.com.mx/taladro")
    assert set(c) <= PERMITIDAS and c["formats"] == {"markdown": True}
    assert c["sharedParams"]["country"] == "MX" and "headers" not in c["sharedParams"]
    assert c["timeoutOpts"]["behavior"] == "return-partial" and c["timeoutOpts"]["milliseconds"] >= 5000
    for invalida in ("format", "proxy_type", "render_js", "wait_for", "cookies"):   # del consejo pegado: darían 400
        assert invalida not in c and invalida not in c["sharedParams"]


def test_cuerpo_scrape_espera_el_contenedor_segun_el_tipo_de_pagina():
    assert cl.cuerpo_scrape("https://listado.mercadolibre.com.mx/x")["sharedParams"]["waitFor"] == cl.SELECTOR_LISTADO
    assert cl.cuerpo_scrape("https://articulo.mercadolibre.com.mx/MLM-1")["sharedParams"]["waitFor"] == cl.SELECTOR_PUBLICACION
    mixto = cl.cuerpo_scrape("https://www.mercadolibre.com.mx/p/MLM1")["sharedParams"]["waitFor"]
    assert cl.SELECTOR_LISTADO in mixto and cl.SELECTOR_PUBLICACION in mixto


def test_cuerpo_scrape_con_cabeceras_opcionales(monkeypatch):
    monkeypatch.setenv("CONTEXT_DEV_HEADERS_JSON", json.dumps({"Cookie": "a=b", "Accept-Language": "es-MX"}))
    assert svc._headers_extra() == {"Cookie": "a=b", "Accept-Language": "es-MX"}
    monkeypatch.setenv("CONTEXT_DEV_HEADERS_JSON", "no es json")
    assert svc._headers_extra() is None
    monkeypatch.setenv("CONTEXT_DEV_HEADERS_JSON", "[1]")
    assert svc._headers_extra() is None
    monkeypatch.delenv("CONTEXT_DEV_HEADERS_JSON")
    assert svc._headers_extra() is None
    assert cl.cuerpo_scrape(URL, headers={"Cookie": "a=b"})["sharedParams"]["headers"] == {"Cookie": "a=b"}


def test_pantalla_de_verificacion_devuelta_como_parcial_se_detecta(monkeypatch):
    monkeypatch.setenv("CONTEXT_DEV_API_KEY", "k")
    _httpx_falso(monkeypatch, _Resp(200, {"markdown": {"success": True, "data": "Verifica que eres humano: resuelve el captcha"}}))
    err = _falla(svc.extraer_markdown(URL))
    assert err.status == 502 and "bloque" in str(err).lower()


# ---------- Respuesta cortada por max_tokens ----------

def _secuencia(monkeypatch, respuestas, enviados):
    pendientes = list(respuestas)

    class Cliente:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None):
            enviados.append(json)
            return pendientes.pop(0)
    monkeypatch.setattr(svc.httpx, "AsyncClient", Cliente)


def _msg(texto, stop):
    return _Resp(200, {"stop_reason": stop, "content": [{"type": "text", "text": texto}]})


def test_si_se_corta_por_max_tokens_reintenta_una_vez_con_el_doble(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_MAX_TOKENS", "4096")
    enviados = []
    _secuencia(monkeypatch, [_msg("{", "max_tokens"), _msg('{"ok": 1}', "end_turn")], enviados)
    assert _correr(svc.evaluar("s", "u")) == '{"ok": 1}'
    assert [e["max_tokens"] for e in enviados] == [4096, 8192]


def test_si_el_reintento_tambien_se_corta_da_502_con_mensaje_claro(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("ANTHROPIC_MAX_TOKENS", "4096")
    enviados = []
    _secuencia(monkeypatch, [_msg("{", "max_tokens"), _msg("{", "max_tokens")], enviados)
    err = _falla(svc.evaluar("s", "u"))
    assert err.status == 502 and "demasiado larga" in str(err) and len(enviados) == 2


def test_tope_de_tokens_por_defecto_es_8192_y_el_reintento_no_pasa_de_16000(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.delenv("ANTHROPIC_MAX_TOKENS", raising=False)
    enviados = []
    _secuencia(monkeypatch, [_msg("{", "max_tokens"), _msg("{}", "end_turn")], enviados)
    _correr(svc.evaluar("s", "u"))
    assert [e["max_tokens"] for e in enviados] == [8192, 16000]
