"""
Seguimiento de cotizaciones: catálogo cerrado (ESPERANDO, PAGADO, ENVIADO, ENTREGADO, OTROS).

`cotizaciones.seguimiento` guarda el valor del catálogo; `cotizaciones.seguimiento_otros`
guarda el detalle en texto libre y solo aplica cuando el valor es OTROS. Los textos libres
anteriores al catálogo se conservan tal cual en `seguimiento` (no se migran): el panel los
muestra como OTROS con ese texto y al editarlos quedan normalizados.

Espejo de `panel_gerencia/src/cotizacion-logica.js` (`SEGUIMIENTOS`, `validarSeguimiento`).
"""
import os
from typing import Optional, Tuple

import mysql.connector
from fastapi import HTTPException

SEGUIMIENTOS = ("ESPERANDO", "PAGADO", "ENVIADO", "ENTREGADO", "OTROS")
OTROS_MAX = 255
COLUMNA_OTROS = "seguimiento_otros"


def normalizar_seguimiento(seguimiento: Optional[str], otros: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """Devuelve (valor, detalle) listos para guardar o lanza 422.

    Vacío/None = sin seguimiento (ambos None). Fuera del catálogo -> 422. OTROS exige detalle
    (máx. OTROS_MAX); en cualquier otro valor el detalle se descarta.
    """
    valor = str(seguimiento or "").strip().upper()
    if not valor:
        return None, None
    if valor not in SEGUIMIENTOS:
        raise HTTPException(status_code=422, detail=f"Seguimiento inválido. Opciones: {', '.join(SEGUIMIENTOS)}")
    if valor != "OTROS":
        return valor, None
    detalle = " ".join(str(otros or "").split())
    if not detalle:
        raise HTTPException(status_code=422, detail="Con OTROS escribe el detalle del seguimiento")
    if len(detalle) > OTROS_MAX:
        raise HTTPException(status_code=422, detail=f"El detalle no puede pasar de {OTROS_MAX} caracteres")
    return valor, detalle


def asegurar_columna_seguimiento():
    """Agrega cotizaciones.seguimiento_otros si falta (se llama en el lifespan)."""
    conn = mysql.connector.connect(
        host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"),
    )
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                       "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'cotizaciones'")
        existentes = {str(f[0]).lower() for f in cursor.fetchall()}
        if COLUMNA_OTROS not in existentes:
            cursor.execute(f"ALTER TABLE cotizaciones ADD COLUMN {COLUMNA_OTROS} VARCHAR({OTROS_MAX}) NULL")
        conn.commit()
    finally:
        cursor.close()
        conn.close()
