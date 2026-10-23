from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from typing import List
import os, html, asyncio
import mysql.connector
import mov_reg
import compras_ubicacion_logica as ubic
from permisos import usuario_autenticado
from servicios.telegram.notificacion import send_telegram_alert
from errores_db import detalle_error_db

router = APIRouter(tags=["/compras"],responses={404: {"Mensaje":"No encontrado"}})

# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

# Estado de ubicación de cada compra (ver compras_ubicacion_logica.py).
COLUMNAS_UBICACION = {
    "ubicada": "TINYINT(1) NOT NULL DEFAULT 0",
    "cantidad_ubicada": "INT NOT NULL DEFAULT 0",
    "sin_ubicacion": "TINYINT(1) NOT NULL DEFAULT 0",
    "fecha_ubicada": "DATETIME NULL",
    "ubicada_por": "VARCHAR(100) NULL",
}


def asegurar_columnas_ubicacion() -> dict:
    """Agrega a `compras` las columnas de ubicación (se llama en el lifespan).

    Al crear `ubicada` por primera vez, todo lo registrado hasta ese momento se marca como
    ubicado: solo las compras nuevas aparecen por ubicar.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    reporte = {"columnas_agregadas": [], "backfill": 0}
    try:
        cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                       "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'compras'")
        existentes = {str(fila[0]).lower() for fila in cursor.fetchall()}
        for columna, definicion in COLUMNAS_UBICACION.items():
            if columna not in existentes:
                cursor.execute(f"ALTER TABLE compras ADD COLUMN {columna} {definicion}")
                reporte["columnas_agregadas"].append(columna)
        if "ubicada" in reporte["columnas_agregadas"]:
            cursor.execute("UPDATE compras SET ubicada = 1, cantidad_ubicada = stock_bodega, sin_ubicacion = 0")
            reporte["backfill"] = cursor.rowcount
        conn.commit()
        return reporte
    finally:
        cursor.close()
        conn.close()


class UbicarCompraModel(BaseModel):
    warehouse_id: str = Field(min_length=1)
    cantidad: int = Field(gt=0)
    cama: int = Field(default=0, ge=0)


class compraPromedio(BaseModel):
    sku: str     
    costo_prom: float

# Modelo para cada ítem de la lista de compra
class CompraModel(BaseModel):
    sku: str
    nombre: str
    stock_bodega: int = Field(gt=0)       # qty del frontend
    costo_total: float = Field(ge=0)      # costo_unit del frontend
    num_factura: str
    proveedor: str
    descuento_pct: float
    iva_pct: float
    subtotal: float
    usuario: str

@router.post("/compras")
async def recibir_compra(compras: List[CompraModel]):
    """
    Ingresa compras sumando stock relativo con bloqueo de fila (sin lost update).
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    res_items = []

    try:
        for compra in compras:
            # Verifico si el producto existe bloqueando la fila.
            cursor.execute(
                "SELECT stock_bodega FROM productos WHERE sku = %s FOR UPDATE",
                (compra.sku,)
            )
            res_existe = cursor.fetchone()

            if not res_existe:
                # Si el SKU no existe, lo anoto y sigo con los demás
                res_items.append({"sku": compra.sku, "msg": "SKU no encontrado, se omitió"})
                continue

            stock_actual = res_existe[0] if res_existe[0] is not None else 0
            stock_nuevo = stock_actual + compra.stock_bodega

            # Incremento relativo: dos compras concurrentes ya no se pisan.
            cursor.execute(
                "UPDATE productos SET stock_bodega = COALESCE(stock_bodega, 0) + %s, costo_total = %s WHERE sku = %s",
                (compra.stock_bodega, compra.costo_total, compra.sku)
            )

            # Registro el movimiento en la tabla de compras
            sql_insert = """
                INSERT INTO compras
                (sku, nombre, stock_bodega, costo_total, num_factura, proveedor, descuento_pct, iva_pct, subtotal, usuario)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            valores = (
                compra.sku, compra.nombre, compra.stock_bodega, compra.costo_total,
                compra.num_factura, compra.proveedor, compra.descuento_pct,
                compra.iva_pct, compra.subtotal, compra.usuario
            )
            cursor.execute(sql_insert, valores)

            res_items.append({
                "sku": compra.sku,
                "msg": "OK",
                "stock_anterior": stock_actual,
                "stock_nuevo": stock_nuevo
            })

        conn.commit()

        total_piezas = sum(c.stock_bodega for c in compras)
        try:
            mov_reg.registrar_movimiento(compras[0].usuario, f"Registró compra: {len(res_items)} items, {total_piezas} piezas", "Compras")
        except Exception as err:
            print(f"Compra registrada, pero falló la bitácora: {err}")

        # Enviamos notificación a Telegram
        message = (
            f"📦 <b>Compra Procesada</b>\n\n"
            f"• <b>Items:</b> {len(res_items)}\n"
            f"• <b>Piezas:</b> {total_piezas}\n"
        )
        asyncio.create_task(send_telegram_alert(message))

        return {"msg": "Compra procesada", "items": res_items}

    except mysql.connector.Error as err:
        # Si la base de datos truena, hago rollback y me entero qué pasó
        conn.rollback()
        print(f"Error en BD: {err}")
        raise HTTPException(status_code=500, detail="Error al procesar la compra en BD")

    finally:
        # Siempre cierro la puerta al salir
        if conn.is_connected():
            cursor.close()
            conn.close()

# ---- Promedio de compras ----
@router.get("/ultimos-costos/{sku}")
async def obtener_ultimos_costos(sku: str):
    """
    Consulta los ultimos costos registrados para realizar un promedio de costo ultimo.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    
    try:
        # Buscamos los últimos 2 costos de ese SKU ordenados por la fecha más reciente
        query = """
            SELECT costo_total 
            FROM compras 
            WHERE sku = %s 
            ORDER BY fecha_registro DESC 
            LIMIT 2
        """
        cursor.execute(query, (sku,))
        resultados = cursor.fetchall()
        
        # Extraemos los costos y los guardamos en una lista de Python
        # Si no hay compras, devolverá una lista vacía []
        costos_historicos = [float(fila["costo_total"]) for fila in resultados if fila["costo_total"] is not None]
        
        return {"sku": sku, "costos": costos_historicos}
        
    except Exception as e:
        print(e)
        raise HTTPException(status_code=500, detail=detalle_error_db(e, "Error interno del servidor"))
    finally:
        cursor.close()
        conn.close()

@router.post("/costoPromedio") # Endpoint para registrar costo promedio
async def costo_promedio(cprom: compraPromedio):
    """
    Dependencia que registra el ultimo costo en la tabla de productos.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    try:
        cursor.execute(
                "UPDATE productos SET costo_total = %s WHERE sku = %s",
                (cprom.costo_prom, cprom.sku)
            )
        
        conn.commit()
        
        # Respuesta
        if cursor.rowcount > 0:
            return {
                "msg": "Compra actualizada",
                "sku": cprom.sku,
                "costo_promedio": cprom.costo_prom
            }
        else:
            # retornamos si no encontramos SKU
            return {"msg": "No se encontró el SKU o no hubo cambios", "sku": cprom.sku}

    except mysql.connector.Error as err:
        # Si la base de datos truena, me entero qué pasó
        print(f"Error en BD: {err}")
        raise HTTPException(status_code=500, detail="Error al procesar la compra en BD")

    finally:
        # Siempre cierro la puerta al salir
        if conn.is_connected():
            cursor.close()
            conn.close()

@router.get("/registro-compras")
async def obtener_compras():
    """
    Registra las compras en la tabla de compras para debido control.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        # Buscamos las compras
        query = """
            SELECT id, sku, nombre, stock_bodega, costo_total, num_factura, proveedor, (subtotal*(1+(iva_pct/100))) AS total, fecha_registro
            FROM compras ORDER BY fecha_registro DESC                    
            
        """
        cursor.execute(query)
        resultados = cursor.fetchall()

        if not resultados:
                raise HTTPException(status_code=404, detail="No se encontraron compras")
        
        return resultados
    
    except Exception as e:
        # Esto imprimirá el error REAL en tu consola de AWS para que sepas qué pasó
        print(f"Error detectado: {e}") 
        raise HTTPException(status_code=500, detail=detalle_error_db(e, "Error en BD"))
    
    finally:
        conn.close()
        cursor.close()

# ---- Compras por ubicar ----

@router.get("/compras/por-ubicar")
async def compras_por_ubicar():
    """Compras que aún no tienen todas sus piezas repartidas en tarjetas, agrupadas por factura."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT id, sku, nombre, stock_bodega AS cantidad, cantidad_ubicada,
                   num_factura, proveedor, fecha_registro
            FROM compras WHERE COALESCE(ubicada, 0) = 0
            ORDER BY fecha_registro DESC, id DESC
        """)
        filas = cursor.fetchall()
        facturas = ubic.agrupar_por_factura(filas)
        return {"total": len(filas), "facturas": facturas}
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))
    finally:
        cursor.close()
        conn.close()


@router.post("/compras/{compra_id}/ubicar")
async def ubicar_compra(compra_id: int, datos: UbicarCompraModel, usuario: str = Depends(usuario_autenticado)):
    """Reparte `cantidad` de la compra en una celda: crea la tarjeta en stock_ubicacion y suma lo ubicado.

    Una sola transacción con la fila bloqueada: dos usuarios no pueden ubicar de más la misma compra.
    Cuando lo ubicado alcanza lo comprado, la compra deja de aparecer.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, sku, stock_bodega, cantidad_ubicada, ubicada FROM compras WHERE id = %s FOR UPDATE",
            (compra_id,))
        compra = cursor.fetchone()
        if not compra:
            raise HTTPException(status_code=404, detail="Compra no encontrada")
        if compra["ubicada"]:
            raise HTTPException(status_code=409, detail="Esta compra ya está ubicada")

        error = ubic.validar_cantidad(datos.cantidad, compra["stock_bodega"], compra["cantidad_ubicada"])
        if error:
            raise HTTPException(status_code=422, detail=error)

        almacen = datos.warehouse_id.strip()
        if not almacen:
            raise HTTPException(status_code=422, detail="Indica la ubicación")

        completa = ubic.queda_ubicada(compra["stock_bodega"], compra["cantidad_ubicada"], datos.cantidad)
        cursor.execute(
            "INSERT INTO stock_ubicacion (sku, warehouse_id, cama, cantidad) VALUES (%s, %s, %s, %s)",
            (compra["sku"], almacen, datos.cama, datos.cantidad))
        cursor.execute(
            "UPDATE compras SET cantidad_ubicada = cantidad_ubicada + %s, ubicada = %s, "
            "fecha_ubicada = IF(%s = 1, NOW(), fecha_ubicada), ubicada_por = IF(%s = 1, %s, ubicada_por) WHERE id = %s",
            (datos.cantidad, int(completa), int(completa), int(completa), usuario, compra_id))
        conn.commit()

        try:
            mov_reg.registrar_movimiento(
                usuario, f"Ubicó {datos.cantidad} de '{compra['sku']}' (compra #{compra_id}) en '{almacen}'", "Compras")
        except Exception as err:
            print(f"Compra ubicada, pero falló la bitácora: {err}")

        return {
            "mensaje": "Ubicación creada",
            "id": compra_id,
            "sku": compra["sku"],
            "warehouse_id": almacen,
            "cantidad": datos.cantidad,
            "pendiente": ubic.pendiente(compra["stock_bodega"], compra["cantidad_ubicada"] + datos.cantidad),
            "ubicada": completa,
        }
    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))
    finally:
        cursor.close()
        conn.close()


@router.post("/compras/{compra_id}/sin-ubicacion")
async def compra_sin_ubicacion(compra_id: int, usuario: str = Depends(usuario_autenticado)):
    """Saca la compra de la lista sin crear tarjetas (p. ej. un SKU que no se almacena o ya se ubicó a mano)."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, sku, ubicada FROM compras WHERE id = %s FOR UPDATE", (compra_id,))
        compra = cursor.fetchone()
        if not compra:
            raise HTTPException(status_code=404, detail="Compra no encontrada")
        if compra["ubicada"]:
            raise HTTPException(status_code=409, detail="Esta compra ya está ubicada")

        cursor.execute(
            "UPDATE compras SET ubicada = 1, sin_ubicacion = 1, fecha_ubicada = NOW(), ubicada_por = %s WHERE id = %s",
            (usuario, compra_id))
        conn.commit()

        try:
            mov_reg.registrar_movimiento(
                usuario, f"Marcó sin ubicación '{compra['sku']}' (compra #{compra_id})", "Compras")
        except Exception as err:
            print(f"Compra marcada, pero falló la bitácora: {err}")

        return {"mensaje": "Compra marcada sin ubicación", "id": compra_id}
    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))
    finally:
        cursor.close()
        conn.close()
