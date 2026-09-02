"""
Servicio de notificaciones persistidas (tabla MySQL `notificaciones`).

Concentra aqui el SQL y la serializacion para que lo puedan usar tanto el router
REST (routers/notificaciones.py) como el WebSocket (routers/sofi_notificaciones.py)
sin importarse entre ellos (import circular).

El push por WebSocket vive en `crear_y_notificar()`: primero se guarda en MySQL
(fuente de verdad) y despues se intenta empujar al UI. Si el empleado no tiene
ninguna pestana abierta el push devuelve 0 y no pasa nada: la vera en el
snapshot inicial la proxima vez que se conecte.
"""
import os
from datetime import datetime
from typing import List, Optional

import mysql.connector
from dotenv import load_dotenv

load_dotenv()

# Tope del snapshot inicial que se manda al conectar el WebSocket. Mismo criterio
# que tenia el endpoint de polling: no arrastrar miles de filas a memoria.
LIMITE_SNAPSHOT = 50

CAMPOS = "id, empleado_id, titulo, mensaje, tipo, leido, fecha_creacion, fecha_lectura"


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )


def serializar(fila: dict) -> dict:
    """
    Deja la fila lista para send_json(): datetime -> ISO string, leido -> bool.
    `send_json` usa json.dumps, que no sabe serializar datetime (a diferencia de
    la respuesta REST, donde Pydantic lo hacia por nosotros).
    """
    salida = dict(fila)
    salida["leido"] = bool(salida.get("leido"))
    for campo in ("fecha_creacion", "fecha_lectura"):
        valor = salida.get(campo)
        if isinstance(valor, datetime):
            salida[campo] = valor.isoformat()
    return salida


def no_leidas(empleado_id: int, limite: int = LIMITE_SNAPSHOT) -> List[dict]:
    """Notificaciones sin leer de un empleado, de la mas reciente a la mas vieja."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            f"""
            SELECT {CAMPOS}
            FROM notificaciones
            WHERE empleado_id = %s AND leido = 0
            ORDER BY fecha_creacion DESC
            LIMIT %s
            """,
            (empleado_id, limite)
        )
        return [serializar(fila) for fila in cursor.fetchall()]
    except mysql.connector.Error as err:
        print(f"Error interno DB (no_leidas): {err}")
        return []
    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()


def obtener(notificacion_id: int) -> Optional[dict]:
    """Una notificacion por id. None si no existe."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            f"SELECT {CAMPOS} FROM notificaciones WHERE id = %s",
            (notificacion_id,)
        )
        fila = cursor.fetchone()
        return serializar(fila) if fila else None
    except mysql.connector.Error as err:
        print(f"Error interno DB (obtener): {err}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()


def marcar_leida(notificacion_id: int) -> bool:
    """Pone leido = 1 y la fecha de lectura. False si el UPDATE fallo."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE notificaciones SET leido = 1, fecha_lectura = NOW() WHERE id = %s",
            (notificacion_id,)
        )
        conn.commit()
        return True
    except mysql.connector.Error as err:
        print(f"Error interno DB (marcar_leida): {err}")
        return False
    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()


def id_de_usuario(nombre_usuario: str) -> Optional[int]:
    """
    `usuarios.id` a partir del nombre de login. Es el mismo id que usan
    `notificaciones.empleado_id` y el indice del manager WebSocket, asi que sirve
    para dirigir una notificacion cuando el router solo tiene el nombre del
    usuario que hizo el movimiento. None si el usuario no existe.
    """
    if not nombre_usuario:
        return None
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id FROM usuarios WHERE nombre_usuario = %s",
            (nombre_usuario,)
        )
        fila = cursor.fetchone()
        return fila["id"] if fila else None
    except mysql.connector.Error as err:
        print(f"Error interno DB (id_de_usuario): {err}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()


def crear(empleado_id: int, titulo: str, mensaje: str, tipo: str) -> Optional[dict]:
    """
    Inserta la notificacion y devuelve la fila ya serializada (con su id y
    fecha_creacion reales). None si el INSERT fallo.
    """
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO notificaciones (empleado_id, titulo, mensaje, tipo) VALUES (%s, %s, %s, %s)",
            (empleado_id, titulo, mensaje, tipo)
        )
        conn.commit()
        nuevo_id = cursor.lastrowid
    except mysql.connector.Error as err:
        print(f"Error interno DB (crear notificacion): {err}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()

    return obtener(nuevo_id)


async def crear_y_notificar(empleado_id: int, titulo: str, mensaje: str, tipo: str) -> Optional[dict]:
    """
    Guarda la notificacion en MySQL y la empuja por WebSocket al empleado dueno.

    El import del manager va adentro de la funcion a proposito: routers/ importa
    este modulo, asi que importarlo arriba armaria un ciclo al arrancar.
    """
    notificacion = crear(empleado_id, titulo, mensaje, tipo)
    if not notificacion:
        return None

    from routers.sofi_notificaciones import manager
    await manager.enviar_a(empleado_id, {"tipo": "notificacion", "notificacion": notificacion})
    return notificacion


async def avisar_leida(empleado_id: int, notificacion_id: int) -> None:
    """
    Avisa a las demas pestanas del mismo usuario que una notificacion ya se leyo,
    para que bajen el contador sin recargar.
    """
    from routers.sofi_notificaciones import manager
    await manager.enviar_a(empleado_id, {"tipo": "leida", "id": notificacion_id})
