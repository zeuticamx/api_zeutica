"""Lógica pura (sin SQL) de las compras por ubicar.

Cada fila de `compras` es una entrada por ubicar. Sus piezas se resuelven de tres formas:
- repartidas en tarjetas de ubicación (`cantidad_ubicada`),
- enviadas a venta directa al cliente, sin pasar por bodega (`cantidad_venta_directa`),
- o toda la compra marcada «sin ubicación».
Queda resuelta (ubicada) cuando ubicada + venta directa alcanzan lo comprado (`stock_bodega`).
"""


def pendiente(total: int, ubicada: int, venta_directa: int = 0) -> int:
    """Piezas de la compra que aún no tienen destino (nunca negativo)."""
    return max(int(total or 0) - int(ubicada or 0) - int(venta_directa or 0), 0)


def validar_cantidad(cantidad: int, total: int, ubicada: int, venta_directa: int = 0) -> str | None:
    """Mensaje de error si `cantidad` no se puede asignar ahora; None si es válida."""
    falta = pendiente(total, ubicada, venta_directa)
    if cantidad <= 0:
        return "La cantidad debe ser mayor a 0"
    if cantidad > falta:
        return f"La cantidad ({cantidad}) excede lo pendiente por ubicar ({falta})"
    return None


def piezas_a_cajas(piezas: int, multiplo: int) -> tuple[int | None, str | None]:
    """Convierte piezas de la compra a cajas de la tarjeta: (cajas, None) o (None, error).

    Las tarjetas guardan cajas; la compra se captura en piezas. Debe ser múltiplo exacto
    (misma política que «empaquetar» en el traspaso).
    """
    multiplo = int(multiplo or 1)
    if multiplo > 1 and piezas % multiplo != 0:
        return None, f"Las piezas deben ser múltiplo de {multiplo} (piezas por caja de este SKU)"
    return piezas // multiplo, None


def queda_ubicada(total: int, ubicada: int, cantidad: int, venta_directa: int = 0) -> bool:
    """True si al asignar `cantidad` más ya no queda nada pendiente."""
    return pendiente(total, int(ubicada or 0) + cantidad, venta_directa) == 0


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
            "cantidad_venta_directa": fila.get("cantidad_venta_directa") or 0,
            "pendiente": pendiente(fila["cantidad"], fila.get("cantidad_ubicada"),
                                   fila.get("cantidad_venta_directa")),
        })
    return list(grupos.values())
