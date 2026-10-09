# Job de ventas Amazon (migración del workflow n8n `ventas_amazon`).
#
# Corre dentro de api_zeutica1 (Easypanel, mismo proceso uvicorn):
# scheduler diario 12:22 America/Mexico_City + disparo manual
# POST /jobs/amazon/run (solo gerencia).
#
# Paridad con n8n: ventana 72h, reporte ALL_ORDERS_DATA_BY_LAST_UPDATE,
# parseo TSV/GZIP, SKU base + multiplicador `SKU_N`, flag
# inventario_descontado 0->1 al descontar y 1->0 al regresar por
# cancelación. Mejora vs n8n: descuento transaccional con guard
# `stock >= cantidad` para no dejar negativos (si falta stock se queda
# en 0 y reintenta en la próxima corrida).
import gzip
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
SP_API = "https://sellingpartnerapi-na.amazon.com"
REPORT_TYPE = "GET_FLAT_FILE_ALL_ORDERS_DATA_BY_LAST_UPDATE_GENERAL"

MAX_INTENTOS = 20
ESPERA_SEG = 60

# Estado del último run (lo lee GET /jobs/amazon/status).
LAST_RUN: dict = {"estado": "nunca", "detalle": None}


def _env(nombre: str, default: str = "") -> str:
    return (os.getenv(nombre) or default).strip()


def _dry_run_default() -> bool:
    return _env("AMAZON_JOB_DRY_RUN", "0") == "1"


# ---------- Parseo del reporte (puro, testeable) ----------

def parse_report_bytes(buffer: bytes) -> list:
    """TSV de Amazon -> una línea por renglón. Clasifica ok/pendiente/cancelada."""
    if len(buffer) >= 2 and buffer[0] == 0x1F and buffer[1] == 0x8B:
        try:
            buffer = gzip.decompress(buffer)
        except OSError as err:
            raise ValueError(f"Reporte GZIP ilegible: {err}")
    texto = buffer.decode("utf-8", errors="strict") if _es_utf8(buffer) else buffer.decode("latin1")
    texto = texto.lstrip("\ufeff")
    filas = [l[:-1] if l.endswith("\r") else l for l in texto.split("\n")]
    filas = [l for l in filas if l.strip()]
    if len(filas) < 2:
        return []
    sep = "\t" if "\t" in filas[0] else ","
    headers = [h.replace('"', "").replace("'", "").strip().lower() for h in filas[0].split(sep)]
    salida = []
    for linea in filas[1:]:
        celdas = [c[1:-1] if len(c) >= 2 and c.startswith('"') and c.endswith('"') else c
                  for c in (x.strip() for x in linea.split(sep))]
        r = {h: (celdas[k] if k < len(celdas) else "") for k, h in enumerate(headers)}
        id_venta = str(r.get("amazon-order-id") or "").strip()
        if not id_venta:
            continue
        estado_orden = str(r.get("order-status") or "")
        estado_item = str(r.get("item-status") or "")
        cantidad = _num(r.get("quantity"))
        if re.search(r"cancel", estado_orden, re.I) or re.search(r"cancel", estado_item, re.I):
            estado = "cancelada"
        elif estado_orden.lower().startswith("pending"):
            estado = "pendiente"
        elif cantidad <= 0:
            estado = "cancelada"
        else:
            estado = "ok"
        salida.append({
            "id_venta": id_venta,
            "codigo": str(r.get("sku") or "").strip(),
            "producto": r.get("product-name") or "",
            "cantidad": int(cantidad),
            "precio": round(_num(str(r.get("item-price") or "0").replace(",", "")), 2),
            "fecha": _fecha_cdmx(str(r.get("purchase-date") or "")),
            "asin": r.get("asin") or "",
            "otros": r.get("payment-method-details") or "",
            "es_full": 0 if str(r.get("fulfillment-channel") or "Amazon").lower() == "merchant" else 1,
            "estado": estado,
            "estado_amazon": estado_orden,
        })
    return salida


def _es_utf8(buffer: bytes) -> bool:
    try:
        texto = buffer.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return "�" not in texto


def _num(valor) -> float:
    try:
        return float(valor)
    except (TypeError, ValueError):
        return 0.0


def _fecha_cdmx(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CDMX)
        return dt.astimezone(CDMX).strftime("%Y-%m-%d")
    except ValueError:
        return datetime.now(CDMX).strftime("%Y-%m-%d")


def _es_por_surtir(linea: dict) -> bool:
    return (linea.get("estado") == "pendiente"
            and re.search(r"pending\s*-\s*waiting\s+for\s+pick\s*-?\s*up",
                          str(linea.get("estado_amazon") or ""), re.I) is not None)


def resolver_sku(raw: str, costos: dict) -> tuple:
    """SKU tal cual si existe en productos; si no, base antes del `_` con multiplicador."""
    raw = (raw or "").strip()
    if raw in costos or "_" not in raw:
        return raw, 1
    base, sufijo = raw.rsplit("_", 1)
    base = base.strip()
    try:
        mult = int(sufijo)
    except ValueError:
        return raw, 1
    return (base, mult) if mult > 0 and base in costos else (raw, 1)


def agrupar_lineas(lineas: list, costos: dict, existentes: set, registro: dict,
                   ordenes_existentes: set) -> list:
    """Agrupa por (orden, sku, estado) y marca es_nueva/a_regresar. Puerto de `Datos1`."""
    agrupadas: dict = {}
    for l in lineas:
        if not l.get("id_venta"):
            continue
        raw = l.get("codigo") or ""
        estado_base = "por_surtir" if _es_por_surtir(l) else l.get("estado")
        sku, mult = resolver_sku(raw, costos)
        tiene_costo = sku in costos
        clave = f"{l['id_venta']}|{sku}"
        viejo = sku != raw and f"{l['id_venta']}|{raw}" in existentes and clave not in existentes
        if (clave_agr := f"{clave}|{estado_base}") in agrupadas:
            p = agrupadas[clave_agr]
            p["cantidad"] += l.get("cantidad", 0)
            p["cantidad_final"] += l.get("cantidad", 0) * mult
            p["precio"] = round(p["precio"] + l.get("precio", 0), 2)
            if raw not in p["codigo"].split(", "):
                p["codigo"] += ", " + raw
            p["es_multiplo"] = p["es_multiplo"] or bool(re.search(r"_[0-9]+$", raw))
            continue
        reg = registro.get(clave, {})
        descontado = reg.get("descontado", False)
        estado = ("ya_registrada" if viejo and estado_base in ("ok", "por_surtir") else estado_base)
        agrupadas[clave_agr] = {
            "id_venta": l["id_venta"], "codigo": raw, "sku": sku, "producto": l.get("producto", ""),
            "cantidad": l.get("cantidad", 0), "multiplicador": mult,
            "cantidad_final": l.get("cantidad", 0) * mult,
            "precio": round(l.get("precio", 0), 2), "fecha": l.get("fecha", ""),
            "asin": l.get("asin", ""), "otros": l.get("otros", ""),
            "es_full": l.get("es_full", 1), "costo": costos.get(sku, 0),
            "sin_costo": (not tiene_costo) or costos.get(sku, 0) <= 0,
            "es_multiplo": bool(re.search(r"_[0-9]+$", raw)),
            "estado": estado, "estado_amazon": l.get("estado_amazon", ""),
            "valida": estado in ("ok", "por_surtir"),
            "es_nueva": clave not in existentes,
            "registrada_antes": l["id_venta"] in ordenes_existentes,
            "registrada_linea": clave in registro,
            "a_regresar": estado == "cancelada" and descontado,
            "cantidad_regresar": reg.get("cantidad", 0) if estado == "cancelada" and descontado else 0,
            "cancelacion_parcial": False,
        }
    resultado = list(agrupadas.values())
    for r in resultado:
        if r["estado"] != "cancelada" or not r["registrada_linea"]:
            continue
        k = f"{r['id_venta']}|{r['sku']}"
        if f"{k}|ok" in agrupadas or f"{k}|por_surtir" in agrupadas:
            r["a_regresar"] = False
            r["cantidad_regresar"] = 0
            r["cancelacion_parcial"] = True
    return resultado


# ---------- Resumen Telegram (puro, testeable) ----------

def build_resumen(lineas: list, ahora=None) -> list:
    ahora = ahora or datetime.now(CDMX)
    corte = ahora.replace(hour=12, minute=0, second=0, microsecond=0)
    desde = corte - timedelta(days=1)
    rango = f"{desde.strftime('%d/%m %H:%M')} → {corte.strftime('%d/%m %H:%M')}"
    esc = lambda s: html.escape(str(s or ""), quote=False)
    money = lambda n: "$" + f"{float(n or 0):,.2f}"
    ddmm = lambda f: (lambda p: f"{p[2]}/{p[1]}" if len(p) == 3 else (f or "?"))(str(f or "").split("-"))
    nuevas = [l for l in lineas if l.get("valida") and l.get("es_nueva")]
    # Ventas del día vs atrasadas: la ventana es de 72h para recuperar, pero el
    # reporte de ventas lista solo el día (corte 12:00); las atrasadas solo van
    # en el bloque de inventario.
    dia_desde = desde.strftime("%Y-%m-%d")
    del_dia = [l for l in nuevas if (l.get("fecha") or "") >= dia_desde]
    atrasadas = [l for l in nuevas if (l.get("fecha") or "") < dia_desde]
    por_surtir = sorted([l for l in del_dia if l.get("estado") == "por_surtir"], key=lambda x: x["id_venta"])
    enviadas = sorted([l for l in del_dia if l.get("estado") == "ok"], key=lambda x: x["id_venta"])
    sin_pagar_todas = sorted([l for l in lineas if l.get("estado") == "pendiente"], key=lambda x: x["id_venta"])
    sin_pagar = [l for l in sin_pagar_todas if (l.get("fecha") or "") >= dia_desde]
    sin_pagar_viejas = [l for l in sin_pagar_todas if (l.get("fecha") or "") < dia_desde]
    regresadas = sorted([l for l in lineas if l.get("a_regresar")], key=lambda x: x["id_venta"])
    manual_todas = sorted([l for l in lineas if l.get("estado") == "cancelada" and l.get("registrada_antes")
                     and not l.get("a_regresar") and (not l.get("registrada_linea") or l.get("cancelacion_parcial"))],
                    key=lambda x: x["id_venta"])
    manual = [l for l in manual_todas if (l.get("fecha") or "") >= dia_desde]
    manual_viejas = [l for l in manual_todas if (l.get("fecha") or "") < dia_desde]
    piezas = lambda l: int(l.get("cantidad_final") or l.get("cantidad") or 0)
    suma = lambda arr, f: sum(float(f(l) or 0) for l in arr)
    ordenes = lambda arr: len({l["id_venta"] for l in arr})

    def detalle(l):
        desc = str(l.get("producto") or "").strip().replace("\n", " ")
        if len(desc) > 40:
            desc = desc[:37] + "..."
        return (f"🔹 <code>{esc(l['id_venta'])}</code> · {esc(l.get('sku'))} · {esc(desc)} · "
                f"×{piezas(l)} · {money(l.get('precio'))}")

    out = [f"📊 <b>Resumen de ventas Amazon</b>\n🕛 Corte: {rango}"]
    if por_surtir:
        out.append(f"\n📦 <b>Por entregar a logística</b> — {ordenes(por_surtir)} orden(es) · "
                   f"{int(suma(por_surtir, piezas))} pzas · {money(suma(por_surtir, lambda l: l.get('precio')))}\n"
                   "<i>Pagadas, esperando recolección. Ya se descontaron de inventario.</i>")
        out += [detalle(l) for l in por_surtir]
    if enviadas:
        out.append(f"\n🚚 <b>Enviadas / entregadas (nuevas)</b> — {ordenes(enviadas)} orden(es) · "
                   f"{int(suma(enviadas, piezas))} pzas · {money(suma(enviadas, lambda l: l.get('precio')))}")
        out += [detalle(l) for l in enviadas]
    if not por_surtir and not enviadas:
        out.append("\nNo hubo ventas nuevas de Amazon desde el último corte.")
    if atrasadas:
        out.append(f"\n📌 <b>Atrasadas</b> — {ordenes(atrasadas)} orden(es) · {int(suma(atrasadas, piezas))} pzas · "
                   f"{money(suma(atrasadas, lambda l: l.get('precio')))} de días previos (registradas y descontadas, ver inventario).")
    if nuevas:
        por_sku: dict = {}
        for l in nuevas:
            por_sku[l["sku"]] = por_sku.get(l["sku"], 0) + piezas(l)
        out.append("\n🧮 <b>Inventario a descontar por SKU</b> (día + atrasadas)")
        out += [f"   • {esc(s)}: −{n} pza(s)" for s, n in sorted(por_sku.items(), key=lambda x: -x[1])]
    if regresadas:
        out.append(f"\n↩️ <b>Stock regresado por cancelación</b> — {ordenes(regresadas)} orden(es) · "
                   f"+{int(suma(regresadas, lambda l: l.get('cantidad_regresar')))} pza(s)")
        for l in regresadas:
            desc = str(l.get("producto") or "").strip().replace("\n", " ")
            if len(desc) > 40:
                desc = desc[:37] + "..."
            out.append(f"🔸 <code>{esc(l['id_venta'])}</code> · {esc(l.get('sku'))} · {esc(desc)} · "
                       f"+{l.get('cantidad_regresar')} · {money(l.get('precio'))}")
    fba = ordenes([l for l in del_dia if l.get("es_full") == 1])
    out.append(f"\n---\n📈 <b>Totales del día</b>\n🛒 Órdenes: {ordenes(del_dia)} "
               f"(FBA: {fba} · Propias: {ordenes(del_dia) - fba})\n🔢 Piezas: {int(suma(del_dia, piezas))}\n"
               f"💵 Monto: {money(suma(del_dia, lambda l: l.get('precio')))}")
    if sin_pagar:
        out.append(f"\n⏳ <b>Pendientes de pago</b> — {ordenes(sin_pagar)} orden(es) (no se registran)")
        for l in sin_pagar:
            desc = str(l.get("producto") or "").strip().replace("\n", " ")
            if len(desc) > 40:
                desc = desc[:37] + "..."
            out.append(f"⏳ <code>{esc(l['id_venta'])}</code> · {esc(l.get('sku'))} · {esc(desc)} · "
                       f"×{piezas(l)} · {money(l.get('precio'))}")
    if sin_pagar_viejas:
        out.append(f"+ {ordenes(sin_pagar_viejas)} pendiente(s) de días previos, sin cambios.")
    sin_costo_lineas = [l for l in del_dia if l.get("sin_costo")]
    if sin_costo_lineas:
        out.append("\n⚠️ <b>Sin costo</b>:")
        for l in sin_costo_lineas:
            desc = str(l.get("producto") or "").strip().replace("\n", " ")
            if len(desc) > 40:
                desc = desc[:37] + "..."
            out.append(f"⚠️ <code>{esc(l['id_venta'])}</code> · {esc(l.get('sku'))} · {esc(desc)} · "
                       f"×{piezas(l)} · {money(l.get('precio'))} · sin costo")
    if manual:
        out.append("\n🚫 <b>Revisión manual</b>:")
        for l in manual:
            desc = str(l.get("producto") or "").strip().replace("\n", " ")
            if len(desc) > 40:
                desc = desc[:37] + "..."
            motivo = "parcial" if l.get("cancelacion_parcial") else "otro formato"
            out.append(f"🚫 <code>{esc(l['id_venta'])}</code> · {esc(l.get('sku'))} · {esc(desc)} · "
                       f"×{piezas(l)} · {money(l.get('precio'))} · {motivo}")
    if manual_viejas:
        out.append(f"+ {ordenes(manual_viejas)} revisión(es) de días previos, sin cambios.")
    chunks, cur = [], ""
    for ln in out:
        if cur and len(cur) + 1 + len(ln) > 3800:
            chunks.append(cur)
            cur = ""
        cur += ("" if not cur else "\n") + ln
    if cur:
        chunks.append(cur)
    return [c if len(chunks) == 1 else f"{c}\n\n<i>({i + 1}/{len(chunks)})</i>" for i, c in enumerate(chunks)]


# ---------- SP-API ----------

class AmazonError(Exception):
    pass


async def _token() -> str:
    faltan = [v for v in ("AMZ_CLIENT_ID", "AMZ_CLIENT_SECRET", "AMZ_REFRESH_TOKEN") if not _env(v)]
    if faltan:
        raise AmazonError(f"Faltan en .env: {', '.join(faltan)}")
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post("https://api.amazon.com/auth/o2/token", data={
                "grant_type": "refresh_token", "refresh_token": _env("AMZ_REFRESH_TOKEN"),
                "client_id": _env("AMZ_CLIENT_ID"), "client_secret": _env("AMZ_CLIENT_SECRET")})
            r.raise_for_status()
            return r.json()["access_token"]
    except (httpx.HTTPError, KeyError) as err:
        raise AmazonError(f"No se pudo obtener token LWA: {err}")


async def _sp(client: httpx.AsyncClient, method: str, path: str, token: str, **kw):
    try:
        r = await client.request(method, SP_API + path,
                                 headers={"x-amz-access-token": token, "Content-Type": "application/json"}, **kw)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as err:
        raise AmazonError(f"SP-API {method} {path}: {err}")


def _ventana_72h(now_cdmx: datetime) -> tuple:
    corte = now_cdmx.replace(hour=12, minute=0, second=0, microsecond=0)
    ini = corte - timedelta(hours=72)
    return ini.astimezone(ZoneInfo("UTC")).isoformat(), corte.astimezone(ZoneInfo("UTC")).isoformat()


async def solicitar_y_esperar_reporte(client: httpx.AsyncClient, token: str) -> dict:
    ini, fin = _ventana_72h(datetime.now(CDMX))
    rep = await _sp(client, "POST", "/reports/2021-06-30/reports", token, json={
        "reportType": REPORT_TYPE, "marketplaceIds": [_env("AMZ_MARKETPLACE_ID", "A1AM78C64UM0Y8")],
        "dataStartTime": ini, "dataEndTime": fin})
    report_id = rep.get("reportId")
    if not report_id:
        raise AmazonError(f"Amazon no devolvió reportId: {rep}")
    import asyncio as _aio
    for intento in range(1, MAX_INTENTOS + 1):
        estado = await _sp(client, "GET", f"/reports/2021-06-30/reports/{report_id}", token)
        proc = estado.get("processingStatus") or "DESCONOCIDO"
        if proc == "DONE":
            return {"reportId": report_id, "reportDocumentId": estado.get("reportDocumentId"),
                    "processingStatus": proc, "intento": intento, "accion": "descargar"}
        if proc in ("CANCELLED", "FATAL") or intento >= MAX_INTENTOS:
            accion = "sin_datos" if proc == "CANCELLED" else "error"
            msg = ("📭 <b>Resumen de ventas Amazon</b>\n\nNo hubo órdenes nuevas ni actualizadas en la ventana."
                   if accion == "sin_datos" else
                   f"⚠️ <b>Amazon: el reporte de órdenes no se generó</b>\n\nEstado: <b>{proc}</b> después de "
                   f"{intento} intento(s).\nHoy NO se registraron ventas de Amazon. Se recuperan solas en la "
                   f"próxima ejecución (la ventana cubre 72 h); si falla tres días seguidos, revisar.")
            return {"reportId": report_id, "reportDocumentId": estado.get("reportDocumentId"),
                    "processingStatus": proc, "intento": intento, "accion": accion, "mensaje_html": msg}
        await _aio.sleep(ESPERA_SEG)
    raise AmazonError("Timeout esperando reporte (inalcanzable)")


async def descargar_bytes(client: httpx.AsyncClient, token: str, document_id: str) -> bytes:
    doc = await _sp(client, "GET", f"/reports/2021-06-30/documents/{document_id}", token)
    url = doc.get("url")
    if not url:
        raise AmazonError(f"Documento sin URL: {doc}")
    try:
        async with httpx.AsyncClient(timeout=60.0) as f:
            r = await f.get(url)
            r.raise_for_status()
            return r.content
    except httpx.HTTPError as err:
        raise AmazonError(f"Descarga del reporte: {err}")


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
        cur.execute("SELECT id_ventas, sku, cantidad, inventario_descontado FROM ventasRegistro WHERE plataforma = 'amazon'")
        existentes, registro, ordenes = set(), {}, set()
        for f in cur.fetchall():
            if f.get("id_ventas") is None:
                continue
            k = f"{f['id_ventas']}|{str(f.get('sku') or '').strip()}"
            existentes.add(k)
            ordenes.add(str(f["id_ventas"]))
            registro[k] = {"descontado": int(f.get("inventario_descontado") or 0) == 1,
                           "cantidad": int(f.get("cantidad") or 0)}
        return costos, existentes, registro, ordenes
    finally:
        cur.close()
        conn.close()


def aplicar_ventas(agrupadas: list, dry_run: bool) -> dict:
    """INSERT + descuento transaccional por línea. Devuelve contadores."""
    nuevas = [l for l in agrupadas if l.get("valida") and l.get("es_nueva")]
    regresar = [l for l in agrupadas if l.get("a_regresar")]
    stats = {"nuevas": len(nuevas), "descontadas": 0, "sin_stock": 0,
             "regresadas": 0, "potenciales": 0, "dry_run": dry_run}
    if dry_run or (not nuevas and not regresar):
        return stats
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        for l in nuevas:
            cur.execute(
                """INSERT INTO ventasRegistro
                   (id_ventas, producto, sku, cantidad, precio, fecha, nombreComprador, otros,
                    plataforma, costo_unitario, es_full)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'amazon',%s,%s)
                   ON DUPLICATE KEY UPDATE precio = VALUES(precio)""",
                (l["id_venta"], l.get("producto", ""), l["sku"], l["cantidad_final"], l.get("precio", 0),
                 l.get("fecha"), l.get("asin", ""), l.get("otros", ""), l.get("costo", 0), l.get("es_full", 1)))
            # Descuento con guard: si no hay stock no deja negativo, queda pendiente al próximo run.
            cur.execute(
                """UPDATE productos p JOIN ventasRegistro v ON v.sku = p.sku
                   SET p.stock_bodega = p.stock_bodega - v.cantidad, v.inventario_descontado = 1
                   WHERE v.id_ventas = %s AND v.sku = %s AND v.plataforma = 'amazon'
                     AND v.inventario_descontado = 0 AND COALESCE(p.stock_bodega, 0) >= v.cantidad""",
                (l["id_venta"], l["sku"]))
            if cur.rowcount == 0:
                # ¿Fue por falta de stock o porque ya estaba descontado? Se verifica stock.
                cur.execute("SELECT COALESCE(stock_bodega, 0) AS s FROM productos WHERE sku = %s", (l["sku"],))
                f = cur.fetchone()
                stats["sin_stock" if f and f[0] < l["cantidad_final"] else "descontadas"] += 1
            else:
                stats["descontadas"] += 1
        for l in regresar:
            cur.execute(
                """UPDATE productos p JOIN ventasRegistro v ON v.sku = p.sku
                   SET p.stock_bodega = p.stock_bodega + v.cantidad, v.inventario_descontado = 0,
                       v.estatus = 'cancelada'
                   WHERE v.id_ventas = %s AND v.sku = %s AND v.plataforma = 'amazon'
                     AND v.inventario_descontado = 1""",
                (l["id_venta"], l["sku"]))
            stats["regresadas"] += cur.rowcount
        # Clientes potenciales (umbral n8n: múltiplos >=2, resto >=5).
        for l in [x for x in agrupadas if x.get("valida") and x.get("es_nueva")
                  and ((x.get("es_multiplo") and x.get("cantidad", 0) >= 2)
                       or (not x.get("es_multiplo") and x.get("cantidad", 0) >= 5))]:
            cur.execute(
                """INSERT IGNORE INTO clientes_potenciales
                   (sku, producto, id_ventas, cantidad, fecha, nombre_comprador, precio, costo_unitario)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (l.get("codigo"), l.get("producto", ""), l["id_venta"], l.get("cantidad", 0),
                 l.get("fecha"), l.get("asin", ""), l.get("precio", 0), l.get("costo", 0)))
            stats["potenciales"] += cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
    return stats


# ---------- Orquestador ----------

async def run_job(dry_run: bool | None = None, motivo: str = "manual") -> dict:
    """Ejecuta el ciclo completo. Nunca truena sin dejar rastro en LAST_RUN."""
    import mov_reg
    from servicios.telegram.notificacion import send_telegram_alert
    dry = _dry_run_default() if dry_run is None else dry_run
    resumen = {"motivo": motivo, "dry_run": dry, "inicio": datetime.now(CDMX).isoformat()}
    try:
        from jobs.schema_ventas import verificar_esquema_job
        verificar_esquema_job()
        async with httpx.AsyncClient(timeout=30.0) as client:
            token = await _token()
            rep = await solicitar_y_esperar_reporte(client, token)
        if rep.get("accion") != "descargar":
            if rep.get("mensaje_html"):
                await send_telegram_alert(rep["mensaje_html"])
            resumen.update({"estado": rep["accion"], "reporte": rep})
        else:
            async with httpx.AsyncClient(timeout=30.0) as client:
                token2 = await _token()
                raw = await descargar_bytes(client, token2, rep["reportDocumentId"])
            lineas = parse_report_bytes(raw)
            costos, existentes, registro, ordenes = cargar_contexto()
            agrupadas = agrupar_lineas(lineas, costos, existentes, registro, ordenes)
            stats = aplicar_ventas(agrupadas, dry)
            for chunk in build_resumen(agrupadas):
                await send_telegram_alert(chunk)
            try:
                mov_reg.registrar_movimiento(
                    "job-amazon",
                    f"Job Amazon {motivo}: {stats['nuevas']} nuevas, {stats['descontadas']} descontadas, "
                    f"{stats['sin_stock']} sin stock, {stats['regresadas']} regresadas"
                    + (" (DRY_RUN)" if dry else ""), "Ventas")
            except Exception as err:
                print(f"Job Amazon: falló bitácora: {err}")
            resumen.update({"estado": "ok", "stats": stats,
                            "ordenes_nuevas": sorted({l["id_venta"] for l in agrupadas
                                                      if l.get("valida") and l.get("es_nueva")})})
    except Exception as err:
        resumen.update({"estado": "error", "error": str(err)})
        print(f"Job Amazon error: {err}")
        try:
            from servicios.telegram.notificacion import send_telegram_alert as _tg
            await _tg(f"⚠️ <b>Job Amazon falló</b>\n\n<code>{html.escape(str(err))}</code>")
        except Exception:
            pass
    resumen["fin"] = datetime.now(CDMX).isoformat()
    LAST_RUN.clear()
    LAST_RUN.update(resumen)
    # Aviso en vivo a todos los conectados (el Telegram ya es el registro).
    try:
        from routers.sofi_notificaciones import manager as _ws_manager
        await _ws_manager.broadcast({"tipo": "job", "job": "amazon", "estado": resumen.get("estado"),
                                     "dry_run": resumen.get("dry_run"), "stats": resumen.get("stats"),
                                     "fin": resumen.get("fin")})
    except Exception as err:
        print(f"Job Amazon: no se pudo avisar por WS: {err}")
    return resumen
