# Promociones MeLi para la página del panel (permiso general).
from fastapi import APIRouter, Depends, HTTPException

from permisos import usuario_autenticado

router = APIRouter(tags=["/meli-promos"])


@router.get("/meli/promociones")
async def promociones(forzar: bool = False, usuario: str = Depends(usuario_autenticado)):
    """Ofertas de MeLi {message, ofertas_meli}. `forzar=1` salta el caché de 10 min."""
    from jobs import meli_promos
    try:
        return await meli_promos.obtener_promos(forzar=forzar)
    except meli_promos.MeliPromosError as err:
        raise HTTPException(status_code=502, detail=f"No se pudieron consultar promociones: {err}")
