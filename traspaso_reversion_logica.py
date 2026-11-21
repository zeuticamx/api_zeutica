"""Lógica pura (sin SQL) de la reversión de traspasos (Traspaso FULL / Clean).

Cada movimiento vive como una fila de `stock_actual` (un SKU):
- `BAJA`  (destino «Mercado Libre FULL»): cantidad negativa; restó de `stock_bodega`.
- `CLEAN` (destino «Cleanest Choice»):    cantidad positiva; restó de `stock_bodega` y sumó a `stock_clean`.
Revertir devuelve las piezas a bodega y deja una fila compensatoria (`REV-BAJA` / `REV-CLEAN`,
signo inverso) para que la auditoría de inventario siga cuadrando. Esas filas no se revierten.
"""
import os
from datetime import datetime, timedelta

DIAS_REVERSION_DEFECTO = 15


def dias_reversion() -> int:
    """Plazo en días para revertir (`TRASPASO_REVERSION_DIAS`, 15 por defecto; inválido → 15)."""
    try:
        dias = int(os.getenv("TRASPASO_REVERSION_DIAS", DIAS_REVERSION_DEFECTO))
    except (TypeError, ValueError):
        return DIAS_REVERSION_DEFECTO
    return dias if dias > 0 else DIAS_REVERSION_DEFECTO


def dentro_de_plazo(fecha: datetime | None, ahora: datetime, dias: int | None = None) -> bool:
    """Ventana móvil: True si el movimiento tiene como máximo `dias` días (exactos) respecto a `ahora`.

    `fecha` y `ahora` deben venir del mismo reloj (la BD). Sin fecha no se puede comprobar → False.
    """
    if fecha is None or ahora is None:
        return False
    limite = timedelta(days=dias if dias is not None else dias_reversion())
    return (ahora - fecha) <= limite


def plan_reversion(almacen: str | None, cantidad: int | None) -> dict | None:
    """Qué hay que mover para revertir la fila, o None si no es revertible.

    Devuelve `tipo`, `piezas` (siempre > 0), cuánto sumar a bodega y restar de clean, y la
    fila compensatoria (`almacen_rev`, `cantidad_rev`).
    """
    nombre = str(almacen or "").strip().upper()
    n = int(cantidad or 0)
    if nombre == "BAJA" and n < 0:
        piezas = -n
        return {"tipo": "BAJA", "piezas": piezas, "sumar_bodega": piezas, "restar_clean": 0,
                "almacen_rev": "REV-BAJA", "cantidad_rev": piezas}
    if nombre == "CLEAN" and n > 0:
        return {"tipo": "CLEAN", "piezas": n, "sumar_bodega": n, "restar_clean": n,
                "almacen_rev": "REV-CLEAN", "cantidad_rev": -n}
    return None


def es_revertible(almacen: str | None, cantidad: int | None, fecha: datetime | None,
                  ahora: datetime, ya_revertido: bool, dias: int | None = None) -> bool:
    """Resumen para el listado: tipo revertible, dentro de plazo y sin reversión previa."""
    return (not ya_revertido
            and plan_reversion(almacen, cantidad) is not None
            and dentro_de_plazo(fecha, ahora, dias))
