# Lógica pura de comisiones (sin BD): exclusión de canales, cálculo por partida
# y agregación de reportes. Todo en Decimal para que los centavos sean exactos.
from collections import OrderedDict
from decimal import Decimal, ROUND_HALF_UP

IVA = Decimal("1.16")
CENT = Decimal("0.01")
SKU_BASE = "*"

# Canales SIN comisión para nadie. Se compara contra la plataforma (y Cleanest
# también contra el comprador, porque sus ventas llevan plataforma 'SISTEMA ZEUTICA').
PLATAFORMAS_EXCLUIDAS = ("CLEANEST", "MERCADO", "MELI", "AMAZON")
COMPRADORES_EXCLUIDOS = ("CLEANEST",)


def _norm(texto) -> str:
    return " ".join(str(texto or "").upper().split())


def canal_excluido(plataforma, comprador=None, meli_key=None, amazon_key=None) -> bool:
    """True si la venta NO genera comisión (Cleanest, Mercado Libre, Amazon)."""
    if meli_key or amazon_key:
        return True
    p = _norm(plataforma)
    if any(m in p for m in PLATAFORMAS_EXCLUIDAS):
        return True
    c = _norm(comprador)
    return any(m in c for m in COMPRADORES_EXCLUIDOS)


def a_decimal(valor) -> Decimal:
    return valor if isinstance(valor, Decimal) else Decimal(str(valor))


def redondear(valor: Decimal) -> Decimal:
    return valor.quantize(CENT, rounding=ROUND_HALF_UP)


def resolver_tasa(tasas: dict, sku: str):
    """(porcentaje, origen). 'sku' = tasa propia, 'base' = tasa base del vendedor, 'sin_tasa' = 0%."""
    if sku in tasas:
        return a_decimal(tasas[sku]), "sku"
    if SKU_BASE in tasas:
        return a_decimal(tasas[SKU_BASE]), "base"
    return Decimal("0"), "sin_tasa"


def calcular_partida(precio_unitario, cantidad, porcentaje) -> dict:
    """
    Comisión SKU = (precio_neto / 1.16) * (porcentaje / 100), con precio_neto = monto
    cobrado con IVA (precio unitario con IVA × cantidad). La comisión sale de la base
    exacta (sin redondear) y solo el resultado final se redondea a centavos.
    """
    neto = a_decimal(precio_unitario) * int(cantidad)
    base = neto / IVA
    pct = a_decimal(porcentaje)
    return {
        "precio_neto": redondear(neto),
        "base_sin_iva": redondear(base),
        "porcentaje": pct,
        "comision": redondear(base * pct / Decimal(100)),
    }


def armar_reporte(filas: list) -> dict:
    """Agrupa partidas (filas de comisiones_ventas + vínculo) por venta, SKU y vendedor."""
    ventas, por_sku, por_vendedor = OrderedDict(), OrderedDict(), OrderedDict()
    cero = Decimal("0")
    tot = {"neto": cero, "base": cero, "comision": cero}

    for f in filas:
        neto, base, com = (a_decimal(f[k]) for k in ("precio_neto", "base_sin_iva", "comision"))
        v = ventas.setdefault(f["id_ventas"], {
            "id_ventas": f["id_ventas"], "fecha": f["fecha_venta"], "vendedor": f["vendedor"],
            "comprador": f.get("comprador"), "seguimiento_id": f.get("seguimiento_id"),
            "cotizacion": f.get("cotizacion"),
            "vinculo_modo": f.get("modo"), "precio_neto": cero, "base_sin_iva": cero,
            "comision": cero, "partidas": [],
        })
        v["precio_neto"] += neto
        v["base_sin_iva"] += base
        v["comision"] += com
        v["partidas"].append({
            "sku": f["sku"], "producto": f.get("producto"), "cantidad": f["cantidad"],
            "precio_neto": neto, "base_sin_iva": base, "porcentaje": a_decimal(f["porcentaje"]),
            "comision": com, "origen_tasa": f["origen_tasa"],
        })

        s = por_sku.setdefault(f["sku"], {"sku": f["sku"], "producto": f.get("producto"), "cantidad": 0,
                                           "base_sin_iva": cero, "comision": cero})
        s["cantidad"] += f["cantidad"]
        s["base_sin_iva"] += base
        s["comision"] += com

        w = por_vendedor.setdefault(f["vendedor"], {"vendedor": f["vendedor"], "ventas": set(),
                                                      "base_sin_iva": cero, "comision": cero})
        w["ventas"].add(f["id_ventas"])
        w["base_sin_iva"] += base
        w["comision"] += com

        tot["neto"] += neto
        tot["base"] += base
        tot["comision"] += com

    return {
        "resumen": {
            "ventas": len(ventas),
            "precio_neto": tot["neto"], "base_sin_iva": tot["base"], "comision": tot["comision"],
            "sin_tasa": sum(1 for f in filas if f["origen_tasa"] == "sin_tasa"),
            "sin_vinculo": sum(1 for v in ventas.values() if not v["seguimiento_id"] and not v["cotizacion"]),
        },
        "por_vendedor": [{**w, "ventas": len(w["ventas"])} for w in por_vendedor.values()],
        "por_sku": list(por_sku.values()),
        "ventas": list(ventas.values()),
    }
