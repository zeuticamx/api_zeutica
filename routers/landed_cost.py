# fichero api del costeo de importación (Landed Cost) de un embarque. Solo gerencia.
# El cálculo vive en landed_cost_logica.py; este router junta entradas y las guarda:
#   - por item: columnas de costo de embarque_items (las agrega routers/embarques.py)
#   - por embarque: embarque_liquidacion (1:1)
#   - cierre: embarque_costeo_cierre guarda la foto del cálculo al marcar la etapa
#     HL_LIQUIDADA (routers/embarques.py llama cerrar_costeo / reabrir_costeo en su
#     misma transacción). Con el costeo cerrado las entradas no se editan (409).
import json
import os
from datetime import date
from decimal import Decimal
from typing import List, Optional

import mysql.connector, mov_reg
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import landed_cost_logica as lc
from errores_db import detalle_error_db
from permisos import requerir_gerencia

router = APIRouter(tags=["/landed-cost"], responses={404: {"Mensaje": "No encontrado"}})

load_dotenv()  # Carga de credenciales .env


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )


# ---- Schemas (solo entradas: los campos calculados que mande el cliente se ignoran) ----

class CostoItemEntrada(BaseModel):
    invoice: Optional[str] = None
    fecha_compra: Optional[date] = None
    paquete_venta: Optional[int] = Field(None, ge=0)
    unit_price_fob_usd: Optional[Decimal] = Field(None, ge=0)
    ctns: Optional[int] = Field(None, ge=0)
    arancel_porc: Optional[Decimal] = Field(None, ge=0, le=1)
    tipo_cambio_china: Optional[Decimal] = Field(None, ge=0)


class LiquidacionEntrada(BaseModel):
    total_cbm_manual: Optional[Decimal] = Field(None, ge=0)
    no_hoja_liquidacion: Optional[str] = None
    tipo_cambio_log: Optional[Decimal] = Field(None, ge=0)
    incrementales_usd: Optional[Decimal] = Field(None, ge=0)
    igi_total_embarque: Optional[Decimal] = Field(None, ge=0)
    iva_total_aduana: Optional[Decimal] = Field(None, ge=0)
    total_hoja_liquidacion: Optional[Decimal] = Field(None, ge=0)
    total_consolidado_porc: Optional[Decimal] = Field(None, ge=0, le=1)
    total_pagado_calculo_excel: Optional[Decimal] = Field(None, ge=0)
    pagado_china_mxn: Optional[Decimal] = Field(None, ge=0)


class ItemSimulacion(CostoItemEntrada):
    id: Optional[int] = None
    sku: Optional[str] = None
    qty: Optional[Decimal] = Field(None, ge=0)
    cbm: Optional[Decimal] = Field(None, ge=0)


class Simulacion(BaseModel):
    liquidacion: Optional[LiquidacionEntrada] = None
    items: List[ItemSimulacion] = []
    invoices: Optional[List[str]] = None


# ---- Lectura y armado (con el cursor que se reciba: lo comparte el cierre) ----

SQL_ITEMS = (
    "SELECT id, sku, qty, cbm, " + ", ".join(lc.CAMPOS_ITEM) +
    " FROM embarque_items WHERE embarque_id = %s ORDER BY id"
)
SQL_LIQUIDACION = (
    "SELECT " + ", ".join(lc.CAMPOS_LIQUIDACION) +
    " FROM embarque_liquidacion WHERE embarque_id = %s"
)


def _json_valor(valor):
    if isinstance(valor, Decimal):
        return float(valor)
    if isinstance(valor, date):
        return valor.isoformat()
    if isinstance(valor, (bytes, bytearray)):
        return bytes(valor).decode("utf-8", "replace")
    return valor


def _limpiar(fila: dict) -> dict:
    return {k: _json_valor(v) for k, v in (fila or {}).items()}


def armar_costeo(cursor, embarque_id: int):
    """Costeo vivo del embarque (cursor con dictionary=True). None si el embarque no existe."""
    cursor.execute(
        "SELECT id, numero_contenedor, llegada_manzanillo_tentativa, fecha_de_recibido FROM embarques WHERE id = %s",
        (embarque_id,)
    )
    embarque = cursor.fetchone()
    if not embarque:
        return None
    cursor.execute("SELECT numero FROM embarque_invoices WHERE embarque_id = %s ORDER BY id", (embarque_id,))
    invoices = [_json_valor(f["numero"]) for f in cursor.fetchall()]
    cursor.execute(SQL_ITEMS, (embarque_id,))
    items = [_limpiar(f) for f in cursor.fetchall()]
    cursor.execute(SQL_LIQUIDACION, (embarque_id,))
    liquidacion = _limpiar(cursor.fetchone()) or {c: None for c in lc.CAMPOS_LIQUIDACION}

    resultado = lc.calcular_embarque(items, liquidacion, invoices)
    return {
        "embarque": {**_limpiar(embarque), "invoices": invoices},
        "liquidacion": liquidacion,
        **resultado,
    }


def _leer_cierre(cursor, embarque_id: int):
    cursor.execute(
        "SELECT resultado, cerrado_por, cerrado_en FROM embarque_costeo_cierre WHERE embarque_id = %s",
        (embarque_id,)
    )
    return cursor.fetchone()


def cerrar_costeo(cursor, embarque_id: int, usuario: str) -> bool:
    """Guarda la foto del costeo (si aún no hay una). True si cerró ahora. No hace commit."""
    if _leer_cierre(cursor, embarque_id):
        return False
    costeo = armar_costeo(cursor, embarque_id)
    if costeo is None:
        return False
    cursor.execute(
        "INSERT INTO embarque_costeo_cierre (embarque_id, resultado, cerrado_por) VALUES (%s, %s, %s)",
        (embarque_id, json.dumps(costeo, ensure_ascii=False), usuario)
    )
    return True


def reabrir_costeo(cursor, embarque_id: int) -> bool:
    """Borra la foto del costeo. True si había una. No hace commit."""
    cursor.execute("DELETE FROM embarque_costeo_cierre WHERE embarque_id = %s", (embarque_id,))
    return cursor.rowcount > 0


def _costeo_o_cierre(cursor, embarque_id: int):
    cierre = _leer_cierre(cursor, embarque_id)
    if cierre:
        foto = json.loads(_json_valor(cierre["resultado"]))
        return {
            **foto, "cerrado": True,
            "cerrado_por": cierre["cerrado_por"], "cerrado_en": _json_valor(cierre["cerrado_en"]),
        }
    costeo = armar_costeo(cursor, embarque_id)
    if costeo is None:
        raise HTTPException(status_code=404, detail="Embarque no encontrado")
    return {**costeo, "cerrado": False, "cerrado_por": None, "cerrado_en": None}


def _validar_editable(cursor, embarque_id: int):
    cursor.execute("SELECT id FROM embarques WHERE id = %s", (embarque_id,))
    if not cursor.fetchone():
        raise HTTPException(status_code=404, detail="Embarque no encontrado")
    if _leer_cierre(cursor, embarque_id):
        raise HTTPException(
            status_code=409,
            detail="El costeo está cerrado (hoja de liquidación liquidada); desmarca la etapa para editarlo"
        )


# ---- Endpoints ----

@router.get("/embarques/{embarque_id}/landed-cost")
async def consultar_costeo(embarque_id: int, usuario: str = Depends(requerir_gerencia)):
    """Costeo del embarque: entradas, cálculo por item, resumen y advertencias (solo gerencia).
    Si la etapa HL_LIQUIDADA está completada devuelve la foto guardada al cerrarla."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        return _costeo_o_cierre(cursor, embarque_id)
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))
    finally:
        cursor.close()
        conn.close()


@router.put("/embarques/{embarque_id}/landed-cost/liquidacion")
async def guardar_liquidacion(embarque_id: int, datos: LiquidacionEntrada, usuario: str = Depends(requerir_gerencia)):
    """Datos de logística y aduana del embarque (reemplaza todos). El usuario sale del token."""
    valores = datos.model_dump()
    columnas = list(lc.CAMPOS_LIQUIDACION)
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        _validar_editable(cursor, embarque_id)
        cursor.execute(
            "INSERT INTO embarque_liquidacion (embarque_id, " + ", ".join(columnas) + ", actualizado_por) "
            "VALUES (%s, " + ", ".join(["%s"] * len(columnas)) + ", %s) ON DUPLICATE KEY UPDATE "
            + ", ".join(f"{c} = VALUES({c})" for c in columnas) + ", actualizado_por = VALUES(actualizado_por)",
            (embarque_id, *[valores[c] for c in columnas], usuario)
        )
        conn.commit()
        respuesta = _costeo_o_cierre(cursor, embarque_id)
    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))
    finally:
        cursor.close()
        conn.close()
    mov_reg.registrar_movimiento(usuario, f"Editó la liquidación (costeo) del embarque #{embarque_id}", "Importaciones")
    return respuesta


@router.patch("/embarques/{embarque_id}/items/{item_id}/costos")
async def guardar_costos_item(embarque_id: int, item_id: int, datos: CostoItemEntrada,
                              usuario: str = Depends(requerir_gerencia)):
    """Entradas de costo de un item; solo se guardan los campos enviados. La invoice debe ser
    una del embarque (se guarda el número: el PUT del embarque recrea las invoices y cambia ids)."""
    cambios = datos.model_dump(exclude_unset=True)
    if isinstance(cambios.get("invoice"), str):
        cambios["invoice"] = cambios["invoice"].strip() or None
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        _validar_editable(cursor, embarque_id)
        cursor.execute(
            "SELECT id FROM embarque_items WHERE id = %s AND embarque_id = %s", (item_id, embarque_id)
        )
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Item no encontrado en este embarque")
        if cambios.get("invoice"):
            cursor.execute(
                "SELECT 1 FROM embarque_invoices WHERE embarque_id = %s AND numero = %s",
                (embarque_id, cambios["invoice"])
            )
            if not cursor.fetchone():
                raise HTTPException(status_code=422, detail="La invoice no pertenece a este embarque")
        if cambios:
            columnas = [c for c in lc.CAMPOS_ITEM if c in cambios]
            cursor.execute(
                "UPDATE embarque_items SET " + ", ".join(f"{c} = %s" for c in columnas) +
                " WHERE id = %s AND embarque_id = %s",
                (*[cambios[c] for c in columnas], item_id, embarque_id)
            )
            conn.commit()
        respuesta = _costeo_o_cierre(cursor, embarque_id)
    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))
    finally:
        cursor.close()
        conn.close()
    if cambios:
        mov_reg.registrar_movimiento(
            usuario, f"Editó costos del item #{item_id} del embarque #{embarque_id}: {', '.join(sorted(cambios))}",
            "Importaciones"
        )
    return respuesta


@router.post("/landed-cost/simular")
async def simular_costeo(datos: Simulacion, usuario: str = Depends(requerir_gerencia)):
    """Calcula sin guardar (vista previa del panel mientras se captura)."""
    items = [_limpiar(it.model_dump()) for it in datos.items]
    liquidacion = _limpiar(datos.liquidacion.model_dump()) if datos.liquidacion else None
    return lc.calcular_embarque(items, liquidacion, datos.invoices)
