# Funcion para registrar movimientos a DB
import mysql.connector
import os
from dotenv import load_dotenv

load_dotenv()

# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

def registrar_movimiento(nombre_usuario: str, movimiento: str, seccion: str):
    conn = get_db_connection()
    cursor = conn.cursor()
    query = """
        INSERT INTO movimientos_registro (nombre_usuario, movimiento, seccion)
        VALUES (%s, %s, %s)
    """
    try:
        cursor.execute(query, (nombre_usuario, movimiento, seccion))
        conn.commit()
        return {"message": "Movimiento registrado exitosamente"}

    except mysql.connector.Error as err:
        
        print(f"Error al registrar movimiento: {err}")
        raise
    
    finally:
        cursor.close()
        conn.close()

def asegurar_tabla_ubicaciones_editadas(cursor):
    """Bitácora de ediciones de ubicación (la lee «Ver registro»). Idempotente:
    en producción la tabla ya existe y no se toca; en bases nuevas/de prueba se crea."""
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS ubicaciones_editadas (
          id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
          sku VARCHAR(100) NOT NULL,
          warehouse_id VARCHAR(100) NOT NULL,
          cama INT NULL,
          cantidad INT NULL,
          fecha DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          INDEX idx_ubi_edit_sku (sku)
        )"""
    )


def registrar_edicionUbi(sku: str, warehouse_id: str, cama: str, cantidad: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    query = """
        INSERT INTO ubicaciones_editadas (sku, warehouse_id, cama, cantidad)
        VALUES (%s, %s, %s, %s)
    """
    try:
        asegurar_tabla_ubicaciones_editadas(cursor)
        cursor.execute(query, (sku, warehouse_id, cama, cantidad))
        conn.commit()
        return {"message": "Ubicación editada exitosamente"}

    except mysql.connector.Error as err:
        
        print(f"Error al registrar edición de ubicación: {err}")
        raise
    
    finally:
        cursor.close()
        conn.close()