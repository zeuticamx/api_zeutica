import mysql.connector, os, mov_reg
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from dotenv import load_dotenv
from typing import Optional

router =APIRouter(tags=["/proveedores"],responses={404: {"Mensaje":"No encontrado"}})
load_dotenv()

# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

@router.get("/proveedores") #Endpoint para consultar proveedores en base de datos
async def obtener_proveedores():
    """
    Consulta los proveedores registrados en DB.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True) 

    query = "SELECT * FROM proveedores ORDER BY proveedor ASC"  # Ordenamos por nombre de proveedor

    try:
        cursor.execute(query)
        proveedores = cursor.fetchall() 

        if not proveedores:
            raise HTTPException(status_code=404, detail="No se han encontrado proveedores en la base de datos.")
        
        return proveedores
    
    except mysql.connector.Error as err:
        print(f"Error en DB: {err}")
        raise HTTPException(status_code=500, detail=f"Error en DB: {err}")
    
    finally:
        cursor.close()
        conn.close()

class proveedor(BaseModel):
    proveedor: str
    contacto: str
    telefono: int
    email: Optional[str] = None
    direccion: Optional[str] = None
    credito: Optional[bool] = None

@router.post("/proveedor-nuevo") #Endpoint para agregar proveedores a la base de datos  
async def agregar_proveedor(nuevo_proveedor: proveedor):
    """
    Agrega un nuevo proveedor a la base de datos.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    query = "INSERT INTO proveedores (proveedor, contacto, telefono, email, direccion, credito) VALUES (%s, %s, %s, %s, %s, %s)"
    values = (nuevo_proveedor.proveedor, nuevo_proveedor.contacto, nuevo_proveedor.telefono, nuevo_proveedor.email, nuevo_proveedor.direccion, nuevo_proveedor.credito)

    try:
        cursor.execute(query, values)
        conn.commit()
        mov_reg.registrar_movimiento("gerencia", "proveedores", f"Proveedor agregado: {nuevo_proveedor.proveedor}")
        return {"mensaje": "Proveedor agregado exitosamente."}
    
    except mysql.connector.Error as err:
        print(f"Error en DB: {err}")
        raise HTTPException(status_code=500, detail=f"Error en DB: {err}")
    
    finally:
        cursor.close()
        conn.close()
   