# Endpoints de los jobs de marketplaces. Permiso general: cualquier usuario
# autenticado puede traer ventas (el usuario sale del token).
#
# Los POST responden 202 de inmediato y el job corre en segundo plano
# (el panel no espera minutos con la petición abierta): el resultado queda en
# LAST_RUN y el panel lo consulta con GET .../status. Si ya hay uno en curso,
# el segundo recibe 409 en vez de duplicar el trabajo.
import asyncio
import os
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Optional
from zoneinfo import ZoneInfo

from permisos import usuario_autenticado

router = APIRouter(tags=["/jobs"], responses={404: {"Mensaje": "No encontrado"}})

_CDMX = ZoneInfo("America/Mexico_City")


def _lanzar(modulo, dry_run, motivo: str, nombre: str):
    """Marca running y dispara el job en background. 409 si ya hay uno en curso."""
    if modulo.LAST_RUN.get("estado") == "running":
        raise HTTPException(status_code=409, detail=f"Job {nombre} ya en curso, espera a que termine")
    modulo.LAST_RUN.clear()
    modulo.LAST_RUN.update({"estado": "running", "motivo": motivo,
                            "inicio": datetime.now(_CDMX).isoformat()})

    async def _fondo():
        try:
            await modulo.run_job(dry_run=dry_run, motivo=motivo)
        except Exception as err:
            print(f"Job {nombre} falló en background: {err}")
            modulo.LAST_RUN.clear()
            modulo.LAST_RUN.update({"estado": "error", "error": str(err),
                                    "fin": datetime.now(_CDMX).isoformat()})

    asyncio.create_task(_fondo())
    return JSONResponse(status_code=202, content={"estado": "running", "job": nombre,
                                                  "detalle": "Procesando en segundo plano, consulta el status"})


class AmazonRunIn(BaseModel):
    dry_run: Optional[bool] = Field(default=None)
    motivo: str = Field(default="manual", max_length=40)


@router.post("/jobs/amazon/run")
async def amazon_run(datos: AmazonRunIn, usuario: str = Depends(usuario_autenticado)):
    """Trae ventas de Amazon en segundo plano (202). Permiso general."""
    from jobs import amazon_ventas
    return _lanzar(amazon_ventas, datos.dry_run, f"{datos.motivo} por {usuario}", "amazon")


@router.get("/jobs/amazon/status")
async def amazon_status(usuario: str = Depends(usuario_autenticado)):
    """Último run + configuración (permiso general)."""
    from jobs import amazon_ventas
    return {
        "ultimo": amazon_ventas.LAST_RUN,
        "config": {
            "enabled": os.getenv("AMAZON_JOB_ENABLED", "1") == "1",
            "dry_run_default": os.getenv("AMAZON_JOB_DRY_RUN", "0") == "1",
            "hora": os.getenv("AMAZON_JOB_HORA", "12:22"),
            "marketplace": os.getenv("AMZ_MARKETPLACE_ID", "A1AM78C64UM0Y8"),
        },
    }


class MeliRunIn(BaseModel):
    dry_run: Optional[bool] = Field(default=None)
    motivo: str = Field(default="manual", max_length=40)


@router.post("/jobs/meli/run")
async def meli_run(datos: MeliRunIn, usuario: str = Depends(usuario_autenticado)):
    """Trae ventas de MeLi en segundo plano (202). Permiso general."""
    from jobs import meli_ventas
    return _lanzar(meli_ventas, datos.dry_run, f"{datos.motivo} por {usuario}", "meli")


@router.get("/jobs/meli/status")
async def meli_status(usuario: str = Depends(usuario_autenticado)):
    """Último run + configuración (permiso general)."""
    from jobs import meli_ventas
    return {
        "ultimo": meli_ventas.LAST_RUN,
        "config": {
            "enabled": os.getenv("MELI_JOB_ENABLED", "1") == "1",
            "dry_run_default": os.getenv("MELI_JOB_DRY_RUN", "0") == "1",
            "hora": os.getenv("MELI_JOB_HORA", "12:05"),
        },
    }
