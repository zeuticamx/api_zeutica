from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List
import os
import mysql.connector

router = APIRouter(tags=["/compras"],responses={404: {"Mensaje":"No encontrado"}})

# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

class compraPromedio(BaseModel):   
    sku: str     
    costo_prom: float

# Modelo para cada ítem de la lista de compra
class CompraModel(BaseModel):
    sku: str
    nombre: str
    stock_bodega: int       # qty del frontend
    costo_total: float      # costo_unit del frontend
    num_factura: str
    proveedor: str
    descuento_pct: float
    iva_pct: float
    subtotal: float
    usuario: str

@router.post("/compras")
async def recibir_compra(compras: List[CompraModel]):
    """
    Ingresa compras modificando el valor de stock y registrando el movimiento
    recibe una lista.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    res_items = []

    try:
        for compra in compras:
            # Verifico si el producto existe antes de moverle el stock
            cursor.execute(
                "SELECT stock_bodega FROM productos WHERE sku = %s",
                (compra.sku,)
            )
            res_existe = cursor.fetchone()

            if not res_existe:
                # Si el SKU no existe, lo anoto y sigo con los demás
                res_items.append({"sku": compra.sku, "msg": "SKU no encontrado, se omitió"})
                continue

            stock_actual = res_existe[0]
            stock_nuevo = stock_actual + compra.stock_bodega

            # Actualizo stock y costo en productos
            cursor.execute(
                "UPDATE productos SET stock_bodega = %s, costo_total = %s WHERE sku = %s",
                (stock_nuevo, compra.costo_total, compra.sku)
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
        raise HTTPException(status_code=500, detail=str(e))
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
        raise HTTPException(status_code=500, detail=f"Error en BD: {str(e)}")
    
    finally:
        conn.close()
        cursor.close()