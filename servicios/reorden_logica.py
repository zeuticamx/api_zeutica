# Lógica pura del punto de reorden (WRP): ventana de meses, estadística de la demanda,
# stock de seguridad, punto de reorden y decisión de compra. Sin SQL ni FastAPI
# (se prueba en tests/test_reorden.py). La usan routers/reorden.py y jobs/reorden_compras.py:
# el panel no recalcula, solo muestra lo que sale de aquí.
import math
import statistics
from datetime import date

LEAD_TIME_DEFAULT = 3.0   # meses
Z_DEFAULT = 1.65          # 95 % de nivel de servicio
CV_VOLATIL = 0.50
MESES_MIN_CON_VENTA = 3   # menos meses con venta = historial corto (la desviación no es confiable)

# Niveles de servicio que ofrece el panel (Z de la normal estándar).
NIVELES_SERVICIO = [
    {"nivel": 90, "z": 1.28},
    {"nivel": 95, "z": 1.65},
    {"nivel": 97.5, "z": 1.96},
    {"nivel": 99, "z": 2.33},
]
Z_PERMITIDOS = {n["z"] for n in NIVELES_SERVICIO}

COMPRAR = "COMPRAR"
OK = "OK"
SIN_DEMANDA = "SIN_DEMANDA"


def ventana_meses(hoy: date, fecha_inicio: date):
    """Devuelve los meses calendario COMPLETOS transcurridos desde `fecha_inicio`

    hasta el último día del mes pasado (excluyendo el mes en curso).

    Devuelve:
        etiquetas: lista 'YYYY-MM' del mes más antiguo al más reciente.
        desde: día 1 del mes de la `fecha_inicio`.
        hasta: día 1 del mes en curso (límite exclusivo para consultas).
    """
    hasta = hoy.replace(day=1)
    desde = fecha_inicio.replace(day=1)

    # Si la fecha de inicio es del mes actual o posterior, no hay meses completos pasados
    if desde >= hasta:
        return [], hasta, hasta

    etiquetas = []
    y, m = desde.year, desde.month

    # Avanzamos mes a mes desde la fecha de inicio hasta llegar al mes en curso
    while (y, m) < (hasta.year, hasta.month):
        etiquetas.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1

    return etiquetas, desde, hasta


def serie_consumo(por_mes: dict, etiquetas: list) -> list:
    """Consumo por mes en el orden de la ventana; un mes sin ventas cuenta como 0
    (omitirlo inflaría el promedio y achicaría la desviación)."""
    return [float(por_mes.get(e) or 0) for e in etiquetas]


def _techo(x: float) -> int:
    # Redondeo al entero superior sin que el ruido de coma flotante (12.0000000001) sume uno.
    return int(math.ceil(round(x, 6)))


def calcular_sku(consumos: list, existencia: float, en_transito: float,
                 lead_time: float = LEAD_TIME_DEFAULT, z: float = Z_DEFAULT) -> dict:
    """Parámetros de reabastecimiento de un SKU. Stock de seguridad y punto de reorden
    van al entero superior. Sin demanda en la ventana no hay decisión de compra."""
    total = float(existencia or 0) + float(en_transito or 0)
    n = len(consumos)
    promedio = sum(consumos) / n if n else 0.0
    desviacion = statistics.stdev(consumos) if n >= 2 else 0.0  # muestral (n-1)
    meses_con_venta = sum(1 for c in consumos if c > 0)

    if promedio <= 0:
        return {
            "total_mercancia": total, "promedio": 0.0, "desviacion": round(desviacion, 2),
            "cv": None, "volatilidad": None, "stock_seguridad": 0, "punto_reorden": 0,
            "meses_stock": None, "decision": SIN_DEMANDA, "meses_con_venta": meses_con_venta,
            "historial_corto": True,
        }

    cv = desviacion / promedio
    stock_seguridad = _techo(z * desviacion * math.sqrt(lead_time))
    punto_reorden = _techo(promedio * lead_time + stock_seguridad)
    return {
        "total_mercancia": total,
        "promedio": round(promedio, 2),
        "desviacion": round(desviacion, 2),
        "cv": round(cv, 2),
        "volatilidad": "VOLATIL" if cv > CV_VOLATIL else "NORMAL",
        "stock_seguridad": stock_seguridad,
        "punto_reorden": punto_reorden,
        "meses_stock": round(total / promedio, 1),
        "decision": COMPRAR if total <= punto_reorden else OK,
        "meses_con_venta": meses_con_venta,
        "historial_corto": meses_con_venta < MESES_MIN_CON_VENTA,
    }


_ORDEN_DECISION = {COMPRAR: 0, OK: 1, SIN_DEMANDA: 2}


def evaluar(productos: list, consumo: dict, transito: dict, parametros: dict, etiquetas: list) -> list:
    """Una fila por producto. consumo = {sku: {'YYYY-MM': cantidad}}, transito = {sku: qty},
    parametros = {sku: {'lead_time_meses', 'nivel_servicio_z'}}. Las llaves de SKU se comparan
    sin mayúsculas ni espacios. Orden: COMPRAR primero (menos meses de stock arriba)."""
    norm = lambda s: str(s or "").strip().upper()
    consumo_n = {norm(k): v for k, v in consumo.items()}
    transito_n = {norm(k): v for k, v in transito.items()}
    param_n = {norm(k): v for k, v in parametros.items()}

    filas = []
    for p in productos:
        clave = norm(p.get("sku"))
        prm = param_n.get(clave) or {}
        lead_time = float(prm.get("lead_time_meses") or LEAD_TIME_DEFAULT)
        z = float(prm.get("nivel_servicio_z") or Z_DEFAULT)
        serie = serie_consumo(consumo_n.get(clave) or {}, etiquetas)
        en_transito = float(transito_n.get(clave) or 0)
        existencia = float(p.get("existencia") or 0)
        fila = {
            "sku": p.get("sku"), "nombre": p.get("nombre"), "categoria": p.get("categoria"),
            "existencia": existencia, "en_transito": en_transito,
            "lead_time_meses": lead_time, "nivel_servicio_z": z,
            "parametros_propios": bool(prm), "consumos": serie,
        }
        fila.update(calcular_sku(serie, existencia, en_transito, lead_time, z))
        filas.append(fila)

    filas.sort(key=lambda f: (_ORDEN_DECISION[f["decision"]],
                              f["meses_stock"] if f["meses_stock"] is not None else math.inf,
                              str(f["sku"] or "")))
    return filas
