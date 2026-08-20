import mysql.connector
import base64, html, asyncio
from pydantic import BaseModel
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
import os
from dotenv import load_dotenv
from typing import Optional, List
import mov_reg
from cotizacion_service import generar_nuevo_codigo, guardar_cotizacion_db
from pdf_cotizacion import generar_pdf_cotizacion
from servicios.telegram.notificacion import send_telegram_alert

# Obtiene la ruta del directorio padre (la raíz)
#sys.path.append(os.path.abspath(os.path.join(os.path.dirname(mov_reg.__file__), '..')))

router =APIRouter(tags=["/genera-pdf"],responses={404: {"Mensaje":"No encontrado"}})
load_dotenv()

# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

class ItemCotizacion(BaseModel):
    sku: str
    nombre_producto: str
    cantidad: int
    precio_unitario: float
    total_linea: float

class CotizacionSchema(BaseModel):   
    codigo_cotizacion: Optional[str] = None  # Se asignará automáticamente 
    empresa: str
    atencion: Optional[str] = None
    email: Optional[str] = None
    domicilio: Optional[str] = None
    telefono: str
    subtotal: float
    iva: float    
    total: float
    costo_envio: float  # Cambié de int a float para aceptar decimales
    forma_pago: str
    metodo_pago: Optional[str] = None  # ej. "PUE - PAGO EN UNA SOLA EXHIBICIÓN"
    comentarios: Optional[str] = None
    usuario: str
    pdf: Optional[str] = None
    items: List[ItemCotizacion]

@router.post("/genera-cotizacion")
async def genera_cotizacion(coti: CotizacionSchema):
    """
    Asigna el consecutivo correcto, genera la cotización en PDF y la
    devuelve como archivo binario (application/pdf).
    """
    conn = get_db_connection()
    try:
        # Consecutivo real desde el servicio compartido: ignoramos lo que
        # venga en el payload para evitar códigos duplicados.
        coti.codigo_cotizacion = generar_nuevo_codigo(conn)

        # Construimos el PDF en memoria (bytes).
        pdf_bytes = generar_pdf_cotizacion(coti)

        # Guardamos el PDF generado como base64 para dejarlo en la DB
        # (así /cotizaciones/base64 lo puede recuperar después).
        coti.pdf = base64.b64encode(pdf_bytes).decode("ascii")

        # Registramos la cotización (maestro + items) con el mismo INSERT
        # que usa /cotizaciones/guardar.
        cotizacion_id = guardar_cotizacion_db(conn, coti)

        mov_reg.registrar_movimiento(
            coti.usuario,
            f"Generó y registró cotización: {coti.codigo_cotizacion}",
            "Cotizaciones",
        )

        # Enviamos notificación a Telegram
        empresa_safe = html.escape(str(coti.empresa))
        usuario_safe = html.escape(str(coti.usuario))
        codigo_safe = html.escape(str(coti.codigo_cotizacion))

        message = (
            f"📋 <b>Nueva cotización generada</b>\n\n"
            f"• <b>Código:</b> <code>{codigo_safe}</code>\n"
            f"• <b>Cliente:</b> {empresa_safe}\n"
            f"• <b>Total:</b> <b>${coti.total:,.2f}</b>\n"
            f"• <b>Usuario:</b> {usuario_safe}"
        )
        asyncio.create_task(send_telegram_alert(message))

        # Devolvemos el binario para que el navegador/cliente lo descargue.
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="cotizacion_{coti.codigo_cotizacion}.pdf"'
                ),
                "X-Codigo-Cotizacion": coti.codigo_cotizacion,
                "X-Cotizacion-Id": str(cotizacion_id),
            },
        )

    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error BD en genera-cotizacion: {err}")
        raise HTTPException(status_code=500, detail=f"Error BD: {str(err)}")
    except Exception as e:
        conn.rollback()
        print(f"Error inesperado en genera-cotizacion: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()
