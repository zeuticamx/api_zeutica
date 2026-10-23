# fichero api de productos
import io
import mysql.connector, html, asyncio
from datetime import datetime
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
import os, mov_reg
from dotenv import load_dotenv
from typing import Optional
from servicios.telegram.notificacion import send_telegram_alert
from permisos import usuario_autenticado, requerir_gerencia
from errores_db import detalle_error_db

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
    usuario: Optional[str] = None  # se ignora: el usuario sale del token
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
    
    query = "SELECT sku, nombre, categoria, medida, ubicacion, stock_minimo," \
    " stock_bodega, stock_fba, stock_clean, stock_total, numero_referencia, costo_total, precio, " \
    "precio_2, precio_3, precio_amazon, precio_clean FROM productos ORDER BY sku ASC"
    
    try:
        cursor.execute(query) 
        productos = cursor.fetchall()
        
        if not productos:
            raise HTTPException(status_code=404, detail="No se han encontrado productos")
            
        return productos # FastAPI lo convierte automáticamente a JSON

    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))
    
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
    query = "SELECT id, sku, nombre, stock_bodega, stock_fba, stock_clean, stock_total, precio, precio_2, precio_3 FROM productos WHERE sku = %s"
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
            "stock_fba": resultado['stock_fba'],
            "stock_clean":resultado['stock_clean'],
            "stock_total": resultado['stock_total'],
            "precio": resultado['precio'],
            "precio_2": resultado['precio_2'],
            "precio_3": resultado['precio_3']
            }
    
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error en la base de datos"))
    
    finally:
        # 4. Siempre cerrar recursos
        cursor.close()
        conn.close()




@router.post("/productos/editados")
async def actualizar_productos(datos: ProdEditSchema, usuario: str = Depends(requerir_gerencia)):
    """
    Recibo una lista de productos editados desde el frontend.
    Solo columnas de la whitelist; stocks con validación >= 0 y detalle en bitácora.
    """
    COLUMNAS_PERMITIDAS = {
        "nombre", "categoria", "medida", "ubicacion", "stock_minimo",
        "numero_referencia", "costo_total", "precio", "precio_2", "precio_3",
        "precio_amazon", "precio_clean", "stock_bodega", "stock_fba", "stock_clean",
    }
    COLUMNAS_STOCK = {"stock_bodega", "stock_fba", "stock_clean", "stock_minimo"}
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
                
                # Construyo la consulta solo con columnas permitidas (evita inyección y
                # sobreescritura de id/sku/stock_total). Desconocidas se reportan.
                campos_actualizar = []
                valores = []
                ignoradas = []

                for columna, valor in prod.items():
                    if columna in ("id", "sku", "nombre"):
                        continue
                    if columna not in COLUMNAS_PERMITIDAS:
                        ignoradas.append(columna)
                        continue
                    if columna in COLUMNAS_STOCK:
                        try:
                            if valor is None or int(valor) < 0:
                                res_errores.append({
                                    "sku": prod["sku"],
                                    "nombre": prod["nombre"],
                                    "problema": f"Campo '{columna}' debe ser entero >= 0",
                                })
                                campos_actualizar = None
                                break
                        except (TypeError, ValueError):
                            res_errores.append({
                                "sku": prod["sku"],
                                "nombre": prod["nombre"],
                                "problema": f"Campo '{columna}' debe ser entero >= 0",
                            })
                            campos_actualizar = None
                            break
                    campos_actualizar.append(f"{columna} = %s")
                    valores.append(valor)

                if campos_actualizar is None:
                    continue

                if ignoradas:
                    res_errores.append({
                        "sku": prod.get("sku"),
                        "problema": f"Columnas ignoradas (no editables): {', '.join(ignoradas)}",
                    })

                # Si no hay campos para actualizar, lo salto (antes reutilizaba el
                # UPDATE del producto anterior y corrompía otro SKU).
                if not campos_actualizar:
                    res_errores.append({
                        "sku": prod["sku"],
                        "nombre": prod["nombre"],
                        "problema": "Sin campos actualizables",
                    })
                    continue

                # Armo el UPDATE SQL con los campos dinámicos (ya validados por whitelist)
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
        detalle_skus = ", ".join(r["sku"] for r in res_actualizados[:10])
        mov_reg.registrar_movimiento(usuario, f"Actualizó productos: {len(res_actualizados)} actualizados ({detalle_skus}), {len(res_errores)} errores", "Productos")
      
        # Enviamos notificación a Telegram
        message = (
            f"🛠️ <b>Actualización de Productos</b>\n\n"
            f"• <b>Usuario:</b> {html.escape(usuario)}\n"
            f"• <b>Actualizados:</b>\n{len(res_actualizados)}\n"
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
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error en base de datos"))
    
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
        # Nota: stock_bodega, stock_fba, stock_clean y stock_total empiezan en 0
        sql_insert = """
            INSERT INTO productos 
            (sku, nombre, categoria, medida, ubicacion, stock_minimo, numero_referencia, 
             costo_total, precio, precio_2, precio_3, stock_bodega, stock_fba, stock_clean)
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
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error en base de datos"))

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
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))

    finally:
        cursor.close()
        conn.close()


@router.post("/productos/ubicacionNueva/{sku}")
async def nueva_ubicacion(sku: str, datos: UbicacionEditSchema, usuario: str = Depends(usuario_autenticado)):
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

        mov_reg.registrar_movimiento(usuario, f"Creó nueva ubicación para SKU '{sku}'", "Productos")

        return {
            "mensaje": "Ubicación creada",
            "sku": sku,
            "warehouse_id": datos.warehouse_id,
            "cama": datos.cama,
            "cantidad": datos.cantidad
        }

    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))

    finally:
        cursor.close()
        conn.close()


@router.delete("/producto/eliminarUbi/{id}")
async def eliminar_ubicacion(id: int, usuario: str = Depends(usuario_autenticado)):
    """
    Borro el registro de stock_ubicacion que tenga ese id. Cualquier usuario con sesión; el usuario
    de la bitácora sale del token. Si no existe, aviso con 404.
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
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))

    finally:
        cursor.close()
        conn.close()


@router.delete("/producto/eliminarUbi/{id}/{_usuario_ignorado}", include_in_schema=False)
async def eliminar_ubicacion_ruta_vieja(id: int, _usuario_ignorado: str, usuario: str = Depends(usuario_autenticado)):
    """Alias de la ruta anterior (el usuario iba en la URL y se confiaba en él). Quitarlo cuando
    ningún panel use la versión vieja de api.jsx."""
    return await eliminar_ubicacion(id, usuario)


@router.put("/ubicacion/editar/{id}")
async def editar_ubicacion(id: str, datos: UbicacionEditSchema, usuario: str = Depends(usuario_autenticado)):
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
        mov_reg.registrar_movimiento(usuario, f"Editó ubicación con id '{id}', cama: {datos.cama}, cantidad: {datos.cantidad} en ubicacion {datos.warehouse_id}", "Productos")
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
        print(f"Error al actualizar ubicación: {err}")
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))

    finally:
        cursor.close()
        conn.close()

class TraspasoUbicacionSchema(BaseModel):
    """Traspaso informativo entre tarjetas del MISMO sku: origen -N, destino +N.
    Nunca toca productos/stock_bodega ni los traspasos FULL/Amazon."""
    sku: str = Field(min_length=1, max_length=100)
    origen_id: int = Field(gt=0)
    destino_id: int = Field(gt=0)
    cantidad: int = Field(gt=0)
    usuario: Optional[str] = None  # se ignora: el usuario sale del token
    nota: Optional[str] = Field(None, max_length=255)


def asegurar_tabla_traspasos_ubicacion(cursor):
    """Bitácora de traspasos entre tarjetas. Idempotente (CREATE IF NOT EXISTS)."""
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS ubicaciones_traspasos (
          id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
          sku VARCHAR(100) NOT NULL,
          origen_id INT NOT NULL,
          origen_warehouse_id VARCHAR(100) NOT NULL,
          destino_id INT NOT NULL,
          destino_warehouse_id VARCHAR(100) NOT NULL,
          cantidad INT NOT NULL,
          usuario VARCHAR(50) NOT NULL,
          nota VARCHAR(255) NULL,
          fecha DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
          INDEX idx_ubi_trasp_sku_fecha (sku, fecha)
        )"""
    )


@router.post("/ubicaciones/traspaso")
async def traspasar_entre_ubicaciones(datos: TraspasoUbicacionSchema, usuario: str = Depends(usuario_autenticado)):
    """
    Mueve cantidad entre dos tarjetas del mismo sku (origen -N, destino +N)
    y deja el registro en ubicaciones_traspasos. Todo en una transacción.
    Solo informativo: no toca productos ni los traspasos FULL/Amazon.
    """
    if datos.origen_id == datos.destino_id:
        raise HTTPException(status_code=422, detail="El origen y el destino deben ser tarjetas diferentes")
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        asegurar_tabla_traspasos_ubicacion(cursor)
        cursor.execute("SELECT id, sku, warehouse_id, cantidad FROM stock_ubicacion WHERE id = %s FOR UPDATE", (datos.origen_id,))
        origen = cursor.fetchone()
        if not origen:
            conn.rollback()
            raise HTTPException(status_code=404, detail=f"No existe la tarjeta origen con id '{datos.origen_id}'")
        cursor.execute("SELECT id, sku, warehouse_id, cantidad FROM stock_ubicacion WHERE id = %s FOR UPDATE", (datos.destino_id,))
        destino = cursor.fetchone()
        if not destino:
            conn.rollback()
            raise HTTPException(status_code=404, detail=f"No existe la tarjeta destino con id '{datos.destino_id}'")
        if (origen["sku"] or "") != datos.sku or (destino["sku"] or "") != datos.sku:
            conn.rollback()
            raise HTTPException(status_code=422, detail=f"El traspaso es solo entre tarjetas del mismo SKU '{datos.sku}'")
        stock_origen = origen["cantidad"] if origen["cantidad"] is not None else 0
        if stock_origen < datos.cantidad:
            conn.rollback()
            raise HTTPException(status_code=422, detail=f"Stock insuficiente en origen: disponible {stock_origen}, solicitado {datos.cantidad}")
        stock_destino = destino["cantidad"] if destino["cantidad"] is not None else 0
        # Regla Oficina (conserva piezas): llegar a Oficina desempaqueta (×mult),
        # salir de Oficina empaqueta (÷mult, solo múltiplos exactos).
        mult = multiplo_bulto(datos.sku)
        origen_es_oficina = es_oficina(origen["warehouse_id"])
        destino_es_oficina = es_oficina(destino["warehouse_id"])
        if destino_es_oficina and not origen_es_oficina:
            modo, suma_destino = "desempaquetar", datos.cantidad * mult
            nota_auto = f"Desempaquetado {datos.cantidad}×{mult}={suma_destino}"
        elif origen_es_oficina and not destino_es_oficina:
            if datos.cantidad % mult != 0:
                conn.rollback()
                raise HTTPException(status_code=422, detail=f"Para empaquetar, la cantidad debe ser múltiplo de {mult}")
            modo, suma_destino = "empaquetar", datos.cantidad // mult
            nota_auto = f"Empaquetado {datos.cantidad}÷{mult}={suma_destino}"
        else:
            modo, suma_destino = "directo", datos.cantidad
            nota_auto = None
        nuevo_origen = stock_origen - datos.cantidad
        nuevo_destino = stock_destino + suma_destino
        cursor.execute("UPDATE stock_ubicacion SET cantidad = %s WHERE id = %s", (nuevo_origen, datos.origen_id))
        cursor.execute("UPDATE stock_ubicacion SET cantidad = %s WHERE id = %s", (nuevo_destino, datos.destino_id))
        nota = "; ".join(p for p in ((datos.nota or "").strip(), nota_auto) if p) or None
        cursor.execute(
            "INSERT INTO ubicaciones_traspasos (sku, origen_id, origen_warehouse_id, destino_id, destino_warehouse_id, cantidad, usuario, nota)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (datos.sku, datos.origen_id, origen["warehouse_id"], datos.destino_id, destino["warehouse_id"], datos.cantidad, usuario, nota),
        )
        conn.commit()
        mov_reg.registrar_movimiento(usuario, f"Traspasó {datos.cantidad} de '{datos.sku}' de {origen['warehouse_id']} a {destino['warehouse_id']}" + (f" ({nota_auto})" if nota_auto else ""), "Productos")
        return {
            "mensaje": "Traspaso registrado",
            "sku": datos.sku,
            "cantidad": datos.cantidad,
            "conversion": {"modo": modo, "factor": mult, "cantidad_destino": suma_destino},
            "origen": {"id": datos.origen_id, "warehouse_id": origen["warehouse_id"], "cantidad": nuevo_origen},
            "destino": {"id": datos.destino_id, "warehouse_id": destino["warehouse_id"], "cantidad": nuevo_destino},
        }

    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))

    finally:
        cursor.close()
        conn.close()


@router.get("/ubicaciones/traspasos/{sku}")
async def historial_traspasos_ubicaciones(sku: str):
    """
    Historial informativo de traspasos entre tarjetas de un SKU.
    Vacío devuelve [] (no 404) para no ensuciar la vista con errores.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        asegurar_tabla_traspasos_ubicacion(cursor)
        cursor.execute("SELECT * FROM ubicaciones_traspasos WHERE sku = %s ORDER BY fecha DESC, id DESC", (sku,))
        return cursor.fetchall()

    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))

    finally:
        cursor.close()
        conn.close()


class MoverUbicacionSchema(BaseModel):
    """Mueve una tarjeta a otra celda: solo cambia warehouse_id (el ID de la celda).
    Cantidad y cama se quedan igual; no es un traspaso de stock."""
    sku: str = Field(min_length=1, max_length=100)
    warehouse_id: str = Field(min_length=1, max_length=100)
    usuario: Optional[str] = None  # se ignora: el usuario sale del token


@router.put("/ubicaciones/mover/{id}")
async def mover_ubicacion(id: int, datos: MoverUbicacionSchema, usuario: str = Depends(usuario_autenticado)):
    """
    Cambia la celda (warehouse_id) de una tarjeta. La celda destino puede
    tener tarjetas de otros SKUs o del mismo; el panel pide confirmación antes.
    """
    destino = datos.warehouse_id.strip()
    if not destino:
        raise HTTPException(status_code=422, detail="Indica la celda destino")
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("SELECT id, sku, warehouse_id, cama, cantidad FROM stock_ubicacion WHERE id = %s FOR UPDATE", (id,))
        tarjeta = cursor.fetchone()
        if not tarjeta:
            conn.rollback()
            raise HTTPException(status_code=404, detail=f"No existe la tarjeta con id '{id}'")
        if (tarjeta["sku"] or "") != datos.sku:
            conn.rollback()
            raise HTTPException(status_code=422, detail=f"La tarjeta #{id} no es del SKU '{datos.sku}'")
        origen = tarjeta["warehouse_id"] or ""
        if origen.strip().lower() == destino.lower():
            conn.rollback()
            raise HTTPException(status_code=422, detail=f"La tarjeta ya está en {destino}")
        cursor.execute("UPDATE stock_ubicacion SET warehouse_id = %s WHERE id = %s", (destino, id))
        conn.commit()
        # El cambio ya está confirmado: si la bitácora falla, no se le responde error al usuario.
        try:
            mov_reg.registrar_movimiento(usuario, f"Movió la tarjeta #{id} de '{datos.sku}' de {origen} a {destino}", "Productos")
            mov_reg.registrar_edicionUbi(datos.sku, destino, tarjeta["cama"], tarjeta["cantidad"])
        except Exception as err:
            print(f"Tarjeta #{id} movida, pero falló la bitácora: {err}")
        return {
            "mensaje": "Tarjeta movida",
            "id": id,
            "sku": datos.sku,
            "origen": origen,
            "destino": destino,
            "cantidad": tarjeta["cantidad"],
        }

    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))

    finally:
        cursor.close()
        conn.close()


# Múltiplos bulto->pieza para el reporte de conteo cíclico.
# ESPEJO de MULTIPLOS_POR_SKU en panel_gerencia/src/ubicaciones-logica.js:
# si se agrega un SKU allá, agregarlo aquí (y viceversa). El test
# test_ubicaciones_traspaso.py fija los valores para que no diverjan.
MULTIPLO_UBICACIONES_DEFAULT = 10
MULTIPLOS_UBICACIONES_POR_SKU = {
    "CUBMANBLA": 20,
    "CUBBARBLA": 20,
    "BOLECO17": 20,
    "BOLECO38": 5,
    "BOLECO30": 5,
    "ESPFARBLA": 1,
}


def multiplo_ubicacion(sku, warehouse_id) -> int:
    """Mismo criterio que obtenerMultiplo() del panel: Oficina siempre x1."""
    if str(warehouse_id or "").strip().lower() == "oficina":
        return 1
    return MULTIPLOS_UBICACIONES_POR_SKU.get(str(sku or "").strip().upper(), MULTIPLO_UBICACIONES_DEFAULT)


def multiplo_bulto(sku) -> int:
    """Múltiplo del SKU sin la regla de Oficina (para empaquetar/desempaquetar)."""
    return MULTIPLOS_UBICACIONES_POR_SKU.get(str(sku or "").strip().upper(), MULTIPLO_UBICACIONES_DEFAULT)


def es_oficina(warehouse_id) -> bool:
    return str(warehouse_id or "").strip().lower() == "oficina"


@router.get("/ubicaciones/reporte-conteo")
async def reporte_conteo_ciclico(sku: Optional[str] = None):
    """
    Plantilla Excel para inventarios cíclicos: una fila por tarjeta con el
    stock en sistema y columnas vacías para el conteo físico en bultos.
    Sin ?sku= trae todo; con ?sku= solo ese SKU. La diferencia es fórmula.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        if sku:
            cursor.execute(
                "SELECT s.sku, s.warehouse_id, s.cama, s.cantidad, p.nombre"
                " FROM stock_ubicacion s LEFT JOIN productos p ON p.sku = s.sku"
                " WHERE s.sku = %s ORDER BY s.warehouse_id",
                (sku,),
            )
        else:
            cursor.execute(
                "SELECT s.sku, s.warehouse_id, s.cama, s.cantidad, p.nombre"
                " FROM stock_ubicacion s LEFT JOIN productos p ON p.sku = s.sku"
                " ORDER BY s.sku, s.warehouse_id"
            )
        filas = cursor.fetchall()

    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))

    finally:
        cursor.close()
        conn.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Conteo ciclico"
    titulo = f"Conteo cíclico — {sku}" if sku else "Conteo cíclico — todas las ubicaciones"
    ws["A1"] = titulo
    ws["A1"].font = Font(bold=True, size=14)
    ws["A2"] = f"Generado: {datetime.now().strftime('%Y-%m-%d %H:%M')} · Cantidades del sistema en bultos; el conteo físico se captura en bultos"
    ws["A2"].font = Font(italic=True, size=10)

    encabezados = ["SKU", "Producto", "Ubicación", "Cama", "Cant. sistema (bultos)",
                   "Múltiplo", "Piezas sistema (ref)", "Conteo 1", "Conteo 2",
                   "Diferencia", "Observaciones"]
    fila_h = 3
    gris = PatternFill("solid", fgColor="D9D9D9")
    editable = PatternFill("solid", fgColor="FFF2CC")
    borde_fino = Border(*(Side(style="thin", color="B0B0B0") for _ in ("l", "r", "t", "b")))
    for col, texto in enumerate(encabezados, start=1):
        celda = ws.cell(row=fila_h, column=col, value=texto)
        celda.font = Font(bold=True)
        celda.fill = gris
        celda.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        celda.border = borde_fino

    # Columnas editables (conteo + observaciones) en amarillo claro.
    for i, fila in enumerate(filas, start=fila_h + 1):
        cantidad = fila["cantidad"] if fila["cantidad"] is not None else 0
        mult = multiplo_ubicacion(fila["sku"], fila["warehouse_id"])
        valores = [fila["sku"], fila.get("nombre") or "", fila["warehouse_id"],
                   fila["cama"] if fila["cama"] is not None else "",
                   cantidad, mult, cantidad * mult, "", "", f'=IF(H{i}="","",H{i}-E{i})', ""]
        for col, valor in enumerate(valores, start=1):
            celda = ws.cell(row=i, column=col, value=valor)
            celda.border = borde_fino
            if col in (8, 9, 11):
                celda.fill = editable

    ultima = fila_h + len(filas)
    ws.cell(row=ultima + 1, column=1, value="TOTALES").font = Font(bold=True)
    for col in ("E", "G", "H", "I", "J"):
        celda = ws.cell(row=ultima + 1, column=ord(col) - 64,
                        value=f"=SUM({col}{fila_h + 1}:{col}{ultima})")
        celda.font = Font(bold=True)
        celda.border = borde_fino

    anchos = [14, 34, 14, 8, 14, 9, 14, 11, 11, 11, 28]
    for col, ancho in enumerate(anchos, start=1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = ancho
    ws.freeze_panes = f"A{fila_h + 1}"
    ws.auto_filter.ref = f"A{fila_h}:K{ultima}"
    ws.sheet_properties.pageSetUpPr = openpyxl.worksheet.properties.PageSetupProperties(fitToPage=True)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    etiqueta = sku if sku else "todos"
    nombre = f"conteo-ciclico-{etiqueta}-{datetime.now().strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={nombre}"},
    )

class DevolucionSchema(BaseModel):
    """Modelo para registrar una devolución de producto"""
    sku: str
    producto: str
    cantidad: int = Field(gt=0)
    plataforma: str
    reingreso: bool
    usuario: str  # Usuario que registra la devolución, para registro de movimientos

# La tabla devoluciones nació sin usuario (solo quedaba en la bitácora).
COLUMNAS_DEVOLUCION = {
    "usuario": "VARCHAR(100) NULL",
}


def asegurar_columnas_devolucion():
    """Agrega a devoluciones las columnas faltantes (se llama en el lifespan)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                       "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'devoluciones'")
        existentes = {str(fila[0]).lower() for fila in cursor.fetchall()}
        for columna, definicion in COLUMNAS_DEVOLUCION.items():
            if columna not in existentes:
                cursor.execute(f"ALTER TABLE devoluciones ADD COLUMN {columna} {definicion}")
        conn.commit()
    finally:
        cursor.close()
        conn.close()

@router.post("/producto/devolucion/{sku}")
async def registrar_devolucion(sku: str, datos: DevolucionSchema):
    """
    Devolución en una sola transacción: INSERT + (si reingreso) UPDATE relativo.
    Un solo commit para no dejar devolución sin stock o viceversa.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        sql_ins = "INSERT INTO devoluciones (sku, producto, cantidad, plataforma, reingreso, usuario) VALUES (%s, %s, %s, %s, %s, %s)"
        cursor.execute(sql_ins, (sku, datos.producto, datos.cantidad, datos.plataforma, datos.reingreso, datos.usuario))

        if datos.reingreso:
            cursor.execute("SELECT stock_bodega FROM productos WHERE sku = %s FOR UPDATE", (sku,))
            fila = cursor.fetchone()
            if not fila:
                conn.rollback()
                raise HTTPException(status_code=404, detail=f"SKU '{sku}' no existe, no se puede reingresar")
            stock_anterior = fila["stock_bodega"] if fila["stock_bodega"] is not None else 0
            cursor.execute("UPDATE productos SET stock_bodega = COALESCE(stock_bodega, 0) + %s WHERE sku = %s", (datos.cantidad, sku,))
            if cursor.rowcount == 0:
                conn.rollback()
                raise HTTPException(status_code=404, detail=f"SKU '{sku}' no existe, no se puede reingresar")
        else:
            stock_anterior = None

        conn.commit()

        if datos.reingreso:
            mov_reg.registrar_movimiento(datos.usuario, f"Registró devolución con reingreso SKU '{sku}' cantidad {datos.cantidad} (stock_bodega {stock_anterior}->{stock_anterior + datos.cantidad})", "Productos")
        else:
            mov_reg.registrar_movimiento(datos.usuario, f"Registró devolución sin reingreso SKU '{sku}' cantidad {datos.cantidad}", "Productos")

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

        return {
            "mensaje": "Devolución registrada",
            "sku": sku,
            "producto": datos.producto,
            "plataforma": datos.plataforma,
            "reingreso": datos.reingreso
        }

    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))

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
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))

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
        mov_reg.asegurar_tabla_ubicaciones_editadas(cursor)
        cursor.execute("SELECT * FROM ubicaciones_editadas WHERE sku = %s", (sku,))
        ubicaciones = cursor.fetchall()

        if not ubicaciones:
            raise HTTPException(status_code=404, detail=f"No se encontraron ubicaciones para SKU '{sku}'")

        return ubicaciones

    except mysql.connector.Error as err:
        print(f"Error al obtener ubicaciones registradas: {err}")
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error de base de datos"))

    finally:
        cursor.close()
        conn.close()