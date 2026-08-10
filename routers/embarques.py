import os
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional

import mysql.connector
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator

import mov_reg
from banxico_service import BanxicoServiceError, obtener_tipo_cambio_dia, obtener_tipo_cambio_fecha_especifica

router = APIRouter(tags=["/embarques"], responses={404: {"Mensaje": "No encontrado"}})
load_dotenv()


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )


_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "embarques_schema.sql")

# Columnas nuevas de embarque_etapas (fecha_pago/monto_mxn/tipo_cambio_referencia/
# fecha_captura reemplazan a fecha_registro/tipo_cambio_usd_mxn). "ADD COLUMN IF NOT
# EXISTS" requiere MySQL 8.0.29+ y truena (1064) en servidores viejos, asi que la
# migracion se hace a mano contra INFORMATION_SCHEMA en vez de en el .sql crudo.
_COLUMNAS_NUEVAS_EMBARQUE_ETAPAS = [
    ("fecha_pago", "DATE NULL AFTER completado"),
    ("monto_mxn", "DECIMAL(12,2) NULL AFTER fecha_pago"),
    ("tipo_cambio_referencia", "DECIMAL(10,4) NULL AFTER monto_mxn"),
    ("fecha_captura", "TIMESTAMP NULL AFTER tipo_cambio_referencia"),
]
_COLUMNAS_VIEJAS_EMBARQUE_ETAPAS = ["fecha_registro", "tipo_cambio_usd_mxn"]

# fecha real de arribo (distinta de llegada_manzanillo_tentativa, que es la estimada).
_COLUMNAS_NUEVAS_EMBARQUES = [
    ("fecha_llegada_real", "DATE NULL AFTER llegada_manzanillo_tentativa"),
]


def _columna_existe(cursor, tabla: str, columna: str) -> bool:
    cursor.execute(
        "SELECT 1 FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (tabla, columna)
    )
    return cursor.fetchone() is not None


def _migrar_columnas_embarques(cursor):
    for columna, definicion in _COLUMNAS_NUEVAS_EMBARQUE_ETAPAS:
        if not _columna_existe(cursor, "embarque_etapas", columna):
            cursor.execute(f"ALTER TABLE embarque_etapas ADD COLUMN {columna} {definicion}")
            print(f"Columna embarque_etapas.{columna} agregada.")

    for columna in _COLUMNAS_VIEJAS_EMBARQUE_ETAPAS:
        if _columna_existe(cursor, "embarque_etapas", columna):
            cursor.execute(f"ALTER TABLE embarque_etapas DROP COLUMN {columna}")
            print(f"Columna embarque_etapas.{columna} eliminada.")

    for columna, definicion in _COLUMNAS_NUEVAS_EMBARQUES:
        if not _columna_existe(cursor, "embarques", columna):
            cursor.execute(f"ALTER TABLE embarques ADD COLUMN {columna} {definicion}")
            print(f"Columna embarques.{columna} agregada.")


def crear_tablas_embarques():
    """
    Corre el schema (CREATE TABLE IF NOT EXISTS, sin FK) y la migracion de
    columnas de embarque_etapas al arrancar el backend. Idempotente: no
    rompe si las tablas/columnas ya estan en su forma final.
    """
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        lineas = [l for l in f if not l.strip().startswith("--")]
    statements = [s.strip() for s in "".join(lineas).split(";") if s.strip()]

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        for stmt in statements:
            cursor.execute(stmt)
        _migrar_columnas_embarques(cursor)
        conn.commit()
        print("Tablas de embarques verificadas/creadas.")
    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error creando tablas de embarques: {err}")
    finally:
        cursor.close()
        conn.close()


class EtapaTipo(str, Enum):
    ANTICIPO_CHINA = "ANTICIPO_CHINA"
    LIQUIDADO_CHINA = "LIQUIDADO_CHINA"
    HL_LIQUIDADA = "HL_LIQUIDADA"


class EstatusTipo(str, Enum):
    CON_FORWARDER = "CON_FORWARDER"
    SALIO_DE_CHINA = "SALIO_DE_CHINA"


# ---- Schemas ----

class EmbarqueItemIn(BaseModel):
    sku: str
    qty: int
    cbm: Optional[float] = None
    pct_contenedor: Optional[float] = None


class EmbarqueCabeceraIn(BaseModel):
    numero_contenedor: Optional[str] = None
    invoice_orders: str
    proveedor: Optional[str] = None
    llegada_manzanillo_tentativa: Optional[date] = None
    fecha_llegada_real: Optional[date] = None
    fecha_de_recibido: Optional[date] = None
    usuario: Optional[str] = "sistema"


class EmbarqueCrear(EmbarqueCabeceraIn):
    items: List[EmbarqueItemIn] = []


class EtapaUpdate(BaseModel):
    completado: bool
    fecha_pago: Optional[date] = None
    monto_mxn: Optional[float] = None
    nota: Optional[str] = None
    usuario: Optional[str] = "sistema"

    @field_validator('fecha_pago')
    @classmethod
    def fecha_no_futura(cls, v):
        if v and v > date.today():
            raise ValueError("La fecha de pago no puede ser futura")
        return v

    @field_validator('monto_mxn')
    @classmethod
    def monto_positivo(cls, v):
        if v is not None and v <= 0:
            raise ValueError("El monto debe ser mayor a 0")
        return v


class EtapaResponse(BaseModel):
    id: int
    embarque_id: int
    tipo: str
    completado: bool
    fecha_pago: Optional[date] = None
    monto_mxn: Optional[float] = None
    tipo_cambio_referencia: Optional[Decimal] = None
    fecha_captura: Optional[datetime] = None
    nota: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class EstatusUpdate(BaseModel):
    activo: bool
    fecha_registro: Optional[date] = None
    usuario: Optional[str] = "sistema"


# ---- Helpers ----

def _obtener_embarque_detalle(embarque_id: int):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM embarques WHERE id = %s", (embarque_id,))
        embarque = cursor.fetchone()
        if not embarque:
            return None

        cursor.execute(
            "SELECT id, sku, qty, cbm, pct_contenedor FROM embarque_items WHERE embarque_id = %s",
            (embarque_id,)
        )
        embarque["items"] = cursor.fetchall()

        cursor.execute(
            "SELECT id, embarque_id, tipo, completado, fecha_pago, monto_mxn, "
            "tipo_cambio_referencia, fecha_captura, nota FROM embarque_etapas WHERE embarque_id = %s",
            (embarque_id,)
        )
        embarque["etapas"] = cursor.fetchall()

        cursor.execute(
            "SELECT tipo, activo, fecha_registro FROM embarque_estatus WHERE embarque_id = %s",
            (embarque_id,)
        )
        embarque["estatus"] = cursor.fetchall()

        return embarque
    finally:
        cursor.close()
        conn.close()


# ---- Endpoints cabecera ----

@router.get("/embarques")
async def listar_embarques(
    proveedor: Optional[str] = None,
    numero_contenedor: Optional[str] = None,
    con_forwarder: Optional[bool] = None,
    salio_de_china: Optional[bool] = None
):
    """
    Lista embarques con filtros opcionales de proveedor, contenedor y estatus.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    query = """
        SELECT e.*,
            cf.activo AS con_forwarder, cf.fecha_registro AS con_forwarder_fecha,
            sc.activo AS salio_de_china, sc.fecha_registro AS salio_de_china_fecha,
            (SELECT COUNT(*) FROM embarque_items i WHERE i.embarque_id = e.id) AS items_count
        FROM embarques e
        LEFT JOIN embarque_estatus cf ON cf.embarque_id = e.id AND cf.tipo = 'CON_FORWARDER'
        LEFT JOIN embarque_estatus sc ON sc.embarque_id = e.id AND sc.tipo = 'SALIO_DE_CHINA'
        WHERE 1=1
    """
    valores = []

    if proveedor:
        query += " AND e.proveedor LIKE %s"
        valores.append(f"%{proveedor}%")
    if numero_contenedor:
        query += " AND e.numero_contenedor LIKE %s"
        valores.append(f"%{numero_contenedor}%")
    if con_forwarder is not None:
        query += " AND cf.activo = %s"
        valores.append(con_forwarder)
    if salio_de_china is not None:
        query += " AND sc.activo = %s"
        valores.append(salio_de_china)

    query += " ORDER BY e.created_at DESC"

    try:
        cursor.execute(query, tuple(valores))
        return cursor.fetchall()
    except mysql.connector.Error as err:
        print(f"Error en BD: {err}")
        raise HTTPException(status_code=500, detail="Error al consultar embarques")
    finally:
        cursor.close()
        conn.close()


@router.get("/embarques/{embarque_id}")
async def obtener_embarque(embarque_id: int):
    """
    Detalle de un embarque con sus SKUs, etapas y estatus.
    """
    embarque = _obtener_embarque_detalle(embarque_id)
    if not embarque:
        raise HTTPException(status_code=404, detail="Embarque no encontrado")
    return embarque


@router.post("/embarques")
async def crear_embarque(payload: EmbarqueCrear):
    """
    Crea un embarque (cabecera + SKUs) y siembra sus etapas/estatus en falso.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            INSERT INTO embarques
                (numero_contenedor, invoice_orders, proveedor, llegada_manzanillo_tentativa,
                 fecha_llegada_real, fecha_de_recibido)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                payload.numero_contenedor, payload.invoice_orders, payload.proveedor,
                payload.llegada_manzanillo_tentativa, payload.fecha_llegada_real, payload.fecha_de_recibido
            )
        )
        embarque_id = cursor.lastrowid

        for etapa in EtapaTipo:
            cursor.execute(
                "INSERT INTO embarque_etapas (embarque_id, tipo) VALUES (%s, %s)",
                (embarque_id, etapa.value)
            )

        for estatus in EstatusTipo:
            cursor.execute(
                "INSERT INTO embarque_estatus (embarque_id, tipo) VALUES (%s, %s)",
                (embarque_id, estatus.value)
            )

        for item in payload.items:
            cursor.execute(
                "INSERT INTO embarque_items (embarque_id, sku, qty, cbm, pct_contenedor) VALUES (%s, %s, %s, %s, %s)",
                (embarque_id, item.sku, item.qty, item.cbm, item.pct_contenedor)
            )

        conn.commit()
        mov_reg.registrar_movimiento(payload.usuario, f"Creo embarque #{embarque_id}", "Importaciones")
        return _obtener_embarque_detalle(embarque_id)

    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error en BD: {err}")
        raise HTTPException(status_code=500, detail="Error al crear embarque")
    finally:
        cursor.close()
        conn.close()


@router.put("/embarques/{embarque_id}")
async def editar_embarque(embarque_id: int, payload: EmbarqueCabeceraIn):
    """
    Edita la cabecera de un embarque.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            UPDATE embarques
            SET numero_contenedor = %s, invoice_orders = %s, proveedor = %s,
                llegada_manzanillo_tentativa = %s, fecha_llegada_real = %s, fecha_de_recibido = %s
            WHERE id = %s
            """,
            (
                payload.numero_contenedor, payload.invoice_orders, payload.proveedor,
                payload.llegada_manzanillo_tentativa, payload.fecha_llegada_real,
                payload.fecha_de_recibido, embarque_id
            )
        )
        conn.commit()

        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Embarque no encontrado")

        mov_reg.registrar_movimiento(payload.usuario, f"Edito embarque #{embarque_id}", "Importaciones")
        return _obtener_embarque_detalle(embarque_id)

    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error en BD: {err}")
        raise HTTPException(status_code=500, detail="Error al editar embarque")
    finally:
        cursor.close()
        conn.close()


@router.delete("/embarques/{embarque_id}")
async def eliminar_embarque(embarque_id: int):
    """
    Elimina un embarque y arrastra items, etapas y estatus.
    No hay FK en DB (usuario sin privilegio REFERENCES), asi que el cascade se hace aqui.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("DELETE FROM embarque_items WHERE embarque_id = %s", (embarque_id,))
        cursor.execute("DELETE FROM embarque_etapas WHERE embarque_id = %s", (embarque_id,))
        cursor.execute("DELETE FROM embarque_estatus WHERE embarque_id = %s", (embarque_id,))
        cursor.execute("DELETE FROM embarques WHERE id = %s", (embarque_id,))
        conn.commit()

        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Embarque no encontrado")

        mov_reg.registrar_movimiento("sistema", f"Elimino embarque #{embarque_id}", "Importaciones")
        return {"mensaje": "Embarque eliminado exitosamente"}

    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error en BD: {err}")
        raise HTTPException(status_code=500, detail="Error al eliminar embarque")
    finally:
        cursor.close()
        conn.close()


# ---- Endpoints items (SKUs) ----

@router.post("/embarques/{embarque_id}/items")
async def agregar_item(embarque_id: int, item: EmbarqueItemIn):
    """
    Agrega un SKU a un embarque existente.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("SELECT id FROM embarques WHERE id = %s", (embarque_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Embarque no encontrado")

        cursor.execute(
            "INSERT INTO embarque_items (embarque_id, sku, qty, cbm, pct_contenedor) VALUES (%s, %s, %s, %s, %s)",
            (embarque_id, item.sku, item.qty, item.cbm, item.pct_contenedor)
        )
        conn.commit()
        return {"mensaje": "Item agregado exitosamente", "item_id": cursor.lastrowid}

    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error en BD: {err}")
        raise HTTPException(status_code=500, detail="Error al agregar item")
    finally:
        cursor.close()
        conn.close()


@router.put("/embarques/{embarque_id}/items/{item_id}")
async def editar_item(embarque_id: int, item_id: int, item: EmbarqueItemIn):
    """
    Edita un SKU de un embarque.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "UPDATE embarque_items SET sku = %s, qty = %s, cbm = %s, pct_contenedor = %s "
            "WHERE id = %s AND embarque_id = %s",
            (item.sku, item.qty, item.cbm, item.pct_contenedor, item_id, embarque_id)
        )
        conn.commit()

        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Item no encontrado en este embarque")

        return {"mensaje": "Item actualizado exitosamente"}

    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error en BD: {err}")
        raise HTTPException(status_code=500, detail="Error al editar item")
    finally:
        cursor.close()
        conn.close()


@router.delete("/embarques/{embarque_id}/items/{item_id}")
async def eliminar_item(embarque_id: int, item_id: int):
    """
    Elimina un SKU de un embarque.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute(
            "DELETE FROM embarque_items WHERE id = %s AND embarque_id = %s",
            (item_id, embarque_id)
        )
        conn.commit()

        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Item no encontrado en este embarque")

        return {"mensaje": "Item eliminado exitosamente"}

    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error en BD: {err}")
        raise HTTPException(status_code=500, detail="Error al eliminar item")
    finally:
        cursor.close()
        conn.close()


# ---- Endpoints etapas (ANTICIPO_CHINA, LIQUIDADO_CHINA, HL_LIQUIDADA) ----

@router.patch("/embarques/{embarque_id}/etapas/{tipo}", response_model=EtapaResponse)
async def marcar_etapa(embarque_id: int, tipo: EtapaTipo, payload: EtapaUpdate):
    """
    Marca/desmarca una etapa de liquidacion. El monto se captura tal cual en
    MXN (sin conversion de USD); el tipo de cambio de la fecha de pago se
    guarda solo como referencia de auditoria, nunca se usa para calcularlo.
    """
    if payload.completado and (payload.fecha_pago is None or payload.monto_mxn is None):
        raise HTTPException(status_code=400, detail="fecha_pago y monto_mxn son requeridos cuando completado=true")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("SELECT id FROM embarques WHERE id = %s", (embarque_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Embarque no encontrado")

        cursor.execute(
            "SELECT id, fecha_pago, tipo_cambio_referencia, nota FROM embarque_etapas "
            "WHERE embarque_id = %s AND tipo = %s",
            (embarque_id, tipo.value)
        )
        etapa = cursor.fetchone()
        if not etapa:
            raise HTTPException(status_code=404, detail="Etapa no encontrada")

        nota_final = payload.nota if payload.nota is not None else etapa["nota"]
        fecha_captura = datetime.now()

        if payload.completado:
            # Solo se vuelve a consultar Banxico si cambio la fecha de pago; si solo
            # cambio el monto, se reusa la referencia ya guardada (nunca se recalcula).
            if etapa["fecha_pago"] == payload.fecha_pago and etapa["tipo_cambio_referencia"] is not None:
                tipo_cambio_referencia = etapa["tipo_cambio_referencia"]
            else:
                try:
                    tipo_cambio_referencia, _ = await obtener_tipo_cambio_fecha_especifica(payload.fecha_pago)
                except BanxicoServiceError as err:
                    raise HTTPException(status_code=502, detail=f"No se pudo obtener tipo de cambio de referencia: {err}")

            cursor.execute(
                "UPDATE embarque_etapas SET completado = TRUE, fecha_pago = %s, monto_mxn = %s, "
                "tipo_cambio_referencia = %s, fecha_captura = %s, nota = %s WHERE id = %s",
                (payload.fecha_pago, payload.monto_mxn, tipo_cambio_referencia, fecha_captura, nota_final, etapa["id"])
            )
        else:
            # fecha_captura NO se toca al desmarcar: queda como auditoria de la ultima captura real.
            cursor.execute(
                "UPDATE embarque_etapas SET completado = FALSE, fecha_pago = NULL, monto_mxn = NULL, "
                "tipo_cambio_referencia = NULL, nota = %s WHERE id = %s",
                (nota_final, etapa["id"])
            )

        conn.commit()
        mov_reg.registrar_movimiento(
            payload.usuario, f"Actualizo etapa {tipo.value} de embarque #{embarque_id}", "Importaciones"
        )

        cursor.execute(
            "SELECT id, embarque_id, tipo, completado, fecha_pago, monto_mxn, "
            "tipo_cambio_referencia, fecha_captura, nota FROM embarque_etapas WHERE id = %s",
            (etapa["id"],)
        )
        return cursor.fetchone()

    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error en BD: {err}")
        raise HTTPException(status_code=500, detail="Error al actualizar etapa")
    finally:
        cursor.close()
        conn.close()


# ---- Endpoints estatus (CON_FORWARDER, SALIO_DE_CHINA) ----

@router.patch("/embarques/{embarque_id}/estatus/{tipo}")
async def marcar_estatus(embarque_id: int, tipo: EstatusTipo, payload: EstatusUpdate):
    """
    Marca/desmarca CON_FORWARDER o SALIO_DE_CHINA.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("SELECT id FROM embarques WHERE id = %s", (embarque_id,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Embarque no encontrado")

        cursor.execute(
            "SELECT id, fecha_registro FROM embarque_estatus WHERE embarque_id = %s AND tipo = %s",
            (embarque_id, tipo.value)
        )
        estatus = cursor.fetchone()
        if not estatus:
            raise HTTPException(status_code=404, detail="Estatus no encontrado")

        if payload.activo:
            fecha_final = payload.fecha_registro or estatus["fecha_registro"] or date.today()
            cursor.execute(
                "UPDATE embarque_estatus SET activo = TRUE, fecha_registro = %s WHERE id = %s",
                (fecha_final, estatus["id"])
            )
        else:
            cursor.execute(
                "UPDATE embarque_estatus SET activo = FALSE, fecha_registro = NULL WHERE id = %s",
                (estatus["id"],)
            )

        conn.commit()
        mov_reg.registrar_movimiento(
            payload.usuario, f"Actualizo estatus {tipo.value} de embarque #{embarque_id}", "Importaciones"
        )

        cursor.execute(
            "SELECT tipo, activo, fecha_registro FROM embarque_estatus WHERE id = %s",
            (estatus["id"],)
        )
        return cursor.fetchone()

    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error en BD: {err}")
        raise HTTPException(status_code=500, detail="Error al actualizar estatus")
    finally:
        cursor.close()
        conn.close()


# ---- Tipo de cambio ----

@router.get("/tipo-cambio/hoy")
async def tipo_cambio_hoy():
    """
    Tipo de cambio USD/MXN del dia (Banxico SF43718, con cache diaria).
    """
    try:
        valor, fecha = await obtener_tipo_cambio_dia()
        return {"valor": valor, "fecha": fecha}
    except BanxicoServiceError as err:
        raise HTTPException(status_code=502, detail=f"No se pudo obtener tipo de cambio: {err}")


@router.get("/tipo-cambio/{fecha}")
async def tipo_cambio_fecha(fecha: date):
    """
    Tipo de cambio USD/MXN de referencia para una fecha especifica (o el dato
    disponible mas reciente antes de esa fecha). Solo para previsualizar en el
    formulario antes de guardar el pago de una etapa; no se usa en calculos.
    """
    try:
        valor, fecha_dato = await obtener_tipo_cambio_fecha_especifica(fecha)
        return {"valor": valor, "fecha": fecha_dato}
    except BanxicoServiceError as err:
        raise HTTPException(status_code=502, detail=f"No se pudo obtener tipo de cambio de referencia: {err}")
