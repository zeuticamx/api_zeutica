import mysql.connector
from pydantic import BaseModel, Field, field_validator
from fastapi import APIRouter, Depends, HTTPException
from typing import Optional
import os, html, asyncio
from dotenv import load_dotenv
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta
import mov_reg
from permisos import requerir_gerencia, usuario_autenticado, es_gerencia
from errores_db import detalle_error_db
from servicios.telegram.notificacion import send_telegram_alert

router =APIRouter(tags=["/pendientes"],responses={404: {"Mensaje":"No encontrado"}})
load_dotenv()

# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

@router.get("/pendientes-registro")
async def pendientes(estado: str):
    """
    Traigo los pendientes con su estado y prioridad.
    """
    conn = get_db_connection()
    # Uso dictionary=True para devolver llaves nombradas y armar el JSON directo
    cursor = conn.cursor(dictionary=True)

    # JOIN por el FK id_ventas: del pendiente saco el saldo pendiente, de la venta el resto
    query = """
        SELECT * FROM pendientes WHERE estado = %s ORDER BY id DESC
    """

    try:
        cursor.execute(query, (estado,))
        res = cursor.fetchall()        
        return res

    except mysql.connector.Error as err:
        print(f"Error DB pendientes: {err}")
        raise HTTPException(status_code=500, detail="Error interno en DB")  
    
    finally:
        cursor.close()
        conn.close()

class registro(BaseModel):
    usuario: str
    actividad: str
    prioridad: str  
    estado: str
    observaciones: Optional[str] = None
    fecha_promesa: Optional[date] = None

@router.post("/pendientes-agregar")
async def agregar_pendiente(registro: registro):
    """
    Agrego un registro a la tabla pendientes.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    query = """
        INSERT INTO pendientes (usuario, actividad, prioridad, estado, observaciones, fecha_promesa)
        VALUES (%s, %s, %s, %s, %s, %s)
    """

    try:
        cursor.execute(query, (registro.usuario, registro.actividad, registro.prioridad, registro.estado, registro.observaciones, registro.fecha_promesa))
        conn.commit()
        
        mov_reg.registrar_movimiento(registro.usuario, "agregar", f"Actividad: {registro.actividad}, Prioridad: {registro.prioridad}, Estado: {registro.estado}, Observaciones: {registro.observaciones}, Fecha Promesa: {registro.fecha_promesa}")

        # Enviamos notificación a Telegram
        message = (
            f"📝 <b>Pendiente Agregado</b>\n\n"
            f"• <b>Usuario:</b> {html.escape(registro.usuario)}\n"
            f"• <b>Actividad:</b> {html.escape(registro.actividad)}\n"
            f"• <b>Prioridad:</b> {html.escape(registro.prioridad)}\n"
            f"• <b>Estado:</b> {html.escape(registro.estado)}\n"
            f"• <b>Observaciones:</b> {html.escape(registro.observaciones or '')}\n"
            f"• <b>Fecha Promesa:</b> {html.escape(str(registro.fecha_promesa))}"
        )
        asyncio.create_task(send_telegram_alert(message))

        return {"mensaje": "Registro agregado exitosamente."}        

    except mysql.connector.Error as err:
        print(f"Error DB agregar registro: {err}")
        raise HTTPException(status_code=500, detail="Error interno en DB")  
    
    finally:
        cursor.close()
        conn.close()

@router.patch("/pendientes-actualizar/{id_pendiente}")
async def actualizar_pendiente(id_pendiente: int, registro: registro):
    """
    Actualizo un registro en la tabla pendientes.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    query = """
        UPDATE pendientes
        SET usuario = %s, actividad = %s, prioridad = %s, estado = %s, observaciones = %s, fecha_promesa = %s
        WHERE id = %s
    """

    try:
        cursor.execute(query, (registro.usuario, registro.actividad, registro.prioridad, registro.estado, registro.observaciones, registro.fecha_promesa, id_pendiente))
        conn.commit()        

        mov_reg.registrar_movimiento(registro.usuario, "actualizar", f"Actividad: {registro.actividad}, Prioridad: {registro.prioridad}, Estado: {registro.estado}, Observaciones: {registro.observaciones}, Fecha Promesa: {registro.fecha_promesa}")

        # Enviamos notificación a Telegram
        message = (
            f"✏️ <b>Pendiente Actualizado</b>\n\n"
            f"• <b>Usuario:</b> {html.escape(registro.usuario)}\n"
            f"• <b>Actividad:</b> {html.escape(registro.actividad)}\n"
            f"• <b>Prioridad:</b> {html.escape(registro.prioridad)}\n"
            f"• <b>Estado:</b> {html.escape(registro.estado)}\n"
            f"• <b>Observaciones:</b> {html.escape(registro.observaciones or '')}\n"
            f"• <b>Fecha Promesa:</b> {html.escape(str(registro.fecha_promesa))}"
        )
        asyncio.create_task(send_telegram_alert(message))

        return {"mensaje": "Pendiente actualizado exitosamente."}

    except mysql.connector.Error as err:
        print(f"Error DB actualizar pendiente: {err}")
        raise HTTPException(status_code=500, detail="Error interno en DB")  
    
    finally:
        cursor.close()
        conn.close()

@router.delete("/pendientes-eliminar/{id_pendiente}")
async def eliminar_pendiente(id_pendiente: int, usuario: str = Depends(requerir_gerencia)):
    """
    Elimino un registro de la tabla pendientes. Solo gerencia; la bitácora lleva a quien lo hizo.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    query = """
        DELETE FROM pendientes WHERE id = %s
    """

    try:
        cursor.execute(query, (id_pendiente,))
        conn.commit()

        mov_reg.registrar_movimiento(usuario, "eliminar", f"ID: {id_pendiente}")

        # Enviamos notificación a Telegram
        message = (
            f"🗑️ <b>Pendiente Eliminado</b>\n\n"
            f"• <b>ID:</b> {html.escape(str(id_pendiente))}\n"
        )
        asyncio.create_task(send_telegram_alert(message))

        return {"mensaje": "Pendiente eliminado exitosamente."}

    except mysql.connector.Error as err:
        print(f"Error DB eliminar pendiente: {err}")
        raise HTTPException(status_code=500, detail="Error interno en DB")  
    
    finally:
        cursor.close()
        conn.close()

class pendientes(BaseModel):
    id_pendiente: int
    usuario: str
    actividad: str
    prioridad: str  
    estado: str
    observaciones: Optional[str] = None
    fecha_promesa: Optional[date] = None

# Endpoints para notificar por telegram cuando un pendiente es tomado o terminado
@router.post("/pendientes-tomar/{id_pendiente}")
async def tomar_pendiente(act: pendientes):
    """
    Tomo un pendiente y notifico por telegram.

    """   
    try:
            # Enviamos notificación a Telegram
        message = (
            f"✅ <b>Pendiente Tomado</b>\n\n"
            f"• <b>ID:</b> {html.escape(str(act.id_pendiente))}\n"
            f"• <b>Usuario:</b> {html.escape(act.usuario)}\n"
            f"• <b>Actividad:</b> {html.escape(act.actividad)}\n"
            f"• <b>Prioridad:</b> {html.escape(act.prioridad)}\n"
            f"• <b>Estado:</b> {html.escape(act.estado)}\n"
            f"• <b>Observaciones:</b> {html.escape(act.observaciones or '')}\n"
            f"• <b>Fecha Promesa:</b> {html.escape(str(act.fecha_promesa))}"
        )
        
        asyncio.create_task(send_telegram_alert(message))

        return {"mensaje": "Pendiente tomado exitosamente."}

    except mysql.connector.Error as err:
        print(f"Error en telegram: {err}")
        raise HTTPException(status_code=500, detail="Error en modulo telegram")  
    
@router.post("/pendientes-terminar/{id_pendiente}")
async def terminar_pendiente(act: pendientes):
    """
    Termino un pendiente y notifico por telegram.

    """   
    try:
            # Enviamos notificación a Telegram
        message = (
            f"✅ <b>Pendiente Terminado</b>\n\n"
            f"• <b>ID:</b> {html.escape(str(act.id_pendiente))}\n"
            f"• <b>Usuario:</b> {html.escape(act.usuario)}\n"
            f"• <b>Actividad:</b> {html.escape(act.actividad)}\n"
            f"• <b>Prioridad:</b> {html.escape(act.prioridad)}\n"
            f"• <b>Estado:</b> {html.escape(act.estado)}\n"
            f"• <b>Observaciones:</b> {html.escape(act.observaciones or '')}\n"
            f"• <b>Fecha Promesa:</b> {html.escape(str(act.fecha_promesa))}"
        )
        asyncio.create_task(send_telegram_alert(message))

        return {"mensaje": "Pendiente terminado exitosamente."}

    except mysql.connector.Error as err:
        print(f"Error en telegram: {err}")
        raise HTTPException(status_code=500, detail="Error en modulo telegram")    


# ===========================================================================
# Cola de atención por usuario (migrada de api_java; ver CLAUDE.md)
# ===========================================================================
# Misma tabla `pendientes` y mismo contrato JSON (camelCase) que tenía api_java, pero sin caché en
# memoria: todo sale de MySQL, así que ya no existe «recargar». El usuario sale del token:
# cada quien ve y opera sus tareas; gerencia puede ver y crear las de cualquiera.
#   cola    = estado 'pendiente' y visible (fecha_visible NULL o ya llegada)
#   orden   = prioridad (alta, media, baja) -> fecha_promesa más cercana -> fecha más antigua
#   atender = 'pendiente' -> 'en proceso'; un usuario puede tener varias abiertas a la vez
#   terminar= -> 'terminado'; si tiene recurrencia, clona la siguiente instancia (oculta hasta su fecha)
PRIORIDADES = ("alta", "media", "baja")
RECURRENCIAS = ("diaria", "semanal", "quincenal", "mensual")
_CAMPOS = "id, fecha, usuario, actividad, prioridad, estado, observaciones, fecha_promesa, recurrencia, fecha_visible, plantilla_id"
_VISIBLE = "(fecha_visible IS NULL OR fecha_visible <= CURDATE())"


def prioridad_numerica(prioridad) -> int:
    """alta=1, media=2, el resto 3 (menor = más urgente)."""
    return {"alta": 1, "media": 2}.get(str(prioridad or "").strip().lower(), 3)


def proxima_fecha(ancla: date, recurrencia: str) -> date:
    """diaria +1 día, semanal +7, quincenal +15, mensual +1 mes (recorta al fin de mes)."""
    return {
        "diaria": ancla + timedelta(days=1),
        "semanal": ancla + timedelta(weeks=1),
        "quincenal": ancla + timedelta(days=15),
        "mensual": ancla + relativedelta(months=1),
    }[str(recurrencia).strip().lower()]


def _iso(valor):
    if valor is None:
        return None
    return (valor.date() if isinstance(valor, datetime) else valor).isoformat()


def pendiente_json(fila: dict) -> dict:
    """Fila de MySQL -> el JSON que consume el panel (mismos nombres que api_java)."""
    return {
        "id": fila["id"],
        "fecha": _iso(fila.get("fecha")),
        "usuario": fila.get("usuario"),
        "actividad": fila.get("actividad"),
        "prioridad": fila.get("prioridad"),
        "prioridadNumerica": prioridad_numerica(fila.get("prioridad")),
        "estado": fila.get("estado"),
        "observaciones": fila.get("observaciones"),
        "fechaPromesa": _iso(fila.get("fecha_promesa")),
        "recurrencia": fila.get("recurrencia"),
        "fechaVisible": _iso(fila.get("fecha_visible")),
        "plantillaId": fila.get("plantilla_id"),
    }


def clave_orden(p: dict):
    """Orden de atención sobre pendiente_json(): prioridad, promesa más cercana, fecha más antigua
    (sin fecha al final); el id desempata para que el orden sea estable."""
    return (p["prioridadNumerica"], p["fechaPromesa"] is None, p["fechaPromesa"] or "",
            p["fecha"] is None, p["fecha"] or "", p["id"])


def _mismo_usuario(a, b) -> bool:
    return str(a or "").strip().lower() == str(b or "").strip().lower()


def _estado(fila: dict) -> str:
    """Estado en minúsculas: las altas desde Monitor Gerencia (pendientes-agregar) lo guardan como
    'Pendiente'/'En proceso' y MySQL los compara sin distinguir mayúsculas, así que Python tampoco debe."""
    return str(fila.get("estado") or "").strip().lower()


def _alcance(usuario_token: str, usuario_param: Optional[str]) -> Optional[str]:
    """Usuario cuyas tareas se consultan; None = todas (solo gerencia). Quien no es gerencia
    solo puede consultar las suyas, aunque mande otro ?usuario=."""
    pedido = (usuario_param or "").strip()
    if es_gerencia(usuario_token):
        return pedido or None
    if pedido and not _mismo_usuario(pedido, usuario_token):
        raise HTTPException(status_code=403, detail="Solo puedes ver tus propios pendientes")
    return usuario_token


def _filtro_usuario(usuario: Optional[str]):
    return (" AND LOWER(usuario) = LOWER(%s)", [usuario]) if usuario else ("", [])


def _cola(cursor, usuario: Optional[str]) -> list:
    filtro, params = _filtro_usuario(usuario)
    cursor.execute(f"SELECT {_CAMPOS} FROM pendientes WHERE estado = 'pendiente' AND {_VISIBLE}{filtro}", tuple(params))
    return sorted((pendiente_json(f) for f in cursor.fetchall()), key=clave_orden)


def _tamano_cola(cursor, usuario: str) -> int:
    cursor.execute(
        f"SELECT COUNT(*) AS n FROM pendientes WHERE estado = 'pendiente' AND {_VISIBLE} AND LOWER(usuario) = LOWER(%s)",
        (usuario,),
    )
    return int((cursor.fetchone() or {}).get("n") or 0)


def _bloquear(cursor, id_pendiente: int):
    cursor.execute(f"SELECT {_CAMPOS}, {_VISIBLE} AS visible FROM pendientes WHERE id = %s FOR UPDATE", (id_pendiente,))
    return cursor.fetchone()


def _conexion():
    conn = get_db_connection()
    return conn, conn.cursor(dictionary=True)


# ---- lecturas (rutas fijas ANTES de /pendientes/{id}) ----

@router.get("/pendientes")
async def cola_listar(usuario: Optional[str] = None, actual: str = Depends(usuario_autenticado)):
    """Cola de atención en orden de prioridad. Sin ?usuario= trae la de todos (solo gerencia)."""
    alcance = _alcance(actual, usuario)
    conn, cursor = _conexion()
    try:
        return _cola(cursor, alcance)
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error al consultar pendientes"))
    finally:
        cursor.close()
        conn.close()


@router.get("/pendientes/en-proceso")
async def cola_en_proceso(usuario: Optional[str] = None, actual: str = Depends(usuario_autenticado)):
    """Tareas abiertas ('en proceso'); un usuario puede tener varias."""
    alcance = _alcance(actual, usuario)
    filtro, params = _filtro_usuario(alcance)
    conn, cursor = _conexion()
    try:
        cursor.execute(f"SELECT {_CAMPOS} FROM pendientes WHERE estado = 'en proceso'{filtro}", tuple(params))
        return sorted((pendiente_json(f) for f in cursor.fetchall()), key=clave_orden)
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error al consultar pendientes en proceso"))
    finally:
        cursor.close()
        conn.close()


@router.get("/pendientes/siguiente")
async def cola_siguiente(usuario: Optional[str] = None, actual: str = Depends(usuario_autenticado)):
    """Próxima tarea a atender (sin tomarla)."""
    alcance = _alcance(actual, usuario)
    conn, cursor = _conexion()
    try:
        cola = _cola(cursor, alcance)
        if not cola:
            raise HTTPException(status_code=404, detail="Cola vacía")
        return cola[0]
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error al consultar pendientes"))
    finally:
        cursor.close()
        conn.close()


@router.get("/pendientes/estadisticas")
async def cola_estadisticas(usuario: Optional[str] = None, actual: str = Depends(usuario_autenticado)):
    """Conteos desde la BD: enCola/porPrioridad son de la cola visible; atendidos = en proceso + terminados.
    (api_java contaba en memoria y se reiniciaba con el proceso.)"""
    alcance = _alcance(actual, usuario)
    filtro, params = _filtro_usuario(alcance)
    conn, cursor = _conexion()
    try:
        cursor.execute(
            f"SELECT LOWER(prioridad) AS prioridad, COUNT(*) AS n FROM pendientes"
            f" WHERE estado = 'pendiente' AND {_VISIBLE}{filtro} GROUP BY LOWER(prioridad)", tuple(params))
        por_prioridad = {str(f["prioridad"] or "sin prioridad"): int(f["n"]) for f in cursor.fetchall()}
        cursor.execute(
            f"SELECT estado, COUNT(*) AS n FROM pendientes WHERE estado IN ('en proceso', 'terminado'){filtro} GROUP BY estado",
            tuple(params))
        por_estado = {f["estado"]: int(f["n"]) for f in cursor.fetchall()}
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error al consultar estadísticas"))
    finally:
        cursor.close()
        conn.close()
    en_cola = sum(por_prioridad.values())
    estados = {"pendiente": en_cola, **por_estado}
    resp = {
        "enCola": en_cola,
        "atendidos": por_estado.get("en proceso", 0) + por_estado.get("terminado", 0),
        "porPrioridad": por_prioridad,
        "porEstado": {k: v for k, v in estados.items() if v},
    }
    if alcance:
        resp["usuario"] = alcance
    return resp


@router.get("/pendientes/{id_pendiente}")
async def cola_detalle(id_pendiente: int, actual: str = Depends(usuario_autenticado)):
    """Una tarea por id, en cualquier estado. Quien no es gerencia solo ve las suyas (404 si no)."""
    conn, cursor = _conexion()
    try:
        cursor.execute(f"SELECT {_CAMPOS} FROM pendientes WHERE id = %s", (id_pendiente,))
        fila = cursor.fetchone()
        if not fila or not (es_gerencia(actual) or _mismo_usuario(fila["usuario"], actual)):
            raise HTTPException(status_code=404, detail="No encontrado")
        return pendiente_json(fila)
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error al consultar el pendiente"))
    finally:
        cursor.close()
        conn.close()


# ---- escrituras ----

class PendienteNuevo(BaseModel):
    actividad: str = Field(min_length=1, max_length=500)
    prioridad: str
    observaciones: Optional[str] = None
    fecha_promesa: Optional[date] = None
    recurrencia: Optional[str] = None
    usuario: Optional[str] = None  # dueño de la tarea; por defecto quien la crea (otro, solo gerencia)

    @field_validator("prioridad")
    @classmethod
    def _prioridad_valida(cls, v):
        if v.strip().lower() not in PRIORIDADES:
            raise ValueError("prioridad debe ser alta, media o baja")
        return v.strip()

    @field_validator("recurrencia")
    @classmethod
    def _recurrencia_valida(cls, v):
        if v is None or not v.strip():
            return None
        if v.strip().lower() not in RECURRENCIAS:
            raise ValueError("recurrencia debe ser diaria, semanal, quincenal o mensual")
        return v.strip().lower()


class RecurrenciaCambio(BaseModel):
    valor: Optional[str] = None  # vacío o null = quitar la recurrencia

    @field_validator("valor")
    @classmethod
    def _valor_valido(cls, v):
        if v is None or not v.strip():
            return None
        if v.strip().lower() not in RECURRENCIAS:
            raise ValueError("valor debe ser diaria, semanal, quincenal o mensual")
        return v.strip().lower()


@router.post("/pendientes/nueva")
async def cola_nueva(datos: PendienteNuevo, actual: str = Depends(usuario_autenticado)):
    """Crea una tarea ('pendiente', visible de inmediato). Para otro usuario se requiere gerencia."""
    dueno = (datos.usuario or "").strip() or actual
    if not _mismo_usuario(dueno, actual) and not es_gerencia(actual):
        raise HTTPException(status_code=403, detail="Solo gerencia puede crear pendientes para otro usuario")
    conn, cursor = _conexion()
    try:
        cursor.execute(
            "INSERT INTO pendientes (fecha, usuario, actividad, prioridad, estado, observaciones, fecha_promesa, recurrencia)"
            " VALUES (CURDATE(), %s, %s, %s, 'pendiente', %s, %s, %s)",
            (dueno, datos.actividad.strip(), datos.prioridad, (datos.observaciones or "").strip() or None,
             datos.fecha_promesa, datos.recurrencia),
        )
        nuevo_id = cursor.lastrowid
        conn.commit()
        cursor.execute(f"SELECT {_CAMPOS} FROM pendientes WHERE id = %s", (nuevo_id,))
        creada = pendiente_json(cursor.fetchone())
    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error al crear el pendiente"))
    finally:
        cursor.close()
        conn.close()
    try:
        mov_reg.registrar_movimiento(actual, f"Creó pendiente #{creada['id']} para {dueno}: {creada['actividad']}", "Pendientes")
    except Exception as err:  # la tarea ya quedó guardada; la bitácora es secundaria
        print(f"Pendiente #{creada['id']} creado, pero falló la bitácora: {err}")
    return creada


def _atender(cursor, conn, id_pendiente: int, actual: str) -> dict:
    fila = _bloquear(cursor, id_pendiente)
    if not fila or not _mismo_usuario(fila["usuario"], actual) or _estado(fila) != "pendiente" or not fila["visible"]:
        conn.rollback()
        raise HTTPException(status_code=404, detail="No encontrado en cola")
    cursor.execute("UPDATE pendientes SET estado = 'en proceso' WHERE id = %s", (id_pendiente,))
    conn.commit()
    return pendiente_json(dict(fila, estado="en proceso"))


@router.post("/pendientes/atender")
async def cola_atender_siguiente(actual: str = Depends(usuario_autenticado)):
    """Toma la siguiente de TU cola (pasa a 'en proceso')."""
    conn, cursor = _conexion()
    try:
        cola = _cola(cursor, actual)
        conn.rollback()  # la lectura no retiene nada; _atender abre su propio bloqueo
        if not cola:
            raise HTTPException(status_code=404, detail="No hay pendientes en cola")
        atendido = _atender(cursor, conn, cola[0]["id"], actual)
        return {"atendido": atendido, "enCola": _tamano_cola(cursor, actual)}
    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error al atender el pendiente"))
    finally:
        cursor.close()
        conn.close()


@router.post("/pendientes/{id_pendiente}/atender")
async def cola_atender(id_pendiente: int, actual: str = Depends(usuario_autenticado)):
    """Toma una tarea tuya elegida (pasa a 'en proceso'), sin importar su prioridad ni las que ya tengas abiertas."""
    conn, cursor = _conexion()
    try:
        atendido = _atender(cursor, conn, id_pendiente, actual)
        return {"atendido": atendido, "enCola": _tamano_cola(cursor, actual)}
    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error al atender el pendiente"))
    finally:
        cursor.close()
        conn.close()


@router.post("/pendientes/{id_pendiente}/terminar")
async def cola_terminar(id_pendiente: int, actual: str = Depends(usuario_autenticado)):
    """Marca tu tarea como terminada. Con recurrencia clona la siguiente instancia (oculta hasta su fecha).
    Repetir la llamada no vuelve a clonar."""
    conn, cursor = _conexion()
    try:
        fila = _bloquear(cursor, id_pendiente)
        if not fila or not _mismo_usuario(fila["usuario"], actual):
            conn.rollback()
            raise HTTPException(status_code=404, detail="No encontrado")
        if _estado(fila) == "terminado":
            conn.rollback()
            return {"terminado": True, "id": id_pendiente}
        cursor.execute("UPDATE pendientes SET estado = 'terminado' WHERE id = %s", (id_pendiente,))
        nueva_id = None
        if fila.get("recurrencia"):
            ancla = fila.get("fecha_promesa") or fila.get("fecha") or date.today()
            ancla = ancla.date() if isinstance(ancla, datetime) else ancla
            proxima = proxima_fecha(ancla, fila["recurrencia"])
            cursor.execute(
                "INSERT INTO pendientes (fecha, usuario, actividad, prioridad, estado, observaciones, fecha_promesa,"
                " recurrencia, fecha_visible, plantilla_id) VALUES (CURDATE(), %s, %s, %s, 'pendiente', %s, %s, %s, %s, %s)",
                (fila["usuario"], fila["actividad"], fila["prioridad"], fila["observaciones"], proxima,
                 fila["recurrencia"], proxima, fila.get("plantilla_id") or fila["id"]),
            )
            nueva_id = cursor.lastrowid
        conn.commit()
        return {"terminado": True, "id": id_pendiente, **({"siguiente": nueva_id} if nueva_id else {})}
    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error al terminar el pendiente"))
    finally:
        cursor.close()
        conn.close()


@router.post("/pendientes/{id_pendiente}/recurrencia")
async def cola_recurrencia(id_pendiente: int, datos: RecurrenciaCambio, actual: str = Depends(usuario_autenticado)):
    """Cambia (o quita) la recurrencia de una tarea. Su dueño o gerencia."""
    conn, cursor = _conexion()
    try:
        fila = _bloquear(cursor, id_pendiente)
        if not fila or not (es_gerencia(actual) or _mismo_usuario(fila["usuario"], actual)):
            conn.rollback()
            raise HTTPException(status_code=404, detail="No encontrado")
        cursor.execute("UPDATE pendientes SET recurrencia = %s WHERE id = %s", (datos.valor, id_pendiente))
        conn.commit()
        return pendiente_json(dict(fila, recurrencia=datos.valor))
    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error al cambiar la recurrencia"))
    finally:
        cursor.close()
        conn.close()
