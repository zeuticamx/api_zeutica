import mysql.connector
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Query
from typing import List, Optional
import os, mov_reg
from dotenv import load_dotenv

router = APIRouter(tags=["/inventario"], responses={404: {"Mensaje": "No encontrado"}})
load_dotenv()

# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

class ConteoItem(BaseModel):
    sku: str
    conteo: int

# usuario va arriba, no repetido en cada producto
class ConteoPayload(BaseModel):
    usuario: str
    productos: List[ConteoItem]

@router.post("/inventario/conteo")
async def registrar_conteo(payload: ConteoPayload):
    """
    Dependencia que recibe conteo de inventario ciclico o completo para registro.
    """
    conn = None
    res_items = []

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        for item in payload.productos:
            # Verifico que el SKU exista antes de registrar
            cursor.execute("SELECT sku FROM productos WHERE sku = %s", (item.sku,))
            res_existe = cursor.fetchone()

            if not res_existe:
                res_items.append({"sku": item.sku, "msg": "SKU no encontrado, se omitió"})
                continue

            cursor.execute(
                "UPDATE productos SET conteo = %s, usuario = %s WHERE sku = %s",
                (item.conteo, payload.usuario, item.sku)
            )
            res_items.append({"sku": item.sku, "msg": "OK"})

        conn.commit()
        mov_reg.registrar_movimiento(payload.usuario, f"Registró conteo de inventario", "Inventario")
        return {"mensaje": "Conteo registrado exitosamente", "items": res_items}

    except mysql.connector.Error as err:
        if conn:
            conn.rollback()
        print(f"Error en BD: {err}")
        raise HTTPException(status_code=500, detail="Error al registrar conteo en BD")

    finally:
        if conn and conn.is_connected():
            cursor.close()
            conn.close()


def _columnas(cursor, tabla: str) -> set:
    cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                   "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (tabla,))
    return {str(fila[0]).lower() for fila in cursor.fetchall()}


INDICES_MOVIMIENTOS = {
    "idx_ventasregistro_fecha_registro": ("ventasRegistro", "fecha_registro"),
    "idx_compras_fecha_registro": ("compras", "fecha_registro"),
    "idx_stock_actual_fecha_registro": ("stock_actual", "fecha_registro"),
    "idx_devoluciones_fecha": ("devoluciones", "fecha"),
}


def asegurar_indices_inventario():
    """Índices por fecha para que /inventario/movimientos no haga full-scan (lifespan)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT INDEX_NAME FROM information_schema.STATISTICS "
                       "WHERE TABLE_SCHEMA = DATABASE()")
        indices = {str(fila[0]).lower() for fila in cursor.fetchall()}
        for nombre, (tabla, columna) in INDICES_MOVIMIENTOS.items():
            if nombre not in indices:
                try:
                    cursor.execute(f"ALTER TABLE {tabla} ADD INDEX {nombre} ({columna})")
                except mysql.connector.Error as err:
                    print(f"No se pudo crear {nombre}: {err}")
        conn.commit()
    finally:
        cursor.close()
        conn.close()


@router.get("/inventario/movimientos")
async def movimientos_inventario(
    sku: Optional[str] = Query(default=None),
    tipo: Optional[str] = Query(default=None),
    desde: Optional[str] = Query(default=None),
    hasta: Optional[str] = Query(default=None),
    limite: int = Query(default=200, ge=1, le=1000),
):
    """Auditoría de descuentos/entradas de inventario (permiso general).

    Une ventas (−), bajas/traspasos (±), compras (+) y devoluciones con
    reingreso (+). Sin costos ni acciones de gerencia: fecha, tipo, SKU,
    cantidad firmada, folio y usuario.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cols_stock = _columnas(cursor, "stock_actual")
        cols_dev = _columnas(cursor, "devoluciones")
        almacen_expr = "almacen" if "almacen" in cols_stock else "NULL"
        tipo_stock = (f"CASE WHEN {almacen_expr} = 'BAJA' THEN 'baja' "
                      f"WHEN {almacen_expr} = 'CLEAN' THEN 'traspaso' ELSE 'traspaso' END"
                      if "almacen" in cols_stock else "'traspaso'")
        usuario_dev = "usuario" if "usuario" in cols_dev else "NULL"

        query = "SELECT * FROM ("
        uniones = []

        def filtros_rama(col_fecha: str, tipos_rama: tuple):
            if tipo and tipo not in tipos_rama:
                return None
            conds = []
            params = []
            if sku:
                conds.append("sku LIKE %s")
                params.append(f"%{sku}%")
            if desde:
                conds.append(f"{col_fecha} >= %s")
                params.append(desde)
            if hasta:
                conds.append(f"{col_fecha} < DATE_ADD(%s, INTERVAL 1 DAY)")
                params.append(hasta)
            return (" AND " + " AND ".join(conds) if conds else "", params)

        ramas = [
            (f"""SELECT fecha_registro AS fecha, 'venta' AS tipo, sku, (0 - cantidad) AS cantidad,
                    CAST(id_ventas AS CHAR) AS folio, usuario, plataforma AS detalle
                FROM ventasRegistro WHERE 1=1""", "fecha_registro", ("venta",)),
            (f"""SELECT fecha_registro AS fecha, {tipo_stock} AS tipo, sku, cantidad,
                    NULL AS folio, usuario, {almacen_expr} AS detalle
                FROM stock_actual WHERE 1=1""", "fecha_registro", ("baja", "traspaso")),
            ("""SELECT fecha_registro AS fecha, 'compra' AS tipo, sku, stock_bodega AS cantidad,
                    num_factura AS folio, usuario, proveedor AS detalle
                FROM compras WHERE 1=1""", "fecha_registro", ("compra",)),
            (f"""SELECT fecha AS fecha, 'devolucion' AS tipo, sku,
                    CASE WHEN reingreso THEN cantidad ELSE 0 END AS cantidad,
                    NULL AS folio, {usuario_dev} AS usuario, plataforma AS detalle
                FROM devoluciones WHERE 1=1""", "fecha", ("devolucion",)),
        ]
        for sql_base, col_fecha, tipos_rama in ramas:
            f = filtros_rama(col_fecha, tipos_rama)
            if f is None:
                continue
            conds, params = f
            # Límite por rama: evita ordenar las 4 tablas completas.
            uniones.append((f"({sql_base}{conds} ORDER BY {col_fecha} DESC LIMIT %s)", params + [limite]))

        if not uniones:
            return []

        query += " UNION ALL ".join(sql for sql, _ in uniones)
        query += ") AS m ORDER BY fecha DESC LIMIT %s"
        valores = [p for _, params in uniones for p in params] + [limite]

        cursor.execute(query, tuple(valores))
        return cursor.fetchall()
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=f"Error al consultar movimientos: {err}")
    finally:
        cursor.close()
        conn.close()