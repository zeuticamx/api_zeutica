# Comisiones por SKU para vendedores.
#
# - Gerencia (permisos.es_gerencia) edita la matriz vendedor × SKU y ve todo.
# - Un vendedor solo ve y vincula lo suyo. El usuario SIEMPRE sale del token.
# - Solo venta directa: Cleanest, Mercado Libre y Amazon no comisionan
#   (ver comisiones_calc.canal_excluido).
# - Las comisiones se calculan al registrar la venta (hook desde routers/ventas.py)
#   y se guardan congeladas en comisiones_ventas.
import os
from datetime import date
from decimal import Decimal
from typing import List, Optional

import mysql.connector
from mysql.connector import errorcode
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

import comisiones_calc as calc
import mov_reg
from permisos import es_gerencia, requerir_gerencia, usuario_autenticado
from routers import crm

router = APIRouter(tags=["/comisiones"], responses={404: {"Mensaje": "No encontrado"}})
load_dotenv()

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sql", "comisiones_schema.sql")


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )


def crear_tablas_comisiones():
    """Crea las tablas de comisiones si no existen (se llama en el lifespan). Idempotente."""
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        lineas = [l for l in f if not l.strip().startswith("--")]
    statements = [s.strip() for s in "".join(lineas).split(";") if s.strip()]

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        for stmt in statements:
            cursor.execute(stmt)
        # Instalaciones anteriores al vínculo por folio de cotización
        for migracion in (
            "ALTER TABLE venta_seguimiento MODIFY seguimiento_id INT NULL",
            "ALTER TABLE venta_seguimiento ADD COLUMN cotizacion VARCHAR(40) NULL AFTER seguimiento_id",
            "ALTER TABLE venta_seguimiento ADD INDEX idx_venta_seguimiento_cot (cotizacion)",
        ):
            try:
                cursor.execute(migracion)
            except mysql.connector.Error as err:
                if err.errno not in (errorcode.ER_DUP_FIELDNAME, errorcode.ER_DUP_KEYNAME):
                    raise
        conn.commit()
        print("Tablas de comisiones verificadas/creadas.")
    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error creando tablas de comisiones: {err}")
    finally:
        cursor.close()
        conn.close()


def _error_db(conn, err, contexto: str):
    try:
        conn.rollback()
    except Exception:
        pass
    print(f"Error DB comisiones ({contexto}): {err}")
    raise HTTPException(status_code=500, detail=f"Error en DB: {err}")


# ---------- Captura de la venta (núcleo, recibe cursor) ----------

def _fecha(valor) -> date:
    if isinstance(valor, date):
        return valor
    try:
        return date.fromisoformat(str(valor)[:10])
    except ValueError:
        return crm.hoy_mx()


def _resolver_cliente(cursor, comprador) -> Optional[int]:
    """clientes.id del comprador por nombre exacto; None si no existe o es ambiguo."""
    nombre = str(comprador or "").strip()
    if not nombre:
        return None
    cursor.execute("SELECT id FROM clientes WHERE TRIM(nombre) = %s LIMIT 2", (nombre,))
    filas = cursor.fetchall()
    return filas[0]["id"] if len(filas) == 1 else None


def _cerrar_como_ganado(cursor, cliente_id: int, usuario: str):
    """El seguimiento se cierra con la venta: cierra los abiertos del cliente y pasa la etapa a 'ganado'."""
    cursor.execute(
        "UPDATE crm_interacciones SET seguimiento_cerrado = 1 WHERE cliente_id = %s AND seguimiento_cerrado = 0 AND eliminado = 0",
        (cliente_id,),
    )
    fila = crm._fila_cliente(cursor, cliente_id)
    if fila:
        anterior = crm._asegurar_cartera(cursor, fila, usuario)
        crm._cambiar_etapa(cursor, cliente_id, anterior, "ganado", None, usuario)


def _vincular(cursor, id_ventas, cliente_id, seguimiento_id, cotizacion, vendedor, modo):
    """Upsert del vínculo: lo que llega vacío (None) no borra lo ya enlazado."""
    cursor.execute(
        """INSERT INTO venta_seguimiento (id_ventas, cliente_id, seguimiento_id, cotizacion, vendedor, modo)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON DUPLICATE KEY UPDATE
             cliente_id = COALESCE(VALUES(cliente_id), cliente_id),
             modo = IF(VALUES(seguimiento_id) IS NULL, modo, VALUES(modo)),
             seguimiento_id = COALESCE(VALUES(seguimiento_id), seguimiento_id),
             cotizacion = COALESCE(VALUES(cotizacion), cotizacion),
             vendedor = VALUES(vendedor)""",
        (str(id_ventas), cliente_id, seguimiento_id, cotizacion, vendedor, modo),
    )


def _cotizacion_existe(cursor, folio: str):
    cursor.execute("SELECT codigo_cotizacion, usuario FROM cotizaciones WHERE codigo_cotizacion = %s", (folio,))
    return cursor.fetchone()


def registrar_comisiones(cursor, id_ventas, vendedor, comprador, plataforma, fecha, items,
                         auto_vincular: bool = True, meli_key=None, amazon_key=None, cotizacion=None) -> dict:
    """
    Calcula y guarda la comisión de cada partida de una venta directa y, si el cliente
    tiene un seguimiento abierto del vendedor, enlaza la venta con el más reciente.
    items: [{sku, producto, cantidad, precio}] con precio unitario CON IVA.
    cotizacion: folio de la cotización de la que salió la venta (si existe, queda ligado).
    No hace commit. Las ventas de canales excluidos no dejan rastro.
    """
    if calc.canal_excluido(plataforma, comprador, meli_key, amazon_key):
        return {"excluida": True, "partidas": 0, "comision": Decimal("0"), "seguimiento_id": None}

    cursor.execute("SELECT sku, porcentaje FROM comisiones_config WHERE vendedor = %s", (vendedor,))
    tasas = {f["sku"]: f["porcentaje"] for f in cursor.fetchall()}

    total, partidas = Decimal("0"), 0
    for it in items:
        pct, origen = calc.resolver_tasa(tasas, it["sku"])
        r = calc.calcular_partida(it["precio"], it["cantidad"], pct)
        cursor.execute(
            """INSERT IGNORE INTO comisiones_ventas
               (id_ventas, sku, producto, cantidad, vendedor, comprador, plataforma, fecha_venta,
                precio_neto, base_sin_iva, porcentaje, comision, origen_tasa)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (str(id_ventas), it["sku"], it.get("producto"), it["cantidad"], vendedor, comprador, plataforma,
             _fecha(fecha), r["precio_neto"], r["base_sin_iva"], r["porcentaje"], r["comision"], origen),
        )
        if cursor.rowcount:
            partidas += 1
            total += r["comision"]

    seguimiento_id = None
    if auto_vincular:
        cliente_id = _resolver_cliente(cursor, comprador)
        if cliente_id:
            cursor.execute(
                """SELECT id FROM crm_interacciones
                   WHERE cliente_id = %s AND vendedor = %s AND seguimiento_cerrado = 0 AND eliminado = 0
                   ORDER BY fecha DESC, id DESC LIMIT 1""",
                (cliente_id, vendedor),
            )
            abierto = cursor.fetchone()
            if abierto:
                seguimiento_id = abierto["id"]
                _vincular(cursor, id_ventas, cliente_id, seguimiento_id, None, vendedor, "auto")
                _cerrar_como_ganado(cursor, cliente_id, vendedor)

    folio = str(cotizacion or "").strip() or None
    if folio and not _cotizacion_existe(cursor, folio):
        folio = None
    if folio:
        _vincular(cursor, id_ventas, None, None, folio, vendedor, "manual")

    return {"excluida": False, "partidas": partidas, "comision": total, "seguimiento_id": seguimiento_id, "cotizacion": folio}


def registrar_comisiones_seguro(id_ventas, vendedor, comprador, plataforma, fecha, items, cotizacion=None):
    """
    Hook para routers/ventas.py: se llama con la venta ya confirmada. Un fallo aquí
    NUNCA debe tumbar ni duplicar una venta, así que solo se registra en consola;
    gerencia puede rellenar huecos con POST /comisiones/recalcular.
    """
    try:
        conn = get_db_connection()
    except Exception as err:
        print(f"Comisiones de la venta {id_ventas} no calculadas: {err}")
        return None
    cursor = conn.cursor(dictionary=True)
    try:
        resultado = registrar_comisiones(cursor, id_ventas, vendedor, comprador, plataforma, fecha, items, cotizacion=cotizacion)
        conn.commit()
        return resultado
    except Exception as err:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"Comisiones de la venta {id_ventas} no calculadas: {err}")
        return None
    finally:
        cursor.close()
        conn.close()


# ---------- Matriz de porcentajes ----------

class TasaItem(BaseModel):
    sku: str = Field(min_length=1, max_length=60)
    # None borra la tasa de ese SKU (vuelve a la tasa base del vendedor)
    porcentaje: Optional[Decimal] = Field(None, ge=0, le=100, max_digits=5, decimal_places=2)

    @field_validator("sku")
    @classmethod
    def sku_limpio(cls, v):
        return v.strip()


class MatrizVendedor(BaseModel):
    vendedor: str = Field(min_length=1, max_length=50)
    items: List[TasaItem] = Field(min_length=1, max_length=500)

    @field_validator("vendedor")
    @classmethod
    def vendedor_limpio(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("El vendedor es obligatorio")
        return v


@router.get("/comisiones/config")
async def comisiones_config(vendedor: Optional[str] = None, usuario: str = Depends(usuario_autenticado)):
    """Matriz de porcentajes. Gerencia: todas (o de un vendedor). Vendedor: solo la suya."""
    if not es_gerencia(usuario):
        vendedor = usuario
    where, params = "", []
    if vendedor:
        where, params = "WHERE vendedor = %s", [vendedor]
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            f"SELECT vendedor, sku, porcentaje, actualizado_por, actualizado FROM comisiones_config {where} ORDER BY vendedor, sku",
            tuple(params),
        )
        return cursor.fetchall()
    except mysql.connector.Error as err:
        _error_db(conn, err, "config")
    finally:
        cursor.close()
        conn.close()


@router.put("/comisiones/config")
async def comisiones_guardar(datos: MatrizVendedor, usuario: str = Depends(requerir_gerencia)):
    """Guarda o borra porcentajes de un vendedor (solo gerencia). sku '*' = tasa base."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT nombre_usuario FROM usuarios WHERE nombre_usuario = %s", (datos.vendedor,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail=f"El usuario '{datos.vendedor}' no existe")
        guardados = borrados = 0
        for it in datos.items:
            if it.porcentaje is None:
                cursor.execute("DELETE FROM comisiones_config WHERE vendedor = %s AND sku = %s", (datos.vendedor, it.sku))
                borrados += cursor.rowcount or 0
            else:
                cursor.execute(
                    """INSERT INTO comisiones_config (vendedor, sku, porcentaje, actualizado_por)
                       VALUES (%s, %s, %s, %s)
                       ON DUPLICATE KEY UPDATE porcentaje = VALUES(porcentaje), actualizado_por = VALUES(actualizado_por)""",
                    (datos.vendedor, it.sku, it.porcentaje, usuario),
                )
                guardados += 1
        conn.commit()
        mov_reg.registrar_movimiento(usuario, f"Actualizó comisiones de {datos.vendedor} ({guardados} guardadas, {borrados} borradas)", "Comisiones")
        return {"vendedor": datos.vendedor, "guardados": guardados, "borrados": borrados}
    except mysql.connector.Error as err:
        _error_db(conn, err, "guardar")
    finally:
        cursor.close()
        conn.close()


# ---------- Reporte ----------

@router.get("/comisiones/reporte")
async def comisiones_reporte(
    desde: Optional[date] = None,
    hasta: Optional[date] = None,
    vendedor: Optional[str] = None,
    usuario: str = Depends(usuario_autenticado),
):
    """Ventas cerradas con desglose por SKU, base sin IVA y comisión. Un vendedor solo ve las suyas."""
    desde, hasta = crm.validar_rango(desde, hasta)
    if not es_gerencia(usuario):
        vendedor = usuario
    where, params = ["cv.fecha_venta >= %s", "cv.fecha_venta <= %s"], [desde, hasta]
    if vendedor:
        where.append("cv.vendedor = %s")
        params.append(vendedor)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            f"""SELECT cv.id_ventas, cv.sku, cv.producto, cv.cantidad, cv.vendedor, cv.comprador, cv.fecha_venta,
                       cv.precio_neto, cv.base_sin_iva, cv.porcentaje, cv.comision, cv.origen_tasa,
                       vs.seguimiento_id, vs.cotizacion, vs.modo
                FROM comisiones_ventas cv LEFT JOIN venta_seguimiento vs ON vs.id_ventas = cv.id_ventas
                WHERE {' AND '.join(where)}
                ORDER BY cv.fecha_venta DESC, cv.id_ventas DESC, cv.id""",
            tuple(params),
        )
        reporte = calc.armar_reporte(cursor.fetchall())
        reporte["periodo"] = {"desde": desde, "hasta": hasta, "vendedor": vendedor}
        return reporte
    except mysql.connector.Error as err:
        _error_db(conn, err, "reporte")
    finally:
        cursor.close()
        conn.close()


# ---------- Vínculo venta ↔ seguimiento ----------

class VinculoNuevo(BaseModel):
    id_ventas: str = Field(min_length=1, max_length=40)
    seguimiento_id: Optional[int] = Field(None, gt=0)
    cotizacion: Optional[str] = Field(None, max_length=40)  # folio (codigo_cotizacion)

    @field_validator("cotizacion")
    @classmethod
    def folio_limpio(cls, v):
        return (v or "").strip() or None

    @model_validator(mode="after")
    def algo_que_vincular(self):
        if self.seguimiento_id is None and self.cotizacion is None:
            raise ValueError("Indica un seguimiento, un folio de cotización o ambos")
        return self


def _venta_autorizada(cursor, id_ventas: str, usuario: str) -> dict:
    cursor.execute(
        "SELECT vendedor, comprador FROM comisiones_ventas WHERE id_ventas = %s LIMIT 1", (id_ventas,)
    )
    venta = cursor.fetchone()
    if not venta:
        raise HTTPException(status_code=404, detail="Venta no encontrada o sin comisión (canal excluido)")
    if not crm.puede_gestionar(usuario, venta["vendedor"]):
        raise HTTPException(status_code=403, detail="Esta venta pertenece a otro vendedor")
    return venta


@router.get("/comisiones/candidatos/{id_ventas}")
async def comisiones_candidatos(id_ventas: str, usuario: str = Depends(usuario_autenticado)):
    """Seguimientos y cotizaciones del cliente de la venta (más recientes primero), para vincular a mano."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        venta = _venta_autorizada(cursor, id_ventas, usuario)
        cliente_id = _resolver_cliente(cursor, venta["comprador"])
        seguimientos = []
        if cliente_id:
            cursor.execute(
                """SELECT id, vendedor, tipo, fecha, resultado, notas, proxima_accion, seguimiento_cerrado
                   FROM crm_interacciones WHERE cliente_id = %s AND vendedor = %s AND eliminado = 0
                   ORDER BY fecha DESC, id DESC LIMIT 20""",
                (cliente_id, venta["vendedor"]),
            )
            seguimientos = cursor.fetchall()
        cursor.execute(
            """SELECT codigo_cotizacion, empresa, fecha, total, vendido FROM cotizaciones
               WHERE TRIM(empresa) = %s AND usuario = %s ORDER BY fecha DESC, id DESC LIMIT 20""",
            (str(venta["comprador"] or "").strip(), venta["vendedor"]),
        )
        return {"cliente_id": cliente_id, "seguimientos": seguimientos, "cotizaciones": cursor.fetchall()}
    except mysql.connector.Error as err:
        _error_db(conn, err, "candidatos")
    finally:
        cursor.close()
        conn.close()


@router.post("/comisiones/vinculos")
async def comisiones_vincular(datos: VinculoNuevo, usuario: str = Depends(usuario_autenticado)):
    """
    Enlaza a mano una venta con un seguimiento del CRM (que se cierra como ganado),
    con el folio de una cotización, o con ambos. Lo no enviado conserva su vínculo previo.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        venta = _venta_autorizada(cursor, datos.id_ventas, usuario)

        seg = None
        if datos.seguimiento_id is not None:
            cursor.execute(
                "SELECT id, cliente_id, vendedor FROM crm_interacciones WHERE id = %s AND eliminado = 0",
                (datos.seguimiento_id,),
            )
            seg = cursor.fetchone()
            if not seg:
                raise HTTPException(status_code=404, detail="Seguimiento no encontrado")
            if not crm.puede_gestionar(usuario, seg["vendedor"]):
                raise HTTPException(status_code=403, detail="Este seguimiento pertenece a otro vendedor")

        if datos.cotizacion is not None:
            cot = _cotizacion_existe(cursor, datos.cotizacion)
            if not cot:
                raise HTTPException(status_code=404, detail=f"La cotización '{datos.cotizacion}' no existe")
            if not crm.puede_gestionar(usuario, cot["usuario"]):
                raise HTTPException(status_code=403, detail="Esta cotización pertenece a otro vendedor")

        _vincular(cursor, datos.id_ventas, seg["cliente_id"] if seg else None, seg["id"] if seg else None,
                  datos.cotizacion, venta["vendedor"], "manual")
        if seg:
            _cerrar_como_ganado(cursor, seg["cliente_id"], usuario)
        conn.commit()
        detalle = " y ".join(
            x for x in (f"el seguimiento {seg['id']}" if seg else "", f"la cotización {datos.cotizacion}" if datos.cotizacion else "") if x
        )
        mov_reg.registrar_movimiento(usuario, f"Vinculó la venta {datos.id_ventas} con {detalle}", "Comisiones")
        return {"id_ventas": datos.id_ventas, "seguimiento_id": seg["id"] if seg else None,
                "cliente_id": seg["cliente_id"] if seg else None, "cotizacion": datos.cotizacion, "modo": "manual"}
    except mysql.connector.Error as err:
        _error_db(conn, err, "vincular")
    finally:
        cursor.close()
        conn.close()


@router.delete("/comisiones/vinculos/{id_ventas}")
async def comisiones_desvincular(id_ventas: str, usuario: str = Depends(usuario_autenticado)):
    """Quita el vínculo (no reabre el seguimiento ni cambia la etapa del cliente)."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        _venta_autorizada(cursor, id_ventas, usuario)
        cursor.execute("DELETE FROM venta_seguimiento WHERE id_ventas = %s", (id_ventas,))
        borrados = cursor.rowcount or 0
        conn.commit()
        if not borrados:
            raise HTTPException(status_code=404, detail="La venta no tenía vínculo")
        mov_reg.registrar_movimiento(usuario, f"Desvinculó la venta {id_ventas} de su seguimiento", "Comisiones")
        return {"id_ventas": id_ventas, "desvinculada": True}
    except mysql.connector.Error as err:
        _error_db(conn, err, "desvincular")
    finally:
        cursor.close()
        conn.close()


# ---------- Rellenar ventas sin comisión (ventas previas o hook caído) ----------

class RangoRecalculo(BaseModel):
    desde: date
    hasta: date


@router.post("/comisiones/recalcular")
async def comisiones_recalcular(datos: RangoRecalculo, usuario: str = Depends(requerir_gerencia)):
    """
    Calcula las comisiones que falten de ventasRegistro en el rango (solo gerencia).
    No pisa partidas ya calculadas ni vincula con el CRM: no cierra seguimientos de ventas viejas.
    """
    desde, hasta = crm.validar_rango(datos.desde, datos.hasta)
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT id_ventas, sku, producto, cantidad, precio, fecha, nombreComprador, plataforma, usuario,
                      meli_key, amazon_key
               FROM ventasRegistro WHERE DATE(fecha_registro) BETWEEN %s AND %s ORDER BY id_ventas, id""",
            (desde, hasta),
        )
        filas = cursor.fetchall()
        por_venta = {}
        for f in filas:
            por_venta.setdefault(str(f["id_ventas"]), []).append(f)

        ventas = partidas = excluidas = 0
        for id_ventas, partes in por_venta.items():
            cab = partes[0]
            vendedor = str(cab["usuario"] or "").strip()
            if not vendedor:
                continue
            r = registrar_comisiones(
                cursor, id_ventas, vendedor, cab["nombreComprador"], cab["plataforma"],
                cab["fecha"],
                [{"sku": p["sku"], "producto": p["producto"], "cantidad": p["cantidad"], "precio": p["precio"]} for p in partes],
                auto_vincular=False,
                meli_key=any(p.get("meli_key") for p in partes), amazon_key=any(p.get("amazon_key") for p in partes),
            )
            if r["excluida"]:
                excluidas += 1
            elif r["partidas"]:
                ventas += 1
                partidas += r["partidas"]
        conn.commit()
        return {"ventas_nuevas": ventas, "partidas_nuevas": partidas, "ventas_excluidas": excluidas}
    except mysql.connector.Error as err:
        _error_db(conn, err, "recalcular")
    finally:
        cursor.close()
        conn.close()
