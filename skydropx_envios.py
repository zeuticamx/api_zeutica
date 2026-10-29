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
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

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


# Una guia cancelada no se reactiva: un evento posterior (retraso, reintento fuera de orden)
# no debe devolverla a «activa». Los UPDATE del webhook usan estas dos expresiones.
_CANCELADOS = ("canceled", "cancelled")
_SQL_ESTATUS = "IF(LOWER(estatus) IN ('canceled', 'cancelled'), estatus, %s)"
_SQL_DESCRIPCION = ("IF(LOWER(estatus) IN ('canceled', 'cancelled'), estatus_descripcion, "
                    "COALESCE(%s, estatus_descripcion))")


def es_cancelado(estatus: Optional[str]) -> bool:
    return (estatus or "").strip().lower() in _CANCELADOS


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
    ("numero_envio", "INT NOT NULL DEFAULT 1 AFTER codigo_cotizacion"),
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


# Tope de compras (envios) por cotizacion. Una compra con varios bultos cuenta una vez.
MAX_ENVIOS_POR_COTIZACION = 10


class LimiteEnviosAlcanzado(Exception):
    """La cotizacion ya tiene MAX_ENVIOS_POR_COTIZACION envios activos."""


class CompraEnCurso(Exception):
    """Otra compra de la misma cotizacion tiene el candado (doble clic o dos usuarios)."""


class ReservaNoDisponible(Exception):
    """No se pudo verificar el tope en la BD; se prefiere no contratar (cuesta dinero)."""


@contextmanager
def reservar_envio(codigo_cotizacion: Optional[str]) -> Iterator[int]:
    """
    Reserva el siguiente numero de envio de una cotizacion y mantiene un candado mientras el
    llamador compra y guarda las guias, para que dos compras simultaneas no pasen del tope.

    Cuenta los envios ACTIVOS: uno cuyas guias estan todas canceladas no cuenta, para poder
    reponerlo. El numero nuevo es MAX + 1 sobre todos (los cancelados incluidos), asi nunca se
    repite. Sin codigo de cotizacion no hay a que limitar y devuelve 1.

    El candado es GET_LOCK sobre la conexion que se queda abierta durante el `with`; con
    timeout 0 una segunda compra recibe CompraEnCurso al instante (no bloquea el event loop).
    Lanza LimiteEnviosAlcanzado, CompraEnCurso o ReservaNoDisponible ANTES de ceder el control.
    """
    if not codigo_cotizacion:
        yield 1
        return

    nombre = "skydropx:" + hashlib.sha1(codigo_cotizacion.encode("utf-8")).hexdigest()
    conn = None
    cursor = None
    try:
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT GET_LOCK(%s, 0)", (nombre,))
            if cursor.fetchone()[0] != 1:
                raise CompraEnCurso(codigo_cotizacion)
            cursor.execute(
                "SELECT COUNT(DISTINCT numero_envio) FROM skydropx_envios "
                "WHERE codigo_cotizacion = %s AND LOWER(estatus) NOT IN ('canceled', 'cancelled')",
                (codigo_cotizacion,)
            )
            activos = int(cursor.fetchone()[0] or 0)
            if activos >= MAX_ENVIOS_POR_COTIZACION:
                raise LimiteEnviosAlcanzado(activos)
            cursor.execute(
                "SELECT COALESCE(MAX(numero_envio), 0) FROM skydropx_envios WHERE codigo_cotizacion = %s",
                (codigo_cotizacion,)
            )
            siguiente = int(cursor.fetchone()[0] or 0) + 1
        except mysql.connector.Error as err:
            print(f"Error reservando envio Skydropx de {codigo_cotizacion}: {err}")
            raise ReservaNoDisponible(codigo_cotizacion) from err
        yield siguiente
    finally:
        if cursor:
            try:
                cursor.execute("SELECT RELEASE_LOCK(%s)", (nombre,))
                cursor.fetchone()
            except mysql.connector.Error:
                pass  # al cerrar la conexion MySQL suelta el candado igual
            cursor.close()
        if conn:
            conn.close()


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
    numero_envio: int = 1,
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
                (codigo_cotizacion, numero_envio, tracking_number, shipment_id, package_id, carrier,
                 servicio, costo, etiqueta_url, orden_detalle_url, tracking_url, usuario)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                codigo_cotizacion = VALUES(codigo_cotizacion),
                numero_envio      = VALUES(numero_envio),
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
            (codigo_cotizacion, numero_envio, tracking, shipment_id, package_id, carrier, servicio,
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


def completar_guias(guias: List[Dict[str, Any]]) -> int:
    """
    Completa tracking, etiqueta y links de guias ya guardadas con lo que trae la
    reconsulta a Skydropx (skydropx_service.extraer_guias). Devuelve cuantas
    filas se actualizaron.

    guardar_envio() no sirve para esto: su UPSERT reescribe codigo_cotizacion con
    el valor recibido. Aqui solo se llenan huecos (COALESCE) y no se toca el
    estatus, la cotizacion ni el costo. Nunca lanza.
    """
    actualizadas = 0
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        for guia in guias:
            valores = (
                _normaliza_tracking(guia.get("tracking_number")),
                guia.get("etiqueta_url") or "",
                guia.get("tracking_url") or "",
                guia.get("orden_detalle_url") or "",
            )
            if not any(valores):
                continue
            if guia.get("package_id"):
                llave, clave = "package_id", guia["package_id"]
            elif guia.get("shipment_id"):
                llave, clave = "shipment_id", guia["shipment_id"]
            else:
                continue
            cursor.execute(
                f"""
                UPDATE skydropx_envios
                SET tracking_number   = COALESCE(tracking_number, %s),
                    etiqueta_url      = COALESCE(NULLIF(%s, ''), etiqueta_url),
                    tracking_url      = COALESCE(NULLIF(%s, ''), tracking_url),
                    orden_detalle_url = COALESCE(NULLIF(%s, ''), orden_detalle_url)
                WHERE {llave} = %s
                """,
                (*valores, clave),
            )
            actualizadas += cursor.rowcount
        conn.commit()
    except mysql.connector.Error as err:
        if conn:
            conn.rollback()
        print(f"Error completando guias Skydropx: {err}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()
    return actualizadas


def _buscar_envio(cursor, package_id, tracking, shipment_id):
    """
    Guia a la que apunta un evento: (columna, fila) con fila = (usuario, codigo_cotizacion,
    estatus), o (None, None). De la llave mas precisa a la mas general: en multipaquete
    varios paquetes comparten shipment_id y ahi un LIMIT 1 podria traer el de otra guia.

    Se busca con SELECT y no con el rowcount de un UPDATE: este conector devuelve filas
    AFECTADAS, asi que un UPDATE que no cambia nada (reintento identico, o evento ignorado
    por la guarda de «cancelada») daria 0 y la guia parecería no existir.
    """
    for columna, valor in (("package_id", package_id), ("tracking_number", tracking),
                           ("shipment_id", shipment_id)):
        if not valor:
            continue
        cursor.execute(
            f"SELECT usuario, codigo_cotizacion, estatus FROM skydropx_envios WHERE {columna} = %s LIMIT 1",
            (valor,)
        )
        fila = cursor.fetchone()
        if fila:
            return columna, fila
    return None, None


def registrar_evento_webhook(evento: Dict[str, Any], payload_crudo: Optional[str] = None) -> Dict[str, Any]:
    """
    Aplica un evento del webhook: actualiza el estatus del envio y lo agrega a la
    bitacora. `evento` es lo que devuelve skydropx_service.extraer_evento_webhook().

    Devuelve {"aplicado", "envio_encontrado", "evento_nuevo", "usuario", "codigo_cotizacion",
    "estatus_protegido", "motivo"} para que el router deje rastro claro. `motivo` dice por
    que no se aplico del todo (sin_llave, sin_estatus, error). `estatus_protegido` es True
    cuando la guia ya estaba cancelada y el evento (no cancelado) no cambio nada.

    Si el envio no existe todavia (webhook antes de que guardemos la guia), se
    crea la fila con lo que trae el evento. El codigo de cotizacion se completa
    despues, cuando guardar_envio() haga su UPSERT sobre el mismo tracking.
    """
    tracking = _normaliza_tracking(evento.get("tracking_number"))
    estatus = (evento.get("estatus") or "").strip() or None
    descripcion = (evento.get("descripcion") or "").strip() or None
    shipment_id = evento.get("shipment_id")
    package_id = evento.get("package_id")
    etiqueta = evento.get("etiqueta_url") or ""
    url_rastreo = evento.get("tracking_url") or ""

    if not tracking and not shipment_id and not package_id:
        print("Webhook Skydropx sin tracking_number, shipment_id ni package_id: no hay como ligarlo.")
        return {
            "aplicado": False, "envio_encontrado": False, "evento_nuevo": False,
            "usuario": None, "codigo_cotizacion": None, "estatus_protegido": False,
            "motivo": "sin_llave",
        }

    # Huella para no duplicar el mismo evento cuando Skydropx reintenta.
    huella = hashlib.sha1(
        f"{tracking or shipment_id or package_id}|{estatus or ''}|{descripcion or ''}".encode("utf-8")
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
            (tracking, shipment_id or package_id, estatus, descripcion, huella, payload_crudo)
        )
        evento_nuevo = cursor.rowcount > 0

        encontrado = False
        if estatus:
            columna, _ = _buscar_envio(cursor, package_id, tracking, shipment_id)
            encontrado = columna is not None

            # OJO con el orden del SET: MySQL asigna de izquierda a derecha y las columnas
            # siguientes ya ven el valor nuevo, asi que la descripcion va ANTES que el estatus
            # para que la guarda de «cancelada» mire el estatus anterior.
            if columna == "package_id":
                # Con V2 la guia nace sin tracking, y en multipaquete varios paquetes comparten
                # shipment_id: package_id es la unica llave que apunta a UNA sola fila.
                cursor.execute(
                    f"""
                    UPDATE skydropx_envios
                    SET estatus_descripcion = {_SQL_DESCRIPCION},
                        estatus = {_SQL_ESTATUS},
                        tracking_number = COALESCE(tracking_number, %s),
                        etiqueta_url = COALESCE(NULLIF(%s, ''), etiqueta_url),
                        tracking_url = COALESCE(NULLIF(%s, ''), tracking_url),
                        shipment_id = COALESCE(shipment_id, %s)
                    WHERE package_id = %s
                    """,
                    (descripcion, estatus, tracking, etiqueta, url_rastreo, shipment_id, package_id)
                )
            elif columna == "tracking_number":
                cursor.execute(
                    f"""
                    UPDATE skydropx_envios
                    SET estatus_descripcion = {_SQL_DESCRIPCION},
                        estatus = {_SQL_ESTATUS},
                        etiqueta_url = COALESCE(NULLIF(%s, ''), etiqueta_url),
                        tracking_url = COALESCE(NULLIF(%s, ''), tracking_url),
                        shipment_id = COALESCE(shipment_id, %s)
                    WHERE tracking_number = %s
                    """,
                    (descripcion, estatus, etiqueta, url_rastreo, shipment_id, tracking)
                )
            elif columna == "shipment_id":
                # Mismos campos que la rama de tracking: un envio cuyo PRIMER evento trae
                # tracking_number Y label_url a la vez (caso comun: «label_created» como unico
                # evento) se encuentra aqui (todavia no tenia tracking guardado), y si no se
                # actualizan aqui tambien se queda con etiqueta_url en NULL para siempre.
                # Un evento de SHIPMENT (sin package_id) actualiza todos sus paquetes.
                cursor.execute(
                    f"""
                    UPDATE skydropx_envios
                    SET estatus_descripcion = {_SQL_DESCRIPCION},
                        estatus = {_SQL_ESTATUS},
                        tracking_number = COALESCE(tracking_number, %s),
                        etiqueta_url = COALESCE(NULLIF(%s, ''), etiqueta_url),
                        tracking_url = COALESCE(NULLIF(%s, ''), tracking_url)
                    WHERE shipment_id = %s
                    """,
                    (descripcion, estatus, tracking, etiqueta, url_rastreo, shipment_id)
                )
            else:
                # Webhook antes que la guia: se crea la fila para no perder el estatus.
                cursor.execute(
                    """
                    INSERT INTO skydropx_envios
                        (codigo_cotizacion, tracking_number, shipment_id, package_id, estatus,
                         estatus_descripcion, etiqueta_url, tracking_url)
                    VALUES (NULL, %s, %s, %s, %s, %s, NULLIF(%s, ''), NULLIF(%s, ''))
                    ON DUPLICATE KEY UPDATE
                        estatus_descripcion = IF(LOWER(estatus) IN ('canceled', 'cancelled'),
                                                 estatus_descripcion,
                                                 COALESCE(VALUES(estatus_descripcion), estatus_descripcion)),
                        estatus = IF(LOWER(estatus) IN ('canceled', 'cancelled'), estatus, VALUES(estatus))
                    """,
                    (tracking, shipment_id, package_id, estatus, descripcion, etiqueta, url_rastreo)
                )

        # Dueno del envio, para poder avisarle por notificaciones_service, y estatus que
        # quedo guardado (la fila pudo localizarse o crearse por cualquiera de las llaves).
        usuario_dueno = None
        codigo_cotizacion = None
        estatus_protegido = False
        if estatus:
            _, fila = _buscar_envio(cursor, package_id, tracking, shipment_id)
            if fila:
                usuario_dueno, codigo_cotizacion = fila[0], fila[1]
                # La guarda del UPDATE dejo la cancelacion intacta: este evento (viejo o
                # tardio) no cambio nada, asi que tampoco debe avisarse como novedad.
                estatus_protegido = es_cancelado(fila[2]) and not es_cancelado(estatus)

        conn.commit()
        return {
            "aplicado": True,
            "envio_encontrado": encontrado,
            "evento_nuevo": evento_nuevo,
            "usuario": usuario_dueno,
            "codigo_cotizacion": codigo_cotizacion,
            "estatus_protegido": estatus_protegido,
            "motivo": None if estatus else "sin_estatus",
        }
    except mysql.connector.Error as err:
        if conn:
            conn.rollback()
        print(f"Error aplicando webhook Skydropx: {err}")
        return {
            "aplicado": False, "envio_encontrado": False, "evento_nuevo": False,
            "usuario": None, "codigo_cotizacion": None, "estatus_protegido": False,
            "motivo": "error",
        }
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


# Dias que se conserva la bitacora cruda del webhook.
DIAS_RETENCION_WEBHOOK = 90


def guardar_webhook_recibido(datos: Dict[str, Any], payload_crudo: Optional[str], resultado: str) -> None:
    """
    Guarda TAL CUAL lo que llego al webhook (ya autenticado por firma), se haya podido ligar a una
    guia o no, junto con lo que se hizo (`resultado`). Es la unica forma de ver despues un evento
    que no se aplico: skydropx_envio_eventos solo recibe los que se pudieron ligar.
    Nunca lanza: guardar el rastro no debe tumbar la respuesta al webhook.
    """
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO skydropx_webhook_recibidos
                (tipo, estatus, tracking_number, shipment_id, package_id, resultado, payload)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (datos.get("tipo"), datos.get("estatus"), _normaliza_tracking(datos.get("tracking_number")),
             datos.get("shipment_id"), datos.get("package_id"), resultado, payload_crudo)
        )
        cursor.execute(
            "DELETE FROM skydropx_webhook_recibidos WHERE recibido_en < NOW() - INTERVAL %s DAY",
            (DIAS_RETENCION_WEBHOOK,)
        )
        conn.commit()
    except Exception as err:
        print(f"No se pudo guardar el webhook Skydropx recibido: {err}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def listar_webhooks_recibidos(limite: int = 50, estatus: Optional[str] = None) -> List[Dict[str, Any]]:
    """Ultimos eventos crudos recibidos (mas reciente primero), opcionalmente de un estatus."""
    limite = max(1, min(int(limite or 50), 500))
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        if estatus:
            cursor.execute(
                "SELECT * FROM skydropx_webhook_recibidos WHERE LOWER(estatus) = %s ORDER BY id DESC LIMIT %s",
                (estatus.strip().lower(), limite)
            )
        else:
            cursor.execute("SELECT * FROM skydropx_webhook_recibidos ORDER BY id DESC LIMIT %s", (limite,))
        filas = cursor.fetchall()
    finally:
        cursor.close()
        conn.close()
    for fila in filas:
        fila["recibido_en"] = str(fila["recibido_en"])
    return filas


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
