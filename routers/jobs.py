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


class MeliStockRunIn(BaseModel):
    dry_run: Optional[bool] = Field(default=None)
    motivo: str = Field(default="manual", max_length=40)

@router.post("/jobs/meli-stock/run")
async def meli_stock_run(datos: MeliStockRunIn, usuario: str = Depends(usuario_autenticado)):
    """Publica stock a MeLi en segundo plano (202). Permiso general."""
    from jobs import meli_stock
    return _lanzar(meli_stock, datos.dry_run, f"{datos.motivo} por {usuario}", "meli-stock")


@router.get("/jobs/meli-stock/status")
async def meli_stock_status(usuario: str = Depends(usuario_autenticado)):
    """Último run + configuración (permiso general)."""
    from jobs import meli_stock
    return {
        "ultimo": meli_stock.LAST_RUN,
        "config": {
            "enabled": os.getenv("MELI_STOCK_ENABLED", "1") == "1",
            "dry_run_default": os.getenv("MELI_STOCK_DRY_RUN", "0") == "1",
            "hora": os.getenv("MELI_STOCK_HORA", "20:00"),
        },
    }


class CleanestRunIn(BaseModel):
    motivo: str = Field(default="manual", max_length=40)


@router.post("/jobs/cleanest/run")
async def cleanest_run(datos: CleanestRunIn, usuario: str = Depends(usuario_autenticado)):
    """Recordatorios Cleanest en segundo plano (202). Permiso general."""
    from jobs import cleanest_recordatorios
    return _lanzar(cleanest_recordatorios, None, f"{datos.motivo} por {usuario}", "cleanest")


@router.get("/jobs/cleanest/status")
async def cleanest_status(usuario: str = Depends(usuario_autenticado)):
    """Último run + configuración (permiso general)."""
    from jobs import cleanest_recordatorios
    return {
        "ultimo": cleanest_recordatorios.LAST_RUN,
        "config": {
            "enabled": os.getenv("CLEANEST_JOB_ENABLED", "1") == "1",
            "hora": os.getenv("CLEANEST_JOB_HORA", "11:00"),
        },
    }


class MeliFullRunIn(BaseModel):
    dry_run: Optional[bool] = Field(default=None)
    motivo: str = Field(default="manual", max_length=40)


@router.post("/jobs/meli-full/run")
async def meli_full_run(datos: MeliFullRunIn, usuario: str = Depends(usuario_autenticado)):
    """Alerta stock Full en segundo plano (202). Permiso general."""
    from jobs import meli_full_stock
    return _lanzar(meli_full_stock, datos.dry_run, f"{datos.motivo} por {usuario}", "meli-full")


@router.get("/jobs/meli-full/status")
async def meli_full_status(usuario: str = Depends(usuario_autenticado)):
    """Último run + configuración (permiso general)."""
    from jobs import meli_full_stock
    return {
        "ultimo": meli_full_stock.LAST_RUN,
        "config": {
            "enabled": os.getenv("MELI_FULL_ENABLED", "1") == "1",
            "hora": os.getenv("MELI_FULL_HORA", "10:30"),
        },
    }


class CotizRunIn(BaseModel):
    motivo: str = Field(default="manual", max_length=40)


@router.post("/jobs/cotizaciones/run")
async def cotizaciones_run(datos: CotizRunIn, usuario: str = Depends(usuario_autenticado)):
    """Vencimientos de cotizaciones en segundo plano (202). Permiso general."""
    from jobs import cotizaciones_vencimiento
    return _lanzar(cotizaciones_vencimiento, None, f"{datos.motivo} por {usuario}", "cotizaciones")


@router.get("/jobs/cotizaciones/status")
async def cotizaciones_status(usuario: str = Depends(usuario_autenticado)):
    """Último run + configuración (permiso general)."""
    from jobs import cotizaciones_vencimiento
    return {
        "ultimo": cotizaciones_vencimiento.LAST_RUN,
        "config": {
            "enabled": os.getenv("COTIZ_JOB_ENABLED", "1") == "1",
            "hora": os.getenv("COTIZ_JOB_HORA", "09:30"),
        },
    }


class CotizVenderRunIn(BaseModel):
    motivo: str = Field(default="manual", max_length=40)


@router.post("/jobs/cotizaciones-vender/run")
async def cotizaciones_vender_run(datos: CotizVenderRunIn, usuario: str = Depends(usuario_autenticado)):
    """Cotizaciones por vender en segundo plano (202). Permiso general."""
    from jobs import cotizaciones_por_vender
    return _lanzar(cotizaciones_por_vender, None, f"{datos.motivo} por {usuario}", "cotizaciones-vender")


@router.get("/jobs/cotizaciones-vender/status")
async def cotizaciones_vender_status(usuario: str = Depends(usuario_autenticado)):
    """Último run + configuración (permiso general)."""
    from jobs import cotizaciones_por_vender
    return {
        "ultimo": cotizaciones_por_vender.LAST_RUN,
        "config": {
            "enabled": os.getenv("COTIZV_JOB_ENABLED", "1") == "1",
            "hora": os.getenv("COTIZV_JOB_HORA", "13:00"),
        },
    }
