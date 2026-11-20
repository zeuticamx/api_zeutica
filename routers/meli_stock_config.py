# Configuración del job de stock MeLi (página «Stock MeLi», sidebar Comercial).
# Reemplaza las constantes IGNORAR y MULTIPLICADORES de jobs/meli_stock.py: ahora viven en BD
# y se editan desde el panel. Cualquier sesión puede ver y editar (decidido); el usuario de la
# bitácora (mov_reg) sale del token. El job lee la configuración una vez al iniciar cada corrida.
import os
from datetime import datetime
from typing import Any, Optional

import mysql.connector
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import mov_reg
from errores_db import detalle_error_db
from jobs import meli_stock
from permisos import usuario_autenticado

router = APIRouter(tags=["/meli-stock"], responses={404: {"Mensaje": "No encontrado"}})

load_dotenv()

SECCION = "Stock MeLi"
JOB_ID = "meli_stock"


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )


def _iso(valor):
    return valor.isoformat() if isinstance(valor, datetime) else None


def _scheduler(request: Request):
    return getattr(request.app.state, "amazon_scheduler", None)


def _info_scheduler(request: Request) -> dict:
    """El job se registra solo si MELI_STOCK_ENABLED=1 en el entorno (apagado maestro de despliegue)."""
    entorno = os.getenv("MELI_STOCK_ENABLED", "1") == "1"
    prox, activo = None, False
    sch = _scheduler(request)
    if sch is not None:
        try:
            job = sch.get_job(JOB_ID)
            activo = job is not None
            nxt = getattr(job, "next_run_time", None) if job else None
            prox = nxt.isoformat() if nxt else None
        except Exception as err:
            print(f"Stock MeLi: no se pudo consultar el scheduler: {err}")
    return {"habilitado_en_entorno": entorno, "scheduler_activo": activo, "proxima_corrida": prox}


def _leer_todo(request: Request) -> dict:
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    try:
        cur.execute("SELECT habilitado, dry_run, hora, version, actualizado_por, actualizado_en "
                    "FROM meli_stock_config WHERE id = 1")
        cfg = cur.fetchone()
        cur.execute("SELECT i.id_meli, i.nota, i.creado_por, i.creado_en, p.sku "
                    "FROM meli_stock_ignorados i LEFT JOIN publicaciones_meli p ON p.id_meli = i.id_meli "
                    "ORDER BY i.id_meli")
        ignorados = cur.fetchall()
        cur.execute("SELECT m.id_meli, m.multiplicador, m.actualizado_por, m.actualizado_en, p.sku "
                    "FROM meli_stock_multiplicadores m LEFT JOIN publicaciones_meli p ON p.id_meli = m.id_meli "
                    "ORDER BY m.multiplicador, m.id_meli")
        mults = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    if not cfg:
        raise HTTPException(status_code=500, detail="Falta la configuración de Stock MeLi en la base de datos")
    lista_ign = [{"id_meli": f["id_meli"], "nota": f.get("nota"), "sku": f.get("sku"),
                  "creado_por": f.get("creado_por"), "creado_en": _iso(f.get("creado_en"))} for f in ignorados]
    lista_mul = [{"id_meli": f["id_meli"], "multiplicador": int(f["multiplicador"]), "sku": f.get("sku"),
                  "actualizado_por": f.get("actualizado_por"), "actualizado_en": _iso(f.get("actualizado_en"))}
                 for f in mults]
    ids_ign = {f["id_meli"] for f in lista_ign}
    return {
        "habilitado": bool(cfg["habilitado"]),
        "dry_run": bool(cfg["dry_run"]),
        "hora": cfg["hora"],
        "version": int(cfg["version"]),
        "actualizado_por": cfg.get("actualizado_por"),
        "actualizado_en": _iso(cfg.get("actualizado_en")),
        "ignorados": lista_ign,
        "multiplicadores": lista_mul,
        # Ignorado gana sobre multiplicador: estos multiplicadores no se usan mientras sigan ignorados.
        "en_ambas": sorted(ids_ign & {f["id_meli"] for f in lista_mul}),
        "ultimo": meli_stock.LAST_RUN,
        **_info_scheduler(request),
    }


def _a_422(fn, valor):
    try:
        return fn(valor)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err))


def _publicacion_conocida(cursor, id_meli: str) -> bool:
    cursor.execute("SELECT sku FROM publicaciones_meli WHERE id_meli = %s", (id_meli,))
    return cursor.fetchone() is not None


@router.get("/meli-stock/config")
async def obtener_config(request: Request, usuario: str = Depends(usuario_autenticado)):
    """Interruptor, dry-run, hora, ignorados, multiplicadores y último run (cualquier sesión)."""
    try:
        return _leer_todo(request)
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))


class ConfigIn(BaseModel):
    habilitado: bool
    dry_run: bool
    hora: str = Field(min_length=1, max_length=10)
    version: int = Field(ge=0)


@router.put("/meli-stock/config")
async def guardar_config(datos: ConfigIn, request: Request, usuario: str = Depends(usuario_autenticado)):
    """Interruptor del job programado, dry-run y hora. `version` = la que se leyó (409 si cambió)."""
    hora = _a_422(meli_stock.validar_hora, datos.hora)
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT hora FROM meli_stock_config WHERE id = 1")
        previa = cursor.fetchone()
        cursor.execute(
            "UPDATE meli_stock_config SET habilitado = %s, dry_run = %s, hora = %s, "
            "version = version + 1, actualizado_por = %s WHERE id = 1 AND version = %s",
            (1 if datos.habilitado else 0, 1 if datos.dry_run else 0, hora, usuario, datos.version))
        if cursor.rowcount == 0:
            conn.rollback()
            raise HTTPException(status_code=409, detail="Otra persona cambió la configuración: recarga para ver los valores actuales")
        conn.commit()
    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))
    finally:
        cursor.close()
        conn.close()
    # La hora se aplica al momento, sin reiniciar la API (si el job no está registrado, solo se guarda).
    if previa and previa.get("hora") != hora:
        sch = _scheduler(request)
        if sch is not None:
            try:
                sch.reschedule_job(JOB_ID, trigger=meli_stock.crear_trigger(hora))
            except Exception as err:
                print(f"Stock MeLi: no se pudo reprogramar a {hora}: {err}")
    mov_reg.registrar_movimiento(
        usuario, f"Stock MeLi: job {'encendido' if datos.habilitado else 'apagado'}, "
                 f"dry-run {'sí' if datos.dry_run else 'no'}, hora {hora}", SECCION)
    return _leer_todo(request)


class IgnoradoIn(BaseModel):
    id_meli: str = Field(min_length=1, max_length=40)
    nota: Optional[str] = Field(default=None, max_length=200)


@router.post("/meli-stock/ignorados", status_code=201)
async def agregar_ignorado(datos: IgnoradoIn, usuario: str = Depends(usuario_autenticado)):
    """Marca una publicación como ignorada: el job no le publica stock."""
    id_meli = _a_422(meli_stock.normalizar_id_meli, datos.id_meli)
    nota = (datos.nota or "").strip() or None
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("INSERT IGNORE INTO meli_stock_ignorados (id_meli, nota, creado_por) VALUES (%s, %s, %s)",
                       (id_meli, nota, usuario))
        if cursor.rowcount == 0:
            conn.rollback()
            raise HTTPException(status_code=409, detail=f"{id_meli} ya está en la lista de ignorados")
        conocida = _publicacion_conocida(cursor, id_meli)
        conn.commit()
    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))
    finally:
        cursor.close()
        conn.close()
    mov_reg.registrar_movimiento(usuario, f"Stock MeLi: {id_meli} agregado a ignorados", SECCION)
    return {"id_meli": id_meli, "advertencia": None if conocida else
            "Esa publicación no tiene SKU asignado en el sistema; se guardó de todos modos"}


@router.delete("/meli-stock/ignorados/{id_meli}")
async def quitar_ignorado(id_meli: str, usuario: str = Depends(usuario_autenticado)):
    """Quita una publicación de ignorados: desde la siguiente corrida se le publica stock."""
    id_meli = _a_422(meli_stock.normalizar_id_meli, id_meli)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM meli_stock_ignorados WHERE id_meli = %s", (id_meli,))
        if cursor.rowcount == 0:
            conn.rollback()
            raise HTTPException(status_code=404, detail=f"{id_meli} no está en la lista de ignorados")
        conn.commit()
    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))
    finally:
        cursor.close()
        conn.close()
    mov_reg.registrar_movimiento(usuario, f"Stock MeLi: {id_meli} quitado de ignorados", SECCION)
    return {"id_meli": id_meli, "eliminado": True}


class MultiplicadorIn(BaseModel):
    multiplicador: Any


@router.put("/meli-stock/multiplicadores/{id_meli}")
async def guardar_multiplicador(id_meli: str, datos: MultiplicadorIn, usuario: str = Depends(usuario_autenticado)):
    """Alta o edición. Un multiplicador de 1 es el valor por defecto, así que equivale a quitarlo."""
    id_meli = _a_422(meli_stock.normalizar_id_meli, id_meli)
    mult = _a_422(meli_stock.validar_multiplicador, datos.multiplicador)
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        if mult == 1:
            cursor.execute("DELETE FROM meli_stock_multiplicadores WHERE id_meli = %s", (id_meli,))
            conocida = True
        else:
            cursor.execute(
                "INSERT INTO meli_stock_multiplicadores (id_meli, multiplicador, actualizado_por) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE multiplicador = VALUES(multiplicador), actualizado_por = VALUES(actualizado_por)",
                (id_meli, mult, usuario))
            conocida = _publicacion_conocida(cursor, id_meli)
        conn.commit()
    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))
    finally:
        cursor.close()
        conn.close()
    mov_reg.registrar_movimiento(usuario, f"Stock MeLi: multiplicador de {id_meli} = {mult}", SECCION)
    return {"id_meli": id_meli, "multiplicador": mult,
            "advertencia": None if conocida else "Esa publicación no tiene SKU asignado en el sistema; se guardó de todos modos"}


@router.delete("/meli-stock/multiplicadores/{id_meli}")
async def quitar_multiplicador(id_meli: str, usuario: str = Depends(usuario_autenticado)):
    """Quita el multiplicador: la publicación vuelve a usar 1."""
    id_meli = _a_422(meli_stock.normalizar_id_meli, id_meli)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM meli_stock_multiplicadores WHERE id_meli = %s", (id_meli,))
        if cursor.rowcount == 0:
            conn.rollback()
            raise HTTPException(status_code=404, detail=f"{id_meli} no tiene multiplicador")
        conn.commit()
    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))
    finally:
        cursor.close()
        conn.close()
    mov_reg.registrar_movimiento(usuario, f"Stock MeLi: multiplicador de {id_meli} quitado", SECCION)
    return {"id_meli": id_meli, "eliminado": True}
