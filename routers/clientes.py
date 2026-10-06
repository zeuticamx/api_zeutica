import mysql.connector, html, asyncio
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException
from typing import Optional, List
import os, mov_reg
from dotenv import load_dotenv
from servicios.telegram.notificacion import send_telegram_alert
from permisos import requerir_gerencia

router =APIRouter(tags=["/clientes"],responses={404: {"Mensaje":"No encontrado"}})
load_dotenv()

# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

# Columnas del borrado lógico: un cliente eliminado se conserva (ventas, CRM y cotizaciones
# siguen apuntando a él) pero no aparece en listados, CRM ni comisiones.
COLUMNAS_ELIMINADO = {
    "eliminado": "TINYINT(1) NOT NULL DEFAULT 0",
    "eliminado_por": "VARCHAR(100) NULL",
    "fecha_eliminado": "DATETIME NULL",
}

def asegurar_columnas_eliminado():
    """Agrega a clientes las columnas del borrado lógico si faltan (se llama en el lifespan)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'clientes'")
        existentes = {str(fila[0]).lower() for fila in cursor.fetchall()}
        for columna, definicion in COLUMNAS_ELIMINADO.items():
            if columna not in existentes:
                cursor.execute(f"ALTER TABLE clientes ADD COLUMN {columna} {definicion}")
        conn.commit()
    finally:
        cursor.close()
        conn.close()

# Definimos un modelo para recibir los datos en JSON (Body)
class Cliente(BaseModel):    
    nombre: str
    email: Optional[str] = None
    empresa: str
    contacto: str
    telefono: int
    direccion: Optional[str] = None

class clienteRfc(Cliente): # molde con herencia para cliente factura
    rfc: Optional[str] = None
    cp: Optional[int] = None
    regimen: Optional[str] = None
    usocdfi: Optional[str] = None
    uso_cfdi: Optional[str] = None  # nombre que envía el panel; usocdfi se conserva por compatibilidad
    frecuencia: Optional[str] = None
    usuario: str
    credito: bool
    monto_credito: Optional[int] = None
    dias_credito: Optional[int] = None
    

class clienteEditar(clienteRfc): # molde para editar cliente con id
    id: int
    frecuencia: str    
    dias_credito: int

@router.get("/clientes") #Endpoint para consultar clientes en base de datos
async def obtener_clientes():
    """
    Consulta los clientes registrados en DB.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True) 

    query = "SELECT * FROM clientes WHERE eliminado = 0 ORDER BY id DESC"

    try:
        cursor.execute(query)
        clientes = cursor.fetchall() 

        if not clientes:
            raise HTTPException(status_code=404, detail="No se han encontrado clientes en la base de datos.")  
        
        return clientes
    
    except mysql.connector.Error as err:
        print(f"Error DB clientes: {err}")
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")
    
    finally:
        cursor.close()
        conn.close()

@router.get("/clientes-eliminados")
async def obtener_clientes_eliminados(usuario: str = Depends(requerir_gerencia)):
    """Clientes dados de baja (solo gerencia), para poder restaurarlos. Sin eliminados devuelve []."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM clientes WHERE eliminado = 1 ORDER BY fecha_eliminado DESC, id DESC")
        return cursor.fetchall()
    except mysql.connector.Error as err:
        print(f"Error DB clientes eliminados: {err}")
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")
    finally:
        cursor.close()
        conn.close()

@router.get("/clientes-potenciales")
async def obtener_clientes_potenciales(descartados: bool = False):
    """
    Consulta los clientes potenciales registrados en DB.
    Por defecto excluye los descartados; con ?descartados=true devuelve solo los descartados.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    query = "SELECT * FROM clientes_potenciales WHERE descartado = %s ORDER BY id DESC LIMIT 1000"

    try:
        cursor.execute(query, (1 if descartados else 0,))
        clientes_potenciales = cursor.fetchall()

        if not clientes_potenciales:
            if descartados:
                return []  # sin descartados no es un error
            raise HTTPException(status_code=404, detail="No se han encontrado clientes potenciales en la base de datos.")  
        
        return clientes_potenciales
    
    except mysql.connector.Error as err:
        print(f"Error DB clientes potenciales: {err}")
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")
    
    finally:
        cursor.close()
        conn.close() 

class ClientePotencial(BaseModel):
    id: int
    revisado: bool
    descartado: bool = False
    correo_encontrado: Optional[str] = None

class NotaCliente(BaseModel):
    id: int
    notas: str

# IMPORTANTE: esta ruta debe declararse ANTES de "/clientes-potenciales/{id}".
# FastAPI resuelve por orden de registro; si {id} va primero intenta parsear
# "notas-lote" como int y responde 422 en lugar de llegar aquí.
@router.patch("/clientes-potenciales/notas-lote")
async def actualizar_notas_por_lista(lista_clientes: List[NotaCliente]):
    conn = get_db_connection()
    cursor = conn.cursor()

    query = """
        UPDATE clientes_potenciales
        SET notas = %s
        WHERE id = %s
    """

    # Preparamos una lista de tuplas: [("Nota A", 1), ("Nota B", 2), ...]
    valores = [(c.notas, c.id) for c in lista_clientes]

    try:
        # executemany ejecuta el UPDATE en bloque para todos los elementos de la lista
        cursor.executemany(query, valores)
        conn.commit()

        return {
            "mensaje": "Notas actualizadas con éxito",
            "total_actualizados": cursor.rowcount
        }

    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error al actualizar notas en lote: {err}")
        raise HTTPException(status_code=500, detail=f"Error en DB: {err}")

    finally:
        cursor.close()
        conn.close()

@router.patch("/clientes-potenciales/{id}") # Endpoint para actualizar un cliente potencial existente
async def actualizar_cliente_potencial(id: int, cliente: ClientePotencial):
    """
    Actualiza un cliente potencial en la base de datos.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    # correo_encontrado es opcional: si no viene en el body no se toca la columna,
    # así el panel puede actualizar solo el check de revisado sin borrar el correo.
    if cliente.correo_encontrado is None:
        query = "UPDATE clientes_potenciales SET revisado = %s, descartado = %s WHERE id = %s"
        valores = (cliente.revisado, cliente.descartado, id)
    else:
        query = "UPDATE clientes_potenciales SET revisado = %s, descartado = %s, correo_encontrado = %s WHERE id = %s"
        valores = (cliente.revisado, cliente.descartado, cliente.correo_encontrado, id)

    try:
        cursor.execute(query, valores)
        conn.commit()  # Guardamos los cambios en la base de datos

        # rowcount == 0 también ocurre cuando los valores enviados son idénticos a
        # los ya guardados, así que no se puede tratar como "no encontrado".
        return {
            "mensaje": "Cliente potencial actualizado con éxito",
            "id": id,
            "revisado": cliente.revisado,
            "descartado": cliente.descartado,
            "correo_encontrado": cliente.correo_encontrado,
        }
    
    except mysql.connector.Error as err:
        conn.rollback()  # Cancelamos la operación si falla
        raise HTTPException(status_code=500, detail=f"Error en DB: {err}")
    
    finally:
        cursor.close()
        conn.close()


@router.post("/clientenuevo/{usuario}") # Enpoint para agregar cliente a la base de datos
async def cliente_nuevo(cliente: clienteRfc, usuario: str):
    """
    Dependencia para ingresar un cliente nuevo a DB.
    """
    nombre = (cliente.nombre or "").strip()
    if not nombre:
        raise HTTPException(status_code=422, detail="El nombre del cliente es obligatorio")

    conn = get_db_connection()
    cursor = conn.cursor() 

    # El Query de inserción
    query = """
        INSERT INTO clientes (nombre, email, empresa, contacto, telefono, direccion, rfc, cp, regimen, usocfdi, frecuencia, usuario, credito, monto_credito, dias_credito) 
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    # El panel manda uso_cfdi; usocdfi (typo histórico) sigue aceptándose
    uso_cfdi = cliente.uso_cfdi or cliente.usocdfi

    # Extraemos los valores del objeto cliente
    valores = (nombre, cliente.email, cliente.empresa, cliente.contacto, cliente.telefono, cliente.direccion, cliente.rfc, cliente.cp, cliente.regimen, uso_cfdi, cliente.frecuencia, cliente.usuario, cliente.credito, cliente.monto_credito, cliente.dias_credito or 0)

    try:
        # El selector de cotizaciones identifica al cliente por nombre: se evitan duplicados al crear
        cursor.execute("SELECT id FROM clientes WHERE LOWER(TRIM(nombre)) = LOWER(%s) LIMIT 1", (nombre,))
        if cursor.fetchone():
            raise HTTPException(status_code=409, detail=f"Ya existe un cliente llamado '{nombre}'")

        cursor.execute(query, valores)
        conn.commit() # ¡Vital para guardar en MySQL!
        nuevo_id = cursor.lastrowid

        mov_reg.registrar_movimiento(usuario, f"Registró un nuevo cliente: {nombre}", "Clientes")

        # Enviamos notificación a Telegram
        empresa_safe = html.escape(str(cliente.empresa))
        usuario_safe = html.escape(str(cliente.usuario))

        message = (
            f"📋 <b>Cliente Nuevo Registrado</b>\n\n"
            f"• <b>Código:</b> <code>{nuevo_id}</code>\n"
            f"• <b>Empresa:</b> {empresa_safe}\n"
            f"• <b>Usuario:</b> {usuario_safe}"
        )
        asyncio.create_task(send_telegram_alert(message))

        # "id " (con espacio) se conserva por compatibilidad; "id" es la clave correcta
        return {"mensaje": "Cliente agregado con éxito ", "id": nuevo_id, "id ": nuevo_id, "nombre": nombre, "empresa": cliente.empresa, "usuario": cliente.usuario}
    
    except mysql.connector.Error as err:
        conn.rollback() # Si falla, cancelamos la operación
        raise HTTPException(status_code=500, detail=f"Error en DB: {err}")
    
    finally:
        cursor.close()
        conn.close()


@router.post("/editcliente/{usuario}") # Endpoint para editar cliente existente en la base de datos
async def edit_cliente(cliente: clienteEditar, usuario: str):
    """
    Dependencia para editar un cliente ya registrado.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    # El Query de actualización
    query = """
        UPDATE clientes 
        SET nombre = %s, email = %s, empresa = %s, contacto = %s, telefono = %s, 
            direccion = %s, rfc = %s, cp = %s, regimen = %s, usocfdi = %s, frecuencia = %s, credito = %s, monto_credito = %s, dias_credito = %s
        WHERE id = %s AND eliminado = 0
    """

    # Extraemos los valores del objeto cliente (el id va al final)
    valores = (cliente.nombre, cliente.email, cliente.empresa, cliente.contacto, cliente.telefono, 
               cliente.direccion, cliente.rfc, cliente.cp, cliente.regimen, cliente.uso_cfdi or cliente.usocdfi, cliente.frecuencia, cliente.credito, cliente.monto_credito, cliente.dias_credito, cliente.id)

    try:
        cursor.execute(query, valores)
        conn.commit() # ¡Vital para guardar en MySQL!        

        # Enviamos notificación a Telegram
        empresa_safe = html.escape(str(cliente.empresa))
        usuario_safe = html.escape(str(cliente.usuario))

        message = (
            f"📋 <b>Cliente Actualizado</b>\n\n"
            f"• <b>Código:</b> <code>{cliente.id}</code>\n"
            f"• <b>Empresa:</b> {empresa_safe}\n"
            f"• <b>Usuario:</b> {usuario_safe}"
        )
        asyncio.create_task(send_telegram_alert(message))

        # Aquí checo si realmente se actualizó un registro
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Cliente no encontrado")

        mov_reg.registrar_movimiento(usuario, f"Actualizó el cliente: {cliente.nombre}", "Clientes")
        
        return {"mensaje": "Cliente actualizado con éxito", "id": cliente.id}
    
    except mysql.connector.Error as err:
        conn.rollback() # Si falla, cancelamos la operación
        raise HTTPException(status_code=500, detail=f"Error en DB: {err}")
    
    finally:
        cursor.close()
        conn.close()


@router.delete("/clientes/{id}") # Endpoint para dar de baja (borrado lógico) a un cliente (solo gerencia)
async def eliminar_cliente(id: int, usuario: str = Depends(requerir_gerencia)):
    """
    Borrado lógico: el cliente se conserva con eliminado = 1 y deja de aparecer en listados, CRM y comisiones.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, nombre, empresa FROM clientes WHERE id = %s AND eliminado = 0", (id,))
        cliente = cursor.fetchone()
        if not cliente:
            raise HTTPException(status_code=404, detail=f"No existe el cliente con id '{id}'")

        cursor.execute(
            "UPDATE clientes SET eliminado = 1, eliminado_por = %s, fecha_eliminado = NOW() WHERE id = %s AND eliminado = 0",
            (usuario, id),
        )
        conn.commit()

        mov_reg.registrar_movimiento(usuario, f"Eliminó el cliente {id}: {cliente['nombre']}", "Clientes")

        message = (
            f"🗑️ <b>Cliente Eliminado</b>\n\n"
            f"• <b>Código:</b> <code>{id}</code>\n"
            f"• <b>Cliente:</b> {html.escape(str(cliente['nombre']))}\n"
            f"• <b>Usuario:</b> {html.escape(str(usuario))}"
        )
        asyncio.create_task(send_telegram_alert(message))
        return {"mensaje": "Cliente eliminado exitosamente", "id": id}

    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")

    finally:
        cursor.close()
        conn.close()


@router.post("/clientes/{id}/restaurar") # Endpoint para restaurar un cliente dado de baja (solo gerencia)
async def restaurar_cliente(id: int, usuario: str = Depends(requerir_gerencia)):
    """
    Revierte la baja lógica. Si ya existe otro cliente activo con el mismo nombre responde 409
    (el selector de cotizaciones identifica al cliente por nombre).
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, nombre FROM clientes WHERE id = %s AND eliminado = 1", (id,))
        cliente = cursor.fetchone()
        if not cliente:
            raise HTTPException(status_code=404, detail=f"No existe un cliente eliminado con id '{id}'")

        cursor.execute(
            "SELECT id FROM clientes WHERE eliminado = 0 AND LOWER(TRIM(nombre)) = LOWER(TRIM(%s)) LIMIT 1",
            (cliente["nombre"],),
        )
        if cursor.fetchone():
            raise HTTPException(status_code=409, detail=f"Ya existe un cliente activo llamado '{cliente['nombre']}'")

        cursor.execute(
            "UPDATE clientes SET eliminado = 0, eliminado_por = NULL, fecha_eliminado = NULL WHERE id = %s AND eliminado = 1",
            (id,),
        )
        conn.commit()

        mov_reg.registrar_movimiento(usuario, f"Restauró el cliente {id}: {cliente['nombre']}", "Clientes")
        return {"mensaje": "Cliente restaurado exitosamente", "id": id}

    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")

    finally:
        cursor.close()
        conn.close()
