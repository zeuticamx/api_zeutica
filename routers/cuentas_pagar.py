import mysql.connector, html, asyncio
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from datetime import date, timedelta
import os
from dotenv import load_dotenv
from servicios.telegram.notificacion import send_telegram_alert

router = APIRouter(tags=["/cuentas-pagar"], responses={404: {"Mensaje": "No encontrado"}})
load_dotenv()


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )


def _init_tablas():
    # Creo las tablas al arrancar el módulo. Si ya existen, no pasa nada.
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cuentas_por_pagar (
                id                INT           AUTO_INCREMENT PRIMARY KEY,
                num_factura       VARCHAR(50)   NOT NULL,
                proveedor         VARCHAR(150)  NOT NULL,
                fecha_factura     DATE          NOT NULL,
                condicion_pago    VARCHAR(20)   NOT NULL DEFAULT 'CREDITO',
                plazo_dias        INT           NOT NULL,
                fecha_vencimiento DATE          NOT NULL,
                total             DECIMAL(12,2) NOT NULL,
                abonado           DECIMAL(12,2) NOT NULL DEFAULT 0,
                saldo_pendiente   DECIMAL(12,2) NOT NULL,
                estado            VARCHAR(20)   NOT NULL DEFAULT 'PENDIENTE',
                usuario           VARCHAR(50),
                created_at        TIMESTAMP     DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pagos_proveedor (
                id          INT           AUTO_INCREMENT PRIMARY KEY,
                id_cuenta   INT           NOT NULL,
                monto       DECIMAL(12,2) NOT NULL,
                fecha_pago  DATE          NOT NULL,
                metodo      VARCHAR(50),
                referencia  VARCHAR(100),
                usuario     VARCHAR(50),
                created_at  TIMESTAMP     DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (id_cuenta) REFERENCES cuentas_por_pagar(id)
            )
        """)
        conn.commit()
    except mysql.connector.Error as err:
        print(f"Error creando tablas CxP: {err}")
    finally:
        cursor.close()
        conn.close()


#_init_tablas()


class CuentaPagar(BaseModel):
    num_factura: str
    proveedor: str
    fecha_factura: date
    condicion_pago: str
    plazo_dias: int
    total: float
    usuario: str


class PagoProveedor(BaseModel):
    id_cuenta: int
    monto: float
    metodo: str
    referencia: str
    fecha_pago: date
    usuario: str


def calc_estado(saldo: float, abonado: float, f_venc: date) -> str:
    # Calculo el estado según saldo y vencimiento para no repetir esto en cada endpoint
    if saldo <= 0:
        return "PAGADA"
    if f_venc < date.today() and saldo > 0:
        return "VENCIDA"
    if abonado > 0:
        return "PARCIAL"
    return "PENDIENTE"


@router.post("/cuentas-por-pagar")
async def crear_cxp(datos: CuentaPagar):
    """
    Registro nueva cuenta por pagar cuando la compra viene en crédito.
    Calculo el vencimiento sumando los días de plazo a la fecha de factura.
    """
    f_venc = datos.fecha_factura + timedelta(days=datos.plazo_dias)
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            """
            INSERT INTO cuentas_por_pagar
                (num_factura, proveedor, fecha_factura, condicion_pago, plazo_dias,
                 fecha_vencimiento, total, abonado, saldo_pendiente, estado, usuario)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, 'PENDIENTE', %s)
            """,
            (
                datos.num_factura, datos.proveedor, datos.fecha_factura,
                datos.condicion_pago, datos.plazo_dias, f_venc,
                datos.total, datos.total, datos.usuario
            )
        )
        new_id = cursor.lastrowid
        conn.commit()

        # Devuelvo el registro completo para que el frontend no tenga que hacer otro GET
        cursor.execute(
            "SELECT * FROM cuentas_por_pagar WHERE id = %s", (new_id,)
        )
        reg = cursor.fetchone()
        return reg

    except mysql.connector.Error as err:
        if conn:
            conn.rollback()
        print(f"Error DB cxp crear: {err}")
        raise HTTPException(status_code=500, detail=f"Error interno en DB: {err}")

    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()


@router.get("/cuentas-por-pagar")
async def listar_cxp():
    """
    Traigo todas las cuentas y recalculo el estado en Python para que siempre esté fresco.
    La DB guarda el último estado conocido, pero aquí lo recalculo con la fecha de hoy.
    """
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            """
            SELECT id, num_factura, proveedor, fecha_factura, fecha_vencimiento,
                   plazo_dias, total, abonado, saldo_pendiente, estado, usuario
            FROM cuentas_por_pagar
            ORDER BY fecha_vencimiento ASC
            """
        )
        rows = cursor.fetchall()
        hoy = date.today()

        for r in rows:
            f_venc = r["fecha_vencimiento"]
            # Si viene como string (algunos drivers lo devuelven así), lo convierto
            if isinstance(f_venc, str):
                f_venc = date.fromisoformat(f_venc)

            r["estado"] = calc_estado(
                float(r["saldo_pendiente"]),
                float(r["abonado"]),
                f_venc
            )
            # dias_vencido: negativo = aún no vence, positivo = ya venció hace N días
            r["dias_vencido"] = (hoy - f_venc).days

        return rows

    except mysql.connector.Error as err:
        print(f"Error DB cxp listar: {err}")
        raise HTTPException(status_code=500, detail=f"Error interno en DB: {err}")

    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()


@router.post("/pagos-proveedor")
async def registrar_pago(datos: PagoProveedor):
    """
    Registro pago parcial o total a proveedor.
    Todo en una transacción: inserto el pago y actualizo la cuenta en el mismo commit.
    Si el monto excede el saldo, lo rechazo antes de tocar la DB.
    """
    if datos.monto <= 0:
        raise HTTPException(status_code=400, detail="El monto debe ser mayor a cero")

    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Traigo la cuenta para validar saldo antes de insertar nada
        cursor.execute(
            "SELECT id, total, abonado, saldo_pendiente, fecha_vencimiento FROM cuentas_por_pagar WHERE id = %s",
            (datos.id_cuenta,)
        )
        cuenta = cursor.fetchone()

        if not cuenta:
            raise HTTPException(status_code=404, detail="Cuenta por pagar no encontrada")

        saldo_actual = float(cuenta["saldo_pendiente"])
        if datos.monto > saldo_actual:
            raise HTTPException(
                status_code=400,
                detail=f"El monto ({datos.monto}) excede el saldo pendiente ({saldo_actual:.2f})"
            )

        nuevo_abonado = float(cuenta["abonado"]) + datos.monto
        nuevo_saldo = float(cuenta["total"]) - nuevo_abonado

        f_venc = cuenta["fecha_vencimiento"]
        if isinstance(f_venc, str):
            f_venc = date.fromisoformat(f_venc)

        nuevo_estado = calc_estado(nuevo_saldo, nuevo_abonado, f_venc)

        # Inserto el pago y actualizo la cuenta en un solo bloque transaccional
        cursor.execute(
            """
            INSERT INTO pagos_proveedor (id_cuenta, monto, metodo, referencia, fecha_pago, usuario)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (datos.id_cuenta, datos.monto, datos.metodo, datos.referencia, datos.fecha_pago, datos.usuario)
        )

        cursor.execute(
            """
            UPDATE cuentas_por_pagar
            SET abonado = %s, saldo_pendiente = %s, estado = %s
            WHERE id = %s
            """,
            (nuevo_abonado, nuevo_saldo, nuevo_estado, datos.id_cuenta)
        )

        conn.commit()

        # Enviamos notificación a Telegram
        message = (
            f"💰 <b>Pago a proveedor registrado</b>\n\n"
            f"• <b>ID Cuenta:</b> {html.escape(str(datos.id_cuenta))}\n"
            f"• <b>Monto:</b> ${datos.monto:,.2f}\n"
            f"• <b>Nuevo saldo pendiente:</b> ${nuevo_saldo:,.2f}\n"
            f"• <b>Estado:</b> {html.escape(nuevo_estado)}"
        )
        asyncio.create_task(send_telegram_alert(message))

        return {
            "mensaje": "Pago registrado correctamente",
            "id_cuenta": datos.id_cuenta,
            "abonado": nuevo_abonado,
            "saldo_pendiente": nuevo_saldo,
            "estado": nuevo_estado
        }

    except HTTPException:
        raise

    except mysql.connector.Error as err:
        if conn:
            conn.rollback()
        print(f"Error DB pago proveedor: {err}")
        raise HTTPException(status_code=500, detail=f"Error interno en DB: {err}")

    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()
