# Servicio de integracion con Banxico (tipo de cambio USD/MXN, serie SF43718)
import os
from datetime import date, datetime, timedelta
import httpx
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

BANXICO_URL = "https://www.banxico.org.mx/SieAPIRest/service/v1/series/SF43718/datos/oportuno"
BANXICO_URL_RANGO = "https://www.banxico.org.mx/SieAPIRest/service/v1/series/SF43718/datos"

def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )


class BanxicoServiceError(Exception):
    """Se lanza cuando Banxico no responde o la respuesta viene rara."""
    pass


def _leer_cache_hoy():
    hoy = date.today()
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT valor, fecha_dato FROM tipo_cambio_cache WHERE fecha_consulta = %s",
            (hoy,)
        )
        return cursor.fetchone()
    except mysql.connector.Error as err:
        print(f"Error leyendo cache tipo_cambio: {err}")
        return None
    finally:
        cursor.close()
        conn.close()


def _guardar_cache_hoy(valor: float, fecha_dato: date):
    hoy = date.today()
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO tipo_cambio_cache (fecha_consulta, valor, fecha_dato)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE valor = VALUES(valor), fecha_dato = VALUES(fecha_dato),
                consultado_en = CURRENT_TIMESTAMP
            """,
            (hoy, valor, fecha_dato)
        )
        conn.commit()
    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error guardando cache tipo_cambio: {err}")
    finally:
        cursor.close()
        conn.close()


def _extraer_dato(data: dict, indice: int) -> tuple[float, date]:
    datos = data["bmx"]["series"][0]["datos"]
    if not datos:
        raise BanxicoServiceError("Banxico no devolvio datos para la serie SF43718")
    dato = datos[indice]
    valor = float(str(dato["dato"]).replace(",", ""))
    fecha_dato = datetime.strptime(dato["fecha"], "%d/%m/%Y").date()
    return valor, fecha_dato


async def _get_banxico(url: str, token: str) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={"token": token}, headers={"Accept": "application/json"})
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPStatusError as err:
        print(f"Error HTTP Banxico: {err}")
        raise BanxicoServiceError(f"Banxico respondio con error: {err.response.status_code}")
    except httpx.RequestError as err:
        print(f"Error de conexion con Banxico: {err}")
        raise BanxicoServiceError("No se pudo conectar con Banxico")


async def _consultar_banxico() -> tuple[float, date]:
    token = os.getenv("BANXICO_API_TOKEN")
    if not token:
        raise BanxicoServiceError("Falta BANXICO_API_TOKEN en el .env")

    data = await _get_banxico(BANXICO_URL, token)
    try:
        return _extraer_dato(data, 0)
    except (KeyError, IndexError, ValueError) as err:
        print(f"Respuesta inesperada de Banxico: {err} -- {data}")
        raise BanxicoServiceError("Respuesta de Banxico con formato inesperado")


async def obtener_tipo_cambio_dia() -> tuple[float, date]:
    """
    Devuelve (valor, fecha_dato) del tipo de cambio FIX del dia.
    Cachea por dia de consulta para no golpear la API de Banxico en cada request.
    Si Banxico no tiene dato para hoy (fin de semana/festivo), el endpoint
    'oportuno' ya regresa el dato disponible mas reciente.
    """
    cacheado = _leer_cache_hoy()
    if cacheado:
        return float(cacheado["valor"]), cacheado["fecha_dato"]

    valor, fecha_dato = await _consultar_banxico()
    _guardar_cache_hoy(valor, fecha_dato)
    return valor, fecha_dato


def _leer_cache_historico(fecha_solicitada: date):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT valor, fecha_dato FROM tipo_cambio_historico WHERE fecha_solicitada = %s",
            (fecha_solicitada,)
        )
        return cursor.fetchone()
    except mysql.connector.Error as err:
        print(f"Error leyendo cache tipo_cambio_historico: {err}")
        return None
    finally:
        cursor.close()
        conn.close()


def _guardar_cache_historico(fecha_solicitada: date, valor: float, fecha_dato: date):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO tipo_cambio_historico (fecha_solicitada, valor, fecha_dato)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE valor = VALUES(valor), fecha_dato = VALUES(fecha_dato),
                consultado_en = CURRENT_TIMESTAMP
            """,
            (fecha_solicitada, valor, fecha_dato)
        )
        conn.commit()
    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error guardando cache tipo_cambio_historico: {err}")
    finally:
        cursor.close()
        conn.close()


async def obtener_tipo_cambio_fecha_especifica(fecha: date) -> tuple[float, date]:
    """
    Tipo de cambio FIX de referencia para una fecha especifica (o el dato
    disponible mas reciente antes de esa fecha, si cae en fin de semana o
    dia festivo). Es puramente informativo/de auditoria: nunca se usa para
    calcular montos, que se capturan directo en MXN.
    Cachea por fecha solicitada (un valor historico ya publicado no cambia).
    """
    cacheado = _leer_cache_historico(fecha)
    if cacheado:
        return float(cacheado["valor"]), cacheado["fecha_dato"]

    token = os.getenv("BANXICO_API_TOKEN")
    if not token:
        raise BanxicoServiceError("Falta BANXICO_API_TOKEN en el .env")

    fecha_inicio = fecha - timedelta(days=10)
    url = f"{BANXICO_URL_RANGO}/{fecha_inicio.isoformat()}/{fecha.isoformat()}"
    data = await _get_banxico(url, token)
    try:
        valor, fecha_dato = _extraer_dato(data, -1)
    except (KeyError, IndexError, ValueError) as err:
        print(f"Respuesta inesperada de Banxico: {err} -- {data}")
        raise BanxicoServiceError("Respuesta de Banxico con formato inesperado")

    _guardar_cache_historico(fecha, valor, fecha_dato)
    return valor, fecha_dato
