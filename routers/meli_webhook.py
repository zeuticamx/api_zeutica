# Webhook de MeLi + MercadoPago: pagadas y cancelaciones en tiempo real.
#
# Se registra en el panel de la app MeLi (tópicos orders) y en MercadoPago
# (tópico payment) con la URL https://tu-dominio/zeutica/meli/webhook
# (root_path /zeutica + esta ruta).
#
# Reglas (misma doc que pasó el usuario):
# - Responder 200 siempre y procesar en background (si tardamos o fallamos,
#   MP reintenta).
# - El aviso solo trae topic + id: se valida por GET (payment u order) con el
#   access token antes de hacer nada.
# - Pagada: payment approved/accredited -> sincroniza (idempotente).
# - Cancelada: payment cancelled/refunded u order cancelled -> regresa stock.
# - Aviso solo por WS + tabla `notificaciones` (crear_y_notificar_todos):
#   NADA de Telegram por aquí.
import asyncio
import hashlib
import hmac
import os
import time

import httpx
from fastapi import APIRouter, Request
from dotenv import load_dotenv

load_dotenv()

router = APIRouter(tags=["/meli-webhook"])

_MELI_TOKEN_URL = "https://api.mercadolibre.com/oauth/token"

# Cooldown del sync completo: eventos seguidos comparten la corrida.
COOLDOWN_SEG = 180
_ultimo_sync = 0.0


def _get_conn():
    from jobs import meli_ventas
    return meli_ventas.get_db_connection()


def _asegurar_tabla_vistos(cursor):
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS meli_webhook_vistos (
               topic VARCHAR(20) NOT NULL, resource_id VARCHAR(64) NOT NULL,
               clase VARCHAR(20) NOT NULL, fecha DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
               PRIMARY KEY (topic, resource_id, clase))""")


def ya_avisado(topic: str, rid: str, clase: str) -> bool:
    """True si este (evento, estado) ya generó aviso. Para INSERT se usa marcar_aviso()."""
    conn = _get_conn()
    cursor = conn.cursor()
    try:
        _asegurar_tabla_vistos(cursor)
        cursor.execute("SELECT 1 FROM meli_webhook_vistos WHERE topic = %s AND resource_id = %s AND clase = %s",
                       (topic, rid, clase))
        return cursor.fetchone() is not None
    finally:
        cursor.close()
        conn.close()


def marcar_aviso(topic: str, rid: str, clase: str):
    conn = _get_conn()
    cursor = conn.cursor()
    try:
        _asegurar_tabla_vistos(cursor)
        cursor.execute("INSERT IGNORE INTO meli_webhook_vistos (topic, resource_id, clase) VALUES (%s, %s, %s)",
                       (topic, rid, clase))
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def normalizar_evento(body: dict, query: dict) -> tuple:
    """(topic, resource_id) desde body MP/nuevo, body MeLi clásico o query IPN."""
    topic = (body.get("topic") or body.get("type") or body.get("action")
             or query.get("topic") or query.get("type"))
    rid = (((body.get("data") or {}) if isinstance(body.get("data"), dict) else {}).get("id")
           or body.get("resource") or body.get("id")
           or query.get("id") or query.get("data.id"))
    if rid and "/" in str(rid):
        rid = str(rid).rstrip("/").split("/")[-1]
    t = str(topic or "").lower()
    if "payment" in t:
        t = "payment"
    elif "claim" in t:
        t = "claims"
    elif "order" in t:
        # orders y orders_v2 traen resource "/orders/{id}".
        t = "orders"
    else:
        t = None
    return (t, str(rid).strip() if rid else None)


def verificar_firma_mp(firma: str | None, data_id: str, request_id: str | None) -> bool:
    """x-signature de MercadoPago (HMAC-SHA256). Sin secreto no se puede
    verificar y se acepta (la validación real es el GET por ID)."""
    secreto = (os.getenv("MELI_WEBHOOK_SECRET") or "").strip()
    if not secreto:
        return True
    if not firma or not data_id:
        return False
    partes = dict(p.split("=", 1) for p in str(firma).split(",") if "=" in p)
    ts, v1 = partes.get("ts", ""), partes.get("v1", "")
    if not ts or not v1:
        return False
    msg = f"id:{data_id};request-id:{request_id or ''};ts:{ts};"
    calc = hmac.new(secreto.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(calc, v1)


def clasificar_pago(pago: dict) -> str:
    """pagada / cancelada / ignorar según status + status_detail."""
    estado = str(pago.get("status") or "").lower()
    detalle = str(pago.get("status_detail") or "").lower()
    if estado == "approved" and detalle in ("accredited", ""):
        return "pagada"
    if estado in ("cancelled", "refunded"):
        return "cancelada"
    return "ignorar"


def clasificar_orden(orden: dict) -> str:
    estado = str(orden.get("status") or "").lower()
    if estado == "paid":
        return "pagada"
    if estado == "cancelled" or orden.get("cancel_detail"):
        return "cancelada"
    return "ignorar"


def clasificar_claim(claim: dict) -> str:
    """abierta si requiere atención humana; lo demás se ignora (un refund
    posterior entra por payment refunded y ahí sí regresa stock)."""
    if str(claim.get("status") or "").lower() == "opened":
        return "abierta"
    return "ignorar"


def ordenes_de_pago(pago: dict) -> list:
    """Ids de orden MeLi referenciados por un pago (varias formas según API)."""
    ids = []
    orden = pago.get("order")
    if isinstance(orden, dict) and orden.get("id"):
        ids.append(str(orden["id"]))
    for clave in ("merchant_order_id", "order_id"):
        if pago.get(clave):
            ids.append(str(pago[clave]))
    return list(dict.fromkeys(ids))


async def _sincronizar_con_cooldown(motivo: str):
    """Sync 72h idempotente, sin Telegram. Coalesce si hubo uno hace poco."""
    global _ultimo_sync
    from jobs import meli_ventas
    ahora = time.monotonic()
    if ahora - _ultimo_sync < COOLDOWN_SEG:
        print(f"Webhook MeLi: sync reciente, se omite ({motivo})")
        return {"estado": "omitido_cooldown"}
    _ultimo_sync = ahora
    return await meli_ventas.run_job(dry_run=False, motivo=motivo, telegram=False)


async def _procesar(topic: str | None, rid: str | None, firma_ok: bool):
    import notificaciones_service
    from jobs import meli_ventas
    if not topic or not rid:
        print("Webhook MeLi: evento sin topic/id, se ignora")
        return
    if not firma_ok:
        print(f"Webhook MeLi: firma inválida para {topic}/{rid}, se ignora")
        return
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            token = await meli_ventas._meli_token(client)
            if topic == "payment":
                pago = await meli_ventas._get(client, f"{meli_ventas.MP_API}/v1/payments/{rid}", token)
                clase = clasificar_pago(pago)
                if clase == "pagada":
                    if ya_avisado(topic, rid, clase):
                        print(f"Webhook MeLi: pago {rid} ya avisado, se omite")
                        return
                    res = await _sincronizar_con_cooldown(f"webhook pago {rid}")
                    nuevas = ((res.get("stats") or {}).get("nuevas", 0)
                              if isinstance(res, dict) and res.get("estado") == "ok" else 0)
                    await notificaciones_service.crear_y_notificar_todos(
                        f"Venta MeLi pagada {rid}",
                        f"Pago {rid} aprobado por ${float((pago.get('transaction_amount') or 0)):,.2f}. "
                        f"Sincronizadas {nuevas} nuevas en esta corrida.", "success")
                    marcar_aviso(topic, rid, clase)
                elif clase == "cancelada":
                    if ya_avisado(topic, rid, clase):
                        print(f"Webhook MeLi: pago {rid} ya avisado, se omite")
                        return
                    total = 0
                    for oid in ordenes_de_pago(pago):
                        r = await asyncio.to_thread(
                            meli_ventas.procesar_cancelacion, oid, f"webhook pago {rid}")
                        total += r.get("piezas", 0)
                    await notificaciones_service.crear_y_notificar_todos(
                        f"MeLi pago {rid} cancelado",
                        f"Pago {rid} {pago.get('status')}. Stock regresado +{total} pzas." if total
                        else f"Pago {rid} {pago.get('status')}, sin stock que regresar.", "warn")
                    marcar_aviso(topic, rid, clase)
                else:
                    print(f"Webhook MeLi: pago {rid} en estado {pago.get('status')}, se ignora")
            elif topic == "claims":
                reclamo = await meli_ventas._get(client, f"{meli_ventas.MELI_API}/claims/{rid}", token)
                clase = clasificar_claim(reclamo)
                if clase != "abierta":
                    print(f"Webhook MeLi: reclamo {rid} en estado {reclamo.get('status')}, se ignora")
                    return
                if ya_avisado(topic, rid, clase):
                    print(f"Webhook MeLi: reclamo {rid} ya avisado, se omite")
                    return
                orden_id = (reclamo.get("order_id") or (reclamo.get("order") or {}).get("id")
                            if isinstance(reclamo.get("order"), dict) else reclamo.get("order_id"))
                motivo = (reclamo.get("reason") or reclamo.get("reason_id")
                          or reclamo.get("claim_reason") or "sin motivo")
                await notificaciones_service.crear_y_notificar_todos(
                    f"Reclamo MeLi {rid} abierto",
                    f"Reclamo {rid} en orden {orden_id or '?'}: {motivo}. Requiere atención humana.", "warn")
                marcar_aviso(topic, rid, clase)
            else:  # orders / orders_v2
                orden = await meli_ventas._get(client, f"{meli_ventas.MELI_API}/orders/{rid}", token)
                clase = clasificar_orden(orden)
                if clase == "pagada":
                    if ya_avisado(topic, rid, clase):
                        print(f"Webhook MeLi: orden {rid} ya avisada, se omite")
                        return
                    await _sincronizar_con_cooldown(f"webhook orden {rid}")
                    await notificaciones_service.crear_y_notificar_todos(
                        f"Venta MeLi {rid} pagada",
                        f"Orden {rid} pagada, sincronizada en esta corrida.", "success")
                    marcar_aviso(topic, rid, clase)
                elif clase == "cancelada":
                    if ya_avisado(topic, rid, clase):
                        print(f"Webhook MeLi: orden {rid} ya avisada, se omite")
                        return
                    r = await asyncio.to_thread(
                        meli_ventas.procesar_cancelacion, rid, "webhook orden")
                    await notificaciones_service.crear_y_notificar_todos(
                        f"Orden MeLi {rid} cancelada",
                        f"Orden {rid} cancelada. Stock regresado +{r.get('piezas', 0)} pzas." if r.get("piezas")
                        else f"Orden {rid} cancelada, sin stock que regresar.", "warn")
                    marcar_aviso(topic, rid, clase)
                else:
                    print(f"Webhook MeLi: orden {rid} en estado {orden.get('status')}, se ignora")
    except Exception as err:
        # Nunca debe tumbar nada: se loguea y el job diario lo recupera (ventana 72h).
        print(f"Webhook MeLi {topic}/{rid}: {err}")


@router.post("/meli/webhook")
async def meli_webhook(request: Request):
    """Recibe el aviso y responde 200 de inmediato; procesa en background."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    topic, rid = normalizar_evento(body, dict(request.query_params))
    firma_ok = verificar_firma_mp(request.headers.get("x-signature"),
                                  rid or "", request.headers.get("x-request-id"))
    asyncio.create_task(_procesar(topic, rid, firma_ok))
    return {"ok": True}


@router.get("/meli/oauth/callback")
async def meli_oauth_callback(code: str | None = None, redirect_uri: str | None = None):
    """Canjea el `code` de MeLi por tokens (paso manual único del OAuth).

    Flujo: abre en el navegador
    https://auth.mercadolibre.com.mx/authorization?response_type=code&client_id=APP_ID&redirect_uri=REDIRECT
    (mismo REDIRECT registrado en la app). MeLi redirige aquí con ?code=... y
    este endpoint devuelve access_token + refresh_token: copia el
    refresh_token a MELI_REFRESH_TOKEN en Easypanel.
    """
    if not code:
        return {"ok": False, "detail": "Falta ?code= (abre primero la URL de autorización de MeLi)"}
    import httpx as _hx
    client_id = (os.getenv("MELI_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("MELI_CLIENT_SECRET") or "").strip()
    redirect = (redirect_uri or os.getenv("MELI_REDIRECT_URI") or "").strip()
    if not client_id or not client_secret or not redirect:
        return {"ok": False, "detail": "Faltan MELI_CLIENT_ID / MELI_CLIENT_SECRET / MELI_REDIRECT_URI en el .env"}
    try:
        async with _hx.AsyncClient(timeout=30.0) as c:
            r = await c.post(f"{_MELI_TOKEN_URL}", data={
                "grant_type": "authorization_code", "client_id": client_id,
                "client_secret": client_secret, "code": code, "redirect_uri": redirect})
            r.raise_for_status()
            datos = r.json()
    except _hx.HTTPError as err:
        print(f"OAuth MeLi: {err}")
        return {"ok": False, "detail": f"MeLi rechazó el code: {err}"}
    print("OAuth MeLi OK: guarda el refresh_token en MELI_REFRESH_TOKEN")
    return {"ok": True, "refresh_token": datos.get("refresh_token"),
            "access_token": datos.get("access_token"), "expires_in": datos.get("expires_in"),
            "nota": "Copia refresh_token a MELI_REFRESH_TOKEN en Easypanel y redeployea"}
