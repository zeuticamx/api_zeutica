import mysql.connector
from fastapi import APIRouter, HTTPException, Query
from typing import Optional
import os
from dotenv import load_dotenv

router =APIRouter(tags=["/consulta_registros"],responses={404: {"Mensaje":"No encontrado"}})
load_dotenv()

# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

@router.get("/consulta-registros")
async def consulta_registros(
    seccion: Optional[str] = Query(default=None),
    q: Optional[str] = Query(default=None),
    desde: Optional[str] = Query(default=None),
    hasta: Optional[str] = Query(default=None),
    limite: int = Query(default=200, ge=1, le=1000),
):
    """
    Auditoría visible para cualquier usuario autenticado (sin restricción de gerencia).
    Filtros opcionales por sección/texto/fechas para conciliar inventario.
    """
    conn = get_db_connection()
    # Uso dictionary=True para devolver llaves nombradas y armar el JSON directo
    cursor = conn.cursor(dictionary=True)

    query = "SELECT * FROM movimientos_registro WHERE 1=1"
    valores = []

    if seccion:
        query += " AND seccion = %s"
        valores.append(seccion)
    if q:
        query += " AND (nombre_usuario LIKE %s OR movimiento LIKE %s OR seccion LIKE %s)"
        like = f"%{q}%"
        valores += [like, like, like]
    if desde:
        query += " AND fecha >= %s"
        valores.append(desde)
    if hasta:
        # Incluye el día completo.
        query += " AND fecha < DATE_ADD(%s, INTERVAL 1 DAY)"
        valores.append(hasta)

    query += " ORDER BY fecha DESC LIMIT %s"
    valores.append(limite)

    try:
        cursor.execute(query, tuple(valores))
        res = cursor.fetchall()        
        return res

    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=f"Error al consultar registros: {err}")
    
    finally:
        cursor.close()
        conn.close()