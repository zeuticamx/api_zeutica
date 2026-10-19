import mysql.connector
from fastapi import APIRouter, HTTPException
import os
from dotenv import load_dotenv
from errores_db import detalle_error_db

router =APIRouter(tags=["/cuentas"],responses={404: {"Mensaje":"No encontrado"}})
load_dotenv()

# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

@router.get("/finanzas/cxc")
async def obtener_cxc():
    """
    En construccion seccion de cuentas pendientes.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    query = """
    SELECT *, 
    (monto_total - monto_pagado) AS saldo_pendiente,
    DATEDIFF(fecha_vencimiento, CURDATE()) AS dias_restantes
    FROM cxc WHERE estado != 'PAGADO'
    """

    try:
        cursor.execute(query)
        registros = cursor.fetchall()
        
        # Si no hay registros, devuelvo lista vacía
        if not registros:
            return {"datos": [], "cantidad": 0}       
                
        return registros

    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error en consulta"))
    
    finally:
        cursor.close()
        conn.close()    