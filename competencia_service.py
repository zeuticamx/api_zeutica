# Clientes de Context.dev (extracción a Markdown) y de la API de Anthropic (evaluación) para el Análisis de
# Competencia ML. Solo IO: validar, calcular y armar prompts vive en competencia_logica.py.
#
# Contrato de Context.dev (según su OpenAPI, https://www.context.dev/openapi.json): POST /v1/web/scrape con
# {url, formats: {markdown: true}}; el Markdown llega en `markdown.data` (y `markdown.success` dice si
# funcionó). OJO: `/v1/scrape` NO existe (da 404 ENDPOINT_NOT_FOUND). La cabecera de auth (Bearer, estándar)
# no se pudo confirmar en el spec y se ajusta por env sin tocar código:
#   CONTEXT_DEV_API_KEY, CONTEXT_DEV_SCRAPE_URL, CONTEXT_DEV_AUTH_HEADER, CONTEXT_DEV_AUTH_SCHEME,
#   ANTHROPIC_API_KEY, ANTHROPIC_MODEL, ANTHROPIC_MAX_TOKENS.
import json
import os
from typing import Optional

import httpx

import competencia_logica as cl

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MODELO_POR_DEFECTO = "claude-sonnet-5-5"


class CompetenciaError(Exception):
    """Falla en un paso del análisis. `origen` = context|anthropic|config, `status` = código HTTP con el que
    responde el router, `mensaje` = texto seguro para el cliente (el detalle técnico va al log)."""

    def __init__(self, origen: str, mensaje: str, status: int = 502, retry_after: Optional[int] = None):
        super().__init__(mensaje)
        self.origen, self.status, self.retry_after = origen, status, retry_after


def _env(nombre: str, defecto: str = "") -> str:
    return (os.getenv(nombre) or defecto).strip()


def max_caracteres() -> int:
    try:
        return max(5000, int(_env("COMPETENCIA_MAX_CHARS", "60000")))
    except ValueError:
        return 60000


def _entero(nombre: str, defecto: int) -> int:
    try:
        return max(0, int(_env(nombre, str(defecto))))
    except ValueError:
        return defecto


def _headers_extra() -> Optional[dict]:
    """Cabeceras opcionales hacia Mercado Libre (p. ej. {"Cookie": "...", "Accept-Language": "es-MX"}), desde
    CONTEXT_DEV_HEADERS_JSON. Solo si Mercado Libre sigue pidiendo verificación; JSON inválido se ignora."""
    crudo = _env("CONTEXT_DEV_HEADERS_JSON")
    if not crudo:
        return None
    try:
        datos = json.loads(crudo)
    except ValueError:
        print("[competencia] CONTEXT_DEV_HEADERS_JSON no es JSON válido: se ignora")
        return None
    if not isinstance(datos, dict):
        return None
    limpio = {str(k): str(v) for k, v in datos.items() if isinstance(v, (str, int)) and len(str(v)) <= 8192}
    return limpio or None


def _retry_after(r: httpx.Response) -> Optional[int]:
    try:
        return max(1, int(r.headers.get("retry-after", "")))
    except ValueError:
        return None


async def extraer_markdown(url: str) -> str:
    """Paso A: contenido limpio de la página en Markdown vía Context.dev."""
    clave = _env("CONTEXT_DEV_API_KEY")
    if not clave:
        raise CompetenciaError("config", "Falta configurar la extracción de datos (Context.dev)", 503)
    cabecera, esquema = _env("CONTEXT_DEV_AUTH_HEADER", "Authorization"), _env("CONTEXT_DEV_AUTH_SCHEME", "Bearer")
    headers = {cabecera: f"{esquema} {clave}".strip(), "Content-Type": "application/json"}
    cuerpo_envio = cl.cuerpo_scrape(url, max_age_ms=_entero("CONTEXT_DEV_MAX_AGE_MS", 3_600_000),
                                    timeout_ms=_entero("CONTEXT_DEV_TIMEOUT_MS", 75_000), headers=_headers_extra())
    try:
        # El timeout de httpx debe superar al de Context.dev (timeoutOpts) para recibir su respuesta parcial.
        async with httpx.AsyncClient(timeout=httpx.Timeout(110.0, connect=10.0)) as client:
            r = await client.post(_env("CONTEXT_DEV_SCRAPE_URL", "https://api.context.dev/v1/web/scrape"),
                                  headers=headers, json=cuerpo_envio)
    except httpx.TimeoutException:
        raise CompetenciaError("context", "Context.dev tardó demasiado en leer la página", 504)
    except httpx.HTTPError as err:
        print(f"[competencia] Context.dev inalcanzable: {err}")
        raise CompetenciaError("context", "No se pudo conectar con Context.dev", 502)
    if r.status_code == 429:
        raise CompetenciaError("context", "Context.dev alcanzó su límite de uso; intenta más tarde", 429, _retry_after(r))
    if r.status_code in (401, 403):
        print(f"[competencia] Context.dev rechazó las credenciales ({r.status_code})")
        raise CompetenciaError("config", "Context.dev rechazó las credenciales configuradas", 503)
    if r.status_code >= 400:
        print(f"[competencia] Context.dev HTTP {r.status_code}: {r.text[:300]}")
        raise CompetenciaError("context", f"Context.dev no pudo leer la página (HTTP {r.status_code})", 502)
    try:
        cuerpo = r.json()
    except ValueError:
        cuerpo = r.text
    motivo = cl.error_scrape(cuerpo)
    if motivo:
        print(f"[competencia] Context.dev no pudo extraer la página: {motivo}")
        if "BLOCK" in motivo.upper():
            raise CompetenciaError("context", "Mercado Libre bloqueó la lectura de la página (verificación anti-bots). Intenta de nuevo en unos minutos", 502)
        raise CompetenciaError("context", "Context.dev no pudo leer esa página (puede estar bloqueada o no disponible)", 502)
    markdown = cl.extraer_markdown(cuerpo)
    if not markdown:
        print(f"[competencia] Context.dev sin Markdown reconocible: {str(r.text)[:300]}")
        raise CompetenciaError("context", "Context.dev no devolvió contenido de la página", 502)
    if cl.parece_bloqueado(markdown):
        raise CompetenciaError("context", "Mercado Libre bloqueó la lectura (verificación anti-bots). Intenta con otra URL", 502)
    return markdown


async def evaluar(system: str, usuario: str) -> str:
    """Paso B: evaluación con Claude. Devuelve el texto de la respuesta (el JSON lo valida la lógica)."""
    clave = _env("ANTHROPIC_API_KEY")
    if not clave:
        raise CompetenciaError("config", "Falta configurar la evaluación con IA (Anthropic)", 503)
    try:
        max_tokens = int(_env("ANTHROPIC_MAX_TOKENS", "8192"))
    except ValueError:
        max_tokens = 8192
    headers = {"x-api-key": clave, "anthropic-version": ANTHROPIC_VERSION, "content-type": "application/json"}
    # Si el JSON se corta por el tope de tokens se reintenta UNA vez con el doble (máximo 16000).
    for intento in (1, 2):
        datos = await _llamar(headers, {"model": _env("ANTHROPIC_MODEL", MODELO_POR_DEFECTO), "max_tokens": max_tokens,
                                        "system": system, "messages": [{"role": "user", "content": usuario}]})
        if datos.get("stop_reason") != "max_tokens":
            return "".join(b.get("text", "") for b in datos.get("content", []) if b.get("type") == "text")
        print(f"[competencia] Respuesta cortada por max_tokens={max_tokens} (intento {intento})")
        if intento == 2 or max_tokens >= 16000:
            break
        max_tokens = min(max_tokens * 2, 16000)
    raise CompetenciaError("anthropic", "La respuesta de la IA fue demasiado larga y quedó incompleta; intenta de nuevo", 502)


async def _llamar(headers: dict, cuerpo: dict) -> dict:
    """Una llamada a /v1/messages; devuelve el JSON de la respuesta o lanza CompetenciaError."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            r = await client.post(ANTHROPIC_URL, headers=headers, json=cuerpo)
    except httpx.TimeoutException:
        raise CompetenciaError("anthropic", "La evaluación con IA tardó demasiado", 504)
    except httpx.HTTPError as err:
        print(f"[competencia] Anthropic inalcanzable: {err}")
        raise CompetenciaError("anthropic", "No se pudo conectar con la IA", 502)
    if r.status_code == 429:
        raise CompetenciaError("anthropic", "La IA alcanzó su límite de uso; intenta en unos minutos", 429, _retry_after(r))
    if r.status_code in (401, 403):
        print(f"[competencia] Anthropic rechazó la clave ({r.status_code})")
        raise CompetenciaError("config", "Anthropic rechazó la clave configurada", 503)
    if r.status_code in (500, 502, 503, 529):
        raise CompetenciaError("anthropic", "La IA está saturada o no disponible; intenta más tarde", 503, _retry_after(r))
    if r.status_code >= 400:
        print(f"[competencia] Anthropic HTTP {r.status_code}: {r.text[:300]}")
        raise CompetenciaError("anthropic", f"La IA rechazó la solicitud (HTTP {r.status_code})", 502)
    try:
        datos = r.json()
        if not isinstance(datos, dict):
            raise ValueError
    except ValueError:
        raise CompetenciaError("anthropic", "Respuesta inesperada de la IA", 502)
    return datos
