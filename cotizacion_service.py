"""
Servicios compartidos para cotizaciones.
Contiene la lógica de negocio reutilizable por los endpoints
(por ejemplo /cotizaciones/nuevo-codigo y /genera-cotizacion).
"""
PREFIJO_COTIZACION = "ZTC-"


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
        sql_maestro = """
            INSERT INTO cotizaciones
            (codigo_cotizacion, empresa, atencion, email, domicilio, telefono, subtotal, iva, total, costo_envio, forma_pago, comentarios, usuario, pdf)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        valores_maestro = (
            cot.codigo_cotizacion, cot.empresa, cot.atencion, cot.email,
            cot.domicilio, cot.telefono, float(cot.subtotal), float(cot.iva),
            float(cot.total), float(cot.costo_envio), cot.forma_pago, cot.comentarios, cot.usuario, cot.pdf
        )
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
