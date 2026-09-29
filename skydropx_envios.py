# Persistencia del modulo de envios (Skydropx Pro).
#
# Vive aparte de skydropx_service.py a proposito: ese modulo solo habla HTTP con
# Skydropx y no toca la base. Aqui esta todo lo que se guarda en MySQL.
#
# Por que existe esta tabla: el webhook de Skydropx llega al servidor de forma
# asincrona, cuando nadie tiene el panel abierto. Sin una tabla, el estatus del
# envio no tendria donde aterrizar; el localStorage del navegador no puede
# recibir nada del servidor.
#
# La llave natural es tracking_number, que es lo unico que el webhook trae para
# identificar el envio. Tanto la generacion de guia como el webhook hacen UPSERT
# sobre esa llave, asi que no importa cual llegue primero: si Skydropx notifica
# antes de que terminemos de guardar, la fila se crea con el estatus y despues se
# completa con el codigo de cotizacion (y al reves).
import hashlib
import json
import os
from typing import Any, Dict, List, Optional

import mysql.connector
from dotenv import load_dotenv

load_dotenv()

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "sql", "skydropx_schema.sql")


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

# Estatus que manda Skydropx -> como se lee en el panel. El tono es el mismo
# vocabulario de badges que ya usa el front (success / warn / danger / info).
# Se traduce aqui para que el panel no tenga que conocer el catalogo de Skydropx.
_ESTATUS = {
    "created":           ("Guía creada", "info"),
    "label_created":     ("Etiqueta lista", "info"),
    "generated":         ("Etiqueta lista", "info"),
    "waiting":           ("Esperando recolección", "warn"),
    "waiting_pickup":    ("Esperando recolección", "warn"),
    "picked_up":         ("Recolectado", "info"),
    "in_transit":        ("En tránsito", "info"),
    "out_for_delivery":  ("En reparto", "info"),
    "delivered":         ("Entregado", "success"),
    "exception":         ("Incidencia", "danger"),
    "failed":            ("Falló", "danger"),
    "cancelled":         ("Cancelado", "danger"),
    "canceled":          ("Cancelado", "danger"),
    "returned":          ("Devuelto", "warn"),
    "expired":           ("Expirado", "warn"),
}


def describir_estatus(estatus: Optional[str]) -> Dict[str, str]:
    """
    Traduce el estatus crudo a texto y tono para el panel. Un estatus que no
    este en el catalogo se muestra tal cual en vez de esconderse: si Skydropx
    agrega uno nuevo, el usuario lo ve aunque no este traducido.
    """
    clave = (estatus or "").strip().lower()
    texto, tono = _ESTATUS.get(clave, (None, None))
    if texto is None:
        texto = (estatus or "Sin estatus").replace("_", " ").capitalize()
        tono = "info"
    return {"estatus_texto": texto, "estatus_tono": tono}


# Columnas agregadas despues de la creacion inicial de la tabla. CREATE TABLE
# IF NOT EXISTS no toca una tabla que ya existe, asi que las bases donde el
# modulo ya corrio necesitan este ALTER (mismo patron que routers/embarques.py).
_COLUMNAS_NUEVAS_ENVIOS = [
    ("orden_detalle_url", "TEXT NULL AFTER etiqueta_url"),
    ("package_id", "VARCHAR(100) NULL AFTER shipment_id"),
]

# Indices que acompanan a columnas nuevas (mismo motivo que arriba).
_INDICES_NUEVOS_ENVIOS = [
    ("uq_sky_package", "UNIQUE KEY uq_sky_package (package_id)"),
]


def _columna_existe(cursor, tabla: str, columna: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (tabla, columna)
    )
    return cursor.fetchone() is not None


def _indice_existe(cursor, tabla: str, indice: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM INFORMATION_SCHEMA.STATISTICS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND INDEX_NAME = %s LIMIT 1",
        (tabla, indice)
    )
    return cursor.fetchone() is not None


def crear_tablas_skydropx():
    """
    Corre el schema (CREATE TABLE IF NOT EXISTS, sin FK) y las migraciones de
    columnas al arrancar el backend. Idempotente: no rompe si las tablas o las
    columnas ya estan en su forma final.
    """
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        lineas = [l for l in f if not l.strip().startswith("--")]
    statements = [s.strip() for s in "".join(lineas).split(";") if s.strip()]

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        for stmt in statements:
            cursor.execute(stmt)
        for columna, definicion in _COLUMNAS_NUEVAS_ENVIOS:
            if not _columna_existe(cursor, "skydropx_envios", columna):
                cursor.execute(f"ALTER TABLE skydropx_envios ADD COLUMN {columna} {definicion}")
                print(f"Columna skydropx_envios.{columna} agregada.")
        for indice, definicion in _INDICES_NUEVOS_ENVIOS:
            if not _indice_existe(cursor, "skydropx_envios", indice):
                cursor.execute(f"ALTER TABLE skydropx_envios ADD {definicion}")
                print(f"Indice skydropx_envios.{indice} agregado.")
        conn.commit()
        print("Tablas de envios Skydropx verificadas/creadas.")
    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error creando tablas de envios Skydropx: {err}")
    finally:
        cursor.close()
        conn.close()


def _normaliza_tracking(valor: Optional[str]) -> Optional[str]:
    """Cadena vacia -> NULL. En el indice unico varios NULL conviven; varios '' no."""
    v = (valor or "").strip()
    return v or None


def guardar_envio(
    codigo_cotizacion: Optional[str],
    tracking_number: Optional[str],
    shipment_id: Optional[str] = None,
    carrier: Optional[str] = None,
    servicio: Optional[str] = None,
    costo: Optional[float] = None,
    etiqueta_url: Optional[str] = None,
    orden_detalle_url: Optional[str] = None,
    tracking_url: Optional[str] = None,
    usuario: Optional[str] = None,
    package_id: Optional[str] = None,
) -> bool:
    """
    Registra la guia recien generada. Devuelve True si se guardo.

    Es UPSERT sobre tracking_number o package_id (los dos son UNIQUE): si el
    webhook ya creo la fila (porque Skydropx notifico antes de que
    termináramos), se completan los datos sin pisar el estatus que ya llego.
    Nunca lanza: la guia ya se contrato y se cobro, y fallar aqui haria que el
    usuario la generara de nuevo.
    """
    tracking = _normaliza_tracking(tracking_number)
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO skydropx_envios
                (codigo_cotizacion, tracking_number, shipment_id, package_id, carrier, servicio,
                 costo, etiqueta_url, orden_detalle_url, tracking_url, usuario)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                codigo_cotizacion = VALUES(codigo_cotizacion),
                tracking_number   = COALESCE(VALUES(tracking_number), tracking_number),
                shipment_id       = COALESCE(VALUES(shipment_id), shipment_id),
                package_id        = COALESCE(VALUES(package_id), package_id),
                carrier           = COALESCE(VALUES(carrier), carrier),
                servicio          = COALESCE(VALUES(servicio), servicio),
                costo             = COALESCE(VALUES(costo), costo),
                etiqueta_url      = COALESCE(VALUES(etiqueta_url), etiqueta_url),
                orden_detalle_url = COALESCE(VALUES(orden_detalle_url), orden_detalle_url),
                tracking_url      = COALESCE(VALUES(tracking_url), tracking_url),
                usuario           = COALESCE(VALUES(usuario), usuario)
            """,
            (codigo_cotizacion, tracking, shipment_id, package_id, carrier, servicio,
             costo, etiqueta_url, orden_detalle_url, tracking_url, usuario)
        )
        conn.commit()
        return True
    except mysql.connector.Error as err:
        if conn:
            conn.rollback()
        print(f"Error guardando envio Skydropx: {err}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def registrar_evento_webhook(evento: Dict[str, Any], payload_crudo: Optional[str] = None) -> Dict[str, Any]:
    """
    Aplica un evento del webhook: actualiza el estatus del envio y lo agrega a la
    bitacora. `evento` es lo que devuelve skydropx_service.extraer_evento_webhook().

    Devuelve {"aplicado": bool, "envio_encontrado": bool, "evento_nuevo": bool}
    para que el router pueda dejar rastro claro en el log.

    Si el envio no existe todavia (webhook antes de que guardemos la guia), se
    crea la fila con lo que trae el evento. El codigo de cotizacion se completa
    despues, cuando guardar_envio() haga su UPSERT sobre el mismo tracking.
    """
    tracking = _normaliza_tracking(evento.get("tracking_number"))
    estatus = (evento.get("estatus") or "").strip() or None
    descripcion = (evento.get("descripcion") or "").strip() or None
    shipment_id = evento.get("shipment_id")
    package_id = evento.get("package_id")

    if not tracking and not shipment_id:
        print("Webhook Skydropx sin tracking_number ni shipment_id: no hay como ligarlo.")
        return {
            "aplicado": False, "envio_encontrado": False, "evento_nuevo": False,
            "usuario": None, "codigo_cotizacion": None,
        }

    # Huella para no duplicar el mismo evento cuando Skydropx reintenta.
    huella = hashlib.sha1(
        f"{tracking or shipment_id}|{estatus or ''}|{descripcion or ''}".encode("utf-8")
    ).hexdigest()

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT IGNORE INTO skydropx_envio_eventos
                (tracking_number, shipment_id, estatus, descripcion, huella, payload)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (tracking, shipment_id, estatus, descripcion, huella, payload_crudo)
        )
        evento_nuevo = cursor.rowcount > 0

        encontrado = False
        if estatus:
            # Primero por package_id: con V2 la guia nace sin tracking, y en
            # multipaquete varios paquetes comparten shipment_id, asi que es la
            # unica llave que apunta a UNA sola fila desde el principio.
            if package_id:
                cursor.execute(
                    """
                    UPDATE skydropx_envios
                    SET estatus = %s,
                        estatus_descripcion = COALESCE(%s, estatus_descripcion),
                        tracking_number = COALESCE(tracking_number, %s),
                        etiqueta_url = COALESCE(NULLIF(%s, ''), etiqueta_url),
                        tracking_url = COALESCE(NULLIF(%s, ''), tracking_url),
                        shipment_id = COALESCE(shipment_id, %s)
                    WHERE package_id = %s
                    """,
                    (estatus, descripcion, tracking, evento.get("etiqueta_url") or "",
                     evento.get("tracking_url") or "", shipment_id, package_id)
                )
                encontrado = cursor.rowcount > 0

            # Luego por tracking; si no, por shipment.
            if tracking and not encontrado:
                cursor.execute(
                    """
                    UPDATE skydropx_envios
                    SET estatus = %s,
                        estatus_descripcion = COALESCE(%s, estatus_descripcion),
                        etiqueta_url = COALESCE(NULLIF(%s, ''), etiqueta_url),
                        tracking_url = COALESCE(NULLIF(%s, ''), tracking_url),
                        shipment_id = COALESCE(shipment_id, %s)
                    WHERE tracking_number = %s
                    """,
                    (estatus, descripcion, evento.get("etiqueta_url") or "",
                     evento.get("tracking_url") or "", shipment_id, tracking)
                )
                encontrado = cursor.rowcount > 0

            if not encontrado and shipment_id:
                # Mismos campos que la rama de arriba (por tracking_number): si no se
                # actualizan aqui tambien, un envio cuyo PRIMER evento trae tracking_number
                # Y label_url a la vez (caso comun: "label_created" como unico evento)
                # se queda con etiqueta_url en NULL para siempre, porque esta rama es la
                # que lo encuentra (todavia no tenia tracking_number guardado) y no
                # llegara un segundo evento que la vuelva a mandar.
                cursor.execute(
                    """
                    UPDATE skydropx_envios
                    SET estatus = %s,
                        estatus_descripcion = COALESCE(%s, estatus_descripcion),
                        tracking_number = COALESCE(tracking_number, %s),
                        etiqueta_url = COALESCE(NULLIF(%s, ''), etiqueta_url),
                        tracking_url = COALESCE(NULLIF(%s, ''), tracking_url)
                    WHERE shipment_id = %s
                    """,
                    (estatus, descripcion, tracking,
                     evento.get("etiqueta_url") or "", evento.get("tracking_url") or "", shipment_id)
                )
                encontrado = cursor.rowcount > 0

            # Webhook antes que la guia: se crea la fila para no perder el estatus.
            if not encontrado:
                cursor.execute(
                    """
                    INSERT INTO skydropx_envios
                        (codigo_cotizacion, tracking_number, shipment_id, package_id, estatus,
                         estatus_descripcion, etiqueta_url, tracking_url)
                    VALUES (NULL, %s, %s, %s, %s, %s, NULLIF(%s, ''), NULLIF(%s, ''))
                    ON DUPLICATE KEY UPDATE
                        estatus = VALUES(estatus),
                        estatus_descripcion = COALESCE(VALUES(estatus_descripcion), estatus_descripcion)
                    """,
                    (tracking, shipment_id, package_id, estatus, descripcion,
                     evento.get("etiqueta_url") or "", evento.get("tracking_url") or "")
                )

        # Dueno del envio, para poder avisarle por notificaciones_service. Se
        # busca por cualquiera de las dos llaves porque la fila puede haberse
        # localizado o creado por una u otra segun lo que trajo el evento.
        usuario_dueno = None
        codigo_cotizacion = None
        if estatus:
            cursor.execute(
                """
                SELECT usuario, codigo_cotizacion FROM skydropx_envios
                WHERE tracking_number = %s OR shipment_id = %s
                LIMIT 1
                """,
                (tracking, shipment_id)
            )
            fila = cursor.fetchone()
            if fila:
                usuario_dueno, codigo_cotizacion = fila[0], fila[1]

        conn.commit()
        return {
            "aplicado": True,
            "envio_encontrado": encontrado,
            "evento_nuevo": evento_nuevo,
            "usuario": usuario_dueno,
            "codigo_cotizacion": codigo_cotizacion,
        }
    except mysql.connector.Error as err:
        if conn:
            conn.rollback()
        print(f"Error aplicando webhook Skydropx: {err}")
        return {
            "aplicado": False, "envio_encontrado": False, "evento_nuevo": False,
            "usuario": None, "codigo_cotizacion": None,
        }
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def _con_estatus(fila: Dict[str, Any]) -> Dict[str, Any]:
    """Agrega estatus_texto / estatus_tono y deja el costo como float serializable."""
    fila = dict(fila)
    fila.update(describir_estatus(fila.get("estatus")))
    if fila.get("costo") is not None:
        fila["costo"] = float(fila["costo"])
    for campo in ("creado_en", "actualizado_en", "recibido_en"):
        if fila.get(campo) is not None:
            fila[campo] = str(fila[campo])
    return fila


def listar_envios(codigo_cotizacion: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Envios registrados, opcionalmente los de una sola cotizacion.
    El panel lo llama una vez al abrir Cotizaciones y arma su mapa por codigo.
    """
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        if codigo_cotizacion:
            cursor.execute(
                "SELECT * FROM skydropx_envios WHERE codigo_cotizacion = %s ORDER BY id DESC",
                (codigo_cotizacion,)
            )
        else:
            # Solo los ligados a una cotizacion: los huerfanos (webhook sin guia
            # nuestra) no tienen donde pintarse en la tabla de cotizaciones.
            cursor.execute(
                "SELECT * FROM skydropx_envios WHERE codigo_cotizacion IS NOT NULL ORDER BY id DESC"
            )
        return [_con_estatus(f) for f in cursor.fetchall()]
    except mysql.connector.Error as err:
        print(f"Error consultando envios Skydropx: {err}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def eventos_de(tracking_number: str) -> List[Dict[str, Any]]:
    """Linea de tiempo de una guia, del mas reciente al mas viejo."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT estatus, descripcion, recibido_en
            FROM skydropx_envio_eventos
            WHERE tracking_number = %s
            ORDER BY id DESC
            """,
            (tracking_number,)
        )
        return [_con_estatus(f) for f in cursor.fetchall()]
    except mysql.connector.Error as err:
        print(f"Error consultando eventos Skydropx: {err}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Recolecciones
# ─────────────────────────────────────────────────────────────────────────────

def envios_de_shipment(shipment_id: str) -> List[Dict[str, Any]]:
    """
    Guias (paquetes) guardadas para un shipment. Lista vacia = el shipment no
    lo genero este sistema, y no se le debe agendar recoleccion desde aqui.
    """
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM skydropx_envios WHERE shipment_id = %s ORDER BY id",
            (shipment_id,)
        )
        return [_con_estatus(f) for f in cursor.fetchall()]
    except mysql.connector.Error as err:
        print(f"Error consultando envios del shipment {shipment_id}: {err}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def _recoleccion_serializable(fila: Dict[str, Any]) -> Dict[str, Any]:
    fila = dict(fila)
    fila.pop("respuesta", None)
    for campo in ("fecha", "creado_en"):
        if fila.get(campo) is not None:
            fila[campo] = str(fila[campo])
    if fila.get("peso_total") is not None:
        fila["peso_total"] = float(fila["peso_total"])
    return fila


def recoleccion_de(shipment_id: str) -> Optional[Dict[str, Any]]:
    """La recoleccion ya agendada para un shipment, o None."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM skydropx_recolecciones WHERE shipment_id = %s", (shipment_id,))
        fila = cursor.fetchone()
        return _recoleccion_serializable(fila) if fila else None
    except mysql.connector.Error as err:
        print(f"Error consultando recoleccion de {shipment_id}: {err}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def listar_recolecciones(codigo_cotizacion: str) -> List[Dict[str, Any]]:
    """Recolecciones agendadas para los envios de una cotizacion."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT * FROM skydropx_recolecciones WHERE codigo_cotizacion = %s ORDER BY id",
            (codigo_cotizacion,)
        )
        return [_recoleccion_serializable(f) for f in cursor.fetchall()]
    except mysql.connector.Error as err:
        print(f"Error consultando recolecciones de {codigo_cotizacion}: {err}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def guardar_recoleccion(
    shipment_id: str,
    codigo_cotizacion: Optional[str],
    pickup_id: Optional[str],
    estatus: Optional[str],
    confirmacion: Optional[str],
    carrier: Optional[str],
    fecha: str,
    hora_inicio: str,
    hora_fin: str,
    paquetes: int,
    peso_total: float,
    usuario: Optional[str],
    respuesta: Optional[Dict[str, Any]] = None,
) -> bool:
    """
    Registra la recoleccion ya agendada en Skydropx. Nunca lanza: la
    recoleccion ya existe con el carrier y fallar aqui haria que el usuario la
    agendara otra vez.
    """
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO skydropx_recolecciones
                (shipment_id, codigo_cotizacion, pickup_id, estatus, confirmacion, carrier,
                 fecha, hora_inicio, hora_fin, paquetes, peso_total, usuario, respuesta)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                pickup_id = VALUES(pickup_id), estatus = VALUES(estatus),
                confirmacion = VALUES(confirmacion), fecha = VALUES(fecha),
                hora_inicio = VALUES(hora_inicio), hora_fin = VALUES(hora_fin),
                paquetes = VALUES(paquetes), peso_total = VALUES(peso_total),
                respuesta = VALUES(respuesta)
            """,
            (shipment_id, codigo_cotizacion, pickup_id, estatus, confirmacion, carrier,
             fecha, hora_inicio, hora_fin, paquetes, peso_total, usuario,
             json.dumps(respuesta, ensure_ascii=False)[:4000] if respuesta is not None else None)
        )
        conn.commit()
        return True
    except mysql.connector.Error as err:
        if conn:
            conn.rollback()
        print(f"Error guardando recoleccion Skydropx: {err}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# Catalogo de cajas (presets de medidas del modal)
# ─────────────────────────────────────────────────────────────────────────────

class CajaDuplicada(Exception):
    """Ya existe una caja con ese nombre (indice unico uq_sky_caja_nombre)."""


def _caja_serializable(fila: Dict[str, Any]) -> Dict[str, Any]:
    fila = dict(fila)
    for campo in ("length", "width", "height", "weight"):
        if fila.get(campo) is not None:
            fila[campo] = float(fila[campo])
    if fila.get("creado_en") is not None:
        fila["creado_en"] = str(fila["creado_en"])
    return fila


def listar_cajas() -> List[Dict[str, Any]]:
    """Cajas en el orden en que se dieron de alta (las 4 de semilla primero)."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id, nombre, length, width, height, weight, package_type, creado_en "
            "FROM skydropx_cajas ORDER BY id"
        )
        return [_caja_serializable(f) for f in cursor.fetchall()]
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def guardar_caja(
    nombre: str,
    length: float,
    width: float,
    height: float,
    weight: float,
    package_type: str = "4G",
    usuario: Optional[str] = None,
) -> Dict[str, Any]:
    """Da de alta una caja y la devuelve. Lanza CajaDuplicada si el nombre ya existe."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            INSERT INTO skydropx_cajas (nombre, length, width, height, weight, package_type, usuario)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (nombre, length, width, height, weight, package_type, usuario)
        )
        nuevo_id = cursor.lastrowid
        conn.commit()
        cursor.execute(
            "SELECT id, nombre, length, width, height, weight, package_type, creado_en "
            "FROM skydropx_cajas WHERE id = %s",
            (nuevo_id,)
        )
        return _caja_serializable(cursor.fetchone())
    except mysql.connector.IntegrityError as err:
        if conn:
            conn.rollback()
        if err.errno == 1062:
            raise CajaDuplicada(nombre)
        raise
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
