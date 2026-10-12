# Migración de clientes_potenciales: el INSERT IGNORE de los jobs solo
# deduplica si existe llave única. Sin ella, cada reproceso duplicaba filas.
import os

import mysql.connector
from dotenv import load_dotenv

load_dotenv()

COLUMNAS_POTENCIALES = {
    "sku": "VARCHAR(50) NULL",
    "producto": "VARCHAR(255) NULL",
    "id_ventas": "VARCHAR(64) NULL",
    "cantidad": "INT NULL",
    "fecha": "DATE NULL",
    "nombre_comprador": "VARCHAR(255) NULL",
    "precio": "DECIMAL(12,2) NULL",
    "costo_unitario": "DECIMAL(12,2) NULL",
    "descartado": "TINYINT(1) NOT NULL DEFAULT 0",
    "revisado": "TINYINT(1) NOT NULL DEFAULT 0",
    "correo_encontrado": "VARCHAR(255) NULL",
}

INDICE_UNICO = ("uq_potencial_venta_sku", "(id_ventas, sku)")


def get_db_connection():
    return mysql.connector.connect(host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
                                   password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"))


def asegurar_tabla_potenciales() -> dict:
    """Crea la tabla si falta, deduplica (MIN(id) por venta+sku) y crea la llave única."""
    reporte = {"tabla_creada": False, "duplicados_eliminados": 0, "indice_creado": False}
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SHOW TABLES LIKE 'clientes_potenciales'")
        if not cursor.fetchone():
            cols = ", ".join(f"{c} {d}" for c, d in COLUMNAS_POTENCIALES.items())
            cursor.execute(f"CREATE TABLE clientes_potenciales (id INT NOT NULL AUTO_INCREMENT PRIMARY KEY, {cols})")
            reporte["tabla_creada"] = True
        else:
            cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                           "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'clientes_potenciales'")
            existentes = {str(fila[0]).lower() for fila in cursor.fetchall()}
            for columna, definicion in COLUMNAS_POTENCIALES.items():
                if columna not in existentes:
                    cursor.execute(f"ALTER TABLE clientes_potenciales ADD COLUMN {columna} {definicion}")
        conn.commit()
        cursor.execute(
            """DELETE t1 FROM clientes_potenciales t1
               JOIN clientes_potenciales t2
                 ON t2.id_ventas <=> t1.id_ventas AND t2.sku <=> t1.sku AND t2.id < t1.id""")
        reporte["duplicados_eliminados"] = cursor.rowcount
        conn.commit()
        cursor.execute("SELECT INDEX_NAME FROM information_schema.STATISTICS "
                       "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'clientes_potenciales' "
                       "AND NON_UNIQUE = 0")
        unicos = {str(fila[0]).lower() for fila in cursor.fetchall()}
        nombre, cols = INDICE_UNICO
        ya_cubierto = nombre in unicos or any(
            n not in ("primary",) and _cubre(cursor, n) for n in unicos)
        if not ya_cubierto:
            try:
                cursor.execute(f"ALTER TABLE clientes_potenciales ADD UNIQUE KEY {nombre} {cols}")
                reporte["indice_creado"] = True
            except mysql.connector.Error as err:
                print(f"No se pudo crear {nombre}: {err}")
        conn.commit()
    finally:
        cursor.close()
        conn.close()
    return reporte


def _cubre(cursor, indice: str) -> bool:
    cursor.execute("SELECT COLUMN_NAME FROM information_schema.STATISTICS "
                   "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'clientes_potenciales' "
                   "AND INDEX_NAME = %s ORDER BY SEQ_IN_INDEX", (indice,))
    return [str(fila[0]).lower() for fila in cursor.fetchall()] == ["id_ventas", "sku"]
