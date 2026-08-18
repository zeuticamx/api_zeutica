"""
Notificaciones en tiempo real (WebSocket) del modulo Sofia.

Cuando un agente de IA (via n8n) o un humano escala/toma una conversacion de
WhatsApp, n8n pega en POST /notificaciones/escalacion y esto se reenvia por
WebSocket a todos los clientes conectados en /ws/notificaciones.

Es efimero: no se guarda en la tabla MySQL `notificaciones` (esa la maneja
routers/notificaciones.py con polling). Si nadie esta conectado, el aviso se
descarta.

Sin BD en el flujo de notificacion, a proposito: el payload de n8n ya trae todo
(session_id, wa_id, motivo, assigned_agent_id), asi que ni el POST ni el
ConnectionManager leen o escriben en ninguna base. El unico uso de MySQL aqui es
validar el token de usuario al abrir el WebSocket (una query por conexion, no
por mensaje) — es el portero, no dato de la funcionalidad.

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
from typing import List, Optional

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
    """

    def __init__(self):
        self.conexiones_activas: List[WebSocket] = []
        # Candado para que un disconnect no mutile la lista mientras otro
        # request la esta recorriendo en un broadcast.
        self._lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        """Acepta el handshake y registra la conexion."""
        await websocket.accept()
        async with self._lock:
            self.conexiones_activas.append(websocket)

    async def disconnect(self, websocket: WebSocket):
        """Saca la conexion de la lista. Idempotente: no truena si ya no esta."""
        async with self._lock:
            if websocket in self.conexiones_activas:
                self.conexiones_activas.remove(websocket)

    async def broadcast(self, mensaje: dict) -> int:
        """
        Manda el mensaje a todas las conexiones y devuelve cuantas lo recibieron.
        Las que fallan al enviar (cliente que ya se murio sin avisar) se
        eliminan de la lista para no dejar conexiones zombie.
        """
        async with self._lock:
            destinatarios = list(self.conexiones_activas)

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


def token_de_usuario_valido(token: str) -> bool:
    """
    Checa el token contra usuarios.token, igual que obtener_usuario_actual en
    main.py. Se replica aqui porque el WebSocket recibe el token por query
    param (el browser no puede mandar header Authorization en un WS) y para no
    importar main.py desde un router (import circular).
    """
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT nombre_usuario FROM usuarios WHERE token = %s", (token,))
        return cursor.fetchone() is not None
    except mysql.connector.Error as err:
        print(f"Error DB token WS: {err}")
        return False
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
    """
    if not token or not token_de_usuario_valido(token):
        # 1008 = policy violation. Se cierra antes de aceptar el canal.
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await manager.connect(websocket)
    try:
        while True:
            # No procesamos nada de lo que manda el cliente; solo nos quedamos
            # esperando. Este await es lo que detecta la desconexion.
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
