# Job de stock MeLi (migración del workflow n8n de sincronización).
# Publica stock_bodega a las publicaciones activas: diario (20:00 CDMX por defecto) +
# disparo manual ("Stock Meli" en Inventario, permiso general).
# Ignorados, multiplicadores, interruptor, dry-run y hora viven en BD y se editan en la
# página «Stock MeLi» (routers/meli_stock_config.py); las constantes SEMILLA_* solo siembran.
#
# Reglas (idénticas a n8n): multiplicador por publicación, 15% del stock
# (8% TAP*), mínimo 2 si hay paquetes, y solo PUT si cambio >50%,
# reabastecimiento (0→x) o agotado (x→0). Con variantes de color solo se
# toca la variante identificada sin ambigüedad.
import asyncio
import math
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

load_dotenv()

CDMX = ZoneInfo("America/Mexico_City")

LAST_RUN: dict = {"estado": "nunca", "detalle": None}

# Valores con los que nacen las tablas de configuración (meli_stock_ignorados y
# meli_stock_multiplicadores) la primera vez. Después la fuente de verdad es la BD,
# editable desde la página «Stock MeLi»; estas constantes solo siembran.
# Se quitaron del multiplicador los dos IDs que además estaban ignorados (el ignorado
# gana, así que su multiplicador nunca se usaba).
SEMILLA_IGNORAR = {"MLM2505625865", "MLM2505490861", "MLM4196624952", "MLM5895219342", "MLM4847073300", "MLM5895336324"}

SEMILLA_MULTIPLICADORES = {
    "MLM772970848": 2, "MLM796825413": 2, "MLM4139312152": 2, "MLM2505531285": 2,
    "MLM3107286609": 2, "MLM3107223607": 2, "MLM2505523267": 2,
    "MLM4362086278": 2, "MLM2755928607": 2, "MLM2755940821": 2, "MLM1855661223": 2,
    "MLM2505577019": 3, "MLM2505435551": 3, "MLM2505539033": 3, "MLM2505526455": 3,
    "MLM1818932463": 5, "MLM1622263356": 5, "MLM4139131818": 5, "MLM2505616857": 5,
    "MLM5895219490": 5, "MLM5674050134": 5, "MLM2505479163": 5, "MLM2505608107": 5,
    "MLM796828075": 5, "MLM770151130": 5, "MLM5674050138": 5, "MLM5674050136": 5,
    "MLM1826694524": 5, "MLM1878003676": 5, "MLM1928059875": 5, "MLM1928068227": 5,
    "MLM5674050140": 5, "MLM1855179253": 5, "MLM5895336322": 5, "MLM5895336320": 5,
    "MLM5895336318": 5, "MLM5895219488": 5, "MLM5895219486": 5,
    "MLM1818958169": 10, "MLM667955641": 10, "MLM1768862508": 10, "MLM2503503977": 10,
    "MLM5895336256": 10, "MLM5895336254": 10, "MLM2505580619": 10,
    "MLM2505529925": 10, "MLM2559464101": 10, "MLM5895336258": 10, "MLM5895219492": 10,
    "MLM4426635048": 10, "MLM4847067816": 10, "MLM643546543": 10, "MLM832663435": 10,
    "MLM5895219494": 10, "MLM5895219496": 10, "MLM1473969298": 10, "MLM1822370273": 10,
    "MLM2672059488": 10, "MLM1870533689": 10, "MLM5675154864": 10, "MLM3108591473": 10,
    "MLM3108604379": 10, "MLM5758082250": 10, "MLM5758082252": 10, "MLM5758082248": 10,
    "MLM5758446014": 10, "MLM5758446016": 10, "MLM5758446012": 10, "MLM5758446018": 10,
}

EXCEPCIONES_COLOR = {"PUR": "VIOLETA"}


def _env(nombre: str, default: str = "") -> str:
    return (os.getenv(nombre) or default).strip()


def _dry_run_default() -> bool:
    return _env("MELI_STOCK_DRY_RUN", "0") == "1"


# ---------- Lógica pura (testeable) ----------

def normalizar(s) -> str:
    import unicodedata
    return "".join(c for c in unicodedata.normalize("NFD", str(s or ""))
                   if unicodedata.category(c) != "Mn").upper()


def es_subsecuencia(codigo: str, texto: str) -> bool:
    i = 0
    for ch in texto:
        if i < len(codigo) and ch == codigo[i]:
            i += 1
    return i == len(codigo) and len(codigo) > 0


def extraer_codigo_color(sku: str) -> str:
    return re.sub(r"[0-9]+$", "", str(sku or "")).strip().upper()[-3:]


def encontrar_variante_color(variantes: list, sku_db: str):
    codigo = extraer_codigo_color(sku_db)
    objetivo = EXCEPCIONES_COLOR.get(codigo, codigo)
    candidatas = [v for v in (variantes or [])
                  if es_subsecuencia(objetivo, normalizar(
                      next((a.get("value_name", "") for a in (v.get("attribute_combinations") or [])
                            if normalizar(a.get("name")) == "COLOR"), "")))]
    return candidatas[0] if len(candidatas) == 1 else None


def calcular_nuevo_stock(sku_db: str, tipo_pub: str, stock_bodega: int, mult: int) -> tuple:
    paquetes = (int(stock_bodega) // mult) if mult else 0
    tasa = 0.08 if (str(sku_db).startswith("TAP") and str(tipo_pub or "")[:1].isdigit()) else 0.15
    nuevo = math.floor(paquetes * tasa)
    if nuevo < 0:
        nuevo = 0
    if paquetes > 0 and nuevo == 0:
        nuevo = 2
    return nuevo, paquetes


def debe_actualizar(nuevo: int, actual: int) -> tuple:
    dif = abs(nuevo - actual)
    pct = (dif / actual * 100) if actual > 0 else 0.0
    mayor50 = actual > 0 and pct > 50
    reabastecimiento = actual == 0 and nuevo > 0
    agotado = nuevo == 0 and actual > 0
    if not (mayor50 or reabastecimiento or agotado):
        return False, "", pct
    motivo = "Agotado" if agotado else ("Reabastecimiento" if reabastecimiento else "Cambio > 50%")
    return True, motivo, pct


ID_MELI_RE = re.compile(r"^MLM\d{6,}$")
MULT_MIN, MULT_MAX = 1, 1000
HORA_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def normalizar_id_meli(valor) -> str:
    """ID de publicación en mayúsculas y sin espacios; ValueError si no parece un MLM válido."""
    id_meli = str(valor or "").strip().upper()
    if not ID_MELI_RE.match(id_meli):
        raise ValueError("El código de MeLi debe verse como MLM seguido de números (ej. MLM1234567890)")
    return id_meli


def validar_multiplicador(valor) -> int:
    """Entero de 1 a 1000. 0 se rechaza: dejaría la publicación sin paquetes (stock 0) sin avisar."""
    if isinstance(valor, bool) or (isinstance(valor, float) and not valor.is_integer()):
        raise ValueError(f"El multiplicador debe ser un entero entre {MULT_MIN} y {MULT_MAX}")
    try:
        n = int(valor) if isinstance(valor, (int, float)) else int(str(valor).strip())
    except (TypeError, ValueError):
        raise ValueError(f"El multiplicador debe ser un entero entre {MULT_MIN} y {MULT_MAX}")
    if not MULT_MIN <= n <= MULT_MAX:
        raise ValueError(f"El multiplicador debe estar entre {MULT_MIN} y {MULT_MAX}")
    return n


def validar_hora(valor) -> str:
    """'HH:MM' en 24 h (hora de CDMX)."""
    hora = str(valor or "").strip()
    if not HORA_RE.match(hora):
        raise ValueError("La hora debe tener el formato HH:MM (24 horas), por ejemplo 20:00")
    return hora


def decidir_actualizaciones(detalles: list, db_map: dict, ignorar=frozenset(), multiplicadores=None) -> tuple:
    """(para_actualizar, sin_match) desde detalles MeLi + mapa id_meli->fila DB.

    `ignorar` (ids que no se tocan) y `multiplicadores` (id -> paquetes por pieza; sin entrada = 1)
    vienen de la configuración en BD (`cargar_config`). Si un id está en ambas, gana el ignorado.
    """
    multiplicadores = multiplicadores or {}
    para_actualizar, sin_match = [], []
    for det in detalles:
        item_id = str(det.get("id") or "").strip()
        if not item_id or item_id in ignorar:
            continue
        fila = db_map.get(item_id)
        if not fila:
            sin_match.append({"item_id": item_id, "sku_db": "", "motivo": "sin registro en DB"})
            continue
        sku_db = str(fila.get("sku") or "").strip().upper()
        stock_bodega = int(fila.get("stock_bodega") or 0)
        tipo_pub = det.get("buying_mode") or ""
        variantes = det.get("variations") or []
        titulo = det.get("title") or ""
        if variantes:
            var = encontrar_variante_color(variantes, sku_db)
            if not var:
                sin_match.append({
                    "item_id": item_id, "sku_db": sku_db, "motivo": "sin match de color",
                    "codigo_color": extraer_codigo_color(sku_db),
                    "colores": [next((a.get("value_name") for a in (v.get("attribute_combinations") or [])
                                       if normalizar(a.get("name")) == "COLOR"), None)
                                for v in variantes]})
                continue
            actual = int(var.get("available_quantity") or 0)
            variation_id = var.get("id")
        else:
            actual = int(det.get("available_quantity") or 0)
            variation_id = None
        mult = multiplicadores.get(item_id, 1)
        nuevo, paquetes = calcular_nuevo_stock(sku_db, tipo_pub, stock_bodega, mult)
        ok, motivo, pct = debe_actualizar(nuevo, actual)
        if ok:
            para_actualizar.append({
                "item_id": item_id, "variation_id": variation_id, "sku": sku_db, "titulo": titulo,
                "stock_bodega": stock_bodega, "mult": mult, "anterior": actual,
                "nuevo": nuevo, "motivo": motivo, "pct": round(pct),
            })
    return para_actualizar, sin_match


def extraer_codigo_error(resp_json, status: int) -> str:
    import json as _json
    texto = _json.dumps(resp_json or {}, ensure_ascii=False)
    m = re.search(r'"code"\s*:\s*"([^"\\]+)"', texto)
    if m:
        return m.group(1)
    if isinstance(resp_json, dict) and resp_json.get("code"):
        return str(resp_json["code"])
    return f"HTTP_{status}"


def build_resumen(exitosos: list, errores: dict, sin_match: list) -> list:
    import html
    esc = lambda s: html.escape(str(s or ""), quote=False)
    total_err = sum(errores.values())
    out = ["📊 <b>Stock MeLi</b>"]
    if exitosos:
        out.append(f"\n✅ <b>Actualizados ({len(exitosos)}):</b>")
        lista = [f"🔹 <code>{esc(e['item_id'])}</code> · {esc(e['sku'])} · "
                 f"{esc((e.get('titulo') or '')[:40])} · {e['anterior']}→{e['nuevo']} · {esc(e['motivo'])}"
                 for e in exitosos]
        out += lista[:30]
        if len(lista) > 30:
            out.append(f"... y {len(lista) - 30} más.")
    else:
        out.append("\n✅ <b>Actualizados:</b> 0")
    if total_err:
        out.append(f"\n❌ <b>Errores ({total_err}):</b>")
        out += [f"⚠️ <b>{esc(c)}</b>: {n} ítem(s)" for c, n in errores.items()]
    else:
        out.append("\n❌ <b>Errores:</b> 0")
    if sin_match:
        out.append(f"\n⚠️ <b>Sin match ({len(sin_match)}):</b>")
        out += [f"• <code>{esc(s['item_id'])}</code> · {esc(s.get('sku_db'))} · {esc(s.get('motivo'))}"
                for s in sin_match[:20]]
        if len(sin_match) > 20:
            out.append(f"... y {len(sin_match) - 20} más.")
    chunks, cur = [], ""
    for ln in out:
        if cur and len(cur) + 1 + len(ln) > 3800:
            chunks.append(cur)
            cur = ""
        cur += ("" if not cur else "\n") + ln
    if cur:
        chunks.append(cur)
    return chunks


# ---------- IO: MeLi + DB ----------

class MeliStockError(Exception):
    pass


def get_db_connection():
    import mysql.connector
    return mysql.connector.connect(host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
                                   password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"))


def asegurar_tabla_publicaciones():
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS publicaciones_meli (
                   sku VARCHAR(50) NOT NULL, id_meli VARCHAR(32) NOT NULL,
                   PRIMARY KEY (sku, id_meli), UNIQUE KEY uq_pub_meli (id_meli))""")
        conn.commit()
    finally:
        cursor.close()
        conn.close()


# ---------- Configuración editable (BD) ----------

class MeliStockConfigError(Exception):
    """No se pudo leer la configuración: se aborta la corrida (no se publica con listas viejas)."""


def hora_por_defecto() -> str:
    try:
        return validar_hora(_env("MELI_STOCK_HORA", "20:00"))
    except ValueError:
        return "20:00"


def crear_trigger(hora: str):
    """Cron diario a la hora dada (CDMX). Lo usan el lifespan y el cambio de hora desde la página."""
    from apscheduler.triggers.cron import CronTrigger
    hh, mm = validar_hora(hora).split(":")
    return CronTrigger(hour=int(hh), minute=int(mm), timezone=CDMX)


def crear_tablas_config():
    """Crea las tablas de configuración y, solo la primera vez, las siembra con las listas históricas.

    La siembra ocurre únicamente si la fila de meli_stock_config no existía: si alguien borra un
    ignorado desde la página, no vuelve a aparecer en el siguiente arranque. Idempotente.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS meli_stock_ignorados (
                   id_meli VARCHAR(32) NOT NULL PRIMARY KEY,
                   nota VARCHAR(200) NULL,
                   creado_por VARCHAR(100) NULL,
                   creado_en TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
               ) ENGINE=InnoDB""")
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS meli_stock_multiplicadores (
                   id_meli VARCHAR(32) NOT NULL PRIMARY KEY,
                   multiplicador INT NOT NULL,
                   actualizado_por VARCHAR(100) NULL,
                   actualizado_en TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
               ) ENGINE=InnoDB""")
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS meli_stock_config (
                   id TINYINT NOT NULL PRIMARY KEY,
                   habilitado TINYINT(1) NOT NULL DEFAULT 1,
                   dry_run TINYINT(1) NOT NULL DEFAULT 0,
                   hora VARCHAR(5) NOT NULL DEFAULT '20:00',
                   version INT NOT NULL DEFAULT 1,
                   actualizado_por VARCHAR(100) NULL,
                   actualizado_en TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
               ) ENGINE=InnoDB""")
        cursor.execute("SELECT COUNT(*) FROM meli_stock_config")
        primera_vez = (cursor.fetchone() or [0])[0] == 0
        if primera_vez:
            cursor.execute("INSERT INTO meli_stock_config (id, habilitado, dry_run, hora) VALUES (1, 1, %s, %s)",
                           (1 if _dry_run_default() else 0, hora_por_defecto()))
            cursor.executemany(
                "INSERT IGNORE INTO meli_stock_ignorados (id_meli, nota, creado_por) VALUES (%s, %s, 'sistema')",
                [(i, "Semilla del job original") for i in sorted(SEMILLA_IGNORAR)])
            cursor.executemany(
                "INSERT IGNORE INTO meli_stock_multiplicadores (id_meli, multiplicador, actualizado_por) VALUES (%s, %s, 'sistema')",
                [(i, m) for i, m in sorted(SEMILLA_MULTIPLICADORES.items()) if i not in SEMILLA_IGNORAR])
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def cargar_config() -> dict:
    """Lee la configuración vigente: interruptor, dry-run, hora, ignorados y multiplicadores.

    Se llama una vez al inicio de cada corrida. Lanza MeliStockConfigError si la BD falla o
    la fila de configuración no existe (el job no corre con valores supuestos).
    """
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT habilitado, dry_run, hora, version FROM meli_stock_config WHERE id = 1")
            fila = cur.fetchone()
            if not fila:
                raise MeliStockConfigError("No existe la fila de configuración de Stock MeLi")
            cur.execute("SELECT id_meli FROM meli_stock_ignorados")
            ignorar = {str(f["id_meli"]).strip().upper() for f in cur.fetchall()}
            cur.execute("SELECT id_meli, multiplicador FROM meli_stock_multiplicadores")
            mults = {str(f["id_meli"]).strip().upper(): int(f["multiplicador"]) for f in cur.fetchall()
                     if int(f["multiplicador"]) >= MULT_MIN}
        finally:
            cur.close()
            conn.close()
    except MeliStockConfigError:
        raise
    except Exception as err:
        raise MeliStockConfigError(f"No se pudo leer la configuración de Stock MeLi: {err}") from err
    return {"habilitado": bool(fila["habilitado"]), "dry_run": bool(fila["dry_run"]),
            "hora": fila["hora"], "version": int(fila["version"]),
            "ignorar": ignorar, "multiplicadores": mults}


async def _ids_activos(client: httpx.AsyncClient, token: str) -> list:
    from jobs.meli_ventas import _get, MELI_API
    me = await _get(client, f"{MELI_API}/users/me", token)
    seller = me.get("id")
    if not seller:
        raise MeliStockError(f"/users/me sin id: {me}")
    ids: list = []
    for page in range(50):
        data = await _get(client, f"{MELI_API}/users/{seller}/items/search", token,
                          {"limit": 200, "status": "active", "offset": page * 200})
        results = data.get("results") or []
        ids += [str(r) for r in results]
        if len(results) < 200:
            break
    return ids


async def _detalles(client: httpx.AsyncClient, token: str, ids: list) -> list:
    from jobs.meli_ventas import _get, MELI_API
    out = []
    for i in range(0, len(ids), 20):
        lote = ids[i:i + 20]
        data = await _get(client, f"{MELI_API}/items", token,
                          {"ids": ",".join(lote),
                           "attributes": "id,title,available_quantity,seller_custom_field,shipping,variations,buying_mode"})
        for r in (data if isinstance(data, list) else []):
            if r.get("code") == 200 and isinstance(r.get("body"), dict):
                out.append(r["body"])
    return out


def mapa_db(ids: list) -> dict:
    if not ids:
        return {}
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        ph = ",".join(["%s"] * len(ids))
        cur.execute(f"SELECT v.sku, v.stock_bodega, s.id_meli FROM productos v "
                    f"JOIN publicaciones_meli s ON v.sku = s.sku WHERE s.id_meli IN ({ph})", tuple(ids))
        return {str(f["id_meli"]): f for f in cur.fetchall() if f.get("id_meli")}
    finally:
        cur.close()
        conn.close()


async def _put_item(client: httpx.AsyncClient, token: str, upd: dict) -> tuple:
    from jobs.meli_ventas import MELI_API
    body = ({"variations": [{"id": upd["variation_id"], "available_quantity": upd["nuevo"]}]}
            if upd.get("variation_id") else {"available_quantity": upd["nuevo"]})
    try:
        r = await client.put(f"{MELI_API}/items/{upd['item_id']}",
                             headers={"Authorization": f"Bearer {token}"},
                             json=body, timeout=30.0)
        if r.status_code >= 400:
            try:
                data = r.json()
            except Exception:
                data = None
            return False, extraer_codigo_error(data, r.status_code)
        return True, None
    except httpx.HTTPError as err:
        return False, f"RED_{type(err).__name__}"


async def _enviar_lote(lista: list) -> tuple:
    """PUT de cada actualización con token fresco. Devuelve (exitosos, [(upd, codigo)])."""
    from jobs.meli_ventas import _meli_token
    exitosos, fallidos = [], []
    if not lista:
        return exitosos, fallidos
    async with httpx.AsyncClient(timeout=30.0) as client:
        token = await _meli_token(client)
        for upd in lista:
            ok, codigo = await _put_item(client, token, upd)
            if ok:
                exitosos.append({**upd, "titulo": upd.get("titulo")})
            else:
                fallidos.append((upd, codigo))
            await asyncio.sleep(1)
    return exitosos, fallidos


# ---------- Reintentos ----------

MAX_REINTENTOS = 2  # además de la corrida original


def _espera_reintento_seg() -> int:
    try:
        return max(1, int(float(_env("MELI_STOCK_REINTENTO_MIN", "10")) * 60))
    except ValueError:
        return 600


async def _esperar(segundos: int):
    await asyncio.sleep(segundos)


def formatear_espera(segundos: int) -> str:
    return f"{segundos} s" if segundos < 60 else f"{round(segundos / 60)} min"


def aviso_reintento(que: str, intento: int, espera_seg: int, codigos: dict | None = None) -> str:
    texto = (f"⚠️ Stock MeLi: MeLi rechazó la petición de {que}. "
             f"Se volverá a intentar en {formatear_espera(espera_seg)} "
             f"(reintento {intento} de {MAX_REINTENTOS}).")
    if codigos:
        texto += " Motivos: " + ", ".join(f"{c} ({n})" for c, n in codigos.items()) + "."
    return texto


# ---------- Orquestador ----------

async def run_job(dry_run: bool | None = None, motivo: str = "manual", telegram: bool = True) -> dict:
    from servicios import mov_reg
    from servicios.telegram.notificacion import send_telegram_alert
    dry = dry_run
    resumen = {"motivo": motivo, "dry_run": dry, "inicio": datetime.now(CDMX).isoformat()}

    async def avisar(texto: str):
        if telegram:
            await send_telegram_alert(texto)

    espera = _espera_reintento_seg()
    try:
        from jobs.meli_ventas import _meli_token

        # Configuración editable (página «Stock MeLi»): se lee UNA vez; si la BD falla se aborta.
        cfg = cargar_config()
        # Interruptor: solo detiene la corrida programada; el disparo manual sigue funcionando.
        if motivo == "scheduler" and not cfg["habilitado"]:
            resumen.update({"estado": "deshabilitado", "dry_run": cfg["dry_run"],
                            "detalle": "El job está apagado desde la página Stock MeLi",
                            "fin": datetime.now(CDMX).isoformat()})
            LAST_RUN.clear()
            LAST_RUN.update(resumen)
            return resumen
        dry = cfg["dry_run"] if dry_run is None else dry_run
        resumen["dry_run"] = dry

        async def _recolectar():
            async with httpx.AsyncClient(timeout=30.0) as client:
                token = await _meli_token(client)
                ids = await _ids_activos(client, token)
                return await _detalles(client, token, ids)

        # Fase de lectura: el job original + hasta MAX_REINTENTOS reintentos.
        for intento in range(MAX_REINTENTOS + 1):
            try:
                detalles = await _recolectar()
                break
            except Exception as err:
                print(f"Job stock MeLi: lectura falló (intento {intento + 1}): {err}")
                if intento >= MAX_REINTENTOS:
                    raise
                await avisar(aviso_reintento("la consulta de publicaciones", intento + 1, espera))
                await _esperar(espera)
        db_map = mapa_db([d.get("id") for d in detalles if d.get("id")])
        para_actualizar, sin_match = decidir_actualizaciones(
            detalles, db_map, cfg["ignorar"], cfg["multiplicadores"])
        stats = {"revisados": len(detalles), "candidatos": len(para_actualizar),
                 "exitosos": 0, "errores": 0, "sin_match": len(sin_match),
                 "reintentos": 0, "dry_run": dry}
        exitosos, errores = [], {}
        if dry:
            resumen.update({"estado": "ok", "stats": stats, "pendientes": para_actualizar[:50]})
        else:
            pendientes = para_actualizar
            for intento in range(MAX_REINTENTOS + 1):
                ok_lote, fallidos = await _enviar_lote(pendientes)
                exitosos += ok_lote
                if not fallidos or intento >= MAX_REINTENTOS:
                    pendientes = fallidos
                    break
                codigos = {}
                for _, codigo in fallidos:
                    codigos[codigo] = codigos.get(codigo, 0) + 1
                await avisar(aviso_reintento(
                    f"la actualización de {len(fallidos)} publicación(es)", intento + 1, espera, codigos))
                await _esperar(espera)
                stats["reintentos"] += 1
                pendientes = [u for u, _ in fallidos]
            for _, codigo in pendientes:
                errores[codigo] = errores.get(codigo, 0) + 1
            stats["exitosos"] = len(exitosos)
            stats["errores"] = len(pendientes)
            for chunk in build_resumen(exitosos, errores, sin_match):
                await avisar(chunk)
            try:
                mov_reg.registrar_movimiento(
                    "job-meli-stock", f"Stock MeLi {motivo}: {stats['exitosos']} actualizados, "
                    f"{stats['errores']} errores, {stats['sin_match']} sin match"
                    + (" (DRY_RUN)" if dry else ""), "Productos")
            except Exception as err:
                print(f"Job stock MeLi: falló bitácora: {err}")
            if not dry:
                try:
                    from servicios import notificaciones_service
                    await notificaciones_service.crear_y_notificar_todos(
                        "Stock MeLi publicado",
                        f"Stock MeLi: {stats['exitosos']} actualizados, {stats['errores']} errores, "
                        f"{stats['sin_match']} sin match.",
                        "success" if not errores else "warn")
                except Exception as err:
                    print(f"Job stock MeLi: falló persistir notificaciones: {err}")
            resumen.update({"estado": "ok" if not errores else "ok_con_errores", "stats": stats})
    except MeliStockConfigError as err:
        resumen.update({"estado": "error", "error": str(err)})
        print(f"Job stock MeLi error de configuración: {err}")
        await avisar("❌ Stock MeLi: no se pudo leer la configuración (ignorados y multiplicadores). "
                     "No se publicó stock para no usar valores desactualizados; se intentará en la próxima "
                     "ejecución programada o puedes lanzarlo manualmente.")
    except Exception as err:
        resumen.update({"estado": "error", "error": str(err)})
        print(f"Job stock MeLi error: {err}")
        await avisar(f"❌ Stock MeLi: MeLi rechazó o no respondió la petición y se agotaron los "
                     f"{MAX_REINTENTOS} reintentos. No se publicó stock; se intentará en la próxima "
                     f"ejecución programada o puedes lanzarlo manualmente.")
    resumen["fin"] = datetime.now(CDMX).isoformat()
    LAST_RUN.clear()
    LAST_RUN.update(resumen)
    try:
        from routers.sofi_notificaciones import manager as _ws_manager
        await _ws_manager.broadcast({"tipo": "job", "job": "meli-stock", "estado": resumen.get("estado"),
                                     "dry_run": resumen.get("dry_run"), "stats": resumen.get("stats"),
                                     "fin": resumen.get("fin")})
    except Exception as err:
        print(f"Job stock MeLi: no se pudo avisar por WS: {err}")
    return resumen
