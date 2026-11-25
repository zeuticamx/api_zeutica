import mysql.connector
import base64, html, asyncio
from pydantic import BaseModel, Field, StrictInt
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
import os
from dotenv import load_dotenv
from typing import Optional, List
from servicios import mov_reg
from servicios.cotizacion_service import generar_nuevo_codigo, guardar_cotizacion_db, validar_cliente_id
from servicios.pdf_cotizacion import generar_pdf_cotizacion
from servicios.telegram.notificacion import send_telegram_alert
from servicios.errores_db import detalle_error_db

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
    # Cliente del catálogo al que se liga la cotización (opcional; entero >= 1 que exista).
    cliente_id: Optional[StrictInt] = Field(default=None, ge=1)
    # Solo para el PDF (no se guardan): los precios de items y subtotal ya vienen con
    # el descuento aplicado; con esto se imprime la fila "Descuento (x%)".
    # Empresa que se imprime en el renglón EMPRESA (empresa guarda el nombre del cliente).
    empresa_pdf: Optional[str] = None
    # Nombre formal del asesor para el PDF (nombres + apellido paterno); lo resuelve el
    # backend desde `empleados`. No se guarda: `usuario` sigue siendo el de la sesión.
    asesor_pdf: Optional[str] = None
    descuento_porcentaje: float = 0
    descuento_monto: float = 0
    items: List[ItemCotizacion]


def armar_nombre_asesor(nombres, apellido_paterno) -> Optional[str]:
    """«Nombres + apellido paterno»; None si no hay nombres (se usará el usuario)."""
    nombres = " ".join(str(nombres or "").split())
    paterno = " ".join(str(apellido_paterno or "").split())
    if not nombres:
        return None
    return f"{nombres} {paterno}".strip()


def obtener_nombre_asesor(conn, usuario: str) -> Optional[str]:
    """Nombre formal del empleado ligado al usuario. Si no existe o la consulta falla,
    devuelve None y el PDF conserva el nombre de usuario (no bloquea la cotización)."""
    cursor = None
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT nombres, apellido_paterno FROM empleados WHERE usuario = %s LIMIT 1",
            (usuario,),
        )
        fila = cursor.fetchone()
        return armar_nombre_asesor(fila[0], fila[1]) if fila else None
    except Exception as e:
        print(f"No se pudo resolver el nombre del asesor de {usuario}: {e}")
        return None
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass

@router.post("/genera-cotizacion")
async def genera_cotizacion(coti: CotizacionSchema):
    """
    Asigna el consecutivo correcto, genera la cotización en PDF y la
    devuelve como archivo binario (application/pdf).
    """
    conn = get_db_connection()
    try:
        # Valida el cliente ANTES de asignar folio o generar el PDF (422).
        validar_cliente_id(conn, coti.cliente_id)

        # Consecutivo real desde el servicio compartido: ignoramos lo que
        # venga en el payload para evitar códigos duplicados.
        coti.codigo_cotizacion = generar_nuevo_codigo(conn)

        # El PDF muestra nombre + apellido paterno del asesor, no el usuario de sesión.
        coti.asesor_pdf = obtener_nombre_asesor(conn, coti.usuario)

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

    except HTTPException:
        raise

    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error BD en genera-cotizacion: {err}")
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error BD"))
    except Exception as e:
        conn.rollback()
        print(f"Error inesperado en genera-cotizacion: {e}")
        raise HTTPException(status_code=500, detail=detalle_error_db(e, "Error interno del servidor"))
    finally:
        conn.close()
