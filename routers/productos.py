# fichero api de productos
import mysql.connector, html, asyncio
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
import os, mov_reg
from dotenv import load_dotenv
from typing import Optional
from servicios.telegram.notificacion import send_telegram_alert

router =APIRouter(tags=["/productos"],responses={404: {"Mensaje":"No encontrado"}})

load_dotenv() # Carga de credenciales .env

# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )



class UbicacionEditSchema(BaseModel):
    """Modelo para editar ubicación por sku"""
    sku: Optional[str]  # SKU del producto, opcional porque a veces solo queremos editar por id
    warehouse_id: str
    cantidad: int
    usuario: str  # Usuario que edita la ubicación, para registro de movimientos
    cama: int

class ProdEditSchema(BaseModel):
    """Modelo para recibir productos editados desde el frontend"""
    productos: list[dict]
    usuario: str  # Usuario que realiza la edición, para registro de movimientos

class ProdNuevoSchema(BaseModel):
    """Modelo para crear un producto nuevo desde Streamlit"""
    sku: str
    nombre: str
    categoria: str
    medida: str
    ubicacion: str
    stock_minimo: int
    numero_referencia: float
    costo_total: float
    precio: float
    precio_2: float
    precio_3: float
    usuario: str  # Usuario que crea el producto, para registro de movimientos


@router.get("/productos")
async def consultar_inventario_completo():
    """
    Consulta inventario de productos para monitoreo.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True) # Usamos dictionary=True para que devuelva claves como 'sku'
    
    query = "SELECT sku, nombre, categoria, medida, ubicacion, stock_minimo, stock_bodega, stock_full, stock_fba, stock_clean, stock_total, numero_referencia, costo_total, precio, precio_2, precio_3, precio_amazon, precio_clean FROM productos"
    
    try:
        cursor.execute(query) 
        productos = cursor.fetchall()
        
        if not productos:
            raise HTTPException(status_code=404, detail="No se han encontrado productos")
            
        return productos # FastAPI lo convierte automáticamente a JSON

    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")
    
    finally:
        cursor.close()
        conn.close()
        

@router.get("/producto/sku/{sku}") # Consulta a DB por sku de producto
async def obtener_producto_por_sku(sku: str):  
    """
    Consulta inventario por sku unico para monitoreo.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)  
    # Usamos %s para prevenir inyección SQL
    query = "SELECT id, sku, nombre, stock_bodega, stock_full, stock_fba, stock_clean, stock_total, precio, precio_2, precio_3 FROM productos WHERE sku = %s"
    try:
        cursor.execute(query, (sku,))    
        resultado = cursor.fetchone()
        if not resultado:
            raise HTTPException(status_code=404, detail="Producto no encontrado")
    
    
        # Mapeamos el resultado a un diccionario para mayor claridad
        return {
            "id": resultado['id'],
            "sku": resultado['sku'],            
            "nombre": resultado['nombre'],
            "stock_bodega": resultado['stock_bodega'],
            "stock_full": resultado['stock_full'],
            "stock_fba": resultado['stock_fba'],
            "stock_clean":resultado['stock_clean'],
            "stock_total": resultado['stock_total'],
            "precio": resultado['precio'],
            "precio_2": resultado['precio_2'],
            "precio_3": resultado['precio_3']
            }
    
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=f"Error en la base de datos: {err}")
    
    finally:
        # 4. Siempre cerrar recursos
        cursor.close()
        conn.close()




@router.post("/productos/editados")
async def actualizar_productos(datos: ProdEditSchema):
    """
    Recibo una lista de productos editados desde el frontend.
    Actualizo los precios y stocks en la BD según lo que me manden.
    """
    conn = get_db_connection()
    
    try:
        cursor = conn.cursor(dictionary=True)
        
        # Guardo los resultados para devolverlos
        res_actualizados = []
        res_errores = []
        
        # Itero cada producto de la lista
        for prod in datos.productos:
            try:
                # Verifico que el producto tenga los datos necesarios
                if "nombre" not in prod or "sku" not in prod:
                    res_errores.append({
                        "problema": "Faltan datos: necesito 'nombre' y 'sku'",
                        "producto": prod
                    })
                    continue
                
                # Construyo la consulta dinámicamente según qué campos vinieron
                columnas_protegidas = ["id", "sku", "in_full", "stock_total"]
                campos_actualizar = []
                valores = []
                
                for columna, valor in prod.items():
                    # Solo agregamos si la columna no está en la lista negra
                    if columna not in columnas_protegidas:
                        campos_actualizar.append(f"{columna} = %s")
                        valores.append(valor)
                
                # Si no hay campos para actualizar, lo salto
                if campos_actualizar:                   
                
                    # Armo el UPDATE SQL con los campos dinámicos
                    sql_update = f"UPDATE productos SET {', '.join(campos_actualizar)} WHERE sku = %s"
                    valores.append(prod.get("sku"))
                
                cursor.execute(sql_update, valores)
                
                # Verifico que se actualizó algo
                if cursor.rowcount > 0:
                    res_actualizados.append({
                        "sku": prod["sku"],
                        "nombre": prod["nombre"],
                        "estado": "actualizado"
                    })
                else:
                    res_errores.append({
                        "sku": prod["sku"],
                        "nombre": prod["nombre"],
                        "problema": "Producto no encontrado en BD"
                    })
            
            except Exception as e:
                # Si algo truena en un producto, lo registro pero continúo
                res_errores.append({
                    "producto": prod,
                    "error": str(e)
                })
        
        # Confirmo todos los cambios de una vez
        conn.commit()
        mov_reg.registrar_movimiento(datos.usuario, f"Actualizó productos: {len(res_actualizados)} actualizados, {len(res_errores)} errores", "Productos")

        # Enviamos notificación a Telegram
        message = (
            f"🛠️ <b>Actualización de Productos</b>\n\n"
            f"• <b>Usuario:</b> {html.escape(datos.usuario)}\n"
            f"• <b>Actualizados:</b> {len(res_actualizados)}\n"
            f"• <b>Errores:</b> {len(res_errores)}"
        )
        asyncio.create_task(send_telegram_alert(message))
        
        return {
            "mensaje": "Actualización completada",
            "actualizados": len(res_actualizados),
            "errores": len(res_errores),
            "detalle_actualizados": res_actualizados,
            "detalle_errores": res_errores
        }
    
    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error fatal en BD: {err}")
        raise HTTPException(status_code=500, detail=f"Error en base de datos: {err}")
    
    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


@router.post("/producto/nuevo")
async def crear_producto(prod: ProdNuevoSchema):
    """
    Creo un producto nuevo con los datos que vienen de Streamlit.
    Inserto todos los campos en la tabla productos.
    """
    conn = get_db_connection()
    
    try:
        cursor = conn.cursor()
        
        # Primero verifico si el SKU ya existe (no dejo duplicados)
        sql_check = "SELECT sku FROM productos WHERE sku = %s"
        cursor.execute(sql_check, (prod.sku,))
        existe = cursor.fetchone()
        
        if existe:
            raise HTTPException(status_code=400, detail=f"El SKU '{prod.sku}' ya existe en la BD")
        
        # Inserto el nuevo producto
        # Nota: stock_bodega, stock_full, stock_fba y stock_total empiezan en 0
        sql_insert = """
            INSERT INTO productos 
            (sku, nombre, categoria, medida, ubicacion, stock_minimo, numero_referencia, 
             costo_total, precio, precio_2, precio_3, stock_bodega, stock_full, stock_fba)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0, 0)
        """
        
        valores = (
            prod.sku,
            prod.nombre,
            prod.categoria,
            prod.medida,
            prod.ubicacion,
            prod.stock_minimo,
            prod.numero_referencia,
            prod.costo_total,
            prod.precio,
            prod.precio_2,
            prod.precio_3
        )
        
        cursor.execute(sql_insert, valores)
        conn.commit()

        mov_reg.registrar_movimiento(prod.usuario, f"Creó producto: {prod.nombre}", "Productos")

        # Enviamos notificación a Telegram
        message = (
            f"🆕 <b>Nuevo Producto Creado</b>\n\n"
            f"• <b>SKU:</b> {html.escape(prod.sku)}\n"
            f"• <b>Nombre:</b> {html.escape(prod.nombre)}\n"
            f"• <b>Categoría:</b> {html.escape(prod.categoria)}\n"
            f"• <b>Usuario:</b> {html.escape(prod.usuario)}"
        )
        asyncio.create_task(send_telegram_alert(message))

        return {
            "mensaje": "Producto creado exitosamente",
            "sku": prod.sku,
            "nombre": prod.nombre,
            "categoria": prod.categoria,
            "estado": "creado"
        }
    
    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error en inserción: {err}")
        raise HTTPException(status_code=500, detail=f"Error en base de datos: {err}")

    finally:
        if conn.is_connected():
            cursor.close()
            conn.close()


@router.get("/productos/ubicaciones/{sku}")
async def ubicaciones_por_sku(sku: str):
    """
    Busco en stock_ubicacion todo lo que tenga ese sku.
    Si no hay nada, mando 404 claro.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True, buffered=True)

    try:
        # Traigo todas las filas de ubicaciones para ese sku
        cursor.execute("SELECT * FROM stock_ubicacion WHERE sku = %s", (sku,))
        ubicaciones = cursor.fetchall()

        if not ubicaciones:
            raise HTTPException(status_code=404, detail=f"No se encontraron ubicaciones para SKU '{sku}'")

        return ubicaciones

    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")

    finally:
        cursor.close()
        conn.close()


@router.post("/productos/ubicacionNueva/{sku}")
async def nueva_ubicacion(sku: str, datos: UbicacionEditSchema):
    """
    Inserto fila nueva en stock_ubicacion para el sku recibido.
    Si ya existe ese sku, igual inserto — pueden haber varias ubicaciones.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        sql_ins = "INSERT INTO stock_ubicacion (sku, warehouse_id, cama, cantidad) VALUES (%s, %s, %s, %s)"
        cursor.execute(sql_ins, (sku, datos.warehouse_id, datos.cama, datos.cantidad))
        conn.commit()

        mov_reg.registrar_movimiento(datos.usuario, f"Creó nueva ubicación para SKU '{sku}'", "Productos")

        return {
            "mensaje": "Ubicación creada",
            "sku": sku,
            "warehouse_id": datos.warehouse_id,
            "cama": datos.cama,
            "cantidad": datos.cantidad
        }

    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")

    finally:
        cursor.close()
        conn.close()


@router.delete("/producto/eliminarUbi/{id}/{usuario}")
async def eliminar_ubicacion(id: int, usuario: str):
    """
    Borro el registro de stock_ubicacion que tenga ese id.
    Si no existe, aviso con 404.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        cursor.execute("DELETE FROM stock_ubicacion WHERE id = %s", (id,))

        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"No existe registro con id '{id}' en stock_ubicacion")

        conn.commit()
        mov_reg.registrar_movimiento(usuario, f"Eliminó ubicación con id '{id}'", "Productos")
        return {"mensaje": "Ubicación eliminada", "id": id}

    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")

    finally:
        cursor.close()
        conn.close()


@router.put("/ubicacion/editar/{id}")
async def editar_ubicacion(id: str, datos: UbicacionEditSchema):
    """
    Actualizo warehouse_id y cantidad en stock_ubicacion para el sku dado.
    Si no existe el registro, aviso con 404.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        sql_upd = "UPDATE stock_ubicacion SET warehouse_id = %s, cama = %s, cantidad = %s WHERE id = %s"
        cursor.execute(sql_upd, (datos.warehouse_id, datos.cama, datos.cantidad, id))

        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"No existe registro en stock_ubicacion para SKU '{id}'")

        conn.commit()
        mov_reg.registrar_movimiento(datos.usuario, f"Editó ubicación con id '{id}', cama: {datos.cama}, cantidad: {datos.cantidad} en ubicacion {datos.warehouse_id}", "Productos")
        mov_reg.registrar_edicionUbi(datos.sku, datos.warehouse_id, datos.cama, datos.cantidad)
        return {
            "mensaje": "Ubicación actualizada",
            "sku": id,
            "warehouse_id": datos.warehouse_id,
            "cama": datos.cama,
            "cantidad": datos.cantidad
        }

    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")

    finally:
        cursor.close()
        conn.close()

class DevolucionSchema(BaseModel):
    """Modelo para registrar una devolución de producto"""
    sku: str
    producto: str
    cantidad: int
    plataforma: str
    reingreso: bool
    usuario: str  # Usuario que registra la devolución, para registro de movimientos

@router.post("/producto/devolucion/{sku}")
async def registrar_devolucion(sku: str, datos: DevolucionSchema):
    """
    Registro una devolución de producto. Inserto una fila en devoluciones con los datos recibidos.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    try:
        sql_ins = "INSERT INTO devoluciones (sku, producto, cantidad, plataforma, reingreso) VALUES (%s, %s, %s, %s, %s)"
        cursor.execute(sql_ins, (sku, datos.producto, datos.cantidad, datos.plataforma, datos.reingreso))
        conn.commit()
        
        mov_reg.registrar_movimiento(datos.usuario, f"Registró devolución para SKU '{sku}'", "Productos")

        # Enviamos notificación a Telegram
        message = (
            f"↩️ <b>Devolución Registrada</b>\n\n"
            f"• <b>SKU:</b> {html.escape(sku)}\n"
            f"• <b>Producto:</b> {html.escape(datos.producto)}\n"
            f"• <b>Cantidad:</b> {datos.cantidad}\n"
            f"• <b>Plataforma:</b> {html.escape(datos.plataforma)}\n"
            f"• <b>Reingreso:</b> {datos.reingreso}\n"
            f"• <b>Usuario:</b> {html.escape(datos.usuario)}"
        )
        asyncio.create_task(send_telegram_alert(message))

        if datos.reingreso == True:
            # Si es reingreso, también actualizo el stock_bodega del producto sumando 1
            cursor.execute("UPDATE productos SET stock_bodega = stock_bodega + %s WHERE sku = %s", (datos.cantidad, sku,))
            conn.commit()

        return {
            "mensaje": "Devolución registrada",
            "sku": sku,
            "producto": datos.producto,
            "plataforma": datos.plataforma,
            "reingreso": datos.reingreso
        }

    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")

    finally:
        cursor.close()
        conn.close()

@router.get("/productos/devoluciones")
async def obtener_devoluciones():
    """
    Traigo todas las devoluciones registradas en la tabla devoluciones.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("SELECT * FROM devoluciones ORDER BY fecha DESC")
        devoluciones = cursor.fetchall()

        if not devoluciones:
            raise HTTPException(status_code=404, detail="No se han registrado devoluciones")

        return devoluciones

    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")

    finally:
        cursor.close()
        conn.close()

@router.get("/ubicaciones/registro/{sku}")
async def obtener_ubicaciones_registro(sku: str):
    """
    Traigo todas las ubicaciones registradas para un SKU específico.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("SELECT * FROM ubicaciones_editadas WHERE sku = %s", (sku,))
        ubicaciones = cursor.fetchall()

        if not ubicaciones:
            raise HTTPException(status_code=404, detail=f"No se encontraron ubicaciones para SKU '{sku}'")

        return ubicaciones

    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")

    finally:
        cursor.close()
        conn.close()