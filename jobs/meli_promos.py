# Promociones MeLi (migración del workflow n8n de la página Promociones Meli).
# On-demand (la página lo pide al abrir) + caché corta en memoria.
# Respuesta única {message, ofertas_meli} para que la página no cambie.
import time
from datetime import datetime, timedelta, timezone

import httpx

_CACHE_TTL_SEG = 600
_CACHE = {"ts": 0.0, "data": None}


class MeliPromosError(Exception):
    pass


def resumir_ofertas(items: list, ahora=None) -> tuple:
    """(resumen, proximas_a_vencer). Puerto del JS de n8n (ventana 6 días)."""
    ahora = ahora or datetime.now(timezone.utc)
    limite = ahora + timedelta(days=6, hours=23, minutes=59, seconds=59)
    resumen, proximas = [], []
    for data in items:
        resumen.append({
            "id": data.get("id"), "name": data.get("name"), "status": data.get("status"),
            "price": data.get("price"), "original_price": data.get("original_price"),
            "meli_percentage": data.get("meli_percentage") or 0,
            "seller_percentage": data.get("seller_percentage") or 0,
            "suggested_discounted_price": data.get("suggested_discounted_price"),
        })
        finish = data.get("finish_date")
        if not finish:
            continue
        try:
            fin = datetime.fromisoformat(str(finish).replace("Z", "+00:00"))
            if fin.tzinfo is None:
                fin = fin.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ahora <= fin <= limite:
            proximas.append({
                "id": data.get("id"), "status": data.get("status"),
                "meli_percentage": data.get("meli_percentage") or 0,
                "seller_percentage": data.get("seller_percentage") or 0,
            })
    return resumen, proximas


async def obtener_promos(forzar: bool = False) -> dict:
    """{message, ofertas_meli}. Usa caché de 10 min salvo forzar=True."""
    ahora_ts = time.monotonic()
    if not forzar and _CACHE["data"] is not None and ahora_ts - _CACHE["ts"] < _CACHE_TTL_SEG:
        return _CACHE["data"]
    from jobs.meli_ventas import _meli_token, _get, MELI_API
    import asyncio as _aio
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            token = await _meli_token(client)
            me = await _get(client, f"{MELI_API}/users/me", token)
            seller = me.get("id")
            if not seller:
                raise MeliPromosError(f"/users/me sin id: {me}")
            ids: list = []
            for page in range(20):
                data = await _get(client, f"{MELI_API}/users/{seller}/items/search", token,
                                  {"status": "active", "limit": 100, "offset": page * 100})
                results = data.get("results") or []
                ids += [str(r) for r in results]
                if len(results) < 100:
                    break
            sem = _aio.Semaphore(5)

            async def una(item_id: str):
                async with sem:
                    try:
                        return await _get(client, f"{MELI_API}/seller-promotions/items/{item_id}",
                                          token, {"app_version": "v2"})
                    except Exception as err:
                        print(f"Promo {item_id}: {err}")
                        return None

            detalles = await _aio.gather(*[una(i) for i in ids])
    except MeliPromosError:
        raise
    except Exception as err:
        raise MeliPromosError(f"MeLi promociones: {err}")
    items: list = []
    for d in detalles:
        if d is None:
            continue
        items += d if isinstance(d, list) else [d]
    resumen, proximas = resumir_ofertas(items)
    if proximas:
        message = f"{len(proximas)} promociones próximas a vencer en 6 días."
    else:
        message = "No hay ítems próximos a vencer en los próximos 6 días."
    salida = {"message": message, "ofertas_meli": resumen,
              "proximas_a_vencer": proximas}
    _CACHE["ts"] = ahora_ts
    _CACHE["data"] = salida
    return salida
