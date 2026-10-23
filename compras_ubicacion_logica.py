"""Lógica pura (sin SQL) de las compras por ubicar.

Cada fila de `compras` es una entrada por ubicar: queda ubicada cuando la suma de lo
repartido en tarjetas (`cantidad_ubicada`) alcanza lo comprado (`stock_bodega`), o cuando
alguien la marca «sin ubicación».
"""


def pendiente(total: int, ubicada: int) -> int:
    """Piezas de la compra que aún no tienen tarjeta (nunca negativo)."""
    return max(int(total or 0) - int(ubicada or 0), 0)


def validar_cantidad(cantidad: int, total: int, ubicada: int) -> str | None:
    """Mensaje de error si `cantidad` no se puede ubicar ahora; None si es válida."""
    falta = pendiente(total, ubicada)
    if cantidad <= 0:
        return "La cantidad debe ser mayor a 0"
    if cantidad > falta:
        return f"La cantidad ({cantidad}) excede lo pendiente por ubicar ({falta})"
    return None


def queda_ubicada(total: int, ubicada: int, cantidad: int) -> bool:
    """True si al repartir `cantidad` más ya no queda nada pendiente."""
    return pendiente(total, int(ubicada or 0) + cantidad) == 0


def agrupar_por_factura(filas: list[dict]) -> list[dict]:
    """Agrupa las filas pendientes por (factura, proveedor), conservando el orden de llegada.

    Cada item trae `pendiente` calculado; las filas sin factura quedan juntas bajo `None`.
    """
    grupos: dict[tuple, dict] = {}
    for fila in filas:
        clave = (fila.get("num_factura"), fila.get("proveedor"))
        grupo = grupos.setdefault(clave, {
            "num_factura": fila.get("num_factura"),
            "proveedor": fila.get("proveedor"),
            "fecha_registro": fila.get("fecha_registro"),
            "items": [],
        })
        grupo["items"].append({
            "id": fila["id"],
            "sku": fila["sku"],
            "nombre": fila.get("nombre"),
            "cantidad": fila["cantidad"],
            "cantidad_ubicada": fila.get("cantidad_ubicada") or 0,
            "pendiente": pendiente(fila["cantidad"], fila.get("cantidad_ubicada")),
        })
    return list(grupos.values())
