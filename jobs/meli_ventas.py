# Job de ventas MeLi + MercadoPago (migración del workflow n8n MeLi).
#
# Mismo proceso uvicorn: scheduler diario 12:05 America/Mexico_City +
# disparo manual POST /jobs/meli/run (solo gerencia).
#
# Paridad con n8n: ventana 72h, orders/search paid paginado, agrupado por
# orden|sku_mod (FULL, 500_ESCFANBLA×5, SKU_N), logística Flex/Full/Colecta,
# INSERT...ON DUPLICATE, resta bodega solo no-Full, neto de MercadoPago con
# margen <15%, potenciales, resumen por chunks. Mejora vs n8n: descuento
# transaccional con guard `stock >= cantidad` (sin negativos; queda
# pendiente al próximo run).
import asyncio
import html
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

CDMX = ZoneInfo("America/Mexico_City")
MELI_API = "https://api.mercadolibre.com"
MP_API = "https://api.mercadopago.com"
MARGEN_UMBRAL = 15.0

LAST_RUN: dict = {"estado": "nunca", "detalle": None}


def _env(nombre: str, default: str = "") -> str:
    return (os.getenv(nombre) or default).strip()


def _dry_run_default() -> bool:
    return _env("MELI_JOB_DRY_RUN", "0") == "1"


def _alert_chat() -> str:
    return _env("MELI_ALERT_CHAT_ID", "630880920")


# ---------- Lógica pura (testeable) ----------

def limpiar_ordenes(paginas: list) -> list:
    """Aplana páginas de orders/search a una fila por orden+sku_mod (suma duplicados)."""
    por_clave: dict = {}
    for pagina in paginas:
        for orden in (pagina.get("results") or []):
            pagos = orden.get("payments") or []
            pago = next((p for p in pagos if p.get("status") == "approved"), pagos[0] if pagos else {})
            try:
                dt = datetime.fromisoformat(str(orden.get("date_created") or "").replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=CDMX)
                dt = dt.astimezone(CDMX)
            except ValueError:
                dt = datetime.now(CDMX)
            fecha, hora = dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")
            for oi in (orden.get("order_items") or []):
                item = oi.get("item") or {}
                codigo = str(item.get("seller_sku") or "").strip()
                es_full = codigo.endswith("FULL")
                sin_full = codigo.split("FULL")[0]
                sku_mod = sin_full.split("_")[0].strip()
                mult = 1
                if sin_full == "500_ESCFANBLA":
                    mult = 5
                elif "_" in sin_full:
                    try:
                        n = int(sin_full.split("_")[-1])
                        if n > 0:
                            mult = n
                    except ValueError:
                        pass
                cantidad = int(float(oi.get("quantity") or 0))
                unit = float(oi.get("unit_price") or 0)
                fee = float(oi.get("sale_fee") or 0)
                bruto = round(unit * cantidad, 2)
                precio = round((unit - fee) * cantidad, 2)
                clave = f"{orden.get('id')}|{sku_mod}"
                if clave in por_clave:
                    p = por_clave[clave]
                    p["cantidad"] += cantidad
                    p["cantidad_final"] += cantidad * mult
                    p["bruto"] = round(p["bruto"] + bruto, 2)
                    p["precio"] = round(p["precio"] + precio, 2)
                    if len(str(item.get("title") or "")) > len(p["producto"]):
                        p["producto"] = item.get("title") or p["producto"]
                    continue
                por_clave[clave] = {
                    "id_venta": str(orden.get("id")), "pack_id": str(orden.get("pack_id")) if orden.get("pack_id") else None,
                    "shipping_id": str((orden.get("shipping") or {}).get("id")) if (orden.get("shipping") or {}).get("id") else None,
                    "payment_id": pago.get("id"), "codigo": codigo, "sku_mod": sku_mod,
                    "producto": item.get("title") or "", "cantidad": cantidad, "multiplicador": mult,
                    "cantidad_final": cantidad * mult, "bruto": bruto, "precio": precio,
                    "fecha": fecha, "hora": hora,
                    "nombre_comprador": ((orden.get("buyer") or {}).get("nickname")) or "",
                    "otros": pago.get("payment_type") or "", "es_full": 1 if es_full else 0,
                    "es_multiplo": (not es_full) and bool(re.search(r"_[0-9]+$", codigo)),
                }
    return list(por_clave.values())


CANAL = {"self_service": "Flex", "fulfillment": "Full", "cross_docking": "Colecta",
         "xd_drop_off": "Punto de entrega", "drop_off": "Punto de entrega"}


def enriquecer_lineas(lineas: list, costos: dict, envios: dict, registro: dict) -> list:
    """Une costo + logística + estado de registro. Puerto del nodo `Datos`."""
    out = []
    for l in lineas:
        existe = l.get("sku_mod") in costos
        costo = costos.get(l.get("sku_mod"), 0) if existe else 0
        clave = f"{l['id_venta']}|{l.get('sku_mod')}"
        ya_reg = clave in registro
        ya_desc = registro.get(clave) is True
        tipo = envios.get(l["shipping_id"]) if l.get("shipping_id") else None
        if tipo:
            canal = CANAL.get(tipo, f"Otro ({tipo})")
        else:
            canal = "Full (por SKU)" if l.get("es_full") else "Sin dato"
        alerta = None
        if tipo == "fulfillment" and not l.get("es_full"):
            alerta = "MeLi lo marca como Full pero el SKU no termina en FULL: se descontó de bodega"
        elif tipo and tipo != "fulfillment" and l.get("es_full"):
            alerta = f"El SKU termina en FULL pero el envío es {canal}: NO se descontó de bodega"
        out.append({**l, "costo": costo, "sin_costo": (not existe) or costo <= 0,
                    "tipo_logistica": tipo, "canal": canal, "es_flex": tipo == "self_service",
                    "alerta_logistica": alerta, "es_nueva": not ya_reg, "ya_descontada": ya_desc,
                    "a_descontar": (not l.get("es_full")) and (not ya_desc)})
    return out


def calcular_margenes(lineas: list, pagos: dict) -> list:
    """Reparte el neto de MP por orden y marca alerta de margen <15% (solo nuevas)."""
    r2 = lambda n: round(float(n), 2)
    ordenes: dict = {}
    for l in lineas:
        ordenes.setdefault(str(l["id_venta"]), []).append(l)
    salida = []
    for id_venta, ls in ordenes.items():
        pago = pagos.get(str(ls[0].get("payment_id")))
        neto = float(((pago or {}).get("transaction_details") or {}).get("net_received_amount") or 0)
        if not pago or neto <= 0:
            continue
        bruto_tot = sum(float(l.get("bruto") or 0) for l in ls)
        costo_tot = sum(float(l.get("costo") or 0) * int(l.get("cantidad_final") or 0) for l in ls)
        sin_costo = any(l.get("sin_costo") for l in ls)
        es_nueva = any(l.get("es_nueva") for l in ls)
        utilidad = neto - costo_tot
        margen = None if (sin_costo or costo_tot <= 0) else (utilidad / costo_tot) * 100
        bajo = margen is not None and margen < MARGEN_UMBRAL
        alerta = bajo and es_nueva
        acum, upd = 0.0, []
        for i, l in enumerate(ls):
            if i == len(ls) - 1:
                precio = r2(neto - acum)
            else:
                precio = r2(neto * float(l.get("bruto") or 0) / bruto_tot) if bruto_tot > 0 else r2(neto / len(ls))
                acum += precio
            upd.append({"id_venta": l["id_venta"], "sku_mod": l["sku_mod"], "precio": precio})
        prod = " + ".join(l.get("producto", "") for l in ls)
        cod = ", ".join(l.get("codigo", "") for l in ls)
        msg = None
        if alerta:
            e = lambda s: html.escape(str(s or ""), quote=False)
            desc = prod.strip().replace("\n", " ")
            if len(desc) > 40:
                desc = desc[:37] + "..."
            skus = "+".join(dict.fromkeys(str(l.get("sku_mod") or "") for l in ls if l.get("sku_mod")))
            cant = sum(int(l.get("cantidad_final") or l.get("cantidad") or 0) for l in ls)
            msg = (f"🚨 <code>{e(id_venta)}</code> · {e(skus)} · {e(desc)} · "
                   f"×{cant} · ${neto:.2f} · margen {margen:.2f}%")
        salida.append({"id_venta": id_venta, "pack_id": ls[0].get("pack_id"), "producto": prod, "codigo": cod,
                       "cantidad": sum(int(l.get("cantidad") or 0) for l in ls),
                       "costo_total": r2(costo_tot), "net_received_amount": r2(neto),
                       "utilidad": r2(utilidad), "margen_pct": None if margen is None else r2(margen),
                       "margen_bajo": bajo, "alerta": alerta, "mensaje_html": msg, "lineas": upd})
    return salida


def build_reporte(lineas: list, margenes: list, ahora=None) -> list:
    ahora = ahora or datetime.now(CDMX)
    esc = lambda s: html.escape(str(s or ""), quote=False)
    money = lambda n: "$" + f"{float(n or 0):,.2f}"
    ddmm = lambda f: (lambda p: f"{p[2]}/{p[1]}" if len(p) == 3 else (f or "?"))(str(f or "").split("-"))
    netos = {m["id_venta"]: m for m in margenes if m.get("id_venta")}
    piezas = lambda l: int(l.get("cantidad_final") or 0)
    suma = lambda a, f: sum(float(f(l) or 0) for l in a)
    ordenes = lambda a: len({l["id_venta"] for l in a})
    nuevas = sorted([l for l in lineas if l.get("es_nueva")],
                    key=lambda x: (x.get("fecha", ""), x.get("hora") or ""))
    icono = lambda c: {"Flex": "🛵", "Colecta": "🚐", "Full": "⚡", "Full (por SKU)": "⚡"}.get(c, "📦")

    def detalle(l):
        desc = str(l.get("producto") or "").strip().replace("\n", " ")
        if len(desc) > 40:
            desc = desc[:37] + "..."
        return (f"🔹 <code>{esc(l['id_venta'])}</code> · {esc(l.get('sku_mod'))} · {esc(desc)} · "
                f"×{piezas(l)} · {money(l.get('precio'))}")

    out = [f"📊 <b>Resumen de ventas MELI</b>\n🕛 {ahora.strftime('%d/%m %H:%M')} · ventana últimas 72 h"]
    if not nuevas:
        repetidas = len(lineas) - len(nuevas)
        out.append(f"\nSin ventas nuevas desde la última corrida{' (' + str(repetidas) + ' ya registradas)' if repetidas else ''}.")
    else:
        por_canal: dict = {}
        for l in nuevas:
            por_canal.setdefault(l.get("canal"), []).append(l)
        for canal, arr in por_canal.items():
            out.append(f"\n{icono(canal)} <b>{esc(canal)}</b> — {ordenes(arr)} orden(es) · {int(suma(arr, piezas))} pzas")
            out += [detalle(l) for l in arr]
        por_sku: dict = {}
        for l in [x for x in nuevas if x.get("a_descontar")]:
            por_sku[l["sku_mod"]] = por_sku.get(l["sku_mod"], 0) + piezas(l)
        if por_sku:
            out.append("\n🧮 <b>Inventario a descontar por SKU</b> (bodega propia, incluye Flex)")
            out += [f"   • {esc(s)}: −{n} pza(s)" for s, n in sorted(por_sku.items(), key=lambda x: -x[1])]
        n_ord = ordenes(nuevas)
        flex = ordenes([l for l in nuevas if l.get("es_flex")])
        full = ordenes([l for l in nuevas if l.get("es_full")])
        net = [n for n in netos.values() if any(l["id_venta"] == n["id_venta"] for l in nuevas)]
        out.append(f"\n---\n📈 <b>Totales (ventas nuevas)</b>\n🛒 Órdenes: {n_ord} (Flex: {flex} · Full: {full} · Otras: {n_ord - flex - full})\n"
                   f"🔢 Piezas vendidas: {int(suma(nuevas, piezas))}\n💵 Neto recibido: {money(suma(net, lambda n: n.get('net_received_amount')))}\n"
                   f"📉 Utilidad est.: {money(suma(net, lambda n: n.get('utilidad')))}")
    sin_costo_lineas = [x for x in nuevas if x.get("sin_costo")]
    if sin_costo_lineas:
        out.append("\n⚠️ <b>Sin costo</b>:")
        for l in sin_costo_lineas:
            desc = str(l.get("producto") or "").strip().replace("\n", " ")
            if len(desc) > 40:
                desc = desc[:37] + "..."
            out.append(f"⚠️ <code>{esc(l['id_venta'])}</code> · {esc(l.get('sku_mod'))} · {esc(desc)} · "
                       f"×{piezas(l)} · {money(l.get('precio'))} · sin costo")
    incong = [l for l in nuevas if l.get("alerta_logistica")]
    if incong:
        out.append("\n🚧 <b>Revisar logística</b>:")
        for l in incong:
            desc = str(l.get("producto") or "").strip().replace("\n", " ")
            if len(desc) > 40:
                desc = desc[:37] + "..."
            out.append(f"🚧 <code>{esc(l['id_venta'])}</code> · {esc(l.get('sku_mod'))} · {esc(desc)} · "
                       f"×{piezas(l)} · {money(l.get('precio'))} · {esc(l.get('canal'))}")
    sin_dato = [l for l in nuevas if l.get("canal") == "Sin dato"]
    if sin_dato:
        out.append(f"\nℹ️ {len(sin_dato)} línea(s) sin dato de logística (no se pudo consultar el envío).")
    chunks, cur = [], ""
    for ln in out:
        if cur and len(cur) + 1 + len(ln) > 3800:
            chunks.append(cur)
            cur = ""
        cur += ("" if not cur else "\n") + ln
    if cur:
        chunks.append(cur)
    return [c if len(chunks) == 1 else f"{c}\n\n<i>({i + 1}/{len(chunks)})</i>" for i, c in enumerate(chunks)]


# ---------- IO: MeLi / MP ----------

class MeliError(Exception):
    pass


async def _meli_token(client: httpx.AsyncClient) -> str:
    faltan = [v for v in ("MELI_CLIENT_ID", "MELI_CLIENT_SECRET", "MELI_REFRESH_TOKEN") if not _env(v)]
    if faltan:
        raise MeliError(f"Faltan en .env: {', '.join(faltan)}")
    try:
        r = await client.post(f"{MELI_API}/oauth/token", data={
            "grant_type": "refresh_token", "refresh_token": _env("MELI_REFRESH_TOKEN"),
            "client_id": _env("MELI_CLIENT_ID"), "client_secret": _env("MELI_CLIENT_SECRET")})
        r.raise_for_status()
        return r.json()["access_token"]
    except (httpx.HTTPError, KeyError) as err:
        raise MeliError(f"Refresh token MeLi: {err}")


async def _get(client: httpx.AsyncClient, url: str, token: str, params: dict | None = None) -> dict:
    try:
        r = await client.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30.0)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as err:
        raise MeliError(f"GET {url}: {err}")


async def buscar_ventas_72h(client: httpx.AsyncClient, token: str) -> list:
    me = await _get(client, f"{MELI_API}/users/me", token)
    seller = me.get("id")
    if not seller:
        raise MeliError(f"/users/me sin id: {me}")
    desde = (datetime.now(CDMX) - timedelta(hours=72)).astimezone(ZoneInfo("UTC")).isoformat()
    paginas = []
    for page in range(40):
        data = await _get(client, f"{MELI_API}/orders/search", token, {
            "seller": seller, "order.status": "paid", "order.date_created.from": desde,
            "sort": "date_asc", "limit": 50, "offset": page * 50})
        results = data.get("results") or []
        paginas.append({"results": results})
        if len(results) < 50:
            break
        await asyncio.sleep(0.3)
    return paginas


async def detalle_envios(client: httpx.AsyncClient, token: str, shipping_ids: list) -> dict:
    out: dict = {}
    sem = asyncio.Semaphore(5)

    async def uno(sid: str):
        async with sem:
            try:
                d = await _get(client, f"{MELI_API}/shipments/{sid}", token)
                out[sid] = d.get("logistic_type") or ((d.get("logistic") or {}).get("type"))
            except MeliError as err:
                print(f"Shipment {sid}: {err}")
            await asyncio.sleep(0.3)

    await asyncio.gather(*[uno(s) for s in dict.fromkeys(shipping_ids) if s])
    return out


async def pagos_mp(client: httpx.AsyncClient, token: str, payment_ids: list) -> dict:
    out: dict = {}
    sem = asyncio.Semaphore(10)

    async def uno(pid: str):
        async with sem:
            try:
                async with httpx.AsyncClient(timeout=30.0) as c:
                    r = await c.get(f"{MP_API}/v1/payments/{pid}", headers={"Authorization": f"Bearer {token}"})
                    r.raise_for_status()
                    out[str(pid)] = r.json()
            except httpx.HTTPError as err:
                print(f"Pago MP {pid}: {err}")

    await asyncio.gather(*[uno(p) for p in dict.fromkeys(payment_ids) if p])
    return out


# ---------- DB ----------

def get_db_connection():
    return mysql.connector.connect(host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
                                   password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"))


def cargar_contexto() -> tuple:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT sku, costo_total FROM productos")
        costos = {str(f["sku"]).strip(): float(f["costo_total"] or 0)
                  for f in cur.fetchall() if f.get("sku") is not None}
        cur.execute("SELECT id_ventas, sku, inventario_descontado FROM ventasRegistro "
                    "WHERE plataforma = 'MERCADOLIBRE' AND fecha >= DATE_SUB(CURDATE(), INTERVAL 15 DAY)")
        registro = {f"{f['id_ventas']}|{str(f.get('sku') or '').strip()}":
                    int(f.get("inventario_descontado") or 0) == 1
                    for f in cur.fetchall() if f.get("id_ventas") is not None}
        return costos, registro
    finally:
        cur.close()
        conn.close()


def aplicar_ventas(lineas: list, dry_run: bool) -> dict:
    """INSERT + resta (solo no-Full, con guard) + potenciales. Una transacción."""
    stats = {"nuevas": 0, "descontadas": 0, "sin_stock": 0, "potenciales": 0, "dry_run": dry_run}
    if dry_run:
        stats["nuevas"] = sum(1 for l in lineas if l.get("es_nueva"))
        return stats
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        for l in lineas:
            cur.execute(
                """INSERT INTO ventasRegistro
                   (id_ventas, producto, sku, cantidad, precio, fecha, nombreComprador, otros,
                    plataforma, costo_unitario, es_full)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'MERCADOLIBRE',%s,%s)
                   ON DUPLICATE KEY UPDATE cantidad = VALUES(cantidad), fecha = VALUES(fecha)""",
                (l["id_venta"], l.get("producto", ""), l.get("sku_mod"), l.get("cantidad_final"),
                 l.get("precio", 0), l.get("fecha"), l.get("nombre_comprador", ""), l.get("otros", ""),
                 l.get("costo", 0), l.get("es_full", 0)))
            if l.get("es_nueva"):
                stats["nuevas"] += 1
            if l.get("a_descontar"):
                cur.execute(
                    """UPDATE productos p JOIN ventasRegistro v ON v.sku = p.sku
                       SET p.stock_bodega = p.stock_bodega - v.cantidad, v.inventario_descontado = 1
                       WHERE v.id_ventas = %s AND v.sku = %s AND v.plataforma = 'MERCADOLIBRE'
                         AND v.es_full = 0 AND v.inventario_descontado = 0
                         AND COALESCE(p.stock_bodega, 0) >= v.cantidad""",
                    (l["id_venta"], l.get("sku_mod")))
                if cur.rowcount:
                    stats["descontadas"] += 1
                else:
                    cur.execute("SELECT COALESCE(stock_bodega, 0) FROM productos WHERE sku = %s", (l.get("sku_mod"),))
                    f = cur.fetchone()
                    if f and f[0] < (l.get("cantidad_final") or 0):
                        stats["sin_stock"] += 1
            if (not l.get("es_full")) and ((l.get("es_multiplo") and (l.get("cantidad") or 0) >= 2)
                                           or ((not l.get("es_multiplo")) and (l.get("cantidad") or 0) >= 5)):
                cur.execute(
                    """INSERT IGNORE INTO clientes_potenciales
                       (sku, producto, id_ventas, cantidad, fecha, nombre_comprador, precio, costo_unitario)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (l.get("codigo"), l.get("producto", ""), l["id_venta"], l.get("cantidad", 0),
                     l.get("fecha"), l.get("nombre_comprador", ""), l.get("precio", 0), l.get("costo", 0)))
                stats["potenciales"] += cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
    return stats


def aplicar_netos(margenes: list) -> tuple:
    """UPDATE precio=neto por línea. Devuelve (actualizadas, errores)."""
    ok, errores = 0, 0
    if not margenes:
        return ok, errores
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        for m in margenes:
            for ln in (m.get("lineas") or []):
                try:
                    cur.execute("UPDATE ventasRegistro SET precio = %s WHERE id_ventas = %s AND sku = %s "
                                "AND plataforma = 'MERCADOLIBRE'",
                                (ln["precio"], ln["id_venta"], ln["sku_mod"]))
                    ok += cur.rowcount
                except mysql.connector.Error:
                    errores += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
    return ok, errores


# ---------- Telegram multichat ----------

async def _tg(texto: str, chat_id: str | None = None):
    import httpx as _hx
    token = _env("TELEGRAM_BOT_TOKEN")
    chat = chat_id or _env("TELEGRAM_GROUP_ID")
    if not token or not chat or not texto:
        print("Telegram omitido (falta token/chat).")
        return
    try:
        async with _hx.AsyncClient(timeout=10.0) as c:
            (await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": texto, "parse_mode": "HTML"})).raise_for_status()
    except _hx.HTTPError as err:
        print(f"Telegram {chat}: {err}")


# ---------- Orquestador ----------

def procesar_cancelacion(order_id: str, motivo: str = "webhook") -> dict:
    """Regresa a bodega lo descontado de una orden MeLi cancelada (una sola vez).

    Solo toca filas con inventario_descontado=1, así que es idempotente ante
    reintentos del webhook. Devuelve piezas regresadas.
    """
    import mov_reg
    oid = str(order_id)
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT sku, cantidad FROM ventasRegistro WHERE id_ventas = %s "
                    "AND plataforma = 'MERCADOLIBRE' AND inventario_descontado = 1", (oid,))
        filas = cur.fetchall()
        if not filas:
            return {"order_id": oid, "regresadas": 0, "piezas": 0}
        cur.execute(
            """UPDATE productos p JOIN ventasRegistro v ON v.sku = p.sku
               SET p.stock_bodega = COALESCE(p.stock_bodega, 0) + v.cantidad, v.inventario_descontado = 0
               WHERE v.id_ventas = %s AND v.plataforma = 'MERCADOLIBRE' AND v.inventario_descontado = 1""",
            (oid,))
        piezas = sum(int(f.get("cantidad") or 0) for f in filas)
        conn.commit()
        try:
            mov_reg.registrar_movimiento(
                "job-meli", f"Cancelación MeLi {oid} ({motivo}): stock regresado +{piezas}", "Ventas")
        except Exception as err:
            print(f"Cancelación {oid}: falló bitácora: {err}")
        return {"order_id": oid, "regresadas": cur.rowcount, "piezas": piezas}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cur.close()
        conn.close()

async def run_job(dry_run: bool | None = None, motivo: str = "manual", telegram: bool = True) -> dict:
    """Si telegram=False no manda Telegram (webhook: avisa por WS + tabla)."""
    import mov_reg
    dry = _dry_run_default() if dry_run is None else dry_run
    resumen = {"motivo": motivo, "dry_run": dry, "inicio": datetime.now(CDMX).isoformat()}

    async def avisar(*a, **k):
        if telegram:
            await _tg(*a, **k)
    try:
        from jobs.schema_ventas import verificar_esquema_job
        verificar_esquema_job()
        async with httpx.AsyncClient(timeout=30.0) as client:
            token = await _meli_token(client)
            paginas = await buscar_ventas_72h(client, token)
        ordenes = [o for p in paginas for o in (p.get("results") or [])]
        if not ordenes:
            await avisar("📭 <b>Resumen de ventas MELI</b>\n\nNo hubo ventas pagadas en la ventana revisada (últimas 72 h).")
            resumen.update({"estado": "sin_ventas", "ordenes": 0})
        else:
            base = limpiar_ordenes(paginas)
            async with httpx.AsyncClient(timeout=30.0) as client:
                token2 = await _meli_token(client)
                envios = await detalle_envios(client, token2, [l["shipping_id"] for l in base if l.get("shipping_id")])
                costos, registro = await asyncio.to_thread(cargar_contexto)
                lineas = enriquecer_lineas(base, costos, envios, registro)
                stats = await asyncio.to_thread(aplicar_ventas, lineas, dry)
                pagos = await pagos_mp(client, token2, [l["payment_id"] for l in lineas if l.get("payment_id")])
            margenes = calcular_margenes(lineas, pagos)
            net_ok, net_err = (0, 0) if dry else await asyncio.to_thread(aplicar_netos, margenes)
            if net_err:
                await avisar("⚠️ Reg_ventas meli: falló 'Actualiza pago recibido' (no se pudo guardar el neto de "
                          "MercadoPago en ventasRegistro). Las ventas SÍ se registraron y el inventario SÍ se "
                          "descontó; revisa la ejecución.", _alert_chat())
            for m in [x for x in margenes if x.get("alerta")]:
                await avisar(m["mensaje_html"], _alert_chat())
            sin_costo_ls = [l for l in lineas if l.get("sin_costo")]
            if sin_costo_ls:
                txt = "⚠️ <b>Sin costo</b>:\n"
                for l in sin_costo_ls:
                    desc = str(l.get("producto") or "").strip().replace("\n", " ")
                    if len(desc) > 40:
                        desc = desc[:37] + "..."
                    cant = int(l.get("cantidad_final") or l.get("cantidad") or 0)
                    txt += (f"⚠️ <code>{html.escape(str(l.get('id_venta')), quote=False)}</code> · "
                            f"{html.escape(str(l.get('sku_mod')), quote=False)} · {html.escape(desc, quote=False)} · "
                            f"×{cant} · ${float(l.get('precio') or 0):,.2f} · sin costo\n")
                await avisar(txt[:4000], _alert_chat())
            for chunk in build_reporte(lineas, margenes):
                await avisar(chunk)
            try:
                mov_reg.registrar_movimiento(
                    "job-meli", f"Job MeLi {motivo}: {stats['nuevas']} nuevas, {stats['descontadas']} descontadas, "
                    f"{stats['sin_stock']} sin stock" + (" (DRY_RUN)" if dry else ""), "Ventas")
            except Exception as err:
                print(f"Job MeLi: falló bitácora: {err}")
            resumen.update({"estado": "ok", "stats": {**stats, "net_ok": net_ok, "net_err": net_err},
                            "ordenes_nuevas": sorted({l["id_venta"] for l in lineas if l.get("es_nueva")})})
    except Exception as err:
        resumen.update({"estado": "error", "error": str(err)})
        print(f"Job MeLi error: {err}")
        await avisar(f"⚠️ <b>Job MeLi falló</b>\n\n<code>{html.escape(str(err))}</code>", _alert_chat())
    resumen["fin"] = datetime.now(CDMX).isoformat()
    LAST_RUN.clear()
    LAST_RUN.update(resumen)
    # Aviso en vivo a todos los conectados (el Telegram ya es el registro).
    try:
        from routers.sofi_notificaciones import manager as _ws_manager
        await _ws_manager.broadcast({"tipo": "job", "job": "meli", "estado": resumen.get("estado"),
                                     "dry_run": resumen.get("dry_run"), "stats": resumen.get("stats"),
                                     "fin": resumen.get("fin")})
    except Exception as err:
        print(f"Job MeLi: no se pudo avisar por WS: {err}")
    return resumen
