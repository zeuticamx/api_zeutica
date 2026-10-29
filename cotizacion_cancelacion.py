"""
Cancelación de cotizaciones (borrado lógico): la cotización no se elimina, queda inactiva
para cotizar/comprar guías con Skydropx, para el modal de Ventas y para los avisos.

Módulo de raíz porque lo usan varios routers (cotizaciones, skydropx, ventas) y los jobs.
"""
import os
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import mysql.connector
from fastapi import HTTPException

CDMX = ZoneInfo("America/Mexico_City")

COLUMNAS_CANCELACION = {
    "cancelada": "TINYINT(1) NOT NULL DEFAULT 0",
    "fecha_cancelada": "DATETIME NULL",
    "cancelada_por": "VARCHAR(100) NULL",
    "motivo_cancelacion": "VARCHAR(500) NULL",
}
MOTIVO_MAX = 500


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
    )


def asegurar_columnas_cancelacion():
    """Agrega a cotizaciones las columnas de cancelación si faltan (se llama en el lifespan)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                       "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'cotizaciones'")
        existentes = {str(fila[0]).lower() for fila in cursor.fetchall()}
        for columna, definicion in COLUMNAS_CANCELACION.items():
            if columna not in existentes:
                cursor.execute(f"ALTER TABLE cotizaciones ADD COLUMN {columna} {definicion}")
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def ahora_cdmx() -> datetime:
    """Hora de México sin zona (así se guardan los DATETIME del proyecto)."""
    return datetime.now(CDMX).replace(tzinfo=None)


def mensaje_cancelada(codigo) -> str:
    return f"La cotización {codigo} está cancelada"


def es_autor_o_gerencia(usuario, autor, es_gerencia_fn) -> bool:
    """Puede cancelar/reactivar el autor de la cotización o gerencia."""
    if es_gerencia_fn(usuario):
        return True
    return bool(usuario) and bool(autor) and str(usuario).strip().lower() == str(autor).strip().lower()


def normalizar_motivo(motivo: Optional[str]) -> Optional[str]:
    """Motivo opcional: recortado, vacío = None. Más de MOTIVO_MAX -> 422."""
    texto = str(motivo or "").strip()
    if not texto:
        return None
    if len(texto) > MOTIVO_MAX:
        raise HTTPException(status_code=422, detail=f"El motivo no puede pasar de {MOTIVO_MAX} caracteres")
    return texto


def valor_cancelada(fila) -> bool:
    """Interpreta el valor de `cancelada` venga como escalar, tupla o dict."""
    if fila is None:
        return False
    if isinstance(fila, dict):
        fila = fila.get("cancelada")
    elif isinstance(fila, (tuple, list)):
        fila = fila[0] if fila else None
    return bool(fila)


def exigir_no_cancelada(cursor, codigo) -> None:
    """409 si la cotización existe y está cancelada. Una inexistente no bloquea (no hay FK:
    `referencia` de Skydropx o el folio de una venta pueden ser texto libre)."""
    folio = str(codigo or "").strip()
    if not folio:
        return
    cursor.execute("SELECT COALESCE(cancelada, 0) AS cancelada FROM cotizaciones WHERE codigo_cotizacion = %s LIMIT 1",
                   (folio,))
    filas = cursor.fetchall()
    if filas and valor_cancelada(filas[0]):
        raise HTTPException(status_code=409, detail=mensaje_cancelada(folio))


def exigir_no_cancelada_codigo(codigo) -> None:
    """Igual que exigir_no_cancelada con conexión propia (para routers sin cursor, como Skydropx).
    Si la consulta falla (p. ej. la migración de columnas no corrió) no bloquea la operación:
    se deja en el log, porque frenar la compra de una guía por un fallo de BD sería peor."""
    folio = str(codigo or "").strip()
    if not folio:
        return
    try:
        conn = get_db_connection()
        try:
            cursor = conn.cursor()
            try:
                exigir_no_cancelada(cursor, folio)
            finally:
                cursor.close()
        finally:
            conn.close()
    except mysql.connector.Error as err:
        print(f"No se pudo verificar si la cotización {folio} está cancelada: {err}")
