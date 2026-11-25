"""Cronómetro de tareas de pendientes (puro, sin SQL).

Solo se cronometran los usuarios de USUARIOS_CRONOMETRO (hoy `fparra`). El tiempo corre desde que
toma la tarea (`inicio_atencion`) hasta que la termina (`fin_atencion`), en tiempo corrido (sin
descontar noches ni fines de semana). Las marcas son DATETIME en hora de México escritas por Python.
Dato faltante o incoherente = None, nunca 0.
"""
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

CDMX = ZoneInfo("America/Mexico_City")
USUARIOS_CRONOMETRO = ("fparra",)
FORMATO = "%Y-%m-%d %H:%M:%S"
# Usuarios que solo VEN el cronómetro de los cronometrados (sin poder operarlo).
USUARIOS_VISORES = ("ventas",)


def se_cronometra(usuario) -> bool:
    return str(usuario or "").strip().lower() in USUARIOS_CRONOMETRO


def es_visor(usuario) -> bool:
    return str(usuario or "").strip().lower() in USUARIOS_VISORES


def ahora_mx() -> datetime:
    """Ahora en hora de México, sin zona (como se guardan los DATETIME)."""
    return datetime.now(CDMX).replace(tzinfo=None, microsecond=0)


def duracion_segundos(inicio: Optional[datetime], fin: Optional[datetime], ahora: Optional[datetime] = None) -> Optional[int]:
    """Segundos entre inicio y fin; sin fin cuenta hasta `ahora` (tarea abierta).
    Sin inicio, sin ninguno de los dos extremos o fin anterior al inicio = None."""
    if not isinstance(inicio, datetime):
        return None
    extremo = fin if isinstance(fin, datetime) else ahora
    if not isinstance(extremo, datetime) or extremo < inicio:
        return None
    return int((extremo - inicio).total_seconds())


def _texto(valor) -> Optional[str]:
    return valor.strftime(FORMATO) if isinstance(valor, datetime) else None


def tarea_tiempo_json(fila: dict, ahora: datetime) -> dict:
    """Fila de `pendientes` -> renglón de la sección «Tiempos» de Monitor Gerencia."""
    inicio, fin = fila.get("inicio_atencion"), fila.get("fin_atencion")
    return {
        "id": fila["id"],
        "usuario": fila.get("usuario"),
        "actividad": fila.get("actividad"),
        "prioridad": fila.get("prioridad"),
        "estado": fila.get("estado"),
        "inicio": _texto(inicio),
        "fin": _texto(fin),
        "en_curso": isinstance(inicio, datetime) and not isinstance(fin, datetime),
        "duracion_segundos": duracion_segundos(inicio, fin, ahora),
        "intercambiado": bool(fila.get("intercambiado")),
        "intercambiado_por": fila.get("intercambiado_por"),
        "fecha_intercambiado": _texto(fila.get("fecha_intercambiado")),
    }


def resumen_kpi(tareas: list) -> dict:
    """KPI sobre los renglones de tarea_tiempo_json: suma solo las TERMINADAS sin intercambiar
    (las abiertas se cuentan aparte y no suman)."""
    sin_intercambiar = [t for t in tareas if not t["intercambiado"] and not t["en_curso"]]
    medibles = [t["duracion_segundos"] for t in sin_intercambiar if t["duracion_segundos"] is not None]
    return {
        "segundos_sin_intercambiar": sum(medibles),
        "tareas_sin_intercambiar": len(sin_intercambiar),
        "tareas_sin_dato": len(sin_intercambiar) - len(medibles),
        "tareas_en_curso": sum(1 for t in tareas if t["en_curso"]),
    }
