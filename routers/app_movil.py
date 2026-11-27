# Descarga de la app Android (APK privado en S3). Cualquier sesión (decidido); el usuario de la
# bitácora sale del token. La lógica de S3 vive en servicios/apk_descarga.py.
import logging

from fastapi import APIRouter, Depends, HTTPException

from servicios import apk_descarga, mov_reg
from servicios.permisos import usuario_autenticado

router = APIRouter(tags=["/app"], responses={404: {"Mensaje": "No encontrado"}})

logger = logging.getLogger("zeutica.apk")

SECCION = "App móvil"


@router.get("/app/download-url")
def url_descarga_apk(usuario: str = Depends(usuario_autenticado)):
    try:
        datos = apk_descarga.generar_url_descarga()
    except apk_descarga.ApkNoConfigurado:
        raise HTTPException(status_code=503, detail="La descarga de la app no está configurada en el servidor")
    except apk_descarga.ApkNoEncontrado:
        raise HTTPException(status_code=404, detail="El instalador de la app no está disponible")
    except apk_descarga.ApkError:
        raise HTTPException(status_code=500, detail="No se pudo generar el enlace de descarga")

    # La bitácora no debe impedir la descarga si la BD falla.
    try:
        mov_reg.registrar_movimiento(usuario, f"Generó enlace de descarga del APK ({datos['filename']})", SECCION)
    except Exception as err:
        logger.error("APK: no se pudo registrar en mov_reg: %s", err)
    return datos
