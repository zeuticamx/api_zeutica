# Lógica pura del punto de reorden (WRP): ventana de meses, estadística de la demanda,
# stock de seguridad, punto de reorden y decisión de compra. Sin SQL ni FastAPI
# (se prueba en tests/test_reorden.py). La usan routers/reorden.py y jobs/reorden_compras.py:
# el panel no recalcula, solo muestra lo que sale de aquí.
import math
import statistics
from datetime import date

MESES_HISTORIAL = 12
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


def ventana_meses(hoy: date, meses: int = MESES_HISTORIAL):
    """Los `meses` meses calendario COMPLETOS antes del mes en curso (el actual va a medias
    y bajaría el promedio). Devuelve (lista 'YYYY-MM' del más viejo al más reciente,
    desde = día 1 del más viejo, hasta = día 1 del mes en curso, exclusivo)."""
    hasta = hoy.replace(day=1)
    y, m = hasta.year, hasta.month
    etiquetas = []
    for _ in range(meses):
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        etiquetas.append(f"{y:04d}-{m:02d}")
    etiquetas.reverse()
    desde = date(int(etiquetas[0][:4]), int(etiquetas[0][5:]), 1)
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
