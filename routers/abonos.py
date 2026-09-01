import mysql.connector, asyncio, html
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
import os
from dotenv import load_dotenv
import mov_reg
import notificaciones_service
from servicios.telegram.notificacion import send_telegram_alert

# Obtiene la ruta del directorio padre (la raíz)
#sys.path.append(os.path.abspath(os.path.join(os.path.dirname(mov_reg.__file__), '..')))

router =APIRouter(tags=["/creditos"],responses={404: {"Mensaje":"No encontrado"}})
load_dotenv()

# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

class usuario(BaseModel):
    usuario: str    

class abono(BaseModel):
    usuario: str
    id_ventas: int
    saldo_abonado: float

@router.get("/abonos-registro")
async def listar_abonos():
    """
    Traigo todos los abonos y les pego los datos de la venta por el FK id_ventas.
    Así en una sola lista veo qué se abonó y de qué venta vino.
    """
    conn = get_db_connection()
    # Uso dictionary=True para devolver llaves nombradas y armar el JSON directo
    cursor = conn.cursor(dictionary=True)

    # JOIN por el FK id_ventas: del abono saco el saldo abonado, de la venta el resto
    query_join = """
        SELECT
            a.id,
            a.id_ventas,
            a.saldo_abonado,
            v.sku,
            v.producto,
            v.cantidad,
            v.precio,
            v.nombreComprador,
            v.saldo_pendiente
        FROM abonos a
        INNER JOIN ventasRegistro v ON a.id_ventas = v.id_ventas
        ORDER BY a.id DESC
    """

    try:
        cursor.execute(query_join)
        res = cursor.fetchall()        
        return res

    except mysql.connector.Error as err:
        # Si truena la DB, me entero aquí qué pasó
        print(f"Error en DB: {err}")
        raise HTTPException(status_code=500, detail=f"Error en DB: {err}")

    finally:
        cursor.close()
        conn.close()

@router.post("/abonos")
async def registrar_abono(abono: abono): # Cambié el nombre de la función para que sea más descriptivo
    """
    Agrega abono de saldo a crédito para clientes con este beneficio
    """
    conn = get_db_connection()
    # Forzamos dictionary=False aquí para asegurar que fetchone() devuelva una tupla indexada por posición
    cursor = conn.cursor(dictionary=False) 

    query_insert = """
        INSERT INTO abonos (id_ventas, saldo_abonado) 
        VALUES (%s, %s)
    """
    valores_insert = (str(abono.id_ventas), abono.saldo_abonado)

    try:
        # 1. Registrar el abono en el historial
        cursor.execute(query_insert, valores_insert)

        # 2. Restar lo abonado al saldo pendiente (Forzamos a string el id_ventas para evitar el 1292)
        query_update = "UPDATE ventasRegistro SET saldo_pendiente = saldo_pendiente - %s WHERE id_ventas = %s"
        cursor.execute(query_update, (abono.saldo_abonado, str(abono.id_ventas)))

        # 3. Consultar el saldo que quedó
        query_select = "SELECT saldo_pendiente FROM ventasRegistro WHERE id_ventas = %s"
        cursor.execute(query_select, (str(abono.id_ventas),))
        res = cursor.fetchone()

        # 4. Validar la respuesta ANTES de hacer el commit definitivo
        if res is None:
            conn.rollback() # Si la venta no existe, deshacemos el INSERT que hicimos arriba
            raise HTTPException(status_code=404, detail="Venta no encontrada en ventasRegistro")

        # Como forzamos dictionary=False, res[0] es completamente seguro
        saldo_restante = res[0]

        # 5. Si todo está perfecto, guardamos los cambios en la base de datos de AWS
        conn.commit()

        # Enviamos notificación a Telegram
        id_safe = html.escape(str(abono.id_ventas))
        usuario_safe = html.escape(str(abono.usuario))
        saldo_safe = html.escape(str(abono.saldo_abonado))
        saldo_safe = float(saldo_safe)

        message = (
            f"📋 <b>Nuevo Abono generado</b>\n\n"
            f"• <b>Código:</b> <code>{id_safe}</code>\n"           
            f"• <b>Saldo Abonado:</b> <b>${saldo_safe:,.2f}</b>\n"
            f"• <b>Usuario:</b> {usuario_safe}"
        )
        asyncio.create_task(send_telegram_alert(message))

        # Registramos el movimiento en el historial de movimientos
        mov_reg.registrar_movimiento(abono.usuario, f"Registró un abono de {abono.saldo_abonado} para la venta {abono.id_ventas}", "Abonos")

        # --- seccion de notificaciones ---
        # Guarda en MySQL y empuja por WebSocket al empleado 2 si esta conectado.
        if saldo_restante <= 0:
            await notificaciones_service.crear_y_notificar(
                2,
                "Deuda Saldada",
                f"La venta {abono.id_ventas} ha sido liquidada totalmente. usuario: {abono.usuario}",
                "credito"
            )
            return {"mensaje": "Deuda saldada", "saldo_pendiente": 0}

        await notificaciones_service.crear_y_notificar(
            2,
            "Abono Realizado",
            f"Se ha realizado un abono para la venta {abono.id_ventas}. usuario: {abono.usuario}",
            "credito"
        )
        return {"mensaje": "Abono realizado", "saldo_pendiente": saldo_restante}

    except mysql.connector.Error as err:
        conn.rollback() 
        raise HTTPException(status_code=500, detail=f"Error en DB: {err}")

    finally:
        cursor.close()
        conn.close()
