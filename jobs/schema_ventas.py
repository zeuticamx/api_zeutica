# Migración de ventasRegistro para los jobs de marketplaces.
# Se corre en el lifespan (como gastos/clientes): idempotente y sin tumbar
# el arranque si la DB no está disponible.
import os

import mysql.connector
from dotenv import load_dotenv

load_dotenv()

COLUMNAS_JOB = {
    "inventario_descontado": "TINYINT(1) NOT NULL DEFAULT 0",
    "costo_unitario": "DECIMAL(12,2) NULL",
    "es_full": "TINYINT(1) NOT NULL DEFAULT 0",
    "estatus": "VARCHAR(20) NOT NULL DEFAULT 'activa'",
}

# Clave para que el INSERT ... ON DUPLICATE KEY de los jobs sea idempotente.
# (id_ventas, sku, plataforma): una venta tiene una fila por partida.
INDICE_PARTIDA = ("uq_venta_partida", "(id_ventas, sku, plataforma)")
# Índice para excluir canceladas en reportes y métricas.
INDICE_ESTATUS = ("idx_ventasregistro_estatus", "(estatus)")

# Plataformas que escriben los jobs/n8n con flag de inventario.
PLATAFORMAS_JOB = ("amazon", "MERCADOLIBRE")


def get_db_connection():
    return mysql.connector.connect(host=os.getenv("DB_HOST"), user=os.getenv("DB_USER"),
                                   password=os.getenv("DB_PASSWORD"), database=os.getenv("DB_NAME"))


def columnas_existentes(cursor, tabla: str) -> set:
    cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                   "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (tabla,))
    return {str(fila[0]).lower() for fila in cursor.fetchall()}


def asegurar_columnas_ventas(backfill_descontado: bool = True) -> dict:
    """Agrega columnas del job, índice de partida y marca lo existente como descontado.

    backfill_descontado=True: las filas amazon/MERCADOLIBRE ya registradas se
    marcan inventario_descontado=1 (hasta hoy n8n sí descontaba). Solo hacia
    adelante lo mantiene el job.
    """
    reporte = {"columnas_agregadas": [], "indice_creado": False, "backfill": 0}
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        existentes = columnas_existentes(cursor, "ventasRegistro")
        for columna, definicion in COLUMNAS_JOB.items():
            if columna not in existentes:
                cursor.execute(f"ALTER TABLE ventasRegistro ADD COLUMN {columna} {definicion}")
                reporte["columnas_agregadas"].append(columna)
        cursor.execute("SELECT INDEX_NAME FROM information_schema.STATISTICS "
                       "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ventasRegistro'")
        indices = {str(fila[0]).lower() for fila in cursor.fetchall()}
        nombre, cols = INDICE_PARTIDA
        if nombre not in indices:
            try:
                cursor.execute(f"ALTER TABLE ventasRegistro ADD UNIQUE KEY {nombre} {cols}")
                reporte["indice_creado"] = True
            except mysql.connector.Error as err:
                # 1062 = ya hay partidas duplicadas: se reporta sin tumbar el arranque.
                print(f"No se pudo crear {nombre} (¿partidas duplicadas?): {err}")
        nombre_est, cols_est = INDICE_ESTATUS
        if nombre_est not in indices:
            try:
                cursor.execute(f"ALTER TABLE ventasRegistro ADD INDEX {nombre_est} {cols_est}")
                reporte["indice_creado"] = True
            except mysql.connector.Error as err:
                print(f"No se pudo crear {nombre_est}: {err}")
        conn.commit()
        if backfill_descontado:
            # Cancelaciones viejas PRIMERO: marketplace, nunca descontadas y fuera
            # de la ventana de 72h (el job ya no las reintenta). Las ya regresadas
            # con el flag anterior caen aquí; fuera de métricas pero en auditoría.
            cursor.execute("UPDATE ventasRegistro SET estatus = 'cancelada' "
                           "WHERE plataforma IN (%s, %s) AND inventario_descontado = 0 "
                           "AND estatus = 'activa' AND fecha_registro < DATE_SUB(NOW(), INTERVAL 3 DAY)",
                           PLATAFORMAS_JOB)
            reporte["canceladas_backfill"] = cursor.rowcount
            conn.commit()
            cursor.execute("UPDATE ventasRegistro SET inventario_descontado = 1 "
                           "WHERE plataforma IN (%s, %s) AND inventario_descontado = 0 "
                           "AND estatus = 'activa'",
                           PLATAFORMAS_JOB)
            reporte["backfill"] = cursor.rowcount
            conn.commit()
    finally:
        cursor.close()
        conn.close()
    return reporte


def verificar_esquema_job() -> None:
    """Falla rápido con mensaje claro si falta alguna columna (antes del poll largo)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        faltan = [c for c in COLUMNAS_JOB if c not in columnas_existentes(cursor, "ventasRegistro")]
    finally:
        cursor.close()
        conn.close()
    if faltan:
        raise RuntimeError(f"Falta(n) columna(s) {', '.join(faltan)} en ventasRegistro; "
                           "se crean solas al arrancar la API (lifespan). Reinicia el servicio.")
