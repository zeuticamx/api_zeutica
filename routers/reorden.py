# fichero api del punto de reorden (WRP): datos por SKU para decidir compras.
# Solo gerencia. El cálculo vive en reorden_logica.py (lo comparte el job de Telegram,
# jobs/reorden_compras.py); este router solo junta datos y guarda parámetros.
#   - existencia  = stock_bodega + stock_fba + stock_clean de productos (stock total)
#   - consumo     = SUM(cantidad) de ventasRegistro activas por mes (todos los canales)
#   - en tránsito = qty de embarque_items cuyos embarques no tienen fecha_de_recibido
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import mysql.connector, mov_reg
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import reorden_logica as rl
from errores_db import detalle_error_db
from permisos import requerir_gerencia

router = APIRouter(tags=["/reorden"], responses={404: {"Mensaje": "No encontrado"}})

load_dotenv()  # Carga de credenciales .env

CDMX = ZoneInfo("America/Mexico_City")
_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "reorden_schema.sql")


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )


def crear_tabla_reorden():
    """Crea reorden_parametros si no existe (se llama en el lifespan). Idempotente."""
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        lineas = [l for l in f if not l.strip().startswith("--")]
    statements = [s.strip() for s in "".join(lineas).split(";") if s.strip()]
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        for stmt in statements:
            cursor.execute(stmt)
        conn.commit()
        print("Tabla reorden_parametros verificada/creada.")
    finally:
        cursor.close()
        conn.close()


SQL_PRODUCTOS = """
    SELECT sku, nombre, categoria,
           COALESCE(stock_bodega, 0) + COALESCE(stock_fba, 0) + COALESCE(stock_clean, 0) AS existencia
    FROM productos ORDER BY sku
"""

# Rango semiabierto sobre fecha_registro (usa idx_ventasregistro_fecha_registro).
# '%%' porque la consulta lleva parámetros: mysql.connector interpreta los '%'.
SQL_CONSUMO = """
    SELECT sku, DATE_FORMAT(fecha_registro, '%%Y-%%m') AS mes, SUM(cantidad) AS cantidad
    FROM ventasRegistro
    WHERE estatus = 'activa' AND fecha_registro >= %s AND fecha_registro < %s
    GROUP BY sku, mes
"""

SQL_TRANSITO = """
    SELECT i.sku, SUM(i.qty) AS qty
    FROM embarque_items i
    JOIN embarques e ON e.id = i.embarque_id
    WHERE e.fecha_de_recibido IS NULL
    GROUP BY i.sku
"""

SQL_PARAMETROS = "SELECT sku, lead_time_meses, nivel_servicio_z FROM reorden_parametros"


def cargar_reorden(hoy=None) -> dict:
    """Junta productos, consumo, tránsito y parámetros y evalúa cada SKU."""
    hoy = hoy or datetime.now(CDMX).date()
    etiquetas, desde, hasta = rl.ventana_meses(hoy)
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(SQL_PRODUCTOS)
        productos = cursor.fetchall()
        cursor.execute(SQL_CONSUMO, (desde, hasta))
        consumo = {}
        for f in cursor.fetchall():
            consumo.setdefault(f["sku"], {})[f["mes"]] = float(f["cantidad"] or 0)
        cursor.execute(SQL_TRANSITO)
        transito = {f["sku"]: float(f["qty"] or 0) for f in cursor.fetchall()}
        cursor.execute(SQL_PARAMETROS)
        parametros = {f["sku"]: f for f in cursor.fetchall()}
    finally:
        cursor.close()
        conn.close()
    return {
        "meses": etiquetas,
        "desde": desde.isoformat(),
        "hasta": hasta.isoformat(),  # exclusivo: día 1 del mes en curso
        "defaults": {"lead_time_meses": rl.LEAD_TIME_DEFAULT, "nivel_servicio_z": rl.Z_DEFAULT},
        "niveles_servicio": rl.NIVELES_SERVICIO,
        "cv_volatil": rl.CV_VOLATIL,
        "filas": rl.evaluar(productos, consumo, transito, parametros, etiquetas),
    }


@router.get("/reorden")
async def consultar_reorden(usuario: str = Depends(requerir_gerencia)):
    """Punto de reorden de todos los SKU con los últimos 12 meses completos (solo gerencia)."""
    try:
        return cargar_reorden()
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))


class ParametrosReorden(BaseModel):
    lead_time_meses: float = Field(gt=0, le=24)
    nivel_servicio_z: float


@router.put("/reorden/parametros/{sku}")
async def guardar_parametros(sku: str, datos: ParametrosReorden, usuario: str = Depends(requerir_gerencia)):
    """Lead time y nivel de servicio de un SKU (solo gerencia). El usuario sale del token."""
    z = round(datos.nivel_servicio_z, 2)
    if z not in rl.Z_PERMITIDOS:
        raise HTTPException(status_code=422, detail="Nivel de servicio no válido")
    lead_time = round(datos.lead_time_meses, 2)
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT sku FROM productos WHERE sku = %s", (sku,))
        fila = cursor.fetchone()
        if not fila:
            raise HTTPException(status_code=404, detail="El SKU no existe")
        cursor.execute(
            "INSERT INTO reorden_parametros (sku, lead_time_meses, nivel_servicio_z, actualizado_por) "
            "VALUES (%s, %s, %s, %s) ON DUPLICATE KEY UPDATE lead_time_meses = VALUES(lead_time_meses), "
            "nivel_servicio_z = VALUES(nivel_servicio_z), actualizado_por = VALUES(actualizado_por)",
            (fila[0], lead_time, z, usuario))
        conn.commit()
    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))
    finally:
        cursor.close()
        conn.close()
    mov_reg.registrar_movimiento(usuario, f"Parámetros de reorden de {sku}: LT {lead_time} meses, Z {z}", "Punto de reorden")
    return {"sku": fila[0], "lead_time_meses": lead_time, "nivel_servicio_z": z}
