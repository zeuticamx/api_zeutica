"""
Servicios compartidos para cotizaciones.
Contiene la lógica de negocio reutilizable por los endpoints
(por ejemplo /cotizaciones/nuevo-codigo y /genera-cotizacion).
"""
PREFIJO_COTIZACION = "ZTC-"

import logging
import os
import re
import unicodedata

import mysql.connector
from fastapi import HTTPException

logger = logging.getLogger("zeutica.cotizaciones")

# Indicador en memoria: lo fija asegurar_columna_cliente_id() al arrancar.
_COLUMNA_CLIENTE_ID = False


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
    )


def columna_cliente_id_existe() -> bool:
    """True si cotizaciones.cliente_id existe (la fija el arranque)."""
    return _COLUMNA_CLIENTE_ID


def normalizar_nombre(texto) -> str:
    """Sin acentos, en minúsculas y con espacios colapsados (para comparar nombres)."""
    if texto is None:
        return ""
    sin_acentos = "".join(
        c for c in unicodedata.normalize("NFD", str(texto)) if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"\s+", " ", sin_acentos).strip().lower()


def rellenar_cliente_id_cotizaciones(conn) -> dict:
    """Liga cotizaciones sin cliente_id con el cliente (eliminado = 0) de nombre único igual a
    `empresa`. Ambiguas, sin coincidencia o vacías quedan NULL. Nunca sobrescribe."""
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id, empresa FROM cotizaciones WHERE cliente_id IS NULL")
        cotizaciones = cursor.fetchall()
        cursor.execute("SELECT id, nombre FROM clientes WHERE eliminado = 0")
        por_nombre: dict = {}
        for cid, nombre in cursor.fetchall():
            clave = normalizar_nombre(nombre)
            if clave:
                por_nombre.setdefault(clave, []).append(cid)
        ligadas = ambiguas = sin_coincidencia = 0
        for cot_id, empresa in cotizaciones:
            clave = normalizar_nombre(empresa)
            ids = por_nombre.get(clave, []) if clave else []
            if len(ids) == 1:
                cursor.execute(
                    "UPDATE cotizaciones SET cliente_id = %s WHERE id = %s AND cliente_id IS NULL",
                    (ids[0], cot_id),
                )
                ligadas += 1
            elif len(ids) > 1:
                ambiguas += 1
            else:
                sin_coincidencia += 1
        conn.commit()
        return {"ligadas": ligadas, "ambiguas": ambiguas, "sin_coincidencia": sin_coincidencia}
    finally:
        cursor.close()


def asegurar_columna_cliente_id() -> None:
    """Crea cotizaciones.cliente_id (con índice, sin FK) y, solo si se acaba de crear, rellena
    las cotizaciones viejas una vez. Nunca propaga errores (se llama en el lifespan)."""
    global _COLUMNA_CLIENTE_ID
    _COLUMNA_CLIENTE_ID = False
    try:
        conn = get_db_connection()
    except Exception:
        logger.exception("cliente_id de cotizaciones: no hubo conexión")
        return
    try:
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                           "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'cotizaciones'")
            existentes = {str(f[0]).lower() for f in cursor.fetchall()}
            creada = False
            if "cliente_id" not in existentes:
                cursor.execute("ALTER TABLE cotizaciones ADD COLUMN cliente_id INT NULL")
                conn.commit()
                creada = True
            _COLUMNA_CLIENTE_ID = True
            if creada:
                try:
                    cursor.execute("ALTER TABLE cotizaciones ADD INDEX idx_cotizaciones_cliente_id (cliente_id)")
                    conn.commit()
                except Exception:
                    logger.exception("cliente_id de cotizaciones: no se pudo crear el índice")
        finally:
            cursor.close()
        if creada:
            try:
                r = rellenar_cliente_id_cotizaciones(conn)
                logger.info(
                    "cliente_id de cotizaciones: %d ligadas, %d ambiguas, %d sin coincidencia",
                    r["ligadas"], r["ambiguas"], r["sin_coincidencia"],
                )
            except Exception:
                logger.exception("cliente_id de cotizaciones: falló el rellenado")
                try:
                    conn.rollback()
                except Exception:
                    pass
    except Exception:
        logger.exception("cliente_id de cotizaciones: no se pudo crear la columna")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def generar_nuevo_codigo(connection) -> str:
    """
    Calcula el siguiente código consecutivo de cotización (ej: ZTC-273).

    Recibe una conexión abierta a la base de datos y NO la cierra:
    de eso se encarga quien la abrió. Así la misma función sirve
    tanto para el endpoint que solo consulta el código como para el
    que además genera y guarda la cotización dentro de la misma
    transacción.
    """
    with connection.cursor(dictionary=True) as cursor:
        cursor.execute(
            "SELECT codigo_cotizacion FROM cotizaciones ORDER BY id DESC LIMIT 1"
        )
        ultimo = cursor.fetchone()

    # Si no hay registros previos, empezamos en ZTC-001
    if not ultimo:
        return f"{PREFIJO_COTIZACION}001"

    # Extraer el número de "ZTC-239" -> 239 y sumar 1
    ultimo_codigo = ultimo["codigo_cotizacion"]
    numero_actual = int(ultimo_codigo.replace(PREFIJO_COTIZACION, ""))
    nuevo_numero = numero_actual + 1

    # Formateamos con ceros a la izquierda (ej: ZTC-240)
    return f"{PREFIJO_COTIZACION}{nuevo_numero:03d}"


def validar_cliente_id(connection, cliente_id) -> None:
    """Valida el cliente_id opcional de una cotización: None no valida nada; si viene, debe
    existir en `clientes` con eliminado = 0, si no 422. NO cierra la conexión. Hay que llamarla
    ANTES de asignar folio o generar el PDF."""
    if cliente_id is None:
        return
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT id FROM clientes WHERE id = %s AND eliminado = 0", (cliente_id,))
        fila = cursor.fetchone()
    finally:
        try:
            cursor.close()
        except Exception:
            pass
    if not fila:
        raise HTTPException(status_code=422, detail="El cliente indicado no existe o fue eliminado")


def guardar_cotizacion_db(connection, cot) -> int:
    """
    Inserta la cotización (maestro + items) en la base de datos y devuelve
    el id generado.

    Recibe una conexión abierta y hace commit; NO la cierra ni maneja
    rollback: eso lo controla quien la abrió. Así el mismo INSERT sirve
    para /cotizaciones/guardar y para /genera-cotizacion.

    `cot` es cualquier objeto con los atributos de CotizacionSchema
    (codigo_cotizacion, empresa, atencion, email, domicilio, telefono,
    subtotal, iva, total, costo_envio, forma_pago, comentarios, usuario,
    pdf, items[]).
    """
    with connection.cursor() as cursor:
        # Insertar en la tabla principal (Maestro)
        # cliente_id es opcional (getattr: un objeto sin el atributo guarda NULL). Gana sobre
        # `empresa`: no se compara con ella. Si el ALTER del arranque falló, se usa la forma vieja.
        cliente_id = getattr(cot, "cliente_id", None)
        valores_maestro = (
            cot.codigo_cotizacion, cot.empresa, cot.atencion, cot.email,
            cot.domicilio, cot.telefono, float(cot.subtotal), float(cot.iva),
            float(cot.total), float(cot.costo_envio), cot.forma_pago, cot.comentarios, cot.usuario, cot.pdf
        )
        if columna_cliente_id_existe():
            sql_maestro = """
                INSERT INTO cotizaciones
                (codigo_cotizacion, empresa, atencion, email, domicilio, telefono, subtotal, iva, total, costo_envio, forma_pago, comentarios, usuario, pdf, cliente_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            valores_maestro = valores_maestro + (cliente_id,)
        else:
            sql_maestro = """
                INSERT INTO cotizaciones
                (codigo_cotizacion, empresa, atencion, email, domicilio, telefono, subtotal, iva, total, costo_envio, forma_pago, comentarios, usuario, pdf)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
        cursor.execute(sql_maestro, valores_maestro)

        # Id generado para vincular los productos
        cotizacion_id = cursor.lastrowid

        # Insertar los productos de golpe
        sql_detalle = """
            INSERT INTO cotizacion_items
            (cotizacion_id, sku, nombre_producto, cantidad, precio_unitario, total_linea)
            VALUES (%s, %s, %s, %s, %s, %s)
        """
        items_data = [
            (cotizacion_id, i.sku, i.nombre_producto, i.cantidad, float(i.precio_unitario), float(i.total_linea))
            for i in cot.items
        ]
        cursor.executemany(sql_detalle, items_data)

        connection.commit()

    return cotizacion_id
