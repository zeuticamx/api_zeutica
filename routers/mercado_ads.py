# Motor Zentor (página Mercado Ads): métricas de Product Ads, reglas, estado del motor y acciones.
# Lectura (métricas y reglas) con cualquier sesión; todo lo que cambia algo (reglas, motor, aplicar,
# evaluar) es solo gerencia. El diagnóstico vive en zentor_logica.py (lo comparte el job del piloto,
# jobs/zentor_piloto.py); el cliente de Mercado Ads en zentor_service.py. Usuario de la bitácora = token.
import os
from datetime import date, datetime
from typing import Literal, Optional
from zoneinfo import ZoneInfo

import mysql.connector, mov_reg
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import zentor_logica as zl
import zentor_service
from errores_db import detalle_error_db
from permisos import requerir_gerencia, usuario_autenticado

router = APIRouter(tags=["/mercado-ads"], responses={404: {"Mensaje": "No encontrado"}})

load_dotenv()  # Carga de credenciales .env

CDMX = ZoneInfo("America/Mexico_City")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "zentor_schema.sql")
SECCION = "Mercado Ads"


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )


def crear_tabla_zentor():
    """Crea zentor_reglas (con su fila única) si no existe. Se llama en el lifespan. Idempotente."""
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        lineas = [l for l in f if not l.strip().startswith("--")]
    statements = [s.strip() for s in "".join(lineas).split(";") if s.strip()]
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        for stmt in statements:
            cursor.execute(stmt)
        conn.commit()
        print("Tabla zentor_reglas verificada/creada.")
    finally:
        cursor.close()
        conn.close()


def leer_reglas() -> dict:
    """Reglas + estado del motor (la fila única). Lo usan el router y el job del piloto."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT target_acos, max_clicks_without_sale, min_spend_threshold, auto_pilot, "
                       "estado_motor, version, actualizado_por, actualizado_en FROM zentor_reglas WHERE id = 1")
        fila = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()
    if not fila:
        return {**zl.REGLAS_DEFAULT, "estado_motor": zl.MOTOR_ACTIVO, "version": 0,
                "actualizado_por": None, "actualizado_en": None}
    en = fila["actualizado_en"]
    return {
        "target_acos": float(fila["target_acos"]),
        "max_clicks_without_sale": int(fila["max_clicks_without_sale"]),
        "min_spend_threshold": float(fila["min_spend_threshold"]),
        "auto_pilot": bool(fila["auto_pilot"]),
        "estado_motor": fila["estado_motor"],
        "version": int(fila["version"]),
        "actualizado_por": fila["actualizado_por"],
        "actualizado_en": en.isoformat() if isinstance(en, datetime) else None,
    }


def _con_estado(reglas: dict) -> dict:
    return {**reglas, "estado": zl.estado_efectivo(reglas["estado_motor"], reglas["auto_pilot"])}


def _lanzar_error_ads(err: zentor_service.MercadoAdsError):
    """503 si no hay conexión usable con Mercado Ads (token/scope), 502 si falló la consulta."""
    if err.sin_acceso:
        raise HTTPException(status_code=503, detail=f"Sin conexión con Mercado Ads: {err}")
    raise HTTPException(status_code=502, detail=f"No se pudo consultar Mercado Ads: {err}")


def _leer_reglas_o_500() -> dict:
    try:
        return leer_reglas()
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))


def _rango_o_422(desde: Optional[date], hasta: Optional[date]) -> tuple:
    try:
        return zl.rango_fechas(desde, hasta, datetime.now(CDMX).date())
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err))


def _reglas_de_diagnostico(reglas: dict) -> dict:
    return {k: reglas[k] for k in ("target_acos", "max_clicks_without_sale", "min_spend_threshold")}


async def cargar_metricas(desde: date, hasta: date, reglas: dict) -> dict:
    """Anuncios del rango con su diagnóstico y los KPIs globales (sin tocar nada en MeLi)."""
    crudos = await zentor_service.listar_anuncios(desde, hasta)
    pubs, evaluadas = zl.evaluar(crudos, _reglas_de_diagnostico(reglas))
    return {
        "desde": desde.isoformat(), "hasta": hasta.isoformat(),
        "kpis": zl.agregar_kpis(pubs, evaluadas),
        "publicaciones": evaluadas,
    }


# ---------- Lectura (cualquier sesión) ----------

@router.get("/mercado-ads/metricas")
async def metricas(desde: Optional[date] = None, hasta: Optional[date] = None,
                   usuario: str = Depends(usuario_autenticado)):
    """KPIs y publicaciones con diagnóstico del rango (por defecto, últimos 7 días; máximo 90)."""
    desde, hasta = _rango_o_422(desde, hasta)
    reglas = _leer_reglas_o_500()
    try:
        datos = await cargar_metricas(desde, hasta, reglas)
    except zentor_service.MercadoAdsError as err:
        _lanzar_error_ads(err)
    return {**datos, "reglas": _con_estado(reglas)}


@router.get("/mercado-ads/reglas")
async def consultar_reglas(usuario: str = Depends(usuario_autenticado)):
    """Reglas del motor, su estado efectivo y la última corrida del job."""
    from jobs import zentor_piloto
    return {**_con_estado(_leer_reglas_o_500()), "ultima_corrida": zentor_piloto.LAST_RUN}


# ---------- Escritura (solo gerencia) ----------

class ReglasIn(BaseModel):
    target_acos: float
    max_clicks_without_sale: int
    min_spend_threshold: float
    auto_pilot: bool
    version: int = Field(ge=0)


@router.put("/mercado-ads/reglas")
async def guardar_reglas(datos: ReglasIn, usuario: str = Depends(requerir_gerencia)):
    """Guarda las reglas. `version` debe ser la que se leyó: si otra persona guardó antes, 409."""
    try:
        reglas = zl.validar_reglas(datos.model_dump())
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err))
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE zentor_reglas SET target_acos = %s, max_clicks_without_sale = %s, min_spend_threshold = %s, "
            "auto_pilot = %s, version = version + 1, actualizado_por = %s WHERE id = 1 AND version = %s",
            (reglas["target_acos"], reglas["max_clicks_without_sale"], reglas["min_spend_threshold"],
             1 if reglas["auto_pilot"] else 0, usuario, datos.version))
        if cursor.rowcount == 0:
            conn.rollback()
            raise HTTPException(status_code=409, detail="Otra persona cambió las reglas: recarga para ver los valores actuales")
        conn.commit()
    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))
    finally:
        cursor.close()
        conn.close()
    mov_reg.registrar_movimiento(
        usuario, f"Reglas Zentor: ACOS objetivo {reglas['target_acos']} %, {reglas['max_clicks_without_sale']} clics sin venta, "
                 f"inversión mínima ${reglas['min_spend_threshold']}, piloto {'encendido' if reglas['auto_pilot'] else 'apagado'}", SECCION)
    return _con_estado(_leer_reglas_o_500())


class MotorIn(BaseModel):
    estado_motor: Literal["activo", "pausa"]


@router.put("/mercado-ads/motor")
async def cambiar_motor(datos: MotorIn, usuario: str = Depends(requerir_gerencia)):
    """Pone el motor en «activo» o en «pausa» (en pausa no corre el piloto ni se aplica nada en MeLi)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE zentor_reglas SET estado_motor = %s, actualizado_por = %s WHERE id = 1",
                       (datos.estado_motor, usuario))
        conn.commit()
    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))
    finally:
        cursor.close()
        conn.close()
    mov_reg.registrar_movimiento(usuario, f"Motor Zentor puesto en {datos.estado_motor}", SECCION)
    return _con_estado(_leer_reglas_o_500())


class AplicarIn(BaseModel):
    item_id: str = Field(min_length=1, max_length=40)
    accion: Literal["pausar", "activar"]
    desde: Optional[date] = None
    hasta: Optional[date] = None
    # manual = decisión de gerencia: no depende de la recomendación del motor.
    manual: bool = False


@router.post("/mercado-ads/acciones/aplicar")
async def aplicar_accion(datos: AplicarIn, usuario: str = Depends(requerir_gerencia)):
    """Aplica en MeLi la recomendación vigente de un anuncio (pausar o activar).

    Antes de tocar nada vuelve a consultar el anuncio y a diagnosticarlo: si la recomendación ya no es la
    que vio quien hizo clic (o ya se aplicó, p. ej. doble clic), responde 409 con la situación actual.

    Con `manual = true` (pausa/reactivación a criterio de gerencia) no se exige que el motor lo recomiende,
    pero sí que el anuncio exista y que no esté ya en el estado pedido (doble clic → 409).
    """
    reglas = _leer_reglas_o_500()
    if reglas["estado_motor"] == zl.MOTOR_PAUSA:
        raise HTTPException(status_code=409, detail="El motor está en pausa: actívalo para aplicar acciones")
    desde, hasta = _rango_o_422(datos.desde, datos.hasta)
    try:
        crudos = await zentor_service.listar_anuncios(desde, hasta)
        _, evaluadas = zl.evaluar(crudos, _reglas_de_diagnostico(reglas))
        actual = next((e for e in evaluadas if e["item_id"] == datos.item_id), None)
        if actual is None:
            raise HTTPException(status_code=404, detail="El anuncio ya no aparece en Mercado Ads")
        nuevo = "paused" if datos.accion == zl.PAUSAR else "active"
        if datos.manual:
            if actual["estado"] == nuevo:
                raise HTTPException(status_code=409, detail=(
                    f"El anuncio ya está {'pausado' if nuevo == 'paused' else 'activo'}. Actualiza la tabla."))
            motivo = "manual"
        else:
            if actual["diagnostico"]["accion"] != datos.accion:
                raise HTTPException(status_code=409, detail=(
                    f"La recomendación cambió: ahora es «{actual['diagnostico']['accion']}» "
                    f"({actual['diagnostico']['motivo']}). Actualiza la tabla."))
            motivo = actual["diagnostico"]["motivo"]
        await zentor_service.cambiar_estado(datos.item_id, nuevo)
    except zentor_service.MercadoAdsError as err:
        _lanzar_error_ads(err)
    mov_reg.registrar_movimiento(
        usuario, f"Zentor: {datos.accion} anuncio {datos.item_id} ({motivo})", SECCION)
    return {"item_id": datos.item_id, "accion": datos.accion, "estado": nuevo}


@router.post("/mercado-ads/evaluar")
async def evaluar_ahora(usuario: str = Depends(requerir_gerencia)):
    """Corre la evaluación del motor ya (la misma del job diario). Con el piloto encendido pausa de verdad."""
    from jobs import _ejecucion, zentor_piloto
    if _leer_reglas_o_500()["estado_motor"] == zl.MOTOR_PAUSA:
        raise HTTPException(status_code=409, detail="El motor está en pausa: actívalo para evaluar")
    if _ejecucion.en_curso(zentor_piloto):
        raise HTTPException(status_code=409, detail="Ya hay una evaluación en curso, espera a que termine")
    resultado = await _ejecucion.ejecutar(zentor_piloto, motivo=f"manual por {usuario}")
    if resultado.get("estado") == "error":
        status = 503 if resultado.get("sin_acceso") else 502
        raise HTTPException(status_code=status, detail=resultado.get("error") or "Falló la evaluación")
    return resultado
