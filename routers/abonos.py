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


# Usuario de cobranza que debe enterarse de todos los abonos, sea quien sea que
# los capture. Se resuelve por nombre y no por id fijo: antes iba hardcodeado al
# id 2 y cualquier otro usuario (fparra, ventas) no recibia nada por WebSocket
# ni lo veia en su snapshot, porque `no_leidas` filtra por empleado_id.
USUARIO_COBRANZA = "gerencia"


async def notificar_abono(usuario: str, titulo: str, mensaje: str) -> None:
    """
    Manda la notificacion a cobranza y a quien registro el abono (sin duplicar
    si son el mismo). Los destinatarios que no existan en `usuarios` se ignoran:
    la notificacion no es critica y no debe tumbar el abono ya commiteado.
    """
    destinatarios = []
    for nombre in (USUARIO_COBRANZA, usuario):
        empleado_id = notificaciones_service.id_de_usuario(nombre)
        if empleado_id is not None and empleado_id not in destinatarios:
            destinatarios.append(empleado_id)

    for empleado_id in destinatarios:
        await notificaciones_service.crear_y_notificar(empleado_id, titulo, mensaje, "credito")

@router.get("/abonos-registro")
async def listar_abonos():
    """
    Cartera de crédito: una fila por venta con saldo pendiente, ya con sus abonos
    sumados. Uso LEFT JOIN para no perder las ventas que todavía no tienen abonos,
    y agrupo para no repetir la venta una vez por cada abono.
    """
    conn = get_db_connection()
    # Uso dictionary=True para devolver llaves nombradas y armar el JSON directo
    cursor = conn.cursor(dictionary=True)

    # Salgo de ventasRegistro (la cartera) y agrupo por venta: una venta de varios
    # productos tiene una fila por partida y en cobranza se cobra completa.
    # Los abonos van pre-agregados en subconsulta; si los cruzara directo, cada
    # partida se multiplicaría por cada abono e inflaría los totales.
    # TRIM porque hay ids capturados con espacios que si no parten la misma venta.
    query_join = """
    SELECT 
        v.id_registro,
        v.id_ventas,
        v.total,
        v.nombreComprador,
        v.partidas,
        v.skus,
        v.saldo_pendiente,
        v.fecha_vencimiento,
        v.fecha,
        COALESCE(ab.abonado, 0) AS abonado,
        COALESCE(ab.num_abonos, 0) AS num_abonos,
        ab.ultimo_abono
    FROM (
        -- 1. Agrupamos EXCLUSIVAMENTE las ventas primero
        SELECT 
            MIN(id) AS id_registro,
            TRIM(id_ventas) AS id_ventas,
            MAX(total) AS total,
            MIN(nombreComprador) AS nombreComprador,
            COUNT(*) AS partidas,
            GROUP_CONCAT(DISTINCT sku ORDER BY sku SEPARATOR ', ') AS skus,
            MAX(saldo_pendiente) AS saldo_pendiente,
            MIN(fecha_vencimiento) AS fecha_vencimiento,
            MIN(fecha) AS fecha
        FROM ventasRegistro
        WHERE saldo_pendiente > 0
        GROUP BY TRIM(id_ventas)
    ) v
    LEFT JOIN (
        -- 2. Agrupamos EXCLUSIVAMENTE los abonos
        SELECT 
            TRIM(id_ventas) AS id_ventas,
            SUM(saldo_abonado) AS abonado,
            COUNT(*) AS num_abonos,
            MAX(fecha_registro) AS ultimo_abono
        FROM abonos
        GROUP BY TRIM(id_ventas)
    ) ab ON v.id_ventas = ab.id_ventas
    
    -- 3. Ordenamiento final
    ORDER BY 
        v.fecha_vencimiento IS NULL, 
        v.fecha_vencimiento ASC, 
        v.id_registro DESC;
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
async def registrar_abono(abono: abono):
    """
    Agrega abono de saldo a crédito para clientes con este beneficio.
    Reglas blindadas en backend (sin triggers).
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=False)

    try:
        # 1. Obtenemos el saldo actual de la venta (MAX para evitar problemas de múltiples SKUs)
        query_check = "SELECT MAX(saldo_pendiente) FROM ventasRegistro WHERE id_ventas = %s"
        cursor.execute(query_check, (str(abono.id_ventas),))
        res = cursor.fetchone()

        # REGLA 1: Validamos si la venta no existe en la tabla
        if res[0] is None:
            raise HTTPException(status_code=404, detail="Operación rechazada: No se encontró el id_ventas.")
        
        saldo_actual = float(res[0])

        # REGLA 2: Validamos si la venta ya está liquidada
        if saldo_actual <= 0:
            raise HTTPException(status_code=400, detail="Operación rechazada: La venta ya fue liquidada.")
        
        # REGLA 3: Validamos que el abono no deje el saldo en negativo
        if (saldo_actual - abono.saldo_abonado) < 0:
            raise HTTPException(status_code=400, detail="Operación rechazada: El abono es mayor al saldo pendiente actual.")

        # 2. Si pasó todas las reglas, hacemos el UPDATE para restar el abono
        query_update = "UPDATE ventasRegistro SET saldo_pendiente = saldo_pendiente - %s WHERE TRIM(id_ventas) = %s"
        cursor.execute(query_update, (abono.saldo_abonado, str(abono.id_ventas)))

        # 3. Registramos el abono en el historial
        query_insert = """
            INSERT INTO abonos (id_ventas, saldo_abonado) 
            VALUES (%s, %s)
        """
        cursor.execute(query_insert, (str(abono.id_ventas), abono.saldo_abonado))

        # Calculamos el saldo restante matemáticamente para las notificaciones
        saldo_restante = saldo_actual - abono.saldo_abonado

        # 4. Confirmamos la transacción (Se guardan el UPDATE y el INSERT al mismo tiempo)
        conn.commit()        

        # Registramos el movimiento en el historial
        mov_reg.registrar_movimiento(abono.usuario, f"Registró un abono de {abono.saldo_abonado} para la venta {abono.id_ventas}", "Abonos")

        # --- sección de notificaciones ---
        if saldo_restante <= 0:
            await notificar_abono(
                abono.usuario,
                "Deuda Saldada",
                f"La venta {abono.id_ventas} ha sido liquidada totalmente. usuario: {abono.usuario}"
            )
            return {"mensaje": "Deuda saldada", "saldo_pendiente": 0}

        await notificar_abono(
            abono.usuario,
            "Abono Realizado",
            f"Se ha realizado un abono para la venta {abono.id_ventas}. usuario: {abono.usuario}"
        )
        return {"mensaje": "Abono realizado", "saldo_pendiente": saldo_restante}

    except mysql.connector.Error as err:
        conn.rollback() 
        raise HTTPException(status_code=500, detail=f"Error en DB: {err}")

    finally:
        cursor.close()
        conn.close()