"""
Notificaciones en tiempo real (WebSocket) del modulo Sofia.

Cuando un agente de IA (via n8n) o un humano escala/toma una conversacion de
WhatsApp, n8n pega en POST /notificaciones/escalacion y esto se reenvia por
WebSocket a todos los clientes conectados en /ws/notificaciones.

Este mismo canal transporta ahora dos cosas:

1. `tipo: "escalacion"` — efimero, viene de n8n. Si nadie esta conectado se
   descarta; no toca la tabla MySQL `notificaciones`.
2. `tipo: "snapshot"` / `tipo: "notificacion"` / `tipo: "leida"` — las
   notificaciones persistidas de la tabla `notificaciones`. Reemplazan al polling
   que hacia el UI cada 60s contra
   GET /empleados/{id}/notificaciones (ese endpoint sigue vivo como respaldo,
   ver routers/notificaciones.py).

El manager registra cada conexion por `empleado_id` (= usuarios.id, el mismo
`id_usuario` que devuelve /login), asi `enviar_a()` entrega solo al dueno de la
notificacion en vez de gritarle a todos.

MySQL se usa aqui solo al abrir el WebSocket: validar el token y mandar el
snapshot inicial de no leidas (dos queries por conexion, no por mensaje).

El estado real de las conversaciones (`conversation_status`, `human_active`) vive
en la Postgres separada del workflow de n8n, no en el MySQL principal. Si algun
dia la UI necesita pedir un snapshot al conectarse, esa consulta va contra el
pool `app.state.db_pool` reusando el patron de get_db_postgres() en
routers/sofi_conversaciones.py. Hoy no se consulta.

Ojo: el manager vive en memoria del proceso. Sirve porque en produccion
corremos un solo worker de uvicorn. Si algun dia se levantan varios workers,
hay que meter Redis pub/sub como capa intermedia.
"""
import asyncio
import os
import secrets

import mysql.connector
from dotenv import load_dotenv
from fastapi import (APIRouter, Depends, Header, HTTPException, Query,
                     WebSocket, WebSocketDisconnect, status)
from pydantic import BaseModel
from typing import Dict, List, Optional

import notificaciones_service

router = APIRouter(tags=["sofi-notificaciones"], responses={404: {"Mensaje": "No encontrado"}})
load_dotenv()

# API key para llamadas server-to-server (n8n). Sin esto el POST responde 500.
N8N_API_KEY = os.getenv("N8N_API_KEY")


# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )


class EscalacionPayload(BaseModel):
    """Lo que manda n8n cuando una conversacion se escala."""
    session_id: str
    wa_id: str
    motivo: Optional[str] = None
    assigned_agent_id: Optional[str] = None


class ConnectionManager:
    """
    Lleva el registro de las conexiones WebSocket vivas de este proceso.

    Dos indices sobre las mismas conexiones:
    - `conexiones_activas`: todas, para el broadcast de escalaciones.
    - `por_empleado`: empleado_id -> conexiones de ese usuario (puede tener
      varias pestanas abiertas), para las notificaciones dirigidas.
    """

    def __init__(self):
        self.conexiones_activas: List[WebSocket] = []
        self.por_empleado: Dict[int, List[WebSocket]] = {}
        # Candado para que un disconnect no mutile la lista mientras otro
        # request la esta recorriendo en un broadcast.
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket, empleado_id: Optional[int] = None):
        """Acepta el handshake y registra la conexion."""
        await websocket.accept()
        async with self._lock:
            self.conexiones_activas.append(websocket)
            if empleado_id is not None:
                self.por_empleado.setdefault(empleado_id, []).append(websocket)

    async def disconnect(self, websocket: WebSocket):
        """Saca la conexion de ambos indices. Idempotente: no truena si ya no esta."""
        async with self._lock:
            if websocket in self.conexiones_activas:
                self.conexiones_activas.remove(websocket)
            for empleado_id, conexiones in list(self.por_empleado.items()):
                if websocket in conexiones:
                    conexiones.remove(websocket)
                if not conexiones:
                    del self.por_empleado[empleado_id]

    async def _enviar(self, destinatarios: List[WebSocket], mensaje: dict) -> int:
        """
        Manda el mensaje a las conexiones dadas y devuelve cuantas lo recibieron.
        Las que fallan al enviar (cliente que ya se murio sin avisar) se
        eliminan de los indices para no dejar conexiones zombie.
        """
        muertas = []
        enviados = 0
        for conexion in destinatarios:
            try:
                await conexion.send_json(mensaje)
                enviados += 1
            except Exception as e:
                print(f"⚠️ Conexion WS muerta, se descarta: {e}")
                muertas.append(conexion)

        for conexion in muertas:
            await self.disconnect(conexion)

        return enviados

    async def broadcast(self, mensaje: dict) -> int:
        """Manda el mensaje a todas las conexiones vivas del proceso."""
        async with self._lock:
            destinatarios = list(self.conexiones_activas)
        return await self._enviar(destinatarios, mensaje)

    async def enviar_a(self, empleado_id: int, mensaje: dict) -> int:
        """
        Manda el mensaje solo a las conexiones de un empleado.
        Si ese empleado no tiene ninguna pestana abierta devuelve 0 y no pasa
        nada: la notificacion ya quedo en MySQL y la vera en el snapshot al
        conectarse.
        """
        async with self._lock:
            destinatarios = list(self.por_empleado.get(empleado_id, []))
        return await self._enviar(destinatarios, mensaje)


# Instancia unica compartida por el WebSocket y el POST de n8n.
manager = ConnectionManager()


def verificar_api_key(x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    """
    Valida la API key de llamadas server-to-server (n8n).
    No usa la sesion de usuario porque n8n no tiene login.
    """
    if not N8N_API_KEY:
        raise HTTPException(
            status_code=500,
            detail="Falta N8N_API_KEY en el .env"
        )
    if not x_api_key or not secrets.compare_digest(x_api_key, N8N_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key invalida o ausente"
        )


def empleado_id_de_token(token: str) -> Optional[int]:
    """
    Resuelve el token contra usuarios.token y devuelve `usuarios.id` (el mismo
    `id_usuario` que /login regresa al UI y que se guarda en
    notificaciones.empleado_id). None si el token no existe.

    Se replica aqui, en vez de usar obtener_usuario_actual de main.py, porque el
    WebSocket recibe el token por query param (el browser no puede mandar header
    Authorization en un WS) y para no importar main.py desde un router (import
    circular).
    """
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id FROM usuarios WHERE token = %s", (token,))
        fila = cursor.fetchone()
        return fila["id"] if fila else None
    except mysql.connector.Error as err:
        print(f"Error DB token WS: {err}")
        return None
    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()


@router.websocket("/ws/notificaciones")
async def ws_notificaciones(websocket: WebSocket, token: Optional[str] = Query(None)):
    """
    Canal de notificaciones en tiempo real.

    Conectar con: ws://host:8000/ws/notificaciones?token=<access_token del login>
    (en produccion, detras del proxy: /zeutica/ws/notificaciones)

    Mensajes que salen de aqui hacia el UI:
    - {"tipo": "snapshot", "notificaciones": [...]}  al conectar (no leidas)
    - {"tipo": "notificacion", "notificacion": {...}} cuando se crea una nueva
    - {"tipo": "leida", "id": N}                     al marcarse leida
    - {"tipo": "escalacion", ...}                    aviso efimero de n8n
    """
    empleado_id = empleado_id_de_token(token) if token else None
    if empleado_id is None:
        # 1008 = policy violation. Se cierra antes de aceptar el canal.
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(websocket, empleado_id)
    try:
        # Snapshot inicial: esto es lo que antes traia el primer GET del polling.
        # Va por el mismo canal para que el UI tenga un solo camino de datos.
        await websocket.send_json({
            "tipo": "snapshot",
            "notificaciones": notificaciones_service.no_leidas(empleado_id),
        })

        while True:
            # No procesamos nada de lo que manda el cliente (solo manda "ping"
            # para mantener viva la conexion detras del proxy). Este await es lo
            # que detecta la desconexion.
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass  # El cliente cerro, es normal.
    except Exception as e:
        print(f"⚠️ Error en WS notificaciones: {e}")
    finally:
        await manager.disconnect(websocket)


@router.post("/notificaciones/escalacion", dependencies=[Depends(verificar_api_key)])
async def notificar_escalacion(payload: EscalacionPayload):
    """
    Recibe la escalacion desde n8n y la reenvia a todos los clientes WebSocket.
    Requiere header X-API-Key (no sesion de usuario).
    """
    mensaje = {"tipo": "escalacion", **payload.model_dump()}
    enviados = await manager.broadcast(mensaje)
    return {
        "enviado": True,
        "clientes_notificados": enviados
    }
