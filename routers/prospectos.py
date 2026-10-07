# Prospectos: perfiles con los mismos datos que un cliente, con seguimiento propio
# y conversión a cliente en un clic.
#
# Flujo de etapas: nuevo -> contactado -> cotizacion -> convertido.
#   - contactado exige medio_contacto (whatsapp|correo|llamada).
#   - convertido solo lo pone POST /prospectos/{id}/convertir (automático).
# Permisos: gerencia ve todo; vendedor solo sus prospectos (vendedor = usuario).
import asyncio
import html
import os
from datetime import date
from typing import Literal, Optional

import mysql.connector
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

import mov_reg
from permisos import es_gerencia, usuario_autenticado
from servicios.telegram.notificacion import send_telegram_alert

router = APIRouter(tags=["/prospectos"], responses={404: {"Mensaje": "No encontrado"}})
load_dotenv()

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sql", "prospectos_schema.sql")

Etapa = Literal["nuevo", "contactado", "cotizacion", "convertido"]
Medio = Literal["whatsapp", "correo", "llamada"]
TipoSeg = Literal["llamada", "correo", "whatsapp", "reunion"]
ResultadoSeg = Literal["contesto", "no_contesto", "interesado", "no_interesado", "pidio_cotizacion"]

# Transiciones manuales permitidas (convertido nunca es manual).
TRANSICIONES = {
    "nuevo": ("contactado", "cotizacion"),
    "contactado": ("cotizacion",),
    "cotizacion": (),
    "convertido": (),
}


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME"),
    )


def crear_tablas_prospectos():
    """Crea las tablas de prospectos si no existen (se llama en el lifespan). Idempotente."""
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        lineas = [l for l in f if not l.strip().startswith("--")]
    statements = [s.strip() for s in "".join(lineas).split(";") if s.strip()]
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        for stmt in statements:
            cursor.execute(stmt)
        conn.commit()
        print("Tablas de prospectos verificadas/creadas.")
    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error creando tablas de prospectos: {err}")
    finally:
        cursor.close()
        conn.close()


def _error_db(conn, err, contexto: str):
    try:
        conn.rollback()
    except Exception:
        pass
    print(f"Error DB prospectos ({contexto}): {err}")
    raise HTTPException(status_code=500, detail=f"Error en DB: {err}")


def _fila(cursor, prospecto_id: int):
    cursor.execute("SELECT * FROM prospectos WHERE id = %s AND eliminado = 0", (prospecto_id,))
    return cursor.fetchone()


def _autorizado(cursor, prospecto_id: int, usuario: str):
    fila = _fila(cursor, prospecto_id)
    if not fila:
        raise HTTPException(status_code=404, detail="Prospecto no encontrado")
    if not es_gerencia(usuario) and str(fila.get("vendedor") or "").strip().lower() != str(usuario).strip().lower():
        raise HTTPException(status_code=403, detail="Este prospecto pertenece a otro vendedor")
    return fila


# ---------- Schemas ----------

class ProspectoNuevo(BaseModel):
    nombre: str
    email: Optional[str] = None
    empresa: Optional[str] = ""
    contacto: Optional[str] = ""
    telefono: Optional[int] = 0
    direccion: Optional[str] = None
    rfc: Optional[str] = None
    cp: Optional[int] = 0
    regimen: Optional[str] = None
    usocfdi: Optional[str] = None
    uso_cfdi: Optional[str] = None
    frecuencia: Optional[str] = None
    credito: bool = False
    monto_credito: Optional[int] = 0
    dias_credito: Optional[int] = 0
    vendedor: Optional[str] = None  # solo gerencia puede asignar a otro

    @field_validator("nombre")
    @classmethod
    def nombre_obligatorio(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("El nombre del prospecto es obligatorio")
        return v


class ProspectoEditar(BaseModel):
    nombre: Optional[str] = None
    email: Optional[str] = None
    empresa: Optional[str] = None
    contacto: Optional[str] = None
    telefono: Optional[int] = None
    direccion: Optional[str] = None
    rfc: Optional[str] = None
    cp: Optional[int] = None
    regimen: Optional[str] = None
    usocfdi: Optional[str] = None
    uso_cfdi: Optional[str] = None
    frecuencia: Optional[str] = None
    credito: Optional[bool] = None
    monto_credito: Optional[int] = None
    dias_credito: Optional[int] = None


class EtapaCambio(BaseModel):
    etapa: Etapa
    medio_contacto: Optional[Medio] = None


class SeguimientoNuevo(BaseModel):
    tipo: TipoSeg
    resultado: Optional[ResultadoSeg] = None
    notas: Optional[str] = Field(None, max_length=2000)
    proxima_accion: Optional[str] = Field(None, max_length=255)
    proxima_fecha: Optional[date] = None


CAMPOS_EDITABLES = (
    "nombre", "email", "empresa", "contacto", "telefono", "direccion", "rfc",
    "cp", "regimen", "usocfdi", "frecuencia", "credito", "monto_credito", "dias_credito",
)


# ---------- Endpoints ----------

@router.get("/prospectos")
async def listar_prospectos(
    q: Optional[str] = Query(None, max_length=100),
    etapa: Optional[Etapa] = None,
    vendedor: Optional[str] = None,
    ver_convertidos: bool = False,
    limit: int = Query(200, ge=1, le=500),
    usuario: str = Depends(usuario_autenticado),
):
    """Lista prospectos. Oculta los convertidos salvo ?ver_convertidos=true.
    Vendedor: solo los suyos. Gerencia: todos o filtra por ?vendedor=."""
    where, params = ["eliminado = 0"], []
    if not ver_convertidos:
        where.append("etapa != 'convertido'")
    if etapa:
        where.append("etapa = %s")
        params.append(etapa)
    if q and q.strip():
        like = f"%{q.strip()}%"
        where.append("(nombre LIKE %s OR empresa LIKE %s OR contacto LIKE %s OR email LIKE %s OR CAST(telefono AS CHAR) LIKE %s)")
        params += [like] * 5
    if es_gerencia(usuario):
        if vendedor:
            where.append("vendedor = %s")
            params.append(vendedor)
    else:
        where.append("vendedor = %s")
        params.append(usuario)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            f"""SELECT p.*,
                (SELECT MAX(s.fecha) FROM prospectos_seguimientos s WHERE s.prospecto_id = p.id) AS ultimo_seguimiento,
                (SELECT COUNT(*) FROM prospectos_seguimientos s WHERE s.prospecto_id = p.id) AS total_seguimientos
                FROM prospectos p WHERE {' AND '.join(where)} ORDER BY p.id DESC LIMIT %s""",
            tuple(params + [limit]),
        )
        return cursor.fetchall()
    except mysql.connector.Error as err:
        _error_db(conn, err, "listar")
    finally:
        cursor.close()
        conn.close()


@router.get("/prospectos/{prospecto_id}")
async def ficha_prospecto(prospecto_id: int, usuario: str = Depends(usuario_autenticado)):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        fila = _autorizado(cursor, prospecto_id, usuario)
        cursor.execute(
            """SELECT id, vendedor, tipo, fecha, resultado, notas, proxima_accion, proxima_fecha
               FROM prospectos_seguimientos WHERE prospecto_id = %s
               ORDER BY fecha DESC, id DESC LIMIT 50""",
            (prospecto_id,),
        )
        return {"prospecto": fila, "seguimientos": cursor.fetchall()}
    except mysql.connector.Error as err:
        _error_db(conn, err, "ficha")
    finally:
        cursor.close()
        conn.close()


@router.post("/prospectos")
async def crear_prospecto(datos: ProspectoNuevo, usuario: str = Depends(usuario_autenticado)):
    nombre = datos.nombre.strip()
    # Solo gerencia puede asignar el prospecto a otro vendedor; el resto es dueño de lo que registra.
    vendedor = datos.vendedor.strip() if (datos.vendedor and es_gerencia(usuario)) else usuario
    if datos.vendedor and not es_gerencia(usuario) and datos.vendedor.strip().lower() != usuario.strip().lower():
        raise HTTPException(status_code=403, detail="Solo gerencia puede asignar prospectos a otro vendedor")

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """INSERT INTO prospectos
               (nombre, email, empresa, contacto, telefono, direccion, rfc, cp, regimen, usocfdi,
                frecuencia, credito, monto_credito, dias_credito, etapa, vendedor, registrado_por)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'nuevo',%s,%s)""",
            (nombre, datos.email, datos.empresa, datos.contacto, datos.telefono or 0,
             datos.direccion, datos.rfc, datos.cp or 0, datos.regimen,
             datos.uso_cfdi or datos.usocfdi, datos.frecuencia, int(bool(datos.credito)),
             datos.monto_credito or 0, datos.dias_credito or 0, vendedor, usuario),
        )
        nuevo_id = cursor.lastrowid
        conn.commit()
        mov_reg.registrar_movimiento(usuario, f"Registró el prospecto: {nombre}", "Prospectos")
        return {"id": nuevo_id, "nombre": nombre, "etapa": "nuevo", "vendedor": vendedor}
    except mysql.connector.Error as err:
        _error_db(conn, err, "crear")
    finally:
        cursor.close()
        conn.close()


@router.patch("/prospectos/{prospecto_id}")
async def editar_prospecto(prospecto_id: int, datos: ProspectoEditar, usuario: str = Depends(usuario_autenticado)):
    campos = {k: getattr(datos, k) for k in datos.model_fields_set}
    campos = {k: v for k, v in campos.items() if k in CAMPOS_EDITABLES}
    if not campos:
        raise HTTPException(status_code=422, detail="No se envió ningún campo para actualizar")
    if "nombre" in campos and not (campos["nombre"] or "").strip():
        raise HTTPException(status_code=422, detail="El nombre del prospecto es obligatorio")
    if "uso_cfdi" in campos:
        campos["usocfdi"] = campos.pop("uso_cfdi") or campos.get("usocfdi")
    if "credito" in campos:
        campos["credito"] = int(bool(campos["credito"]))

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        _autorizado(cursor, prospecto_id, usuario)
        cursor.execute(
            f"UPDATE prospectos SET {', '.join(f'{c} = %s' for c in campos)} WHERE id = %s AND eliminado = 0",
            tuple(list(campos.values()) + [prospecto_id]),
        )
        conn.commit()
        return {"id": prospecto_id, "actualizados": list(campos.keys())}
    except mysql.connector.Error as err:
        _error_db(conn, err, "editar")
    finally:
        cursor.close()
        conn.close()


@router.patch("/prospectos/{prospecto_id}/etapa")
async def cambiar_etapa(prospecto_id: int, datos: EtapaCambio, usuario: str = Depends(usuario_autenticado)):
    if datos.etapa == "convertido":
        raise HTTPException(status_code=422, detail="La conversión se hace con el botón 'Convertir a cliente'")
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        fila = _autorizado(cursor, prospecto_id, usuario)
        anterior = fila["etapa"]
        if datos.etapa == anterior:
            return {"id": prospecto_id, "etapa": anterior, "cambio": False}
        if datos.etapa not in TRANSICIONES.get(anterior, ()):
            raise HTTPException(status_code=422, detail=f"No se puede pasar de '{anterior}' a '{datos.etapa}'")
        if datos.etapa == "contactado" and not datos.medio_contacto:
            raise HTTPException(status_code=422, detail="Indica el medio usado: whatsapp, correo o llamada")
        cursor.execute(
            "UPDATE prospectos SET etapa = %s, medio_contacto = %s, etapa_actualizada = NOW() WHERE id = %s",
            (datos.etapa, datos.medio_contacto if datos.etapa == "contactado" else fila.get("medio_contacto"), prospecto_id),
        )
        conn.commit()
        mov_reg.registrar_movimiento(usuario, f"Cambió etapa del prospecto {fila['nombre']}: {anterior} → {datos.etapa}", "Prospectos")
        return {"id": prospecto_id, "etapa": datos.etapa, "etapa_anterior": anterior, "cambio": True}
    except mysql.connector.Error as err:
        _error_db(conn, err, "etapa")
    finally:
        cursor.close()
        conn.close()


@router.post("/prospectos/{prospecto_id}/seguimientos")
async def registrar_seguimiento(prospecto_id: int, datos: SeguimientoNuevo, usuario: str = Depends(usuario_autenticado)):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        fila = _autorizado(cursor, prospecto_id, usuario)
        cursor.execute(
            """INSERT INTO prospectos_seguimientos
               (prospecto_id, vendedor, tipo, resultado, notas, proxima_accion, proxima_fecha)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (prospecto_id, usuario, datos.tipo, datos.resultado,
             (datos.notas or "").strip() or None, (datos.proxima_accion or "").strip() or None, datos.proxima_fecha),
        )
        nuevo_id = cursor.lastrowid
        conn.commit()
        mov_reg.registrar_movimiento(usuario, f"Registró {datos.tipo} con el prospecto {fila['nombre']}", "Prospectos")
        return {"id": nuevo_id, "prospecto_id": prospecto_id}
    except mysql.connector.Error as err:
        _error_db(conn, err, "seguimiento")
    finally:
        cursor.close()
        conn.close()


@router.post("/prospectos/{prospecto_id}/convertir")
async def convertir_a_cliente(prospecto_id: int, usuario: str = Depends(usuario_autenticado)):
    """Crea el cliente con los datos del prospecto, lo da de alta en el CRM y
    migra TODO el historial de seguimientos. Marca el prospecto como convertido (automático)."""
    # El avance comercial se preserva: cotización -> cotizado, lo demás entra como contacto inicial.
    ETAPA_CRM = {"cotizacion": "cotizado"}.get
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        fila = _autorizado(cursor, prospecto_id, usuario)
        if fila["etapa"] == "convertido":
            raise HTTPException(status_code=409, detail="Este prospecto ya fue convertido a cliente")
        nombre = (fila["nombre"] or "").strip()
        cursor.execute("SELECT id FROM clientes WHERE LOWER(TRIM(nombre)) = LOWER(%s) LIMIT 1", (nombre,))
        if cursor.fetchone():
            raise HTTPException(status_code=409, detail=f"Ya existe un cliente llamado '{nombre}'")
        cursor.execute(
            """INSERT INTO clientes
               (nombre, email, empresa, contacto, telefono, direccion, rfc, cp, regimen, usocfdi,
                frecuencia, usuario, credito, monto_credito, dias_credito)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (nombre, fila.get("email"), fila.get("empresa"), fila.get("contacto"), fila.get("telefono") or 0,
             fila.get("direccion"), fila.get("rfc"), fila.get("cp") or 0, fila.get("regimen"),
             fila.get("usocfdi"), fila.get("frecuencia"), usuario,
             int(bool(fila.get("credito"))), fila.get("monto_credito") or 0, fila.get("dias_credito") or 0),
        )
        cliente_id = cursor.lastrowid

        # Alta en el CRM con el dueño del prospecto (así cae en su cartera / Mis Seguimientos).
        dueno = (fila.get("vendedor") or usuario).strip() or usuario
        etapa_crm = ETAPA_CRM(fila.get("etapa"), "contacto_inicial")
        cursor.execute(
            "INSERT INTO crm_cartera (cliente_id, vendedor, etapa) VALUES (%s, %s, %s)",
            (cliente_id, dueno, etapa_crm),
        )
        cursor.execute(
            "INSERT INTO crm_etapas_historial (cliente_id, etapa_anterior, etapa_nueva, usuario) VALUES (%s, NULL, %s, %s)",
            (cliente_id, etapa_crm, usuario),
        )

        # Migración completa del historial: el más reciente conserva su próxima acción
        # abierta (si trae proxima_fecha); el resto queda cerrado para no duplicar pendientes.
        cursor.execute(
            """SELECT vendedor, tipo, fecha, resultado, notas, proxima_accion, proxima_fecha
               FROM prospectos_seguimientos WHERE prospecto_id = %s ORDER BY fecha DESC, id DESC""",
            (prospecto_id,),
        )
        historial = cursor.fetchall()
        for i, s in enumerate(historial):
            # Regla: solo el más reciente con proxima_fecha queda abierto (0); todo lo demás cerrado (1).
            abierto = 0 if (i == 0 and s.get("proxima_fecha")) else 1
            cursor.execute(
                """INSERT INTO crm_interacciones
                   (cliente_id, vendedor, tipo, fecha, resultado, notas, proxima_accion, proxima_fecha, seguimiento_cerrado)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (cliente_id, s.get("vendedor") or dueno, s.get("tipo"), s.get("fecha"),
                 s.get("resultado"), s.get("notas"), s.get("proxima_accion"), s.get("proxima_fecha"), abierto),
            )

        cursor.execute(
            "UPDATE prospectos SET etapa = 'convertido', cliente_id = %s, etapa_actualizada = NOW() WHERE id = %s",
            (cliente_id, prospecto_id),
        )
        conn.commit()
        mov_reg.registrar_movimiento(usuario, f"Convirtió el prospecto {nombre} al cliente #{cliente_id} ({len(historial)} seguimientos migrados)", "Prospectos")
        asyncio.create_task(send_telegram_alert(
            f"🎉 <b>Prospecto convertido</b>\n\n"
            f"• <b>Prospecto:</b> {html.escape(nombre)}\n"
            f"• <b>Cliente:</b> <code>{cliente_id}</code>\n"
            f"• <b>Usuario:</b> {html.escape(str(usuario))}"
        ))
        return {"id": prospecto_id, "cliente_id": cliente_id, "etapa": "convertido",
                "etapa_crm": etapa_crm, "seguimientos_migrados": len(historial), "nombre": nombre}
    except mysql.connector.Error as err:
        _error_db(conn, err, "convertir")
    finally:
        cursor.close()
        conn.close()


@router.delete("/prospectos/{prospecto_id}")
async def eliminar_prospecto(prospecto_id: int, usuario: str = Depends(usuario_autenticado)):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        fila = _autorizado(cursor, prospecto_id, usuario)
        cursor.execute("UPDATE prospectos SET eliminado = 1 WHERE id = %s AND eliminado = 0", (prospecto_id,))
        conn.commit()
        mov_reg.registrar_movimiento(usuario, f"Eliminó el prospecto {fila['nombre']}", "Prospectos")
        return {"id": prospecto_id, "eliminado": True}
    except mysql.connector.Error as err:
        _error_db(conn, err, "eliminar")
    finally:
        cursor.close()
        conn.close()
