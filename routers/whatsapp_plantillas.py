# Endpoints del modulo "Enviar plantillas" (WhatsApp Cloud API de Meta).
# La logica de Graph vive en whatsapp_service.py; aqui solo va el contrato HTTP.
from typing import List, Optional

import mov_reg
import whatsapp_service
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from whatsapp_service import WhatsAppServiceError

router = APIRouter(tags=["whatsapp-plantillas"], responses={404: {"Mensaje": "No encontrado"}})


class EnvioPlantilla(BaseModel):
    telefono: str
    plantilla: str
    idioma: str
    variables_encabezado: List[str] = Field(default_factory=list)
    variables_cuerpo: List[str] = Field(default_factory=list)
    # Solo para plantillas con encabezado de medios (IMAGE / VIDEO / DOCUMENT).
    url_encabezado: Optional[str] = None
    tipo_encabezado: Optional[str] = None
    nombre_archivo: Optional[str] = None
    usuario: Optional[str] = None
    # Referencia opcional del destinatario, solo para la bitacora de movimientos.
    destinatario: Optional[str] = None


@router.get("/whatsapp/plantillas")
async def obtener_plantillas(refrescar: bool = Query(False, description="Ignora la cache de 5 min")):
    """
    Plantillas aprobadas en la cuenta de WhatsApp Business, tal como estan en Meta.
    Cada una trae sus `componentes` para que el panel arme la vista previa.
    """
    try:
        plantillas = await whatsapp_service.listar_plantillas(forzar=refrescar)
    except WhatsAppServiceError as err:
        raise HTTPException(status_code=err.status, detail=err.detalle)

    return {
        "plantillas": plantillas,
        "configuracion": whatsapp_service.configuracion_lista(),
    }


@router.get("/whatsapp/configuracion")
async def obtener_configuracion():
    """Que credenciales de Meta estan cargadas. No expone ningun valor, solo si existe."""
    return whatsapp_service.configuracion_lista()


@router.post("/whatsapp/enviar-plantilla")
async def enviar_plantilla(datos: EnvioPlantilla):
    """
    Envia una plantilla aprobada a un numero. Devuelve el message_id de Meta.

    Meta cobra por conversacion iniciada por la empresa: cada llamada exitosa aqui
    es un mensaje que ya salio y no se puede cancelar.
    """
    try:
        resultado = await whatsapp_service.enviar_plantilla(
            telefono=datos.telefono,
            plantilla=datos.plantilla,
            idioma=datos.idioma,
            variables_encabezado=datos.variables_encabezado,
            variables_cuerpo=datos.variables_cuerpo,
            url_encabezado=datos.url_encabezado,
            tipo_encabezado=datos.tipo_encabezado,
            nombre_archivo=datos.nombre_archivo,
        )
    except WhatsAppServiceError as err:
        raise HTTPException(status_code=err.status, detail=err.detalle)

    # Bitacora. Si el registro falla no se tumba la respuesta: el mensaje ya se envio
    # y ocultarlo con un 500 haria que el usuario lo intentara de nuevo (y cobrara doble).
    try:
        referencia = datos.destinatario or resultado["destino"]
        mov_reg.registrar_movimiento(
            datos.usuario or "sistema",
            f"Envio plantilla '{datos.plantilla}' ({datos.idioma}) a {referencia}",
            "Enviar plantillas",
        )
    except Exception as err:
        print(f"Error al registrar movimiento de plantilla WhatsApp: {err}")

    return {"status": "success", **resultado}
