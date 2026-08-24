import mysql.connector, html, asyncio
from pydantic import BaseModel, ConfigDict
from fastapi import APIRouter, HTTPException
import os, mov_reg
from dotenv import load_dotenv
from servicios.telegram.notificacion import send_telegram_alert

router =APIRouter(tags=["/gastos"],responses={404: {"Mensaje":"No encontrado"}})
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
class Gasto(BaseModel):
    usuario_registro: str
    descripcion: str
    costo: float
    cantidad: int

# Modelo para respuesta de gastos consultados
class GastoResp(BaseModel):
    descripcion: str
    costo: float
    cantidad: int
    usuario_registro: str
    
    model_config = ConfigDict(
        alias_generator=lambda field_name: ''.join(
            word.capitalize() if i > 0 else word 
            for i, word in enumerate(field_name.split('_'))
        ),
        populate_by_name=True
    )

@router.post("/gastos") # Endpoint para registrar gastos operativos
async def registrar_gasto(gasto: Gasto):
    """
    Registra gastos operativos usados en cedis por usuario para la operacion.
    """
    conn = get_db_connection()
    cursor = conn.cursor()

    query = "INSERT INTO gastos (descripcion, costo, cantidad, usuario_registro) VALUES (%s, %s, %s, %s)"
    values = (gasto.descripcion, gasto.costo, gasto.cantidad, gasto.usuario_registro)

    try:
        cursor.execute(query, values)
        conn.commit()

        mov_reg.registrar_movimiento(gasto.usuario_registro, f"Registró un gasto: {gasto.descripcion} por {gasto.costo * gasto.cantidad}", "Gastos")

        # Enviamos notificación a Telegram
        message = (
            f"💸 <b>Gasto Registrado</b>\n\n"
            f"• <b>Descripción:</b> {html.escape(gasto.descripcion)}\n"
            f"• <b>Costo:</b> ${gasto.costo:,.2f}\n"
            f"• <b>Cantidad:</b> {gasto.cantidad}\n"
            f"• <b>Total:</b> ${gasto.costo * gasto.cantidad:,.2f}\n"
            f"• <b>Usuario:</b> {html.escape(gasto.usuario_registro)}"
        )
        asyncio.create_task(send_telegram_alert(message))

        return {"mensaje": "Gasto registrado exitosamente"}
    
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")
    
    finally:
        cursor.close()
        conn.close()

@router.get("/gastos") # Endpoint para listar todos los gastos operativos (dashboards/reportes)
async def listar_gastos():
    """
    Devuelve todos los gastos operativos registrados, con fecha, sin filtrar por usuario.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        query = "SELECT descripcion, costo, cantidad, total, usuario_registro, fecha_registro FROM gastos ORDER BY fecha_registro DESC"
        cursor.execute(query)
        return cursor.fetchall()

    except mysql.connector.Error as err:
        print(f"Error en consulta: {err}")
        raise HTTPException(status_code=500, detail=f"Error en consulta: {err}")

    finally:
        cursor.close()
        conn.close()

@router.get("/consultagastos") # Endpoint para consultar gastos del usuario actual
async def cons_gastos(usuario: str):
    """
    Consulta los gastos registrados por el usuario que envía la petición.
    Solo retorna los registros donde el usuario_registro coincida con el parámetro.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        if usuario == "fparra" or usuario == "gerencia":  # Usuarios con permisos para ver todos los gastos
            query = "SELECT descripcion, costo, cantidad, total, usuario_registro, fecha_registro FROM gastos ORDER BY fecha_registro DESC"

            cursor.execute(query)
            registros = cursor.fetchall()

        else:
            # Aquí traigo solo los gastos del usuario que consulta
            query = "SELECT descripcion, costo, cantidad, total, usuario_registro, fecha_registro FROM gastos WHERE usuario_registro = %s ORDER BY fecha_registro DESC"    
    
            cursor.execute(query, (usuario,))
            registros = cursor.fetchall()
        
        # Si no hay registros, devuelvo lista vacía
        if not registros:
            return {"datos": [], "cantidad": 0}        
        
        
        return registros
    
    except mysql.connector.Error as err:
        print(f"Error en consulta: {err}")
        raise HTTPException(status_code=500, detail=f"Error en consulta: {err}")
    
    finally:
        cursor.close()
        conn.close()