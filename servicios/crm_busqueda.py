"""Búsqueda de texto del CRM (seguimientos y bitácora). Puro, sin BD.

`q` nunca se interpola en el SQL: solo viaja como parámetro enlazado, con `\\`, `%` y `_`
escapados para que se busquen como texto literal (el SQL lleva `ESCAPE '\\\\'`).
Independiente a propósito de la exportación a Excel.
"""
from typing import List, Optional, Tuple

Q_MAX = 100
# Texto SQL (lo que ve MySQL): ESCAPE '\\'
CLAUSULA_ESCAPE = "ESCAPE '\\\\'"


def escapar_like(texto: str) -> str:
    """Escapa \\, % y _ para usarlos dentro de un LIKE con ESCAPE '\\\\'."""
    return texto.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def normalizar_q(q: Optional[str]) -> Optional[str]:
    """Recorta; vacío o solo espacios = sin búsqueda (None)."""
    if q is None:
        return None
    limpio = q.strip()
    return limpio or None


def fragmento_busqueda(q: Optional[str], columnas: List[str]) -> Optional[Tuple[str, list]]:
    """('(col1 LIKE %s ESCAPE ... OR ...)', [patron, ...]) o None si q está vacío."""
    limpio = normalizar_q(q)
    if limpio is None:
        return None
    patron = f"%{escapar_like(limpio)}%"
    sql = " OR ".join(f"{c} LIKE %s {CLAUSULA_ESCAPE}" for c in columnas)
    return f"({sql})", [patron] * len(columnas)
