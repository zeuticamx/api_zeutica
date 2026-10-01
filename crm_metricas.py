# Lógica pura del CRM: agrupa filas que ya trajo MySQL en las estructuras que
# consume el panel. Sin SQL ni FastAPI, para poder probarla sola (tests/test_crm.py).
from datetime import date, timedelta

TIPOS = ("llamada", "correo", "whatsapp", "reunion")
ETAPAS = ("contacto_inicial", "en_seguimiento", "cotizado", "ganado", "perdido")
ETIQUETAS_ETAPA = {
    "contacto_inicial": "Contacto inicial",
    "en_seguimiento": "En seguimiento",
    "cotizado": "Cotizado",
    "ganado": "Cerrado / Ganado",
    "perdido": "Perdido",
}


def _a_fecha(valor):
    """MySQL devuelve date/datetime; los fakes de tests pueden mandar 'YYYY-MM-DD'."""
    if valor is None:
        return None
    if hasattr(valor, "date") and callable(valor.date):
        return valor.date()
    if isinstance(valor, date):
        return valor
    return date.fromisoformat(str(valor)[:10])


def agrupar_seguimientos(filas, hoy: date):
    """Separa los seguimientos abiertos en vencidos / hoy / próximos (resto)."""
    vencidos, de_hoy, proximos = [], [], []
    for f in filas:
        fecha = _a_fecha(f.get("proxima_fecha"))
        if fecha is None:
            continue
        item = dict(f)
        item["dias_atraso"] = max((hoy - fecha).days, 0)
        if fecha < hoy:
            vencidos.append(item)
        elif fecha == hoy:
            de_hoy.append(item)
        else:
            proximos.append(item)
    # Lo más atrasado primero; lo próximo, lo más cercano primero.
    vencidos.sort(key=lambda x: (-x["dias_atraso"], x.get("id") or 0))
    proximos.sort(key=lambda x: (_a_fecha(x["proxima_fecha"]), x.get("id") or 0))
    return {
        "vencidos": vencidos,
        "hoy": de_hoy,
        "proximos": proximos,
        "totales": {"vencidos": len(vencidos), "hoy": len(de_hoy), "proximos": len(proximos)},
    }


def _vendedor_vacio(nombre):
    base = {"vendedor": nombre, "total": 0, "clientes": 0, "vencidos": 0}
    base.update({t: 0 for t in TIPOS})
    base.update({f"{e}_periodo": 0 for e in ETAPAS})
    return base


def armar_resumen(conteos, clientes, vencidos, movimientos, desde: date, hasta: date):
    """
    conteos:     [{vendedor, tipo, dia, total}]       interacciones del periodo
    clientes:    [{vendedor, clientes}]                clientes distintos contactados
    vencidos:    [{vendedor, vencidos}]                foto actual, no depende del periodo
    movimientos: [{vendedor, etapa, total}]            cambios de etapa en el periodo
    """
    por_tipo = {t: 0 for t in TIPOS}
    por_dia = {}
    vendedores = {}

    def vend(nombre):
        nombre = nombre or "(sin vendedor)"
        if nombre not in vendedores:
            vendedores[nombre] = _vendedor_vacio(nombre)
        return vendedores[nombre]

    for f in conteos:
        n = int(f.get("total") or 0)
        tipo = f.get("tipo")
        v = vend(f.get("vendedor"))
        v["total"] += n
        if tipo in por_tipo:
            por_tipo[tipo] += n
            v[tipo] += n
        dia = _a_fecha(f.get("dia"))
        if dia is not None:
            por_dia[dia] = por_dia.get(dia, 0) + n

    for f in clientes:
        vend(f.get("vendedor"))["clientes"] = int(f.get("clientes") or 0)
    for f in vencidos:
        vend(f.get("vendedor"))["vencidos"] = int(f.get("vencidos") or 0)
    for f in movimientos:
        etapa = f.get("etapa")
        if etapa in ETAPAS:
            vend(f.get("vendedor"))[f"{etapa}_periodo"] += int(f.get("total") or 0)

    # Serie continua: los días sin actividad van en 0 para que la gráfica no mienta.
    serie = []
    dia = desde
    while dia <= hasta:
        serie.append({"dia": dia.isoformat(), "total": por_dia.get(dia, 0)})
        dia += timedelta(days=1)

    filas_vend = sorted(vendedores.values(), key=lambda v: (-v["total"], v["vendedor"]))
    return {
        "desde": desde.isoformat(),
        "hasta": hasta.isoformat(),
        "totales": {
            "interacciones": sum(por_tipo.values()),
            "por_tipo": por_tipo,
            "vencidos": sum(v["vencidos"] for v in filas_vend),
            "ganados": sum(v["ganado_periodo"] for v in filas_vend),
        },
        "por_vendedor": filas_vend,
        "serie": serie,
    }


def armar_embudo(actual, movimientos):
    """
    actual:      [{vendedor, etapa, total}]  clientes por etapa hoy (crm_cartera)
    movimientos: [{etapa, total}]            entradas a cada etapa en el periodo
    """
    por_etapa = {e: 0 for e in ETAPAS}
    entradas = {e: 0 for e in ETAPAS}
    vendedores = {}

    for f in actual:
        etapa = f.get("etapa")
        if etapa not in por_etapa:
            continue
        n = int(f.get("total") or 0)
        por_etapa[etapa] += n
        nombre = f.get("vendedor") or "(sin vendedor)"
        v = vendedores.setdefault(nombre, {"vendedor": nombre, "total": 0, **{e: 0 for e in ETAPAS}})
        v[etapa] += n
        v["total"] += n

    for f in movimientos:
        if f.get("etapa") in entradas:
            entradas[f["etapa"]] += int(f.get("total") or 0)

    cerrados = entradas["ganado"] + entradas["perdido"]
    return {
        "etapas": [
            {"etapa": e, "label": ETIQUETAS_ETAPA[e], "actual": por_etapa[e], "entradas_periodo": entradas[e]}
            for e in ETAPAS
        ],
        "total": sum(por_etapa.values()),
        # Tasa de cierre del periodo: ganados / (ganados + perdidos). None si no hubo cierres.
        "tasa_cierre": round(entradas["ganado"] / cerrados, 4) if cerrados else None,
        "por_vendedor": sorted(vendedores.values(), key=lambda v: (-v["total"], v["vendedor"])),
    }
