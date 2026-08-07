import mysql.connector
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from typing import Optional, List
import os, mov_reg
from dotenv import load_dotenv

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
    frecuencia: Optional[str] = None
    usuario: str
    credito: bool
    monto_credito: Optional[int] = None
    

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

    query = "SELECT * FROM clientes ORDER BY id DESC"  # Ordenamos por nombre de cliente

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

@router.get("/clientes-potenciales")
async def obtener_clientes_potenciales():
    """
    Consulta los clientes potenciales registrados en DB.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True) 

    query = "SELECT * FROM clientes_potenciales ORDER BY id DESC LIMIT 1000"  # Ordenamos por nombre de cliente

    try:
        cursor.execute(query)
        clientes_potenciales = cursor.fetchall() 

        if not clientes_potenciales:
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
        query = "UPDATE clientes_potenciales SET revisado = %s WHERE id = %s"
        valores = (cliente.revisado, id)
    else:
        query = "UPDATE clientes_potenciales SET revisado = %s, correo_encontrado = %s WHERE id = %s"
        valores = (cliente.revisado, cliente.correo_encontrado, id)

    try:
        cursor.execute(query, valores)
        conn.commit()  # Guardamos los cambios en la base de datos

        # rowcount == 0 también ocurre cuando los valores enviados son idénticos a
        # los ya guardados, así que no se puede tratar como "no encontrado".
        return {
            "mensaje": "Cliente potencial actualizado con éxito",
            "id": id,
            "revisado": cliente.revisado,
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
    conn = get_db_connection()
    cursor = conn.cursor() 

    # El Query de inserción
    query = """
        INSERT INTO clientes (nombre, email, empresa, contacto, telefono, direccion, rfc, cp, regimen, usocfdi, frecuencia, usuario, credito, monto_credito) 
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    # Extraemos los valores del objeto cliente
    valores = (cliente.nombre, cliente.email, cliente.empresa, cliente.contacto, cliente.telefono, cliente.direccion, cliente.rfc, cliente.cp, cliente.regimen, cliente.usocdfi, cliente.frecuencia, cliente.usuario, cliente.credito, cliente.monto_credito)

    try:
        cursor.execute(query, valores)
        conn.commit() # ¡Vital para guardar en MySQL!
        mov_reg.registrar_movimiento(usuario, f"Registró un nuevo cliente: {cliente.nombre}", "Clientes")
        return {"mensaje": "Cliente agregado con éxito ", "id ": cursor.lastrowid}
    
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
        WHERE id = %s
    """

    # Extraemos los valores del objeto cliente (el id va al final)
    valores = (cliente.nombre, cliente.email, cliente.empresa, cliente.contacto, cliente.telefono, 
               cliente.direccion, cliente.rfc, cliente.cp, cliente.regimen, cliente.usocdfi, cliente.frecuencia, cliente.credito, cliente.monto_credito, cliente.dias_credito, cliente.id)

    try:
        cursor.execute(query, valores)
        conn.commit() # ¡Vital para guardar en MySQL!
        mov_reg.registrar_movimiento(usuario, f"Actualizó el cliente: {cliente.nombre}", "Clientes")
        # Aquí checo si realmente se actualizó un registro
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Cliente no encontrado")
        
        return {"mensaje": "Cliente actualizado con éxito", "id": cliente.id}
    
    except mysql.connector.Error as err:
        conn.rollback() # Si falla, cancelamos la operación
        raise HTTPException(status_code=500, detail=f"Error en DB: {err}")
    
    finally:
        cursor.close()
        conn.close()


