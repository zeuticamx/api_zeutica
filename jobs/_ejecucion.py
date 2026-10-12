# Ejecución con lock por job: endpoints, scheduler y webhook comparten el
# mismo candado, así dos corridas nunca se traslapan (origen de duplicados).
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

_CDMX = ZoneInfo("America/Mexico_City")
_LOCKS: dict = {}


def _lock(nombre: str) -> asyncio.Lock:
    try:
        return _LOCKS[nombre]
    except KeyError:
        lock = asyncio.Lock()
        _LOCKS[nombre] = lock
        return lock


def en_curso(modulo) -> bool:
    """True si el job tiene una corrida en curso (para responder 409 rápido)."""
    lock = _LOCKS.get(modulo.__name__)
    return bool(lock and lock.locked())


async def ejecutar(modulo, *, dry_run=None, motivo: str = "manual", telegram: bool = True) -> dict:
    """Corre run_job si está libre; si no, devuelve ocupado sin hacer nada."""
    lock = _lock(modulo.__name__)
    if lock.locked():
        return {"estado": "ocupado", "detalle": "ya hay una corrida en curso",
                "fin": datetime.now(_CDMX).isoformat()}
    async with lock:
        modulo.LAST_RUN.clear()
        modulo.LAST_RUN.update({"estado": "running", "motivo": motivo,
                                "inicio": datetime.now(_CDMX).isoformat()})
        return await modulo.run_job(dry_run=dry_run, motivo=motivo, telegram=telegram)
