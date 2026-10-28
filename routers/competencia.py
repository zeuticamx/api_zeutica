# Análisis de Competencia ML: extrae una página de Mercado Libre México (Context.dev), la evalúa con Claude y
# guarda el resultado en `competencia_analisis`. Esa misma tabla es el historial, el cupo y la caché:
#   - Cupo: 1 análisis NUEVO por usuario por semana calendario (lunes a domingo, hora de México); gerencia
#     queda exenta. Solo cuentan los terminados bien (y los en curso de hace menos de 10 min): un fallo no
#     gasta el cupo. La reserva se hace con GET_LOCK por usuario para que dos clics no se cuelen.
#   - Caché: misma URL + enfoque del mismo usuario con resultado de menos de 24 h se reutiliza sin llamar a
#     las APIs ni gastar cupo (el precio sugerido se recalcula con el SKU y margen de la nueva consulta).
# Los números (promedio, mediana, precio sugerido) salen de competencia_logica.py, no del LLM. Usuario de la
# bitácora = token. Rutas fijas antes de /competencia/{id}.
import functools
import json
import os
from datetime import datetime, timedelta
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import mysql.connector, mov_reg
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import competencia_logica as cl
import competencia_service as svc
from errores_db import detalle_error_db
from permisos import es_gerencia, usuario_autenticado

router = APIRouter(tags=["/competencia"], responses={404: {"Mensaje": "No encontrado"}})

load_dotenv()

CDMX = ZoneInfo("America/Mexico_City")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "competencia_schema.sql")
SECCION = "Análisis de Competencia"
LIMITE_SEMANAL = 1
CACHE_HORAS = 24
EN_PROCESO_MINUTOS = 10


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )


def _ahora() -> datetime:
    """Hora de México sin zona (así se guardan y comparan todas las fechas de la tabla)."""
    return datetime.now(CDMX).replace(tzinfo=None, microsecond=0)


def crear_tabla_competencia():
    """Crea competencia_analisis si no existe. Se llama en el lifespan. Idempotente."""
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        lineas = [l for l in f if not l.strip().startswith("--")]
    statements = [s.strip() for s in "".join(lineas).split(";") if s.strip()]
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        for stmt in statements:
            cursor.execute(stmt)
        conn.commit()
        print("Tabla competencia_analisis verificada/creada.")
    finally:
        cursor.close()
        conn.close()


def _con_errores_db(fn):
    @functools.wraps(fn)
    def envoltura(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except mysql.connector.Error as err:
            raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))
    return envoltura


# ---------- Acceso a datos ----------

def _contar_usados(cursor, usuario: str, ahora: datetime) -> int:
    inicio, fin = cl.semana_actual(ahora)
    cursor.execute(
        "SELECT COUNT(*) FROM competencia_analisis WHERE usuario = %s AND creado_en >= %s AND creado_en < %s "
        "AND origen_cache_id IS NULL AND (estado = 'ok' OR (estado = 'en_proceso' AND creado_en >= %s))",
        (usuario, inicio, fin, ahora - timedelta(minutes=EN_PROCESO_MINUTOS)))
    return int(cursor.fetchone()[0])


@_con_errores_db
def obtener_cupo(usuario: str, ahora: datetime) -> dict:
    inicio, fin = cl.semana_actual(ahora)
    ilimitado = es_gerencia(usuario)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        usados = _contar_usados(cursor, usuario, ahora)
    finally:
        cursor.close()
        conn.close()
    return {"limite": None if ilimitado else LIMITE_SEMANAL, "usados": usados, "ilimitado": ilimitado,
            "restantes": None if ilimitado else max(0, LIMITE_SEMANAL - usados),
            "semana_inicio": inicio.isoformat(), "renueva_en": fin.isoformat()}


@_con_errores_db
def costo_de_sku(sku: str):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT costo_total FROM productos WHERE sku = %s AND eliminado = 0", (sku,))
        fila = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()
    if not fila:
        raise HTTPException(status_code=404, detail=f"El SKU {sku} no existe o está eliminado")
    return fila["costo_total"]


@_con_errores_db
def buscar_cache(usuario: str, url: str, enfoque: str, ahora: datetime) -> Optional[dict]:
    """Último análisis bueno del usuario para esa URL y enfoque de las últimas 24 h (None si no hay)."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, resultado, truncado FROM competencia_analisis "
            "WHERE usuario = %s AND url = %s AND enfoque = %s AND estado = 'ok' AND resultado IS NOT NULL "
            "AND origen_cache_id IS NULL AND finalizado_en >= %s ORDER BY id DESC LIMIT 1",
            (usuario, url, enfoque, ahora - timedelta(hours=CACHE_HORAS)))
        fila = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()
    if not fila:
        return None
    try:
        resultado = json.loads(fila["resultado"])
    except (TypeError, ValueError):
        return None
    # Solo se reutilizan análisis originales (no reutilizaciones): la ventana de 24 h no se alarga sola.
    return {"origen": fila["id"], "resultado": resultado, "truncado": bool(fila["truncado"])}


@_con_errores_db
def reservar_cupo(usuario: str, url: str, enfoque: str, sku, margen, comision, ahora: datetime) -> int:
    """Revisa el cupo semanal y crea la fila `en_proceso`, todo bajo un candado por usuario (así dos
    solicitudes simultáneas no pasan las dos). 429 si ya usó su análisis de la semana."""
    conn = get_db_connection()
    cursor = conn.cursor()
    candado = f"competencia:{usuario}"[:60]
    try:
        cursor.execute("SELECT GET_LOCK(%s, 5)", (candado,))
        if cursor.fetchone()[0] != 1:
            raise HTTPException(status_code=409, detail="Ya tienes un análisis en curso: espera a que termine")
        try:
            if not es_gerencia(usuario) and _contar_usados(cursor, usuario, ahora) >= LIMITE_SEMANAL:
                _, fin = cl.semana_actual(ahora)
                raise HTTPException(
                    status_code=429,
                    detail=f"Ya usaste tu análisis de esta semana. Se renueva el lunes {fin.strftime('%d/%m/%Y')}",
                    headers={"Retry-After": str(int((fin - ahora).total_seconds()))})
            cursor.execute(
                "INSERT INTO competencia_analisis (usuario, url, enfoque, sku, margen_objetivo, comision_pct, estado, creado_en) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'en_proceso', %s)", (usuario, url, enfoque, sku, margen, comision, ahora))
            nuevo = cursor.lastrowid
            conn.commit()
            return nuevo
        finally:
            try:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (candado,))
                cursor.fetchall()
            except mysql.connector.Error:
                pass
    finally:
        cursor.close()
        conn.close()


@_con_errores_db
def guardar_cache(usuario, url, enfoque, sku, margen, comision, origen: int, resultado: dict, truncado: bool, ahora) -> int:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO competencia_analisis (usuario, url, enfoque, sku, margen_objetivo, comision_pct, estado, costo_base, "
            "precio_sugerido, precio_promedio, resultado, truncado, origen_cache_id, creado_en, finalizado_en) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'ok', %s, %s, %s, %s, %s, %s, %s, %s)",
            (usuario, url, enfoque, sku, margen, comision, resultado.get("costo_base"), _precio_sugerido(resultado),
             _precio_promedio(resultado), json.dumps(resultado, ensure_ascii=False), int(truncado), origen, ahora, ahora))
        conn.commit()
        return cursor.lastrowid
    finally:
        cursor.close()
        conn.close()


def finalizar_ok(id_: int, resultado: dict, truncado: bool, ahora: datetime):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE competencia_analisis SET estado = 'ok', costo_base = %s, precio_sugerido = %s, precio_promedio = %s, "
            "resultado = %s, truncado = %s, finalizado_en = %s WHERE id = %s",
            (resultado.get("costo_base"), _precio_sugerido(resultado), _precio_promedio(resultado),
             json.dumps(resultado, ensure_ascii=False), int(truncado), ahora, id_))
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def finalizar_error(id_: int, origen: str, detalle: str):
    """Marca la fila como error (no gasta cupo). Nunca lanza: se llama mientras ya se está fallando."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        try:
            cursor.execute("UPDATE competencia_analisis SET estado = 'error', error_origen = %s, error_detalle = %s, "
                           "finalizado_en = %s WHERE id = %s", (origen[:20], str(detalle)[:300], _ahora(), id_))
            conn.commit()
        finally:
            cursor.close()
            conn.close()
    except Exception as err:
        print(f"[competencia] No se pudo marcar el análisis {id_} como error: {err}")


# ---------- Armado del resultado ----------

def _precio_sugerido(resultado: dict):
    return (resultado.get("sugerencia") or {}).get("precio_sugerido")


def _precio_promedio(resultado: dict):
    return (resultado.get("estadisticas") or {}).get("promedio")


def armar_resultado(base: dict, costo, margen, comision) -> dict:
    """Une lo que dijo el LLM con los números calculados aquí. `base` trae precios Decimal (análisis nuevo)
    o float (leído de la caché): se rehidratan para calcular igual en ambos casos."""
    competidores = [{**c, "precio": cl.a_decimal(c.get("precio"))} for c in base.get("competidores", [])]
    estadisticas = cl.estadisticas_precios(competidores)
    sugerencia = cl.precio_sugerido(costo, margen, comision, estadisticas) if margen is not None else None
    costo_dec = cl.a_decimal(costo) if margen is not None else None
    return cl.a_json({
        "resumen": base.get("resumen"), "nivel_oferta": base.get("nivel_oferta"),
        "competidores": competidores,
        "fortalezas": base.get("fortalezas", []), "debilidades": base.get("debilidades", []),
        "insights": base.get("insights", []),
        "estadisticas": estadisticas, "sugerencia": sugerencia, "costo_base": costo_dec,
    })


def _fecha(valor):
    return valor.isoformat() if isinstance(valor, datetime) else None


def _numero(valor):
    return float(valor) if valor is not None else None


def _fila_publica(f: dict, con_resultado: bool) -> dict:
    salida = {
        "id": f["id"], "usuario": f["usuario"], "url": f["url"], "enfoque": f["enfoque"], "sku": f["sku"],
        "margen_objetivo": _numero(f["margen_objetivo"]), "comision_pct": _numero(f["comision_pct"]),
        "estado": f["estado"], "precio_sugerido": _numero(f["precio_sugerido"]),
        "precio_promedio": _numero(f["precio_promedio"]), "truncado": bool(f["truncado"]),
        "reutilizado": f["origen_cache_id"] is not None, "error_origen": f["error_origen"],
        "error_detalle": f["error_detalle"], "creado_en": _fecha(f["creado_en"]),
        "finalizado_en": _fecha(f["finalizado_en"]),
    }
    if con_resultado:
        try:
            salida["resultado"] = json.loads(f["resultado"]) if f.get("resultado") else None
        except ValueError:
            salida["resultado"] = None
    return salida


_COLUMNAS = ("id, usuario, url, enfoque, sku, margen_objetivo, comision_pct, estado, precio_sugerido, precio_promedio, "
             "truncado, origen_cache_id, error_origen, error_detalle, creado_en, finalizado_en")


@_con_errores_db
def leer_analisis(id_: int) -> Optional[dict]:
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(f"SELECT {_COLUMNAS}, resultado FROM competencia_analisis WHERE id = %s", (id_,))
        return cursor.fetchone()
    finally:
        cursor.close()
        conn.close()


@_con_errores_db
def listar_historial(usuario: Optional[str], limite: int) -> list:
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        if usuario:
            cursor.execute(f"SELECT {_COLUMNAS} FROM competencia_analisis WHERE usuario = %s ORDER BY id DESC LIMIT %s",
                           (usuario, limite))
        else:
            cursor.execute(f"SELECT {_COLUMNAS} FROM competencia_analisis ORDER BY id DESC LIMIT %s", (limite,))
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()


# ---------- Rutas ----------

class AnalizarIn(BaseModel):
    url: str = Field(max_length=cl.MAX_URL + 100)
    enfoque: Literal["completo", "precios", "fortalezas"] = "completo"
    sku: Optional[str] = Field(default=None, max_length=100)
    margen_objetivo: Optional[float] = None   # % sobre ingreso neto sin IVA; obligatorio si se manda SKU
    comision_pct: Optional[float] = 0         # comisión de Mercado Libre, % del precio publicado


def _error_servicio(err: svc.CompetenciaError) -> HTTPException:
    headers = {"Retry-After": str(err.retry_after)} if err.retry_after else None
    return HTTPException(status_code=err.status, detail=str(err), headers=headers)


@router.get("/competencia/cupo")
async def cupo(usuario: str = Depends(usuario_autenticado)):
    """Cuántos análisis nuevos le quedan al usuario esta semana y cuándo se renueva."""
    return obtener_cupo(usuario, _ahora())


@router.get("/competencia/historial")
async def historial(usuario_filtro: Optional[str] = None, limite: int = 50, usuario: str = Depends(usuario_autenticado)):
    """Análisis anteriores: cada quien ve los suyos; gerencia ve todos (y puede filtrar por usuario)."""
    limite = max(1, min(limite, 200))
    if es_gerencia(usuario):
        objetivo = (usuario_filtro or "").strip() or None
    else:
        if usuario_filtro and usuario_filtro.strip().lower() != usuario.lower():
            raise HTTPException(status_code=403, detail="Solo puedes ver tu propio historial")
        objetivo = usuario
    return [_fila_publica(f, False) for f in listar_historial(objetivo, limite)]


@router.get("/competencia/{id_analisis}")
async def detalle(id_analisis: int, usuario: str = Depends(usuario_autenticado)):
    """Un análisis guardado completo (sin volver a llamar a las APIs). Dueño o gerencia."""
    fila = leer_analisis(id_analisis)
    if not fila:
        raise HTTPException(status_code=404, detail="Análisis no encontrado")
    if fila["usuario"].lower() != usuario.lower() and not es_gerencia(usuario):
        raise HTTPException(status_code=403, detail="Ese análisis es de otra persona")
    return _fila_publica(fila, True)


def _bitacora(usuario: str, texto: str):
    try:
        mov_reg.registrar_movimiento(usuario, texto, SECCION)
    except Exception as err:  # el análisis ya se hizo y se pagó: no se pierde por la bitácora
        print(f"[competencia] No se pudo registrar en la bitácora: {err}")


@router.post("/competencia/analizar")
async def analizar(datos: AnalizarIn, usuario: str = Depends(usuario_autenticado)):
    """Extrae la página, la evalúa con Claude y devuelve el análisis estructurado + precio sugerido."""
    try:
        url = cl.validar_url(datos.url)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err))

    sku = (datos.sku or "").strip() or None
    margen = comision = costo = None
    if sku and datos.margen_objetivo is None:
        raise HTTPException(status_code=422, detail="Indica el margen de ganancia esperado para calcular el precio sugerido")
    if not sku and datos.margen_objetivo is not None:
        raise HTTPException(status_code=422, detail="Elige el SKU del producto para calcular el precio sugerido")
    if sku:
        try:
            margen, comision = cl.validar_margen_comision(datos.margen_objetivo, datos.comision_pct)
        except ValueError as err:
            raise HTTPException(status_code=422, detail=str(err))
        costo = costo_de_sku(sku)

    ahora = _ahora()
    firma = (usuario, url, datos.enfoque, sku, margen, comision)

    previo = buscar_cache(usuario, url, datos.enfoque, ahora)
    if previo:
        resultado = armar_resultado(previo["resultado"], costo, margen, comision)
        id_ = guardar_cache(*firma, previo["origen"], resultado, previo["truncado"], ahora)
        _bitacora(usuario, f"Análisis de competencia (reutilizado, sin gasto): {url} [{datos.enfoque}]")
        reutilizado, truncado = True, previo["truncado"]
    else:
        id_ = reservar_cupo(*firma, ahora)
        try:
            markdown = await svc.extraer_markdown(url)
            markdown, truncado = cl.truncar(markdown, svc.max_caracteres())
            texto = await svc.evaluar(cl.SYSTEM_PROMPT, cl.construir_prompt_usuario(url, datos.enfoque, markdown))
            try:
                base = cl.validar_resultado(cl.extraer_json(texto))
            except ValueError as err:
                print(f"[competencia] Respuesta del modelo inválida: {err}")
                raise svc.CompetenciaError("anthropic", "La IA no devolvió un análisis utilizable: intenta de nuevo", 502)
            resultado = armar_resultado(base, costo, margen, comision)
            finalizar_ok(id_, resultado, truncado, _ahora())
        except svc.CompetenciaError as err:
            finalizar_error(id_, err.origen, str(err))
            raise _error_servicio(err)
        except BaseException as err:  # incluye cancelación del cliente: no dejar la fila colgada
            finalizar_error(id_, "interno", type(err).__name__)
            raise
        _bitacora(usuario, f"Análisis de competencia: {url} [{datos.enfoque}]" + (f" · SKU {sku}" if sku else ""))
        reutilizado = False

    fila = leer_analisis(id_)
    return {**_fila_publica(fila, True), "reutilizado": reutilizado, "truncado": truncado,
            "cupo": obtener_cupo(usuario, _ahora())}
