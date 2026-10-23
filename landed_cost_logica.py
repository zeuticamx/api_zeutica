# Lógica pura del costeo de importación (Landed Cost) de un embarque, sacada de la plantilla
# «PRECIOS PRODUCTOS FELIPE FER.xlsx» (Tabla6102). Sin SQL ni FastAPI (se prueba en
# tests/test_landed_cost.py). La usa routers/landed_cost.py; el panel no recalcula.
#   - Entradas por item: columnas de embarque_items. Entradas del embarque: embarque_liquidacion.
#   - Todo se calcula con Decimal y se redondea solo al responder.
#   - Una división entre 0 o un dato faltante da None (más una advertencia), nunca 0:
#     un costo en 0 parece un costo real.
#   - Regla de la plantilla: la suma de `comprobacion` del embarque debe ser igual a la hoja
#     de liquidación (`resumen.cuadra`).
from decimal import Decimal, InvalidOperation

TASA_IVA_ADUANA = Decimal("0.16")
TOLERANCIA_COMPROBACION = Decimal("1")  # MXN
DECIMALES_RESPUESTA = 6

# Entradas capturadas a mano (celdas amarillas de la plantilla).
CAMPOS_ITEM = (
    "invoice", "fecha_compra", "paquete_venta", "unit_price_fob_usd",
    "ctns", "arancel_porc", "tipo_cambio_china",
)
CAMPOS_LIQUIDACION = (
    "total_cbm_manual", "no_hoja_liquidacion", "tipo_cambio_log", "incrementales_usd",
    "igi_total_embarque", "iva_total_aduana", "total_hoja_liquidacion",
    "total_consolidado_porc", "total_pagado_calculo_excel", "pagado_china_mxn",
)
# Sin estos la parte de logística y aduana no se puede calcular: el costeo queda «estimado».
REQUERIDOS_LIQUIDACION = (
    "tipo_cambio_log", "incrementales_usd", "igi_total_embarque",
    "iva_total_aduana", "total_hoja_liquidacion",
)
REQUERIDOS_ITEM = {
    "paquete_venta": "paquete de venta",
    "unit_price_fob_usd": "precio FOB",
    "qty": "cantidad",
    "cbm": "CBM",
    "arancel_porc": "arancel",
    "tipo_cambio_china": "tipo de cambio China",
}

ESTIMADO = "estimado"
COMPLETO = "completo"


def _dec(valor):
    """Decimal de un número (o texto numérico); None si falta o no es número."""
    if valor is None or valor == "":
        return None
    if isinstance(valor, Decimal):
        return valor
    try:
        return Decimal(str(valor))
    except (InvalidOperation, ValueError):
        return None


def _div(a, b):
    if a is None or b is None or b == 0:
        return None
    return a / b


def _mul(*factores):
    resultado = Decimal(1)
    for f in factores:
        if f is None:
            return None
        resultado *= f
    return resultado


def _suma(*sumandos):
    if any(s is None for s in sumandos):
        return None
    return sum(sumandos, Decimal(0))


def _resta(a, *restas):
    if a is None or any(r is None for r in restas):
        return None
    return a - sum(restas, Decimal(0))


def _redondear(valor):
    return None if valor is None else round(float(valor), DECIMALES_RESPUESTA)


def _fmt(valor) -> str:
    return f"{float(valor):,.4f}".rstrip("0").rstrip(".")


def total_cbm(items, total_cbm_manual):
    """(total usado, suma de items, origen). El manual manda si se capturó; si no, la suma."""
    suma = _suma(*[_dec(it.get("cbm")) or Decimal(0) for it in items])
    manual = _dec(total_cbm_manual)
    if manual is not None:
        return manual, suma, "manual"
    return (suma if suma else None), suma, "items"


def calcular_item(item: dict, liquidacion: dict, total_cbm_embarque):
    """Calcula los campos de un item. Devuelve (calculado, advertencias) con Decimal/None."""
    liq = liquidacion or {}
    qty = _dec(item.get("qty"))
    cbm = _dec(item.get("cbm"))
    paquete_venta = _dec(item.get("paquete_venta"))
    fob = _dec(item.get("unit_price_fob_usd"))
    arancel = _dec(item.get("arancel_porc"))
    tc_china = _dec(item.get("tipo_cambio_china"))
    tc_log = _dec(liq.get("tipo_cambio_log"))
    incrementales = _dec(liq.get("incrementales_usd"))
    igi_total = _dec(liq.get("igi_total_embarque"))
    iva_total = _dec(liq.get("iva_total_aduana"))
    hoja = _dec(liq.get("total_hoja_liquidacion"))

    advertencias = [f"Falta {nombre}" for campo, nombre in REQUERIDOS_ITEM.items() if _dec(item.get(campo)) is None]
    if paquete_venta == 0:
        advertencias.append("Paquete de venta en 0: no se puede calcular el costo por paquete")
    elif qty is not None and paquete_venta is not None and qty % paquete_venta != 0:
        advertencias.append("La cantidad no es múltiplo del paquete de venta (paquetes fraccionarios)")
    if tc_china == 0:
        advertencias.append("Tipo de cambio China en 0")
    if cbm == 0:
        advertencias.append("CBM en 0: el item no absorbe flete ni incrementales")

    # Paso 1: costo del material en China.
    total_paquetes = _div(qty, paquete_venta)
    total_usd = _mul(fob, qty)
    costo_paq_china_mxn = _div(_mul(total_usd, tc_china), total_paquetes)
    # Columna O de la plantilla («TOTAL PAGADO MATERIAL CHINA EN MXN»): en realidad son USD
    # por paquete (su formato es [$USD]); se renombra aquí.
    costo_paq_china_usd = _div(costo_paq_china_mxn, tc_china)

    # Paso 2: distribución por CBM y aduana (IGI e IVA estimados del item).
    porc_cbm = _div(cbm, total_cbm_embarque)
    incremental = _mul(incrementales, porc_cbm)
    valor_aduana = _suma(incremental, total_usd)
    # IGI = valor en aduana × TC logística × arancel (celda Z3). La fórmula por defecto de la
    # tabla en el Excel (`#REF! + costo paquete × arancel`) es una versión vieja y rota.
    igi_item = _mul(valor_aduana, tc_log, arancel)
    igi_paquete = _div(igi_item, total_paquetes)
    iva_item = _mul(_suma(_mul(valor_aduana, tc_log), igi_item), TASA_IVA_ADUANA)
    iva_paquete = _div(iva_item, total_paquetes)

    # Paso 3: envío y logística (hoja de liquidación sin IGI ni IVA reales, prorrateada por CBM).
    envio_total = _resta(hoja, igi_total, iva_total)
    envio_item = _mul(envio_total, porc_cbm)
    envio_paquete = _div(envio_item, total_paquetes)
    igi_iva_paquete = _div(_suma(igi_item, iva_item), total_paquetes)
    envio_paq_c_iva = _suma(envio_paquete, igi_iva_paquete)
    envio_paq_sin_iva = _resta(envio_paq_c_iva, iva_paquete)
    comprobacion = _mul(envio_paq_c_iva, total_paquetes)

    # Paso 4: costo puesto en bodega.
    relacion = _div(envio_paq_c_iva, costo_paq_china_mxn)
    costo_c_iva = _suma(envio_paq_c_iva, costo_paq_china_mxn)
    costo_sin_iva = _suma(costo_paq_china_mxn, envio_paq_sin_iva)
    qty_paq_primario = _div(qty, paquete_venta)
    zeutica_paga = _mul(qty_paq_primario, costo_c_iva)

    calculado = {
        "total_paquetes_venta": total_paquetes,
        "piezas_m2": qty,
        "total_usd_sin_igi": total_usd,
        "costo_paq_venta_china_mxn_sin_igi": costo_paq_china_mxn,
        "costo_paq_venta_china_usd": costo_paq_china_usd,
        "porc_cbm_en_barco": porc_cbm,
        "incremental_proporcional": incremental,
        "valor_mercancia_aduana_total": valor_aduana,
        "igi_correspondiente_item": igi_item,
        "igi_por_paquete": igi_paquete,
        "iva_item_aduana": iva_item,
        "iva_item_por_paquete": iva_paquete,
        "costo_envio_item_sin_iva_igi": envio_item,
        "costo_envio_paquete_sin_iva_igi": envio_paquete,
        "igi_mas_iva_por_paquete": igi_iva_paquete,
        "costo_envio_paq_venta_c_iva": envio_paq_c_iva,
        "costo_envio_paq_venta_sin_iva": envio_paq_sin_iva,
        "comprobacion": comprobacion,
        "relacion_costo_material_vs_envio": relacion,
        "costo_por_paquete_c_iva_bodega": costo_c_iva,
        "costo_por_paquete_sin_iva_bodega": costo_sin_iva,
        "qty_paq_primario": qty_paq_primario,
        "zeutica_paga_total": zeutica_paga,
    }
    return calculado, advertencias


def calcular_embarque(items, liquidacion=None, invoices=None) -> dict:
    """Costeo completo de un embarque.

    items: dicts con id, sku, qty, cbm y los CAMPOS_ITEM.
    liquidacion: dict con los CAMPOS_LIQUIDACION (None = aún no se captura).
    invoices: números de invoice del embarque (None = no se valida la invoice del item).
    """
    items = items or []
    liq = liquidacion or {}
    advertencias = []

    faltantes = [c for c in REQUERIDOS_LIQUIDACION if _dec(liq.get(c)) is None]
    estado = ESTIMADO if faltantes else COMPLETO
    if faltantes:
        advertencias.append("Falta capturar la hoja de liquidación: " + ", ".join(faltantes))

    cbm_usado, cbm_items, origen = total_cbm(items, liq.get("total_cbm_manual"))
    if not cbm_usado:
        advertencias.append("Sin CBM total del embarque: no se puede prorratear flete ni incrementales")
    elif origen == "manual" and cbm_items is not None and abs(cbm_usado - cbm_items) > Decimal("0.0001"):
        advertencias.append(
            f"El CBM total capturado ({_fmt(cbm_usado)}) no coincide con la suma de los items ({_fmt(cbm_items)})"
        )

    hoja = _dec(liq.get("total_hoja_liquidacion"))
    igi_total = _dec(liq.get("igi_total_embarque"))
    iva_total = _dec(liq.get("iva_total_aduana"))
    envio_total = _resta(hoja, igi_total, iva_total)
    if envio_total is not None and envio_total < 0:
        advertencias.append("La hoja de liquidación es menor que el IGI más el IVA de aduana")

    invoices_validas = {str(i).strip() for i in invoices} if invoices is not None else None
    filas, calculados = [], []
    for it in items:
        calculado, adv = calcular_item(it, liq, cbm_usado)
        invoice = (it.get("invoice") or "").strip()
        if invoices_validas is not None and invoice and invoice not in invoices_validas:
            adv.append(f"La invoice {invoice} ya no está en el embarque")
        calculados.append(calculado)
        filas.append({
            "item_id": it.get("id"),
            "sku": it.get("sku"),
            "entrada": {
                "qty": it.get("qty"), "cbm": it.get("cbm"),
                **{c: it.get(c) for c in CAMPOS_ITEM},
            },
            "calculado": {k: _redondear(v) for k, v in calculado.items()},
            "advertencias": adv,
        })

    def total(campo):
        valores = [c[campo] for c in calculados]
        return _suma(*valores) if valores else None

    suma_comprobacion = total("comprobacion")
    diferencia = _resta(suma_comprobacion, hoja)
    cuadra = None if diferencia is None else abs(diferencia) <= TOLERANCIA_COMPROBACION
    if cuadra is False:
        advertencias.append(
            f"La suma de la comprobación no cuadra con la hoja de liquidación (diferencia {_fmt(diferencia)})"
        )

    suma_zeutica = total("zeutica_paga_total")
    pagado_china = _dec(liq.get("pagado_china_mxn"))
    total_pagado_real = _suma(hoja, pagado_china)

    resumen = {
        "total_cbm_usado": cbm_usado,
        "total_cbm_items": cbm_items,
        "origen_total_cbm": origen,
        "suma_porc_cbm": total("porc_cbm_en_barco"),
        "costo_envio_total_sin_iva_igi": envio_total,
        "suma_comprobacion": suma_comprobacion,
        "total_hoja_liquidacion": hoja,
        "diferencia_comprobacion": diferencia,
        "igi_estimado_total": total("igi_correspondiente_item"),
        "igi_total_embarque": igi_total,
        "iva_estimado_total": total("iva_item_aduana"),
        "iva_total_aduana": iva_total,
        "suma_zeutica_paga_total": suma_zeutica,
        "asx": hoja,
        "pagado_china_mxn": pagado_china,
        "total_pagado_real": total_pagado_real,
        "diferencia_pagado": _resta(suma_zeutica, total_pagado_real),
        "total_pagado_calculo_excel": _dec(liq.get("total_pagado_calculo_excel")),
    }
    resumen = {k: (_redondear(v) if isinstance(v, Decimal) else v) for k, v in resumen.items()}
    resumen["cuadra"] = cuadra
    resumen["tolerancia"] = float(TOLERANCIA_COMPROBACION)

    return {"estado": estado, "items": filas, "resumen": resumen, "advertencias": advertencias}
