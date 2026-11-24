# Motor Zentor (Mercado Ads / Product Ads): lógica pura, sin SQL ni HTTP.
# La comparten routers/mercado_ads.py y jobs/zentor_piloto.py; el panel no recalcula nada.
#   ACOS  = inversión / ventas publicitarias × 100   (null si no hay ventas: nunca 0)
#   ROAS  = ventas / inversión                        (null si no hay inversión)
#   CVR   = unidades vendidas / clics × 100           (null si no hay clics)
# Solo «pausar» y «activar» se aplican en MeLi; subir/bajar puja son recomendaciones informativas.
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# ---------- Acciones y semáforo ----------
PAUSAR = "pausar"
ACTIVAR = "activar"
BAJAR_PUJA = "bajar_puja"
SUBIR_PUJA = "subir_puja"
MANTENER = "mantener"

ROJO, AMARILLO, VERDE = "rojo", "amarillo", "verde"

ACCIONES_APLICABLES = {PAUSAR, ACTIVAR}

# Estados del anuncio en MeLi que el motor entiende.
ACTIVO, PAUSADO = "active", "paused"

# ---------- Estado del motor ----------
MOTOR_ACTIVO, MOTOR_PAUSA = "activo", "pausa"
ESTADOS_MOTOR = (MOTOR_ACTIVO, MOTOR_PAUSA)
ESTADO_ACTIVO, ESTADO_PILOTO, ESTADO_PAUSA = "activo", "piloto", "pausa"

# ---------- Reglas ----------
REGLAS_DEFAULT = {"target_acos": 20.0, "max_clicks_without_sale": 30, "min_spend_threshold": 100.0, "auto_pilot": False}

# Con ACOS ≤ objetivo × este factor el anuncio rinde de sobra y se sugiere subir la puja.
FACTOR_ESCALAR = Decimal("0.7")

# Rango de fechas de la consulta.
DIAS_POR_DEFECTO = 7
DIAS_MAXIMOS = 90


def _dec(valor) -> Decimal:
    """Decimal tolerante: None, '' o basura cuentan como 0."""
    if valor is None or valor == "":
        return Decimal(0)
    try:
        return Decimal(str(valor))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def _redondear(valor: Decimal | None, decimales: int = 2) -> float | None:
    if valor is None:
        return None
    return float(valor.quantize(Decimal(1).scaleb(-decimales), rounding=ROUND_HALF_UP))


def _porcentaje(numerador: Decimal, denominador: Decimal) -> Decimal | None:
    """numerador / denominador × 100, o None si el denominador es 0 (nunca 0 inventado)."""
    if denominador <= 0:
        return None
    return numerador / denominador * 100


# ---------- Rango de fechas ----------

def rango_fechas(desde: date | None, hasta: date | None, hoy: date) -> tuple[date, date]:
    """(desde, hasta) válido. Sin fechas: últimos 7 días terminando hoy. ValueError con el motivo."""
    if hasta is None:
        hasta = hoy
    if desde is None:
        desde = hasta - timedelta(days=DIAS_POR_DEFECTO - 1)
    if desde > hasta:
        raise ValueError("La fecha inicial no puede ser posterior a la final")
    if hasta > hoy:
        raise ValueError("La fecha final no puede ser futura")
    if (hasta - desde).days + 1 > DIAS_MAXIMOS:
        raise ValueError(f"El rango máximo es de {DIAS_MAXIMOS} días")
    return desde, hasta


# ---------- Reglas ----------

def validar_reglas(datos: dict) -> dict:
    """Reglas limpias a partir de lo capturado. ValueError con el motivo si algo no vale."""
    try:
        acos = Decimal(str(datos.get("target_acos")))
    except (InvalidOperation, ValueError):
        raise ValueError("El ACOS objetivo debe ser un número")
    if not acos.is_finite() or acos <= 0 or acos > 100:
        raise ValueError("El ACOS objetivo debe ser mayor a 0 y hasta 100 %")

    clics = datos.get("max_clicks_without_sale")
    if isinstance(clics, bool) or not isinstance(clics, int) or clics < 1 or clics > 10000:
        raise ValueError("Los clics sin venta deben ser un entero entre 1 y 10,000")

    try:
        gasto = Decimal(str(datos.get("min_spend_threshold")))
    except (InvalidOperation, ValueError):
        raise ValueError("La inversión mínima debe ser un número")
    if not gasto.is_finite() or gasto < 0 or gasto > 1_000_000:
        raise ValueError("La inversión mínima debe estar entre $0 y $1,000,000")

    piloto = datos.get("auto_pilot")
    if not isinstance(piloto, bool):
        raise ValueError("El piloto automático debe ser verdadero o falso")

    return {
        "target_acos": _redondear(acos),
        "max_clicks_without_sale": clics,
        "min_spend_threshold": _redondear(gasto),
        "auto_pilot": piloto,
    }


def estado_efectivo(estado_motor: str, auto_pilot: bool) -> str:
    """Lo que ve el usuario: la pausa manda sobre el piloto, y el piloto sobre «activo»."""
    if estado_motor == MOTOR_PAUSA:
        return ESTADO_PAUSA
    return ESTADO_PILOTO if auto_pilot else ESTADO_ACTIVO


# ---------- Publicaciones ----------

def normalizar_publicacion(crudo: dict) -> dict:
    """Una fila de la API de Mercado Ads → publicación con métricas numéricas y sin ruido.

    Tolera campos ausentes (cuentas sin ventas) y las dos formas de traer las ventas
    (total_amount, o directas + indirectas).
    """
    metricas = crudo.get("metrics") or {}
    ventas = _dec(metricas.get("total_amount"))
    if ventas == 0:
        ventas = _dec(metricas.get("direct_amount")) + _dec(metricas.get("indirect_amount"))
    unidades = _dec(metricas.get("units_quantity"))
    if unidades == 0:
        unidades = _dec(metricas.get("direct_units_quantity")) + _dec(metricas.get("indirect_units_quantity"))
    estado = str(crudo.get("status") or "").strip().lower()
    return {
        "item_id": str(crudo.get("item_id") or crudo.get("id") or "").strip(),
        "titulo": str(crudo.get("title") or "").strip(),
        "imagen": str(crudo.get("thumbnail") or crudo.get("image") or "").strip(),
        "estado": estado,
        "campana_id": crudo.get("campaign_id"),
        "impresiones": int(_dec(metricas.get("prints"))),
        "clics": int(_dec(metricas.get("clicks"))),
        "unidades": int(unidades),
        "inversion": _dec(metricas.get("cost")),
        "ventas": ventas,
    }


def _acos(pub: dict) -> Decimal | None:
    return _porcentaje(pub["inversion"], pub["ventas"])


def diagnosticar(pub: dict, reglas: dict) -> dict:
    """{accion, semaforo, motivo, aplicable} de una publicación normalizada.

    Orden de las reglas (la primera que aplica gana):
      pausado         → «activar» solo si rindió bien en el periodo; si no, mantener.
      sin ventas      → «pausar» con clics ≥ límite; con poca inversión, «mantener / sin datos».
      inversión baja  → mantener (no hay base para evaluar).
      ACOS > objetivo → bajar puja.  ACOS ≤ objetivo × 0.7 → subir puja.  Si no, mantener.
    """
    objetivo = _dec(reglas["target_acos"])
    limite_clics = int(reglas["max_clicks_without_sale"])
    minimo = _dec(reglas["min_spend_threshold"])
    acos = _acos(pub)
    sin_ventas = pub["ventas"] <= 0 and pub["unidades"] <= 0

    def resultado(accion, semaforo, motivo):
        return {"accion": accion, "semaforo": semaforo, "motivo": motivo, "aplicable": accion in ACCIONES_APLICABLES}

    if pub["estado"] == PAUSADO:
        if acos is not None and pub["inversion"] >= minimo and acos <= objetivo * FACTOR_ESCALAR:
            return resultado(ACTIVAR, VERDE, f"Pausado, pero rindió con ACOS {_redondear(acos)} % (objetivo {_redondear(objetivo)} %)")
        return resultado(MANTENER, AMARILLO, "Pausado")

    if pub["estado"] != ACTIVO:
        return resultado(MANTENER, AMARILLO, f"Estado «{pub['estado'] or 'desconocido'}»: sin acción")

    if sin_ventas:
        if pub["clics"] >= limite_clics:
            return resultado(PAUSAR, ROJO, f"{pub['clics']} clics sin ninguna venta (límite {limite_clics})")
        return resultado(MANTENER, AMARILLO, "Sin ventas todavía: sin datos suficientes para evaluar")

    if pub["inversion"] < minimo:
        return resultado(MANTENER, AMARILLO, f"Inversión menor al mínimo de evaluación (${_redondear(minimo)})")

    if acos is None:  # con ventas > 0 no debería pasar; defensivo
        return resultado(MANTENER, AMARILLO, "Sin ACOS calculable")
    if acos > objetivo:
        return resultado(BAJAR_PUJA, ROJO, f"ACOS {_redondear(acos)} % sobre el objetivo de {_redondear(objetivo)} %")
    if acos <= objetivo * FACTOR_ESCALAR:
        return resultado(SUBIR_PUJA, VERDE, f"ACOS {_redondear(acos)} % holgado frente al objetivo de {_redondear(objetivo)} %")
    return resultado(MANTENER, AMARILLO, f"ACOS {_redondear(acos)} % dentro del objetivo")


def publicacion_json(pub: dict, diagnostico: dict) -> dict:
    """Publicación + diagnóstico lista para JSON (floats redondeados, métricas derivadas)."""
    return {
        "item_id": pub["item_id"],
        "titulo": pub["titulo"],
        "imagen": pub["imagen"],
        "estado": pub["estado"],
        "campana_id": pub["campana_id"],
        "impresiones": pub["impresiones"],
        "clics": pub["clics"],
        "unidades": pub["unidades"],
        "inversion": _redondear(pub["inversion"]),
        "ventas": _redondear(pub["ventas"]),
        "acos": _redondear(_acos(pub)),
        "diagnostico": diagnostico,
    }


def evaluar(crudos: list, reglas: dict) -> tuple[list, list]:
    """(publicaciones normalizadas, publicaciones en JSON con diagnóstico). Descarta filas sin item_id."""
    pubs = [p for p in (normalizar_publicacion(c) for c in crudos) if p["item_id"]]
    return pubs, [publicacion_json(p, diagnosticar(p, reglas)) for p in pubs]


# ---------- Totales ----------

def agregar_kpis(pubs: list, evaluadas: list) -> dict:
    """KPIs globales. ACOS/ROAS/CVR en null cuando el denominador es 0 (no 0 inventado)."""
    inversion = sum((p["inversion"] for p in pubs), Decimal(0))
    ventas = sum((p["ventas"] for p in pubs), Decimal(0))
    clics = sum(p["clics"] for p in pubs)
    unidades = sum(p["unidades"] for p in pubs)
    impresiones = sum(p["impresiones"] for p in pubs)
    return {
        "inversion": _redondear(inversion),
        "ventas": _redondear(ventas),
        "acos": _redondear(_porcentaje(inversion, ventas)),
        "roas": _redondear(ventas / inversion) if inversion > 0 else None,
        "clics": clics,
        "impresiones": impresiones,
        "cvr": _redondear(_porcentaje(Decimal(unidades), Decimal(clics))),
        "publicaciones": len(pubs),
        "en_alerta": sum(1 for e in evaluadas if e["diagnostico"]["semaforo"] == ROJO),
        "por_pausar": sum(1 for e in evaluadas if e["diagnostico"]["accion"] == PAUSAR),
    }


# ---------- Piloto automático ----------

def pausas_a_aplicar(evaluadas: list, tope: int) -> tuple[list, list]:
    """(a pausar, omitidas por el tope). El piloto solo pausa; las de más clics sin venta van primero."""
    candidatas = [e for e in evaluadas if e["diagnostico"]["accion"] == PAUSAR and e["estado"] == ACTIVO]
    candidatas.sort(key=lambda e: (-e["clics"], e["item_id"]))
    tope = max(0, int(tope))
    return candidatas[:tope], candidatas[tope:]
