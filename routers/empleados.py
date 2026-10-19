import mysql.connector
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException, Request
from typing import Optional
import os
from dotenv import load_dotenv
from errores_db import detalle_error_db

router =APIRouter(tags=["/empleados"],responses={404: {"Mensaje":"No encontrado"}})
load_dotenv()

# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

# Endpoint para llamar empleados con usuario a DB
@router.get("/empleados-usuarios")
async def empleados_con_usuarios():
    conn = get_db_connection()
    # Usamos dictionary=True para que nos devuelva los datos listos como un objeto JSON
    cursor = conn.cursor(dictionary=True)

    # El Query une ambas tablas usando la clave foránea
    query = """
        SELECT 
            e.id AS empleado_id,
            e.nombres,
            e.apellido_paterno,
            e.apellido_materno,
            e.edad,
            e.curp,
            e.nss,
            e.estatus,
            u.nombre_usuario AS usuario,            
            u.rol                     
        FROM empleados e
        INNER JOIN usuarios u ON e.usuario = u.nombre_usuario
    """

    try:
        cursor.execute(query)
        resultados = cursor.fetchall()

        for emp in resultados:
            emp["estatus"] = bool(emp["estatus"])
        
        # Si no hay empleados todavía, devolvemos una lista vacía
        return resultados

    except mysql.connector.Error as err:        
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error en la base de datos"))
        
    finally:
        cursor.close()
        conn.close()
    
# Schema base sin id — lo reutilizo para crear y para editar
class EmpleadoBase(BaseModel):
    nombres: str
    apellido_paterno: str
    apellido_materno: str
    edad: int
    curp: Optional[str] = None
    nss: Optional[str] = None
    usuario: str
    estatus: bool

# Para el PUT necesito el id además de los datos base
class EmpleadoUpdate(EmpleadoBase):
    id: int

@router.put("/empleados-editados")
async def actualizar_empleado(request: Request):
    # Agarro el body crudo para ver qué llega antes de que Pydantic lo rechace
    raw_body = await request.json()
    try:
        datos = EmpleadoUpdate(**raw_body)
    except Exception as val_err:
        print(f"[DEBUG 422] Body recibido: {raw_body}")
        print(f"[DEBUG 422] Error de validación: {val_err}")
        raise HTTPException(status_code=422, detail=str(val_err))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # El Query actualizará todos los campos basados en el ID único
    query = """
        UPDATE empleados 
        SET nombres = %s, 
            apellido_paterno = %s, 
            apellido_materno = %s, 
            edad = %s, 
            curp = %s, 
            nss = %s, 
            usuario = %s,
            estatus = %s
        WHERE id = %s
    """
    
    valores = (
        datos.nombres,
        datos.apellido_paterno,
        datos.apellido_materno,
        datos.edad,
        datos.curp,
        datos.nss,
        datos.usuario,
        datos.estatus,  # Python convierte True/False automáticamente a 1/0 para MySQL
        datos.id
    )

    try:
        cursor.execute(query, valores)
        conn.commit()

        # rowcount nos dice cuántas filas se modificaron en la base de datos
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"No se encontró ningún empleado con el ID {datos.id}")

        return {"status": "success", "message": f"Empleado con ID {datos.id} actualizado correctamente"}

    except mysql.connector.Error as err:
        # Si mandan un usuario que no existe, la clave foránea saltará aquí (Error 1452)
        raise HTTPException(status_code=400, detail=detalle_error_db(err, "Error al actualizar la base de datos"))

    finally:
        cursor.close()
        conn.close()

@router.post("/empleado/nuevo", status_code=201)
async def crear_empleado(datos: EmpleadoBase):
    conn = get_db_connection()
    cursor = conn.cursor()

    query = """
        INSERT INTO empleados (nombres, apellido_paterno, apellido_materno, edad, curp, nss, usuario, estatus)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """

    vals = (
        datos.nombres,
        datos.apellido_paterno,
        datos.apellido_materno,
        datos.edad,
        datos.curp,
        datos.nss,
        datos.usuario,
        datos.estatus,
    )

    try:
        cursor.execute(query, vals)
        conn.commit()
        # Guardo el id generado para regresárselo al cliente
        nuevo_id = cursor.lastrowid
        return {"status": "success", "message": "Empleado creado", "id": nuevo_id}

    except mysql.connector.Error as err:
        # Error 1452 = FK fail (usuario no existe), 1062 = CURP/NSS duplicado
        raise HTTPException(status_code=400, detail=detalle_error_db(err, "Error al insertar empleado"))

    finally:
        cursor.close()
        conn.close()

@router.get("/registro-login")
async def obtener_registro_login():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        query = "SELECT * FROM registro_login ORDER BY fecha DESC"
        cursor.execute(query)
        resultados = cursor.fetchall()

        if not resultados:
            raise HTTPException(status_code=404, detail="No se encontraron registros de login")

    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error al obtener registros de login"))

    finally:
        cursor.close()
        conn.close()

    return {"status": "success", "data": resultados}