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
]


def _columna_existe(cursor, tabla: str, columna: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (tabla, columna)
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
) -> bool:
    """
    Registra la guia recien generada. Devuelve True si se guardo.

    Es UPSERT: si el webhook ya creo la fila (porque Skydropx notifico antes de
    que termináramos), se completan los datos sin pisar el estatus que ya llego.
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
                (codigo_cotizacion, tracking_number, shipment_id, carrier, servicio,
                 costo, etiqueta_url, orden_detalle_url, tracking_url, usuario)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                codigo_cotizacion = VALUES(codigo_cotizacion),
                shipment_id       = COALESCE(VALUES(shipment_id), shipment_id),
                carrier           = COALESCE(VALUES(carrier), carrier),
                servicio          = COALESCE(VALUES(servicio), servicio),
                costo             = COALESCE(VALUES(costo), costo),
                etiqueta_url      = COALESCE(VALUES(etiqueta_url), etiqueta_url),
                orden_detalle_url = COALESCE(VALUES(orden_detalle_url), orden_detalle_url),
                tracking_url      = COALESCE(VALUES(tracking_url), tracking_url),
                usuario           = COALESCE(VALUES(usuario), usuario)
            """,
            (codigo_cotizacion, tracking, shipment_id, carrier, servicio,
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

    if not tracking and not shipment_id:
        print("Webhook Skydropx sin tracking_number ni shipment_id: no hay como ligarlo.")
        return {"aplicado": False, "envio_encontrado": False, "evento_nuevo": False}

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
            # Primero por tracking (la llave que trae el webhook); si no, por shipment.
            if tracking:
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
                cursor.execute(
                    """
                    UPDATE skydropx_envios
                    SET estatus = %s,
                        estatus_descripcion = COALESCE(%s, estatus_descripcion),
                        tracking_number = COALESCE(tracking_number, %s)
                    WHERE shipment_id = %s
                    """,
                    (estatus, descripcion, tracking, shipment_id)
                )
                encontrado = cursor.rowcount > 0

            # Webhook antes que la guia: se crea la fila para no perder el estatus.
            if not encontrado:
                cursor.execute(
                    """
                    INSERT INTO skydropx_envios
                        (codigo_cotizacion, tracking_number, shipment_id, estatus,
                         estatus_descripcion, etiqueta_url, tracking_url)
                    VALUES (NULL, %s, %s, %s, %s, NULLIF(%s, ''), NULLIF(%s, ''))
                    ON DUPLICATE KEY UPDATE
                        estatus = VALUES(estatus),
                        estatus_descripcion = COALESCE(VALUES(estatus_descripcion), estatus_descripcion)
                    """,
                    (tracking, shipment_id, estatus, descripcion,
                     evento.get("etiqueta_url") or "", evento.get("tracking_url") or "")
                )

        conn.commit()
        return {"aplicado": True, "envio_encontrado": encontrado, "evento_nuevo": evento_nuevo}
    except mysql.connector.Error as err:
        if conn:
            conn.rollback()
        print(f"Error aplicando webhook Skydropx: {err}")
        return {"aplicado": False, "envio_encontrado": False, "evento_nuevo": False}
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
