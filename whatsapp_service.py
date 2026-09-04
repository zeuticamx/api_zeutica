# Servicio de integracion con Meta WhatsApp Cloud API (plantillas de mensaje).
#
# Dos cosas hace este modulo:
#   1. Listar las plantillas ya aprobadas en la WABA (GET /{WABA_ID}/message_templates)
#   2. Enviar una plantilla a un numero (POST /{PHONE_NUMBER_ID}/messages)
#
# Meta es la fuente de verdad de las plantillas: aqui no se guarda ningun catalogo
# local, para no quedar desfasados cuando alguien edite o apruebe una en Meta.
import hashlib
import hmac
import os
import re
import time
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

GRAPH_BASE = "https://graph.facebook.com"

# Las plantillas cambian poco (aprobarlas en Meta tarda horas) y la vista las pide
# en cada montaje. Cache en memoria del proceso para no pegarle al Graph por gusto.
_CACHE_TTL_SEGUNDOS = 300
_cache_plantillas: Dict[str, Any] = {"datos": None, "expira": 0.0}

# Meta pagina de 25 en 25 por defecto; se pide el maximo y se sigue el cursor.
_LIMITE_POR_PAGINA = 200
_MAX_PAGINAS = 10  # tope de seguridad para no quedarse en un bucle de cursores

TIMEOUT = httpx.Timeout(20.0)


class WhatsAppServiceError(Exception):
    """
    Falla al hablar con Meta. `detalle` es el texto que Meta devolvio (lo mas util
    para el usuario: "template name does not exist", "24h window", etc.).
    """

    def __init__(self, detalle: str, status: int = 502, codigo: Optional[int] = None):
        super().__init__(detalle)
        self.detalle = detalle
        self.status = status
        self.codigo = codigo


def _config() -> Dict[str, str]:
    return {
        "token": (os.getenv("META_WA_TOKEN") or "").strip(),
        "phone_number_id": (os.getenv("META_WA_PHONE_NUMBER_ID") or "").strip(),
        "waba_id": (os.getenv("META_WA_BUSINESS_ACCOUNT_ID") or "").strip(),
        "version": (os.getenv("META_GRAPH_VERSION") or "v21.0").strip(),
        "codigo_pais": (os.getenv("META_WA_CODIGO_PAIS") or "52").strip(),
        "app_secret": (os.getenv("META_APP_SECRET") or "").strip(),
    }


def _exigir(cfg: Dict[str, str], *claves: str) -> None:
    """Falla claro y temprano si al .env le faltan credenciales, en vez de mandar un 401 de Meta."""
    faltantes = [c for c in claves if not cfg.get(c)]
    if faltantes:
        nombres = {
            "token": "META_WA_TOKEN",
            "phone_number_id": "META_WA_PHONE_NUMBER_ID",
            "waba_id": "META_WA_BUSINESS_ACCOUNT_ID",
        }
        pendientes = ", ".join(nombres.get(c, c) for c in faltantes)
        raise WhatsAppServiceError(
            f"Falta configurar {pendientes} en el .env de api_zeutica1.",
            status=503,
        )


def _params_auth(cfg: Dict[str, str]) -> Dict[str, str]:
    """appsecret_proof solo si la App lo exige (META_APP_SECRET con valor)."""
    if not cfg["app_secret"]:
        return {}
    proof = hmac.new(
        cfg["app_secret"].encode("utf-8"),
        cfg["token"].encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {"appsecret_proof": proof}


def _leer_error(respuesta: httpx.Response) -> WhatsAppServiceError:
    """
    Traduce el error de Graph a algo mostrable. Meta devuelve
    {"error": {"message", "code", "error_data": {"details"}}} y `details` casi
    siempre es la frase concreta que explica el rechazo.
    """
    try:
        cuerpo = respuesta.json()
    except Exception:
        cuerpo = None

    error = (cuerpo or {}).get("error") or {}
    detalles = (error.get("error_data") or {}).get("details")
    mensaje = detalles or error.get("message") or respuesta.text.strip() or "Meta rechazo la peticion"
    # 4xx de Meta se pasan tal cual (es un problema del dato enviado o del token);
    # 5xx se reportan como 502 porque el que fallo fue el proveedor, no nosotros.
    status = respuesta.status_code if 400 <= respuesta.status_code < 500 else 502
    return WhatsAppServiceError(str(mensaje), status=status, codigo=error.get("code"))


def normalizar_telefono(telefono: str) -> str:
    """
    Deja solo digitos y antepone la lada del pais cuando el numero viene local.
    Meta espera el numero completo en formato internacional, sin '+' ni separadores.
    """
    digitos = re.sub(r"\D", "", telefono or "")
    if not digitos:
        raise WhatsAppServiceError("El numero de WhatsApp viene vacio.", status=422)

    codigo = re.sub(r"\D", "", _config()["codigo_pais"]) or "52"
    if len(digitos) == 10:  # numero nacional capturado sin lada
        digitos = f"{codigo}{digitos}"

    if len(digitos) < 10 or len(digitos) > 15:  # E.164 topa en 15 digitos
        raise WhatsAppServiceError(
            f"El numero '{telefono}' no parece valido (quedaron {len(digitos)} digitos).",
            status=422,
        )
    return digitos


def _resumir_plantilla(plantilla: Dict[str, Any]) -> Dict[str, Any]:
    """
    Deja la plantilla lista para el panel: se conservan los `components` crudos de
    Meta (el frontend arma la vista previa con ellos) y se agrega el conteo de
    variables de encabezado y cuerpo, que es lo que decide cuantos campos pintar.
    """
    componentes = plantilla.get("components") or []
    variables_encabezado = 0
    variables_cuerpo = 0
    tipo_encabezado = None

    for comp in componentes:
        tipo = (comp.get("type") or "").upper()
        if tipo == "HEADER":
            tipo_encabezado = (comp.get("format") or "TEXT").upper()
            if tipo_encabezado == "TEXT":
                variables_encabezado = len(set(re.findall(r"\{\{(\d+)\}\}", comp.get("text") or "")))
        elif tipo == "BODY":
            variables_cuerpo = len(set(re.findall(r"\{\{(\d+)\}\}", comp.get("text") or "")))

    return {
        "nombre": plantilla.get("name"),
        "idioma": plantilla.get("language"),
        "categoria": plantilla.get("category"),
        "estado": plantilla.get("status"),
        "id": plantilla.get("id"),
        "componentes": componentes,
        "variables_encabezado": variables_encabezado,
        "variables_cuerpo": variables_cuerpo,
        "tipo_encabezado": tipo_encabezado,
    }


async def listar_plantillas(forzar: bool = False) -> List[Dict[str, Any]]:
    """
    Plantillas APPROVED de la WABA. Solo las aprobadas: mandar una en revision o
    rechazada siempre devuelve error de Meta, no tiene caso ofrecerla en el panel.
    """
    ahora = time.time()
    if not forzar and _cache_plantillas["datos"] is not None and ahora < _cache_plantillas["expira"]:
        return _cache_plantillas["datos"]

    cfg = _config()
    _exigir(cfg, "token", "waba_id")

    url = f"{GRAPH_BASE}/{cfg['version']}/{cfg['waba_id']}/message_templates"
    params = {
        "limit": _LIMITE_POR_PAGINA,
        "fields": "name,status,category,language,components",
        **_params_auth(cfg),
    }
    cabeceras = {"Authorization": f"Bearer {cfg['token']}"}

    recolectadas: List[Dict[str, Any]] = []
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
            siguiente: Optional[str] = url
            paginas = 0
            while siguiente and paginas < _MAX_PAGINAS:
                # La URL de `next` ya trae sus propios query params firmados por Meta.
                respuesta = await cliente.get(
                    siguiente,
                    params=params if siguiente == url else None,
                    headers=cabeceras,
                )
                if respuesta.status_code >= 400:
                    raise _leer_error(respuesta)
                cuerpo = respuesta.json()
                recolectadas.extend(cuerpo.get("data") or [])
                siguiente = ((cuerpo.get("paging") or {}).get("next")) or None
                paginas += 1
    except httpx.HTTPError as err:
        raise WhatsAppServiceError(f"No se pudo contactar a Meta: {err}", status=504)

    aprobadas = [
        _resumir_plantilla(p)
        for p in recolectadas
        if (p.get("status") or "").upper() == "APPROVED"
    ]
    aprobadas.sort(key=lambda p: (p["nombre"] or "", p["idioma"] or ""))

    _cache_plantillas["datos"] = aprobadas
    _cache_plantillas["expira"] = ahora + _CACHE_TTL_SEGUNDOS
    return aprobadas


def _componentes_envio(
    variables_encabezado: List[str],
    variables_cuerpo: List[str],
    url_encabezado: Optional[str],
    tipo_encabezado: Optional[str],
    nombre_archivo: Optional[str],
) -> List[Dict[str, Any]]:
    """Arma el arreglo `components` que espera el endpoint de mensajes de Meta."""
    componentes: List[Dict[str, Any]] = []
    formato = (tipo_encabezado or "TEXT").upper()

    if url_encabezado and formato in ("IMAGE", "VIDEO", "DOCUMENT"):
        media: Dict[str, Any] = {"link": url_encabezado}
        if formato == "DOCUMENT" and nombre_archivo:
            media["filename"] = nombre_archivo
        componentes.append({
            "type": "header",
            "parameters": [{"type": formato.lower(), formato.lower(): media}],
        })
    elif variables_encabezado:
        componentes.append({
            "type": "header",
            "parameters": [{"type": "text", "text": v} for v in variables_encabezado],
        })

    if variables_cuerpo:
        componentes.append({
            "type": "body",
            "parameters": [{"type": "text", "text": v} for v in variables_cuerpo],
        })

    return componentes


async def enviar_plantilla(
    telefono: str,
    plantilla: str,
    idioma: str,
    variables_encabezado: Optional[List[str]] = None,
    variables_cuerpo: Optional[List[str]] = None,
    url_encabezado: Optional[str] = None,
    tipo_encabezado: Optional[str] = None,
    nombre_archivo: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Manda la plantilla y devuelve {destino, message_id, estado} con el id que
    asigno Meta (sirve para rastrear el mensaje en los webhooks de estado).
    """
    cfg = _config()
    _exigir(cfg, "token", "phone_number_id")

    destino = normalizar_telefono(telefono)
    componentes = _componentes_envio(
        variables_encabezado or [],
        variables_cuerpo or [],
        url_encabezado,
        tipo_encabezado,
        nombre_archivo,
    )

    payload: Dict[str, Any] = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": destino,
        "type": "template",
        "template": {"name": plantilla, "language": {"code": idioma}},
    }
    if componentes:
        payload["template"]["components"] = componentes

    url = f"{GRAPH_BASE}/{cfg['version']}/{cfg['phone_number_id']}/messages"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as cliente:
            respuesta = await cliente.post(
                url,
                json=payload,
                params=_params_auth(cfg) or None,
                headers={"Authorization": f"Bearer {cfg['token']}"},
            )
    except httpx.HTTPError as err:
        raise WhatsAppServiceError(f"No se pudo contactar a Meta: {err}", status=504)

    if respuesta.status_code >= 400:
        raise _leer_error(respuesta)

    cuerpo = respuesta.json()
    mensajes = cuerpo.get("messages") or []
    return {
        "destino": destino,
        "message_id": mensajes[0].get("id") if mensajes else None,
        "estado": (mensajes[0].get("message_status") if mensajes else None) or "accepted",
    }


def configuracion_lista() -> Dict[str, Any]:
    """Que credenciales estan puestas, para que el panel avise antes de intentar enviar."""
    cfg = _config()
    return {
        "token": bool(cfg["token"]),
        "phone_number_id": bool(cfg["phone_number_id"]),
        "waba_id": bool(cfg["waba_id"]),
        "version": cfg["version"],
        "codigo_pais": cfg["codigo_pais"],
    }
