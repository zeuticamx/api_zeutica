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

        query = f"""
            SELECT * FROM (
                SELECT fecha_registro AS fecha, 'venta' AS tipo, sku, (0 - cantidad) AS cantidad,
                    CAST(id_ventas AS CHAR) AS folio, usuario, plataforma AS detalle
                FROM ventasRegistro
                UNION ALL
                SELECT fecha_registro AS fecha, {tipo_stock} AS tipo, sku, cantidad,
                    NULL AS folio, usuario, {almacen_expr} AS detalle
                FROM stock_actual
                UNION ALL
                SELECT fecha_registro AS fecha, 'compra' AS tipo, sku, stock_bodega AS cantidad,
                    num_factura AS folio, usuario, proveedor AS detalle
                FROM compras
                UNION ALL
                SELECT fecha AS fecha, 'devolucion' AS tipo, sku,
                    CASE WHEN reingreso THEN cantidad ELSE 0 END AS cantidad,
                    NULL AS folio, {usuario_dev} AS usuario, plataforma AS detalle
                FROM devoluciones
            ) AS m WHERE 1=1
        """
        valores: list = []
        if tipo:
            query += " AND tipo = %s"
            valores.append(tipo)
        if sku:
            query += " AND sku LIKE %s"
            valores.append(f"%{sku}%")
        if desde:
            query += " AND fecha >= %s"
            valores.append(desde)
        if hasta:
            query += " AND fecha < DATE_ADD(%s, INTERVAL 1 DAY)"
            valores.append(hasta)
        query += " ORDER BY fecha DESC LIMIT %s"
        valores.append(limite)

        cursor.execute(query, tuple(valores))
        return cursor.fetchall()
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=f"Error al consultar movimientos: {err}")
    finally:
        cursor.close()
        conn.close()