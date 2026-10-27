# Cliente de Mercado Ads (Product Ads, "PADS") para el Motor Zentor. Solo IO: nada de decidir aquí
# (el diagnóstico vive en zentor_logica.py). Reutiliza el refresh token de MeLi de jobs/meli_ventas.py.
#
# OJO al activar los scopes de publicidad en la app de MeLi: las rutas de abajo son las de la
# documentación de Product Ads que se conocían al escribir esto (Api-Version 1 para advertisers y 2 para
# ads). Si MeLi las cambió, se corrigen aquí o por variable de entorno, sin tocar el resto:
#   ZENTOR_ADVERTISERS_PATH, ZENTOR_ADS_SEARCH_PATH, ZENTOR_ADS_STATUS_PATH,
#   ZENTOR_ADVERTISER_ID y ZENTOR_SITE_ID (fijan el anunciante y se salta el descubrimiento).
# Un 401/403/404 del descubrimiento (sin scope o Product Ads sin activar) se reporta como `sin_acceso`.
import os
from datetime import date

import httpx

MELI_API = "https://api.mercadolibre.com"
PAGINA = 50
MAX_PAGINAS = 40  # 2,000 anuncios como máximo por consulta

METRICAS = "clicks,prints,cost,cvr,acos,roas,units_quantity,total_amount,direct_amount,indirect_amount"

_ANUNCIANTE: dict = {}  # caché en memoria {"advertiser_id", "site_id"}


class MercadoAdsError(Exception):
    """Falla al hablar con Mercado Ads. `sin_acceso` = no hay conexión usable (token, scope o Product Ads
    sin activar): el router responde 503 en vez de 502 para que el panel lo explique."""

    def __init__(self, mensaje: str, sin_acceso: bool = False):
        super().__init__(mensaje)
        self.sin_acceso = sin_acceso


def _env(nombre: str, defecto: str = "") -> str:
    return (os.getenv(nombre) or defecto).strip()


def _ruta_busqueda() -> str:
    return _env("ZENTOR_ADS_SEARCH_PATH", "/advertising/{site_id}/advertisers/{advertiser_id}/product_ads/ads/search")


def _ruta_estado() -> str:
    return _env("ZENTOR_ADS_STATUS_PATH", "/advertising/{site_id}/advertisers/{advertiser_id}/product_ads/ads/{item_id}")


async def _token(client: httpx.AsyncClient) -> str:
    from jobs.meli_ventas import _meli_token, MeliError
    try:
        return await _meli_token(client)
    except MeliError as err:
        raise MercadoAdsError(f"Token de MeLi: {err}", sin_acceso=True)


async def _pedir(client, metodo: str, ruta: str, token: str, version: str,
                 params: dict | None = None, cuerpo: dict | None = None) -> dict:
    try:
        r = await client.request(
            metodo, f"{MELI_API}{ruta}", params=params, json=cuerpo, timeout=30.0,
            headers={"Authorization": f"Bearer {token}", "Api-Version": version})
    except httpx.HTTPError as err:
        raise MercadoAdsError(f"{metodo} {ruta}: {err}")
    if r.status_code in (401, 403):
        raise MercadoAdsError(f"MeLi rechazó el acceso a Mercado Ads ({r.status_code}): falta el scope de publicidad", sin_acceso=True)
    if r.status_code >= 400:
        raise MercadoAdsError(f"{metodo} {ruta}: HTTP {r.status_code} {r.text[:200]}")
    try:
        return r.json() if r.content else {}
    except ValueError:
        raise MercadoAdsError(f"{metodo} {ruta}: respuesta no es JSON")


async def _anunciante(client, token: str) -> tuple[str, str]:
    """(advertiser_id, site_id). Variables de entorno primero; si no, se descubre y se guarda en memoria."""
    fijo, sitio = _env("ZENTOR_ADVERTISER_ID"), _env("ZENTOR_SITE_ID", "MLM")
    if fijo:
        return fijo, sitio
    if _ANUNCIANTE:
        return _ANUNCIANTE["advertiser_id"], _ANUNCIANTE["site_id"]
    try:
        datos = await _pedir(client, "GET", _env("ZENTOR_ADVERTISERS_PATH", "/advertising/advertisers"),
                             token, "1", params={"product_id": "PADS"})
    except MercadoAdsError as err:
        if "HTTP 404" in str(err):  # 404 = «No permissions found»: Product Ads sin activar
            raise MercadoAdsError("La cuenta no tiene Product Ads activado en Mercado Libre", sin_acceso=True)
        raise
    lista = datos.get("advertisers") or []
    if not lista:
        raise MercadoAdsError("La cuenta no tiene un anunciante de Product Ads", sin_acceso=True)
    _ANUNCIANTE.update({"advertiser_id": str(lista[0].get("advertiser_id")), "site_id": str(lista[0].get("site_id") or sitio)})
    return _ANUNCIANTE["advertiser_id"], _ANUNCIANTE["site_id"]


async def listar_anuncios(desde: date, hasta: date) -> list:
    """Anuncios (crudos, tal como los manda MeLi) con sus métricas del rango. Pagina hasta MAX_PAGINAS."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        token = await _token(client)
        anunciante, sitio = await _anunciante(client, token)
        ruta = _ruta_busqueda().format(site_id=sitio, advertiser_id=anunciante)
        filas: list = []
        for pagina in range(MAX_PAGINAS):
            datos = await _pedir(client, "GET", ruta, token, "2", params={
                "date_from": desde.isoformat(), "date_to": hasta.isoformat(), "metrics": METRICAS,
                "limit": PAGINA, "offset": pagina * PAGINA})
            resultados = datos.get("results") or []
            filas += resultados
            total = (datos.get("paging") or {}).get("total")
            if len(resultados) < PAGINA or (isinstance(total, int) and len(filas) >= total):
                break
        return filas


async def cambiar_estado(item_id: str, estado: str) -> None:
    """Pausa (`paused`) o activa (`active`) un anuncio en Product Ads."""
    if estado not in ("paused", "active"):
        raise ValueError("estado inválido")
    async with httpx.AsyncClient(timeout=30.0) as client:
        token = await _token(client)
        anunciante, sitio = await _anunciante(client, token)
        ruta = _ruta_estado().format(site_id=sitio, advertiser_id=anunciante, item_id=item_id)
        await _pedir(client, "PUT", ruta, token, "2", cuerpo={"status": estado})
