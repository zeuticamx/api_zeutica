"""
Notificaciones persistidas del empleado (tabla MySQL `notificaciones`).

El UI ya NO hace polling contra GET /empleados/{id}/notificaciones: ahora recibe
el snapshot inicial y las nuevas por el WebSocket /ws/notificaciones
(routers/sofi_notificaciones.py). Este endpoint se conserva como respaldo para
cuando el WebSocket no logra conectar (proxy, red) y para consultas manuales.

El SQL y la serializacion viven en notificaciones_service.py, compartido con el
WebSocket.
"""
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import notificaciones_service

router = APIRouter(tags=["/creditos"], responses={404: {"Mensaje": "No encontrado"}})


class Notificacion(BaseModel):
    id: int
    titulo: str
    mensaje: str
    tipo: str
    leido: bool
    fecha_creacion: datetime
    fecha_lectura: Optional[datetime] = None


@router.get("/empleados/{empleado_id}/notificaciones", response_model=List[Notificacion])
async def obtener_notificaciones(empleado_id: str):
    """
    Historial de notificaciones sin leer de un empleado, de la mas reciente a la
    mas antigua. Respaldo del WebSocket; ya no se llama en bucle.
    """
    # Devuelve [] y no null si no hay nada, para que el map() del frontend no truene.
    return notificaciones_service.no_leidas(empleado_id)


@router.post("/notificaciones/marcar-leida/{notificacion_id}")
async def marcar_notificacion_leida(notificacion_id: int):
    """
    Marca una notificacion como leida y avisa por WebSocket a las demas pestanas
    del mismo empleado para que bajen el contador sin recargar.
    """
    notificacion = notificaciones_service.obtener(notificacion_id)
    if not notificacion:
        raise HTTPException(status_code=404, detail="Notificación no encontrada.")

    if not notificaciones_service.marcar_leida(notificacion_id):
        raise HTTPException(status_code=500, detail="Error al actualizar la notificación.")

    await notificaciones_service.avisar_leida(notificacion["empleado_id"], notificacion_id)
    return {"mensaje": "Notificación marcada como leída."}
