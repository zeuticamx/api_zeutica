import mysql.connector, html, asyncio
from pydantic import BaseModel, ConfigDict, Field, field_validator
from fastapi import APIRouter, HTTPException, Depends
import os, mov_reg
from dotenv import load_dotenv
from servicios.telegram.notificacion import send_telegram_alert
from permisos import requerir_gerencia

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

# Modelo para editar un gasto existente (solo gerencia)
class GastoEditar(BaseModel):
    descripcion: str
    costo: float = Field(ge=0)
    cantidad: int = Field(ge=1)

    @field_validator("descripcion")
    @classmethod
    def descripcion_no_vacia(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("La descripción no puede estar vacía")
        return v

# Columnas del borrado lógico: un gasto eliminado se conserva pero no cuenta en listados ni en $$
COLUMNAS_ELIMINADO = {
    "eliminado": "TINYINT(1) NOT NULL DEFAULT 0",
    "eliminado_por": "VARCHAR(100) NULL",
    "fecha_eliminado": "DATETIME NULL",
}

def asegurar_columnas_eliminado():
    """Agrega a gastos las columnas del borrado lógico si faltan (se llama en el lifespan)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'gastos'")
        existentes = {str(fila[0]).lower() for fila in cursor.fetchall()}
        for columna, definicion in COLUMNAS_ELIMINADO.items():
            if columna not in existentes:
                cursor.execute(f"ALTER TABLE gastos ADD COLUMN {columna} {definicion}")
        conn.commit()
    finally:
        cursor.close()
        conn.close()

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

        """
        message = (
            f"💸 <b>Gasto Registrado</b>\n\n"
            f"• <b>Descripción:</b> {html.escape(gasto.descripcion)}\n"
            f"• <b>Costo:</b> ${gasto.costo:,.2f}\n"
            f"• <b>Cantidad:</b> {gasto.cantidad}\n"
            f"• <b>Total:</b> ${gasto.costo * gasto.cantidad:,.2f}\n"
            f"• <b>Usuario:</b> {html.escape(gasto.usuario_registro)}"
        )
        asyncio.create_task(send_telegram_alert(message))
        """
        
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
        query = "SELECT id, descripcion, costo, cantidad, total, usuario_registro, fecha_registro FROM gastos WHERE eliminado = 0 ORDER BY fecha_registro DESC"
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
            query = "SELECT id, descripcion, costo, cantidad, total, usuario_registro, fecha_registro FROM gastos WHERE eliminado = 0 ORDER BY fecha_registro DESC"

            cursor.execute(query)
            registros = cursor.fetchall()

        else:
            # Aquí traigo solo los gastos del usuario que consulta
            query = "SELECT id, descripcion, costo, cantidad, total, usuario_registro, fecha_registro FROM gastos WHERE eliminado = 0 AND usuario_registro = %s ORDER BY fecha_registro DESC"    
    
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


@router.put("/gastos/{id}") # Endpoint para editar un gasto operativo (solo gerencia)
async def editar_gasto(id: int, datos: GastoEditar, usuario: str = Depends(requerir_gerencia)):
    """
    Actualiza descripción, costo y cantidad de un gasto. El total se recalcula en BD.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("SELECT id, descripcion, costo, cantidad FROM gastos WHERE id = %s AND eliminado = 0", (id,))
        anterior = cursor.fetchone()
        if not anterior:
            raise HTTPException(status_code=404, detail=f"No existe el gasto con id '{id}'")

        cursor.execute(
            "UPDATE gastos SET descripcion = %s, costo = %s, cantidad = %s WHERE id = %s AND eliminado = 0",
            (datos.descripcion, datos.costo, datos.cantidad, id),
        )
        conn.commit()

        mov_reg.registrar_movimiento(
            usuario,
            f"Editó el gasto {id}: '{anterior['descripcion']}' {anterior['costo']} x {anterior['cantidad']} -> '{datos.descripcion}' {datos.costo} x {datos.cantidad}",
            "Gastos",
        )
        return {"mensaje": "Gasto actualizado exitosamente", "id": id}

    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")

    finally:
        cursor.close()
        conn.close()

@router.delete("/gastos/{id}") # Endpoint para eliminar (marcar como eliminado) un gasto operativo (solo gerencia)
async def eliminar_gasto(id: int, usuario: str = Depends(requerir_gerencia)):
    """
    Borrado lógico: el gasto se conserva con eliminado = 1 y deja de contar en listados y totales.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        cursor.execute("SELECT id, descripcion, costo, cantidad FROM gastos WHERE id = %s AND eliminado = 0", (id,))
        gasto = cursor.fetchone()
        if not gasto:
            raise HTTPException(status_code=404, detail=f"No existe el gasto con id '{id}'")

        cursor.execute(
            "UPDATE gastos SET eliminado = 1, eliminado_por = %s, fecha_eliminado = NOW() WHERE id = %s AND eliminado = 0",
            (usuario, id),
        )
        conn.commit()

        mov_reg.registrar_movimiento(
            usuario,
            f"Eliminó el gasto {id}: '{gasto['descripcion']}' {gasto['costo']} x {gasto['cantidad']}",
            "Gastos",
        )
        return {"mensaje": "Gasto eliminado exitosamente", "id": id}

    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")

    finally:
        cursor.close()
        conn.close()
