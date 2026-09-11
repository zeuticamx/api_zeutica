# Servicio de integracion con Skydropx Pro (cotizacion de envios y generacion de guias).
#
# Modulo aislado: no toca base de datos ni ningun otro servicio del sistema.
# Todo lo que sabe hacer es hablar con Skydropx y traducir sus errores a algo
# que el router pueda convertir en HTTPException, igual que whatsapp_service.py.
#
# Dos cosas que Skydropx exige y que aqui se resuelven de una vez por todas:
#
#   1. OAuth client_credentials: el token dura 2 horas. Pedir uno nuevo en cada
#      request seria gastar la mitad de las llamadas en autenticarse, asi que se
#      cachea en memoria del proceso y se renueva solo cuando esta por vencer.
#      Funciona porque en produccion corremos un solo worker de uvicorn (mismo
#      supuesto documentado en routers/sofi_notificaciones.py). Si algun dia se
#      levantan varios workers, cada uno tendra su propio token: no rompe nada,
#      solo pide un token por worker.
#
#   2. Rate limit de 2 solicitudes por segundo: se serializa la salida dejando
#      medio segundo entre llamadas. Es preferible tardar tantito a comerse un
#      429 en medio de la generacion de una guia.
import asyncio
import base64
import hashlib
import hmac
import os
import time
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv

load_dotenv() 

# Sandbox por defecto: una guia generada en produccion es real y se cobra.
# Para produccion, cambiar SKYDROPX_BASE_URL a https://pro.skydropx.com en el .env.
BASE_URL_POR_DEFECTO = "https://sb-pro.skydropx.com"

RUTA_TOKEN = "/api/v1/oauth/token"
RUTA_COTIZACIONES = "/api/v1/quotations"
RUTA_ENVIOS = "/api/v1/shipments"
RUTA_RASTREO = "/api/v1/shipments/tracking"

# Generar una guia puede tardar: el carrier responde por debajo de Skydropx.
TIMEOUT = httpx.Timeout(30.0)

# El token vive 7200s. Se renueva 5 min antes para no quedar a medias en una
# llamada que salio justo en el filo del vencimiento.
_MARGEN_RENOVACION_SEGUNDOS = 300
_DURACION_TOKEN_POR_DEFECTO = 7200

# Skydropx tolera 2 req/s. Medio segundo entre llamadas nos deja justo debajo.
_INTERVALO_MINIMO_SEGUNDOS = 0.5

_cache_token: Dict[str, Any] = {"token": None, "expira": 0.0}
_candado_token = asyncio.Lock()   # evita que dos requests simultaneos pidan dos tokens
_candado_ritmo = asyncio.Lock()   # serializa la salida para respetar el rate limit
_ultima_llamada = 0.0

# Cabecera donde Skydropx manda la firma HMAC del webhook. Es configurable porque
# el nombre depende de como quedo dado de alta el webhook en el panel.
_CABECERA_FIRMA_POR_DEFECTO = "X-Skydropx-Signature"


class SkydropxServiceError(Exception):
    """
    Falla al hablar con Skydropx. `detalle` es el texto que Skydropx devolvio,
    que casi siempre explica el rechazo concreto ("postal_code is invalid",
    "rate_id not found", etc.). `status` es el codigo que debe salir al panel.
    """

    def __init__(self, detalle: str, status: int = 502, codigo: Optional[str] = None):
        super().__init__(detalle)
        self.detalle = detalle
        self.status = status
        self.codigo = codigo


def _config() -> Dict[str, str]:
    """
    Credenciales desde el .env. SKYDROP_API_KEY / SKYDROP_API_SECRET son las que
    ya estaban cargadas antes de este modulo; se dejan como respaldo para no
    obligar a recapturarlas, pero SKYDROPX_CLIENT_ID / SKYDROPX_CLIENT_SECRET
    mandan si tienen valor.
    """
    client_id = (os.getenv("SKYDROPX_CLIENT_ID") or os.getenv("SKYDROP_API_KEY") or "").strip()
    client_secret = (os.getenv("SKYDROPX_CLIENT_SECRET") or os.getenv("SKYDROP_API_SECRET") or "").strip()
    base_url = (os.getenv("SKYDROPX_BASE_URL") or BASE_URL_POR_DEFECTO).strip().rstrip("/")
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "base_url": base_url,
        "webhook_secret": (os.getenv("SKYDROPX_WEBHOOK_SECRET") or "").strip(),
        "cabecera_firma": (os.getenv("SKYDROPX_WEBHOOK_HEADER") or _CABECERA_FIRMA_POR_DEFECTO).strip(),
    }


def _exigir(cfg: Dict[str, str], *claves: str) -> None:
    """Falla claro y temprano si faltan credenciales, en vez de mandar un 401 de Skydropx."""
    nombres = {
        "client_id": "SKYDROPX_CLIENT_ID (o SKYDROP_API_KEY)",
        "client_secret": "SKYDROPX_CLIENT_SECRET (o SKYDROP_API_SECRET)",
        "webhook_secret": "SKYDROPX_WEBHOOK_SECRET",
    }
    faltantes = [nombres.get(c, c) for c in claves if not cfg.get(c)]
    if faltantes:
        raise SkydropxServiceError(
            f"Falta configurar {', '.join(faltantes)} en el .env de api_zeutica1.",
            status=503,
        )


def configuracion_lista() -> Dict[str, Any]:
    """Que esta configurado. No expone ningun valor secreto, solo si existe."""
    cfg = _config()
    return {
        "client_id": bool(cfg["client_id"]),
        "client_secret": bool(cfg["client_secret"]),
        "webhook_secret": bool(cfg["webhook_secret"]),
        "base_url": cfg["base_url"],
        "ambiente": "sandbox" if "sb-pro" in cfg["base_url"] else "produccion",
        "token_en_cache": bool(_cache_token["token"]) and time.time() < _cache_token["expira"],
    }


def _leer_error(respuesta: httpx.Response) -> SkydropxServiceError:
    """
    Traduce el error de Skydropx a algo mostrable. El cuerpo puede venir como
    {"errors": [...]}, {"error": "..."} o {"message": "..."} segun el endpoint,
    asi que se revisan las tres formas antes de caer al texto crudo.
    """
    try:
        cuerpo = respuesta.json()
    except Exception:
        cuerpo = None

    mensaje = None
    codigo = None
    if isinstance(cuerpo, dict):
        errores = cuerpo.get("errors")
        if isinstance(errores, list) and errores:
            partes = []
            for err in errores:
                if isinstance(err, dict):
                    codigo = codigo or err.get("code")
                    detalle = err.get("detail") or err.get("message") or err.get("title")
                    campo = (err.get("source") or {}).get("pointer") if isinstance(err.get("source"), dict) else None
                    partes.append(f"{campo}: {detalle}" if campo and detalle else str(detalle or err))
                else:
                    partes.append(str(err))
            mensaje = " | ".join(p for p in partes if p)
        elif isinstance(errores, dict):
            # Formato de validacion tipo Rails: {"postal_code": ["is invalid"]}
            mensaje = " | ".join(f"{campo}: {', '.join(map(str, val))}" if isinstance(val, list) else f"{campo}: {val}"
                                 for campo, val in errores.items())
        mensaje = mensaje or cuerpo.get("error_description") or cuerpo.get("message") or cuerpo.get("error")

    mensaje = mensaje or respuesta.text.strip() or "Skydropx rechazo la peticion"

    # 4xx es problema del dato que mandamos o de las credenciales: se pasa tal cual
    # para que el panel muestre que corregir. 5xx se reporta como 502 porque el que
    # fallo fue el proveedor, no nosotros.
    status = respuesta.status_code if 400 <= respuesta.status_code < 500 else 502

    if respuesta.status_code == 401:
        mensaje = f"Skydropx rechazo las credenciales (401): {mensaje}"
    elif respuesta.status_code == 404:
        mensaje = f"Skydropx no encontro el recurso (404): {mensaje}"
    elif respuesta.status_code == 422:
        mensaje = f"Skydropx rechazo los datos del envio (422): {mensaje}"
    elif respuesta.status_code == 429:
        espera = respuesta.headers.get("Retry-After")
        sufijo = f" Reintenta en {espera}s." if espera else " Reintenta en unos segundos."
        mensaje = f"Se excedio el limite de 2 solicitudes por segundo de Skydropx.{sufijo} ({mensaje})"

    return SkydropxServiceError(str(mensaje), status=status, codigo=str(codigo) if codigo else None)


async def _esperar_turno() -> None:
    """
    Deja al menos medio segundo entre llamadas salientes para no pasarse del
    limite de 2 req/s. Serializa: si dos requests coinciden, el segundo espera.
    """
    global _ultima_llamada
    async with _candado_ritmo:
        transcurrido = time.monotonic() - _ultima_llamada
        if transcurrido < _INTERVALO_MINIMO_SEGUNDOS:
            await asyncio.sleep(_INTERVALO_MINIMO_SEGUNDOS - transcurrido)
        _ultima_llamada = time.monotonic()


def invalidar_token() -> None:
    """Tira el token cacheado. Se usa cuando Skydropx contesta 401 con un token que creiamos vigente."""
    _cache_token["token"] = None
    _cache_token["expira"] = 0.0


async def obtener_token(forzar: bool = False) -> str:
    """
    Bearer token vigente. Reusa el cacheado mientras le queden mas de 5 minutos
    de vida; si no, pide uno nuevo. El candado evita que varios requests
    simultaneos disparen varias solicitudes de token a la vez.
    """
    ahora = time.time()
    if not forzar and _cache_token["token"] and ahora < _cache_token["expira"]:
        return _cache_token["token"]

    async with _candado_token:
        # Otro request pudo haberlo renovado mientras esperabamos el candado.
        ahora = time.time()
        if not forzar and _cache_token["token"] and ahora < _cache_token["expira"]:
            return _cache_token["token"]

        cfg = _config()
        _exigir(cfg, "client_id", "client_secret")

        await _esperar_turno()
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
                respuesta = await cliente.post(
                    f"{cfg['base_url']}{RUTA_TOKEN}",
                    json={
                        "grant_type": "client_credentials",
                        "client_id": cfg["client_id"],
                        "client_secret": cfg["client_secret"],
                    },
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                )
        except httpx.HTTPError as err:
            raise SkydropxServiceError(f"No se pudo contactar a Skydropx: {err}", status=504)

        if respuesta.status_code >= 400:
            raise _leer_error(respuesta)

        try:
            cuerpo = respuesta.json()
        except Exception:
            raise SkydropxServiceError("Skydropx devolvio un token con formato inesperado.", status=502)

        token = cuerpo.get("access_token")
        if not token:
            raise SkydropxServiceError("Skydropx no devolvio access_token.", status=502)

        duracion = cuerpo.get("expires_in") or _DURACION_TOKEN_POR_DEFECTO
        try:
            duracion = int(duracion)
        except (TypeError, ValueError):
            duracion = _DURACION_TOKEN_POR_DEFECTO

        _cache_token["token"] = token
        # created_at no se usa: el reloj que importa es el nuestro, desde que llego la respuesta.
        _cache_token["expira"] = time.time() + max(duracion - _MARGEN_RENOVACION_SEGUNDOS, 60)
        return token


async def _pedir(
    metodo: str,
    ruta: str,
    *,
    json: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    _reintento: bool = False,
) -> Dict[str, Any]:
    """
    Llamada autenticada a Skydropx. Centraliza token, rate limit y traduccion de
    errores para que los metodos de abajo solo se ocupen del cuerpo.

    Si Skydropx contesta 401 con un token que creiamos vigente (revocado, o
    credenciales rotadas), se tira el cache y se reintenta una sola vez.
    """
    cfg = _config()
    token = await obtener_token()

    await _esperar_turno()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
            respuesta = await cliente.request(
                metodo,
                f"{cfg['base_url']}{ruta}",
                json=json,
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
    except httpx.HTTPError as err:
        raise SkydropxServiceError(f"No se pudo contactar a Skydropx: {err}", status=504)

    if respuesta.status_code == 401 and not _reintento:
        invalidar_token()
        await obtener_token(forzar=True)
        return await _pedir(metodo, ruta, json=json, params=params, _reintento=True)

    if respuesta.status_code >= 400:
        raise _leer_error(respuesta)

    if not respuesta.content:
        return {}

    try:
        cuerpo = respuesta.json()
    except Exception:
        raise SkydropxServiceError("Skydropx devolvio una respuesta que no es JSON.", status=502)

    # Skydropx siempre devuelve objeto en estos endpoints; si llegara una lista se
    # envuelve para que el contrato del router no cambie de forma.
    return cuerpo if isinstance(cuerpo, dict) else {"datos": cuerpo}


# ─────────────────────────────────────────────────────────────────────────────
# Flujo de envios
# ─────────────────────────────────────────────────────────────────────────────

async def crear_cotizacion(
    address_from: Dict[str, Any],
    address_to: Dict[str, Any],
    parcels: List[Dict[str, Any]],
    extras: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Crea la cotizacion. Skydropx la procesa de forma asincrona: la respuesta trae
    el id y a veces las `rates` todavia vacias o en estado "creating", por eso
    existe obtener_cotizacion() para volver a consultar.

    `extras` pasa tal cual campos opcionales del contrato (requested_carriers,
    consignment_note, etc.) sin que este modulo tenga que conocerlos.
    """
    cuerpo: Dict[str, Any] = {
        "quotation": {
        "address_from": address_from,
        "address_to": address_to,
        "parcels": parcels,
        }
    }
    if extras:
        cuerpo.update(extras)
    return await _pedir("POST", RUTA_COTIZACIONES, json=cuerpo)


async def obtener_cotizacion(cotizacion_id: str) -> Dict[str, Any]:
    """Consulta una cotizacion ya creada para leer su arreglo de `rates` y los rate_id."""
    return await _pedir("GET", f"{RUTA_COTIZACIONES}/{cotizacion_id}")


async def esperar_tarifas(
    cotizacion_id: str,
    intentos: int = 4,
    espera_segundos: float = 1.0,
) -> Dict[str, Any]:
    """
    Reconsulta la cotizacion hasta que aparezcan tarifas o se agoten los intentos.

    Skydropx cotiza contra varios carriers en paralelo, asi que la primera lectura
    suele venir sin `rates`. Devuelve la ultima respuesta aunque venga vacia: que
    no haya tarifas para un codigo postal es un resultado valido, no un error.
    """
    resultado: Dict[str, Any] = {}
    for numero in range(max(intentos, 1)):
        resultado = await obtener_cotizacion(cotizacion_id)
        if extraer_tarifas(resultado):
            return resultado
        if numero < intentos - 1:
            await asyncio.sleep(espera_segundos)
    return resultado


def extraer_tarifas(cotizacion: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Saca el arreglo de `rates` sin importar si viene en la raiz, bajo `data` o
    bajo `attributes` (JSON:API). Aisla al panel de la forma exacta de Skydropx.
    """
    if not isinstance(cotizacion, dict):
        return []

    candidatos = [cotizacion]
    datos = cotizacion.get("data")
    if isinstance(datos, dict):
        candidatos.append(datos)
        atributos = datos.get("attributes")
        if isinstance(atributos, dict):
            candidatos.append(atributos)
    atributos_raiz = cotizacion.get("attributes")
    if isinstance(atributos_raiz, dict):
        candidatos.append(atributos_raiz)
    incluidos = cotizacion.get("included")

    for candidato in candidatos:
        tarifas = candidato.get("rates")
        if isinstance(tarifas, list) and tarifas:
            return tarifas

    # JSON:API mete las rates como recursos relacionados en `included`.
    if isinstance(incluidos, list):
        tarifas = [item for item in incluidos
                   if isinstance(item, dict) and str(item.get("type", "")).startswith("rate")]
        if tarifas:
            return tarifas

    return []


async def crear_envio(
    rate_id: str,
    address_from: Optional[Dict[str, Any]] = None,
    address_to: Optional[Dict[str, Any]] = None,
    parcels: Optional[List[Dict[str, Any]]] = None,
    extras: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Genera la guia a partir de un rate_id de la cotizacion.

    OJO: en produccion esto cuesta dinero y no se puede deshacer desde aqui.
    Las direcciones son opcionales porque el rate ya trae las de la cotizacion;
    se mandan solo si el llamador quiere sobreescribir datos de contacto.
    """
    envio: Dict[str, Any] = {"rate_id": rate_id}
    if address_from:
        envio["address_from"] = address_from
    if address_to:
        envio["address_to"] = address_to
    if parcels:
        envio["parcels"] = parcels
    if extras:
        envio.update(extras)
    return await _pedir("POST", RUTA_ENVIOS, json={"shipment": envio})


async def rastrear(tracking_number: str, carrier_name: str) -> Dict[str, Any]:
    """Estado actual de una guia. Skydropx pide numero de rastreo y nombre del carrier."""
    return await _pedir(
        "GET",
        RUTA_RASTREO,
        params={"tracking_number": tracking_number, "carrier_name": carrier_name},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Webhook
# ─────────────────────────────────────────────────────────────────────────────

def verificar_firma_webhook(cuerpo_crudo: bytes, firma_recibida: Optional[str]) -> None:
    """
    Valida el HMAC-SHA512 del webhook sobre el cuerpo CRUDO (sin reserializar:
    cualquier cambio de espacios o de orden de llaves rompe la firma).

    Lanza SkydropxServiceError si no cuadra. Acepta la firma en hex o en base64,
    con o sin prefijo "sha512=", porque el formato varia segun como quedo dado de
    alta el webhook.
    """
    cfg = _config()
    _exigir(cfg, "webhook_secret")

    if not firma_recibida:
        raise SkydropxServiceError(
            f"Falta la cabecera de firma {cfg['cabecera_firma']} en el webhook.",
            status=401,
        )

    firma = firma_recibida.strip()
    if "=" in firma and firma.lower().startswith("sha512"):
        firma = firma.split("=", 1)[1].strip()

    digest = hmac.new(cfg["webhook_secret"].encode("utf-8"), cuerpo_crudo, hashlib.sha512)
    esperado_hex = digest.hexdigest()
    esperado_b64 = base64.b64encode(digest.digest()).decode("ascii")

    # compare_digest: comparacion en tiempo constante, no con == .
    if not (hmac.compare_digest(firma, esperado_hex) or hmac.compare_digest(firma, esperado_b64)):
        raise SkydropxServiceError("Firma del webhook de Skydropx invalida.", status=401)
