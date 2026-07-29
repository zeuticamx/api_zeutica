import mysql.connector
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from typing import List
from decimal import Decimal
import datetime
import os, mov_reg
from dotenv import load_dotenv
from typing import Optional
from cotizacion_service import generar_nuevo_codigo, guardar_cotizacion_db

router =APIRouter(tags=["/cotizaciones"],responses={404: {"Mensaje":"No encontrado"}})
load_dotenv() # Cargar credenciales .env

# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

from pydantic import BaseModel # Bases clase para recibir la informacion de pdf
from typing import List

class ItemCotizacion(BaseModel):
    sku: str
    nombre_producto: str
    cantidad: int
    precio_unitario: float
    total_linea: float

class CotizacionSchema(BaseModel):
    codigo_cotizacion: str
    empresa: str
    atencion: str    
    email: str
    domicilio: Optional[str] = None
    telefono: str
    subtotal: float
    iva: float    
    total: float
    costo_envio: float  # Cambié de int a float para aceptar decimales
    forma_pago: str
    comentarios: str
    usuario: str
    pdf: str
    items: List[ItemCotizacion]

# Creamos el "molde" para los datos que enviará el frontend
class VinculoFactura(BaseModel):
    id: int
    codigo_cotizacion: str  
    relacion_factura: Optional[str] = None
    metodo_pago: Optional[str] = None
    fecha_pago: Optional[str] = None
    usuario: str

@router.get("/cotizaciones/nuevo-codigo")
async def obtener_nuevo_codigo():
    """
    Consulta las cotizaciones registradas para asignar nuevo codigo a la cotizacion siguiente.
    """
    connection = get_db_connection()
    try:
        # La lógica del consecutivo vive en el servicio de raíz para
        # reutilizarla también en /genera-cotizacion.
        nuevo_codigo = generar_nuevo_codigo(connection)
        return {"nuevo_codigo": nuevo_codigo}
    except mysql.connector.Error as err:
        print(f"Error BD en nuevo-codigo: {err}")
        raise HTTPException(status_code=500, detail=f"Error BD: {str(err)}")
    finally:
        connection.close()


@router.get("/complemento-pago/{codigo_cotizacion}")
async def obtener_complemento_pago(codigo_cotizacion: str):
    """
    Obtiene el complemento de pago para una cotización específica.
    """
    connection = get_db_connection()
    try:
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute("SELECT * FROM complemento_pago WHERE id_cot = %s", (codigo_cotizacion,))
            # Una cotización puede tener varios complementos: siempre devolvemos lista
            # (vacía si no hay), para que el front no tenga que tratar el 404 como caso normal.
            return cursor.fetchall()

    # 1. Atrapamos explícitamente las HTTPExceptions y las re-lanzamos sin modificar
    except HTTPException:
        raise

    # 2. Atrapamos cualquier otro error inesperado (fallos en la BD, etc.) como 500
    except Exception as e:
        print(f"Error al obtener complemento de pago: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
    finally:
        connection.close()

@router.post("/cotizaciones/guardar")
async def guardar_cotizacion(cot: CotizacionSchema):
    """
    Registra la cotizacion en la tabla cotizaciones de DB.
    """
    connection = get_db_connection()
    try:
        # El INSERT vive en el servicio de raíz para reutilizarlo también
        # en /genera-cotizacion.
        cotizacion_id = guardar_cotizacion_db(connection, cot)

        mov_reg.registrar_movimiento(cot.usuario, f"Registró una nueva cotización: {cot.codigo_cotizacion}", "Cotizaciones")

        return {"status": "success", "id": cotizacion_id}

    except mysql.connector.Error as err:
        connection.rollback()
        print(f"Error BD al guardar cotización: {err}")
        raise HTTPException(status_code=500, detail=f"Error BD: {str(err)}")
    
    except Exception as e:
        connection.rollback()
        print(f"Error inesperado: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
    finally:
        connection.close()

@router.get("/consulta/cotizacion")
async def consulta_cotizacion():
    """
    Dependencia para consultar cotizaciones con sus items agrupados.
    """
    connection = get_db_connection()
    try:
        with connection.cursor(dictionary=True) as cursor:
            # Agregamos c.id al SELECT para poder usarlo en Python
            cursor.execute("""
                SELECT 
                    c.id, 
                    c.codigo_cotizacion, 
                    c.relacion_factura,
                    c.metodo_pago,
                    c.forma_pago,
                    c.fecha_pago,
                    c.empresa,
                    c.fecha,
                    c.subtotal,
                    c.total,
                    c.firma_envio,
                    c.vendido,
                    i.nombre_producto
                FROM cotizaciones c                  
                JOIN cotizacion_items i ON c.id = i.cotizacion_id 
                ORDER BY c.codigo_cotizacion DESC;
            """)
            
            # CORREGIDO: Un solo fetchall trae toda la información combinada
            filas_combinadas = cursor.fetchall()

            if not filas_combinadas:
                raise HTTPException(status_code=404, detail="No se encontraron cotizaciones")

            # Estructura para agrupar los ítems dentro de su cotización correspondiente
            cotizaciones_acumuladas = {}

            for fila in filas_combinadas:
                id_cotizacion = fila["id"]
                
                # Si es la primera vez que vemos esta cotización, creamos su base
                if id_cotizacion not in cotizaciones_acumuladas:
                    cotizaciones_acumuladas[id_cotizacion] = {
                        "id": id_cotizacion,
                        "codigo_cotizacion": fila["codigo_cotizacion"],
                        "relacion_factura": fila["relacion_factura"],
                        "metodo_pago": fila["metodo_pago"],
                        "forma_pago": fila["forma_pago"],
                        "fecha_pago": str(fila["fecha_pago"]) if isinstance(fila["fecha_pago"], (datetime.date, datetime.datetime)) else fila["fecha_pago"],
                        "firma_envio": fila["firma_envio"],
                        "vendido": fila["vendido"],
                        "empresa": fila["empresa"],
                        "fecha": str(fila["fecha"]) if isinstance(fila["fecha"], (datetime.date, datetime.datetime)) else fila["fecha"],
                        "subtotal": str(fila["subtotal"]) if isinstance(fila["subtotal"], Decimal) else fila["subtotal"],
                        "total": str(fila["total"]) if isinstance(fila["total"], Decimal) else fila["total"],
                        "items": [] # Lista vacía lista para recibir sus productos
                    }
                
                # Extraemos el ítem y lo metemos a la lista de esa cotización
                if fila["nombre_producto"]:
                    cotizaciones_acumuladas[id_cotizacion]["items"].append({
                        "nombre_producto": fila["nombre_producto"]
                    })

            # Convertimos el diccionario acumulador en una lista limpia para retornar como JSON
            lista_final = list(cotizaciones_acumuladas.values())

            return {"cotizaciones": lista_final}

    except Exception as e:
        print(f"Error detectado: {e}")
        raise HTTPException(status_code=500, detail=f"Error en BD: {str(e)}")
    finally:
        connection.close()

@router.post("/relacionFactura")
async def vincular_factura(vinculos: List[VinculoFactura]):
    """
    Registra la relacion de cotizacion a factura realizada para vincular pago.
    """
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            # Recorremos cada cotización modificada que llegó desde frontend
            for v in vinculos:
                # Actualizamos el registro.                 
                sql = "UPDATE cotizaciones SET relacion_factura = %s, metodo_pago = %s, fecha_pago = %s WHERE codigo_cotizacion = %s"
                cursor.execute(sql, (v.relacion_factura, v.metodo_pago, v.fecha_pago, v.codigo_cotizacion))            
            
            # Guardamos los cambios
            connection.commit()

            mov_reg.registrar_movimiento(vinculos[0].usuario, f"Vinculó factura {v.relacion_factura} a cotización {v.codigo_cotizacion}", "Cotizaciones")
            
        return {"status": "success", "mensaje": "Facturas vinculadas"}
        
    except Exception as e:
        connection.rollback()
        print(f"Error al vincular factura: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        connection.close()

class vinculoPago(BaseModel):
    codigo_cotizacion: str
    complemento: str
    pago: float
    usuario: Optional[str] = None

@router.post("/complemento-pago")   
async def registrar_complemento_pago(vinculo: vinculoPago):
    """
    Registra un complemento de pago para una cotización específica.
    """
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            # Inyectamos el complemento de pago en la cotización
            sql_pago = "INSERT INTO complemento_pago (id_cot, complemento, pago) VALUES (%s, %s, %s)"
            cursor.execute(sql_pago, (vinculo.codigo_cotizacion, vinculo.complemento, vinculo.pago))

            connection.commit()

            mov_reg.registrar_movimiento(vinculo.usuario or "", f"Registró complemento de pago para cotización con ID {vinculo.codigo_cotizacion}", "Cotizaciones")
            
        return {"status": "success", "mensaje": "Complemento de pago registrado"}
        
    except Exception as e:
        connection.rollback()
        print(f"Error al registrar complemento de pago: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        connection.close()


class FirmaEnvio(BaseModel):
    codigo_cotizacion: str
    firma_base64: str
    usuario: str
    fecha_firma: str

@router.post("/firma-ventas")
async def guardar_firma(firma: FirmaEnvio):
    """
    Registra firma de cliente cuando retira mercancia de cedis.
    """
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            # Inyecto la firma en la cotización que me piden, nada más
            sql = "UPDATE cotizaciones SET firma_envio = %s WHERE codigo_cotizacion = %s"
            cursor.execute(sql, (firma.firma_base64, firma.codigo_cotizacion))

            if cursor.rowcount == 0:
                # Si no encontró la cotización, aviso antes de hacer commit
                raise HTTPException(status_code=404, detail=f"Cotización {firma.codigo_cotizacion} no encontrada")

            connection.commit()

            mov_reg.registrar_movimiento(firma.usuario, f"Guardó firma para cotización {firma.codigo_cotizacion}", "Cotizaciones")

            return {"status": "success", "mensaje": f"Firma guardada en {firma.codigo_cotizacion}"}

    except mysql.connector.Error as err:
        connection.rollback()
        print(f"Error BD al guardar firma: {err}")
        raise HTTPException(status_code=500, detail=f"Error BD: {str(err)}")
    
    finally:
        connection.close()


@router.get("/cotizacion/{cotizacion_id}") # Endpoint para items de cotizacion
async def obtener_items_cotizacion(cotizacion_id: int):
    """
    Dependencia para obtener items asignados a cotizacion.
    """
    connection = get_db_connection()
    
    try:
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute("SELECT * FROM cotizacion_items WHERE cotizacion_id = %s", (cotizacion_id,))
            
            # ¡AQUÍ ESTÁ EL CAMBIO CLAVE! Usamos fetchall()
            items = cursor.fetchall() 
            
        if not items:
            return []
            
        return items
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error al obtener items: {str(e)}"
        )
    finally:
        connection.close()

class vendido(BaseModel):
    vendido: int
    codigo_cotizacion: str

@router.post("/cotizaciones/vendido")
async def marcar_vendido(vendido: vendido):
    """
    Marca como vendida una cotizacion para que ya no este disponible en seccion de ventas.
    """
    connection = get_db_connection()
    try:
        with connection.cursor() as cursor:
            # Insertar en la tabla principal (Maestro)
            sql_maestro = """
              UPDATE cotizaciones SET vendido = %s WHERE codigo_cotizacion = %s
            """

            cursor.execute(sql_maestro, (vendido.vendido, vendido.codigo_cotizacion))      
                                 
            connection.commit()
            return {"status": "success"}            
   
    except Exception as e:
        connection.rollback()
        print(f"Error inesperado: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
    finally:
        connection.close()

@router.get("/cotizaciones/ventas")
async def cotizaciones_para_venta():
    """
    Exclusivo para la seccion de Ventas: cotizaciones abiertas (no vendidas)
    con sus items completos para cargar directo al carrito.
    No incluye el pdf para mantener la respuesta ligera.
    """
    connection = get_db_connection()
    try:
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute("""
                SELECT
                    c.id,
                    c.codigo_cotizacion,
                    c.empresa,
                    c.fecha,
                    c.subtotal,
                    c.total,
                    c.vendido,
                    i.sku,
                    i.nombre_producto,
                    i.cantidad,
                    i.precio_unitario,
                    i.total_linea
                FROM cotizaciones c
                LEFT JOIN cotizacion_items i ON c.id = i.cotizacion_id
                WHERE c.vendido = 0 OR c.vendido IS NULL
                ORDER BY c.codigo_cotizacion DESC;
            """)
            filas = cursor.fetchall()

            # Agrupo los items dentro de su cotización correspondiente
            cotizaciones_acumuladas = {}
            for fila in filas:
                id_cotizacion = fila["id"]

                if id_cotizacion not in cotizaciones_acumuladas:
                    cotizaciones_acumuladas[id_cotizacion] = {
                        "id": id_cotizacion,
                        "codigo_cotizacion": fila["codigo_cotizacion"],
                        "empresa": fila["empresa"],
                        "fecha": str(fila["fecha"]) if isinstance(fila["fecha"], (datetime.date, datetime.datetime)) else fila["fecha"],
                        "subtotal": str(fila["subtotal"]) if isinstance(fila["subtotal"], Decimal) else fila["subtotal"],
                        "total": str(fila["total"]) if isinstance(fila["total"], Decimal) else fila["total"],
                        "vendido": fila["vendido"],
                        "items": []
                    }

                # LEFT JOIN puede dejar sku en None si la cotización no tiene items
                if fila["sku"]:
                    cotizaciones_acumuladas[id_cotizacion]["items"].append({
                        "sku": fila["sku"],
                        "nombre_producto": fila["nombre_producto"],
                        "cantidad": fila["cantidad"],
                        "precio_unitario": float(fila["precio_unitario"]) if isinstance(fila["precio_unitario"], Decimal) else fila["precio_unitario"],
                        "total_linea": float(fila["total_linea"]) if isinstance(fila["total_linea"], Decimal) else fila["total_linea"],
                    })

            return {"cotizaciones": list(cotizaciones_acumuladas.values())}

    except Exception as e:
        print(f"Error en cotizaciones/ventas: {e}")
        raise HTTPException(status_code=500, detail=f"Error en BD: {str(e)}")
    finally:
        connection.close()

@router.get("/cotizaciones/base64/{codigo_cotizacion}")
async def obtener_pdf_base64(codigo_cotizacion: str):
    """
    Obtiene el PDF en base64 de la cotización especificada.
    """
    connection = get_db_connection()
    try:
        with connection.cursor(dictionary=True) as cursor:
            cursor.execute("SELECT pdf FROM cotizaciones WHERE codigo_cotizacion = %s", (codigo_cotizacion,))
            resultado = cursor.fetchone()
            
            if not resultado or not resultado.get("pdf"):
                raise HTTPException(status_code=404, detail=f"No se encontró PDF para la cotización {codigo_cotizacion}")
            
            return {"pdf_base64": resultado["pdf"]}
    
    except Exception as e:
        print(f"Error al obtener PDF: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    
    finally:
        connection.close()