# CRM ligero: bitácora de interacciones, etapa comercial por cliente,
# "Mis seguimientos" del vendedor y métricas para gerencia.
#
# Permisos (el usuario SIEMPRE sale del token, nunca de un parámetro):
#   - Gerencia (permisos.es_gerencia) ve y gestiona todo; solo ella borra
#     interacciones, reasigna cartera y consulta métricas.
#   - Vendedor: gestiona los clientes de su cartera. Dueño de un cliente =
#     crm_cartera.vendedor; si el cliente aún no entra al CRM, su dueno
#     provisional es clientes.usuario (quien lo dio de alta). Un cliente sin
#     ninguno de los dos lo "toma" quien registre la primera interacción.
import os
from datetime import date, datetime, timedelta, timezone
from typing import List, Literal, Optional

import mysql.connector
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

import crm_metricas
import mov_reg
from permisos import es_gerencia, requerir_gerencia, usuario_autenticado

router = APIRouter(tags=["/crm"], responses={404: {"Mensaje": "No encontrado"}})
load_dotenv()

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "sql", "crm_schema.sql")

RANGO_MAX_DIAS = 366
DIAS_PROXIMOS = 7  # "Mis seguimientos" muestra también lo de la próxima semana

Tipo = Literal["llamada", "correo", "whatsapp", "reunion"]
Etapa = Literal["contacto_inicial", "en_seguimiento", "cotizado", "ganado", "perdido"]
Resultado = Literal["contesto", "no_contesto", "interesado", "no_interesado", "pidio_cotizacion"]


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )


# México no tiene horario de verano desde 2022: si el SO no trae la base IANA
# (Windows sin tzdata) UTC-6 fijo da la misma hora.
try:
    from zoneinfo import ZoneInfo
    _TZ_MX = ZoneInfo("America/Mexico_City")
except Exception:
    _TZ_MX = timezone(timedelta(hours=-6))


def ahora_mx() -> datetime:
    """Hora local de México sin tzinfo (así se guarda en DATETIME)."""
    return datetime.now(_TZ_MX).replace(tzinfo=None, microsecond=0)


def hoy_mx() -> date:
    return ahora_mx().date()


def crear_tablas_crm():
    """Crea las tablas del CRM si no existen (se llama en el lifespan). Idempotente."""
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        lineas = [l for l in f if not l.strip().startswith("--")]
    statements = [s.strip() for s in "".join(lineas).split(";") if s.strip()]

    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        for stmt in statements:
            cursor.execute(stmt)
        conn.commit()
        print("Tablas del CRM verificadas/creadas.")
    except mysql.connector.Error as err:
        conn.rollback()
        print(f"Error creando tablas del CRM: {err}")
    finally:
        cursor.close()
        conn.close()


# ---------- Schemas ----------

def _texto_opcional(v):
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def _validar_proxima_fecha(v):
    if v is not None and v < hoy_mx():
        raise ValueError("La fecha compromiso no puede estar en el pasado")
    return v


class InteraccionNueva(BaseModel):
    # Solo cliente y tipo son obligatorios: lo demás se captura si hay tiempo.
    cliente_id: int = Field(gt=0)
    tipo: Tipo
    fecha: Optional[datetime] = None  # por defecto, ahora
    resultado: Optional[Resultado] = None
    notas: Optional[str] = Field(None, max_length=2000)
    proxima_accion: Optional[str] = Field(None, max_length=255)
    proxima_fecha: Optional[date] = None
    etapa: Optional[Etapa] = None  # cambio de etapa en la misma captura
    motivo_perdida: Optional[str] = Field(None, max_length=255)

    _limpiar = field_validator("notas", "proxima_accion", "motivo_perdida")(_texto_opcional)
    _proxima = field_validator("proxima_fecha")(_validar_proxima_fecha)

    @field_validator("fecha")
    @classmethod
    def fecha_no_futura(cls, v):
        if v is None:
            return v
        if v.tzinfo is not None:
            v = v.astimezone(_TZ_MX).replace(tzinfo=None)
        if v > ahora_mx() + timedelta(minutes=5):
            raise ValueError("La fecha de la interacción no puede estar en el futuro")
        return v.replace(microsecond=0)


class InteraccionEditar(BaseModel):
    resultado: Optional[Resultado] = None
    notas: Optional[str] = Field(None, max_length=2000)
    proxima_accion: Optional[str] = Field(None, max_length=255)
    proxima_fecha: Optional[date] = None
    seguimiento_cerrado: Optional[bool] = None

    _limpiar = field_validator("notas", "proxima_accion")(_texto_opcional)
    _proxima = field_validator("proxima_fecha")(_validar_proxima_fecha)


class EtapaCambio(BaseModel):
    etapa: Etapa
    motivo_perdida: Optional[str] = Field(None, max_length=255)

    _limpiar = field_validator("motivo_perdida")(_texto_opcional)


class AsignacionVendedor(BaseModel):
    vendedor: str = Field(min_length=1, max_length=50)

    @field_validator("vendedor")
    @classmethod
    def no_vacio(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("El vendedor es obligatorio")
        return v


# ---------- Reglas de permisos (puras) ----------

def vendedor_efectivo(fila) -> Optional[str]:
    """Dueño del cliente: el de la cartera CRM o, si no ha entrado, quien lo registró."""
    for campo in ("vendedor", "registrado_por"):
        valor = str(fila.get(campo) or "").strip()
        if valor:
            return valor
    return None


def puede_gestionar(usuario: str, vendedor: Optional[str]) -> bool:
    if es_gerencia(usuario):
        return True
    if vendedor is None:  # cliente sin dueno: cualquiera lo puede tomar
        return True
    return vendedor.strip().lower() == str(usuario).strip().lower()


def validar_rango(desde: Optional[date], hasta: Optional[date]):
    """Por defecto los últimos 30 días. 422 si el rango está invertido o es enorme."""
    hasta = hasta or hoy_mx()
    desde = desde or (hasta - timedelta(days=29))
    if desde > hasta:
        raise HTTPException(status_code=422, detail="'desde' no puede ser posterior a 'hasta'")
    if (hasta - desde).days + 1 > RANGO_MAX_DIAS:
        raise HTTPException(status_code=422, detail=f"El rango máximo es de {RANGO_MAX_DIAS} días")
    return desde, hasta


def _en(columna: str, valores) -> tuple:
    """Fragmento 'columna IN (%s, ...)' con sus parámetros."""
    return f"{columna} IN ({', '.join(['%s'] * len(valores))})", list(valores)


# ---------- Acceso a datos ----------

def _fila_cliente(cursor, cliente_id: int):
    cursor.execute(
        """SELECT c.id, c.nombre, c.empresa, c.telefono, c.email, c.usuario AS registrado_por,
                  cc.cliente_id AS en_cartera, cc.vendedor, cc.etapa, cc.motivo_perdida, cc.etapa_actualizada
           FROM clientes c LEFT JOIN crm_cartera cc ON cc.cliente_id = c.id
           WHERE c.id = %s""",
        (cliente_id,),
    )
    return cursor.fetchone()


def _cliente_autorizado(cursor, cliente_id: int, usuario: str):
    fila = _fila_cliente(cursor, cliente_id)
    if not fila:
        raise HTTPException(status_code=404, detail="Cliente no encontrado")
    if not puede_gestionar(usuario, vendedor_efectivo(fila)):
        raise HTTPException(status_code=403, detail="Este cliente pertenece a la cartera de otro vendedor")
    return fila


def _asegurar_cartera(cursor, fila, usuario: str) -> str:
    """Mete al cliente al CRM si aún no está. Devuelve la etapa actual."""
    if fila.get("en_cartera"):
        return fila["etapa"]
    dueno = vendedor_efectivo(fila) or usuario
    cursor.execute(
        "INSERT INTO crm_cartera (cliente_id, vendedor, etapa) VALUES (%s, %s, 'contacto_inicial')",
        (fila["id"], dueno),
    )
    cursor.execute(
        "INSERT INTO crm_etapas_historial (cliente_id, etapa_anterior, etapa_nueva, usuario) VALUES (%s, NULL, 'contacto_inicial', %s)",
        (fila["id"], usuario),
    )
    fila.update({"en_cartera": fila["id"], "vendedor": dueno, "etapa": "contacto_inicial"})
    return "contacto_inicial"


def _cambiar_etapa(cursor, cliente_id: int, anterior: str, nueva: str, motivo: Optional[str], usuario: str) -> bool:
    if nueva == anterior and nueva != "perdido":
        return False
    motivo = motivo if nueva == "perdido" else None
    cursor.execute(
        "UPDATE crm_cartera SET etapa = %s, motivo_perdida = %s, etapa_actualizada = %s WHERE cliente_id = %s",
        (nueva, motivo, ahora_mx(), cliente_id),
    )
    if nueva != anterior:
        cursor.execute(
            "INSERT INTO crm_etapas_historial (cliente_id, etapa_anterior, etapa_nueva, usuario) VALUES (%s, %s, %s, %s)",
            (cliente_id, anterior, nueva, usuario),
        )
    return nueva != anterior


def _error_db(conn, err, contexto: str):
    try:
        conn.rollback()
    except Exception:
        pass
    print(f"Error DB CRM ({contexto}): {err}")
    raise HTTPException(status_code=500, detail=f"Error en DB: {err}")


# Dueño efectivo en SQL (mismo criterio que vendedor_efectivo)
_DUENO_SQL = "COALESCE(cc.vendedor, NULLIF(TRIM(c.usuario), ''))"


# ---------- Clientes ----------

@router.get("/crm/clientes")
async def crm_clientes(
    q: Optional[str] = Query(None, max_length=100),
    etapa: Optional[Etapa] = None,
    vendedor: Optional[str] = None,
    solo_mios: bool = False,
    limit: int = Query(20, ge=1, le=500),
    usuario: str = Depends(usuario_autenticado),
):
    """
    Buscador / cartera. Vendedor: sus clientes y los que no tienen dueno
    (solo_mios=true quita estos últimos). Gerencia: todos, o filtra por vendedor.
    """
    where, params = ["1=1"], []
    if q and q.strip():
        like = f"%{q.strip()}%"
        where.append("(c.nombre LIKE %s OR c.empresa LIKE %s OR c.contacto LIKE %s OR CAST(c.telefono AS CHAR) LIKE %s)")
        params += [like] * 4
    if etapa:
        where.append("cc.etapa = %s")
        params.append(etapa)
    if es_gerencia(usuario):
        if vendedor:
            where.append(f"{_DUENO_SQL} = %s")
            params.append(vendedor)
    elif solo_mios:
        where.append(f"{_DUENO_SQL} = %s")
        params.append(usuario)
    else:
        where.append(f"({_DUENO_SQL} = %s OR {_DUENO_SQL} IS NULL)")
        params.append(usuario)

    query = f"""
        SELECT c.id, c.nombre, c.empresa, c.contacto, c.telefono, c.email,
               {_DUENO_SQL} AS vendedor, cc.etapa,
               (SELECT MAX(i.fecha) FROM crm_interacciones i
                 WHERE i.cliente_id = c.id AND i.eliminado = 0) AS ultima_interaccion,
               (SELECT MIN(i.proxima_fecha) FROM crm_interacciones i
                 WHERE i.cliente_id = c.id AND i.eliminado = 0 AND i.seguimiento_cerrado = 0
                   AND i.proxima_fecha IS NOT NULL) AS proximo_seguimiento
        FROM clientes c LEFT JOIN crm_cartera cc ON cc.cliente_id = c.id
        WHERE {' AND '.join(where)}
        ORDER BY c.nombre
        LIMIT %s
    """
    params.append(limit)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(query, tuple(params))
        return cursor.fetchall()
    except mysql.connector.Error as err:
        _error_db(conn, err, "clientes")
    finally:
        cursor.close()
        conn.close()


@router.get("/crm/clientes/{cliente_id}")
async def crm_cliente_ficha(cliente_id: int, usuario: str = Depends(usuario_autenticado)):
    """Ficha: datos, dueno, etapa, últimas 20 interacciones e historial de etapas."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        fila = _cliente_autorizado(cursor, cliente_id, usuario)
        cursor.execute(
            """SELECT id, vendedor, tipo, fecha, resultado, notas, proxima_accion, proxima_fecha, seguimiento_cerrado
               FROM crm_interacciones WHERE cliente_id = %s AND eliminado = 0
               ORDER BY fecha DESC, id DESC LIMIT 20""",
            (cliente_id,),
        )
        interacciones = cursor.fetchall()
        cursor.execute(
            """SELECT etapa_anterior, etapa_nueva, usuario, fecha FROM crm_etapas_historial
               WHERE cliente_id = %s ORDER BY fecha DESC, id DESC LIMIT 10""",
            (cliente_id,),
        )
        historial = cursor.fetchall()
        cliente = {k: v for k, v in fila.items() if k not in ("en_cartera", "registrado_por")}
        cliente["vendedor"] = vendedor_efectivo(fila)
        return {"cliente": cliente, "interacciones": interacciones, "historial_etapas": historial}
    except mysql.connector.Error as err:
        _error_db(conn, err, "ficha")
    finally:
        cursor.close()
        conn.close()


@router.patch("/crm/clientes/{cliente_id}/etapa")
async def crm_cambiar_etapa(cliente_id: int, datos: EtapaCambio, usuario: str = Depends(usuario_autenticado)):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        fila = _cliente_autorizado(cursor, cliente_id, usuario)
        anterior = _asegurar_cartera(cursor, fila, usuario)
        cambio = _cambiar_etapa(cursor, cliente_id, anterior, datos.etapa, datos.motivo_perdida, usuario)
        conn.commit()
        if cambio:
            mov_reg.registrar_movimiento(usuario, f"Cambió etapa de {fila['nombre']}: {anterior} → {datos.etapa}", "CRM")
        return {"cliente_id": cliente_id, "etapa": datos.etapa, "etapa_anterior": anterior, "cambio": cambio}
    except mysql.connector.Error as err:
        _error_db(conn, err, "etapa")
    finally:
        cursor.close()
        conn.close()


@router.put("/crm/clientes/{cliente_id}/vendedor")
async def crm_asignar_vendedor(cliente_id: int, datos: AsignacionVendedor, usuario: str = Depends(requerir_gerencia)):
    """Asigna o reasigna el cliente a un vendedor (solo gerencia)."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        fila = _fila_cliente(cursor, cliente_id)
        if not fila:
            raise HTTPException(status_code=404, detail="Cliente no encontrado")
        cursor.execute("SELECT nombre_usuario FROM usuarios WHERE nombre_usuario = %s", (datos.vendedor,))
        existe = cursor.fetchone()
        if not existe:
            raise HTTPException(status_code=422, detail=f"El usuario '{datos.vendedor}' no existe")
        vendedor = existe["nombre_usuario"]
        if fila.get("en_cartera"):
            cursor.execute("UPDATE crm_cartera SET vendedor = %s WHERE cliente_id = %s", (vendedor, cliente_id))
        else:
            cursor.execute(
                "INSERT INTO crm_cartera (cliente_id, vendedor, etapa) VALUES (%s, %s, 'contacto_inicial')",
                (cliente_id, vendedor),
            )
            cursor.execute(
                "INSERT INTO crm_etapas_historial (cliente_id, etapa_anterior, etapa_nueva, usuario) VALUES (%s, NULL, 'contacto_inicial', %s)",
                (cliente_id, usuario),
            )
        conn.commit()
        mov_reg.registrar_movimiento(usuario, f"Asignó el cliente {fila['nombre']} a {vendedor}", "CRM")
        return {"cliente_id": cliente_id, "vendedor": vendedor, "vendedor_anterior": vendedor_efectivo(fila)}
    except mysql.connector.Error as err:
        _error_db(conn, err, "asignar")
    finally:
        cursor.close()
        conn.close()


@router.get("/crm/vendedores")
async def crm_vendedores(usuario: str = Depends(requerir_gerencia)):
    """Usuarios activos, para los filtros y la reasignación del panel de gerencia."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT u.nombre_usuario AS vendedor FROM usuarios u
               LEFT JOIN empleados e ON e.usuario = u.nombre_usuario
               WHERE COALESCE(e.estatus, 1) = 1 ORDER BY u.nombre_usuario"""
        )
        return [f["vendedor"] for f in cursor.fetchall()]
    except mysql.connector.Error as err:
        _error_db(conn, err, "vendedores")
    finally:
        cursor.close()
        conn.close()


# ---------- Interacciones ----------

@router.post("/crm/interacciones")
async def crm_registrar_interaccion(datos: InteraccionNueva, usuario: str = Depends(usuario_autenticado)):
    """
    Registra una interacción. Si el cliente no estaba en el CRM entra en
    'contacto_inicial'. Los seguimientos abiertos del cliente se cierran: esta
    interacción es el seguimiento. Opcionalmente cambia la etapa en la misma captura.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        fila = _cliente_autorizado(cursor, datos.cliente_id, usuario)
        etapa_actual = _asegurar_cartera(cursor, fila, usuario)

        cursor.execute(
            "UPDATE crm_interacciones SET seguimiento_cerrado = 1 WHERE cliente_id = %s AND seguimiento_cerrado = 0 AND eliminado = 0",
            (datos.cliente_id,),
        )
        cerrados = cursor.rowcount

        cursor.execute(
            """INSERT INTO crm_interacciones
               (cliente_id, vendedor, tipo, fecha, resultado, notas, proxima_accion, proxima_fecha)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
            (datos.cliente_id, usuario, datos.tipo, datos.fecha or ahora_mx(), datos.resultado,
             datos.notas, datos.proxima_accion, datos.proxima_fecha),
        )
        nuevo_id = cursor.lastrowid

        etapa_final = etapa_actual
        if datos.etapa:
            _cambiar_etapa(cursor, datos.cliente_id, etapa_actual, datos.etapa, datos.motivo_perdida, usuario)
            etapa_final = datos.etapa

        conn.commit()
        mov_reg.registrar_movimiento(usuario, f"Registró {datos.tipo} con {fila['nombre']}", "CRM")
        return {
            "id": nuevo_id,
            "cliente_id": datos.cliente_id,
            "etapa": etapa_final,
            "seguimientos_cerrados": max(cerrados or 0, 0),
        }
    except mysql.connector.Error as err:
        _error_db(conn, err, "registrar")
    finally:
        cursor.close()
        conn.close()


@router.get("/crm/interacciones")
async def crm_bitacora(
    desde: Optional[date] = None,
    hasta: Optional[date] = None,
    vendedor: Optional[List[str]] = Query(None),
    tipo: Optional[List[Tipo]] = Query(None),
    cliente_id: Optional[int] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    usuario: str = Depends(usuario_autenticado),
):
    """
    Bitácora filtrable. Con cliente_id devuelve todo el historial de ese cliente
    (si es de tu cartera). Sin cliente_id, un vendedor solo ve lo que él registró
    aunque mande otro vendedor en el filtro.
    """
    desde, hasta = validar_rango(desde, hasta)
    where = ["i.eliminado = 0", "i.fecha >= %s", "i.fecha < %s"]
    params = [desde, hasta + timedelta(days=1)]

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        if cliente_id is not None:
            _cliente_autorizado(cursor, cliente_id, usuario)
            where.append("i.cliente_id = %s")
            params.append(cliente_id)
        elif not es_gerencia(usuario):
            where.append("i.vendedor = %s")
            params.append(usuario)

        if vendedor and es_gerencia(usuario):
            frag, vals = _en("i.vendedor", vendedor)
            where.append(frag)
            params += vals
        if tipo:
            frag, vals = _en("i.tipo", tipo)
            where.append(frag)
            params += vals

        filtro = " AND ".join(where)
        cursor.execute(f"SELECT COUNT(*) AS total FROM crm_interacciones i WHERE {filtro}", tuple(params))
        total = (cursor.fetchone() or {}).get("total", 0)
        cursor.execute(
            f"""SELECT i.id, i.cliente_id, c.nombre AS cliente, i.vendedor, i.tipo, i.fecha, i.resultado,
                       i.notas, i.proxima_accion, i.proxima_fecha, i.seguimiento_cerrado
                FROM crm_interacciones i LEFT JOIN clientes c ON c.id = i.cliente_id
                WHERE {filtro}
                ORDER BY i.fecha DESC, i.id DESC LIMIT %s OFFSET %s""",
            tuple(params + [limit, offset]),
        )
        return {"total": total, "limit": limit, "offset": offset, "items": cursor.fetchall()}
    except mysql.connector.Error as err:
        _error_db(conn, err, "bitácora")
    finally:
        cursor.close()
        conn.close()


@router.patch("/crm/interacciones/{interaccion_id}")
async def crm_editar_interaccion(interaccion_id: int, datos: InteraccionEditar, usuario: str = Depends(usuario_autenticado)):
    """Edita notas / resultado / próxima acción o marca el seguimiento como hecho. Autor o gerencia."""
    campos = {k: getattr(datos, k) for k in datos.model_fields_set}
    if not campos:
        raise HTTPException(status_code=422, detail="No se envió ningún campo para actualizar")

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, vendedor FROM crm_interacciones WHERE id = %s AND eliminado = 0", (interaccion_id,))
        fila = cursor.fetchone()
        if not fila:
            raise HTTPException(status_code=404, detail="Interacción no encontrada")
        if not es_gerencia(usuario) and fila["vendedor"].strip().lower() != usuario.strip().lower():
            raise HTTPException(status_code=403, detail="Solo el autor o gerencia pueden editar esta interacción")

        # Orden fijo de columnas: el nombre sale del schema, nunca del cliente.
        columnas = [c for c in ("resultado", "notas", "proxima_accion", "proxima_fecha", "seguimiento_cerrado") if c in campos]
        valores = [int(campos[c]) if c == "seguimiento_cerrado" else campos[c] for c in columnas]
        cursor.execute(
            f"UPDATE crm_interacciones SET {', '.join(f'{c} = %s' for c in columnas)} WHERE id = %s",
            tuple(valores + [interaccion_id]),
        )
        conn.commit()
        return {"id": interaccion_id, "actualizados": columnas}
    except mysql.connector.Error as err:
        _error_db(conn, err, "editar")
    finally:
        cursor.close()
        conn.close()


@router.delete("/crm/interacciones/{interaccion_id}")
async def crm_eliminar_interaccion(interaccion_id: int, usuario: str = Depends(requerir_gerencia)):
    """Borrado lógico (solo gerencia): deja de contar en bitácora, seguimientos y métricas."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("UPDATE crm_interacciones SET eliminado = 1 WHERE id = %s AND eliminado = 0", (interaccion_id,))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Interacción no encontrada")
        conn.commit()
        mov_reg.registrar_movimiento(usuario, f"Eliminó la interacción CRM {interaccion_id}", "CRM")
        return {"id": interaccion_id, "eliminado": True}
    except mysql.connector.Error as err:
        _error_db(conn, err, "eliminar")
    finally:
        cursor.close()
        conn.close()


# ---------- Seguimientos ----------

@router.get("/crm/seguimientos")
async def crm_seguimientos(vendedor: Optional[str] = None, usuario: str = Depends(usuario_autenticado)):
    """
    Seguimientos abiertos (vencidos, de hoy y de los próximos 7 días). Se asignan
    al dueno actual del cliente, así una reasignación mueve también sus pendientes.
    Vendedor: los suyos. Gerencia: todos o los de ?vendedor=.
    """
    hoy = hoy_mx()
    dueno = "COALESCE(cc.vendedor, i.vendedor)"
    where = ["i.eliminado = 0", "i.seguimiento_cerrado = 0", "i.proxima_fecha IS NOT NULL", "i.proxima_fecha <= %s"]
    params = [hoy + timedelta(days=DIAS_PROXIMOS)]
    if not es_gerencia(usuario):
        where.append(f"{dueno} = %s")
        params.append(usuario)
    elif vendedor:
        where.append(f"{dueno} = %s")
        params.append(vendedor)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            f"""SELECT i.id, i.cliente_id, c.nombre AS cliente, c.empresa, c.telefono, c.email,
                       {dueno} AS vendedor, i.tipo, i.fecha, i.resultado, i.notas,
                       i.proxima_accion, i.proxima_fecha, cc.etapa
                FROM crm_interacciones i
                LEFT JOIN clientes c ON c.id = i.cliente_id
                LEFT JOIN crm_cartera cc ON cc.cliente_id = i.cliente_id
                WHERE {' AND '.join(where)}
                ORDER BY i.proxima_fecha, i.id""",
            tuple(params),
        )
        resultado = crm_metricas.agrupar_seguimientos(cursor.fetchall(), hoy)
        resultado["fecha"] = hoy.isoformat()
        return resultado
    except mysql.connector.Error as err:
        _error_db(conn, err, "seguimientos")
    finally:
        cursor.close()
        conn.close()


# ---------- Métricas (gerencia) ----------

@router.get("/crm/metricas/resumen")
async def crm_metricas_resumen(
    desde: Optional[date] = None,
    hasta: Optional[date] = None,
    vendedor: Optional[List[str]] = Query(None),
    tipo: Optional[List[Tipo]] = Query(None),
    usuario: str = Depends(requerir_gerencia),
):
    """Totales por tipo, tabla por vendedor, serie diaria y seguimientos vencidos."""
    desde, hasta = validar_rango(desde, hasta)
    where = ["i.eliminado = 0", "i.fecha >= %s", "i.fecha < %s"]
    params = [desde, hasta + timedelta(days=1)]
    if vendedor:
        frag, vals = _en("i.vendedor", vendedor)
        where.append(frag)
        params += vals
    if tipo:
        frag, vals = _en("i.tipo", tipo)
        where.append(frag)
        params += vals
    filtro = " AND ".join(where)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            f"""SELECT i.vendedor, i.tipo, DATE(i.fecha) AS dia, COUNT(*) AS total
                FROM crm_interacciones i WHERE {filtro}
                GROUP BY i.vendedor, i.tipo, DATE(i.fecha)""",
            tuple(params),
        )
        conteos = cursor.fetchall()

        cursor.execute(
            f"""SELECT i.vendedor, COUNT(DISTINCT i.cliente_id) AS clientes
                FROM crm_interacciones i WHERE {filtro} GROUP BY i.vendedor""",
            tuple(params),
        )
        clientes = cursor.fetchall()

        # Vencidos: foto de hoy, sin importar el periodo; por dueno actual del cliente.
        w_venc = ["i.eliminado = 0", "i.seguimiento_cerrado = 0", "i.proxima_fecha < %s"]
        p_venc = [hoy_mx()]
        if vendedor:
            frag, vals = _en("COALESCE(cc.vendedor, i.vendedor)", vendedor)
            w_venc.append(frag)
            p_venc += vals
        cursor.execute(
            f"""SELECT COALESCE(cc.vendedor, i.vendedor) AS vendedor, COUNT(*) AS vencidos
                FROM crm_interacciones i LEFT JOIN crm_cartera cc ON cc.cliente_id = i.cliente_id
                WHERE {' AND '.join(w_venc)} GROUP BY COALESCE(cc.vendedor, i.vendedor)""",
            tuple(p_venc),
        )
        vencidos = cursor.fetchall()

        movimientos = _movimientos_etapa(cursor, desde, hasta, vendedor, por_vendedor=True)
        return crm_metricas.armar_resumen(conteos, clientes, vencidos, movimientos, desde, hasta)
    except mysql.connector.Error as err:
        _error_db(conn, err, "resumen")
    finally:
        cursor.close()
        conn.close()


def _movimientos_etapa(cursor, desde, hasta, vendedor, por_vendedor: bool):
    where = ["h.fecha >= %s", "h.fecha < %s"]
    params = [desde, hasta + timedelta(days=1)]
    if vendedor:
        frag, vals = _en("cc.vendedor", vendedor)
        where.append(frag)
        params += vals
    columnas = "cc.vendedor, h.etapa_nueva" if por_vendedor else "h.etapa_nueva"
    cursor.execute(
        f"""SELECT {'cc.vendedor AS vendedor, ' if por_vendedor else ''}h.etapa_nueva AS etapa, COUNT(*) AS total
            FROM crm_etapas_historial h LEFT JOIN crm_cartera cc ON cc.cliente_id = h.cliente_id
            WHERE {' AND '.join(where)} GROUP BY {columnas}""",
        tuple(params),
    )
    return cursor.fetchall()


@router.get("/crm/metricas/embudo")
async def crm_metricas_embudo(
    desde: Optional[date] = None,
    hasta: Optional[date] = None,
    vendedor: Optional[List[str]] = Query(None),
    usuario: str = Depends(requerir_gerencia),
):
    """Clientes por etapa hoy (por vendedor) y entradas a cada etapa en el periodo."""
    desde, hasta = validar_rango(desde, hasta)
    where, params = ["1=1"], []
    if vendedor:
        frag, vals = _en("cc.vendedor", vendedor)
        where.append(frag)
        params += vals

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            f"""SELECT cc.vendedor, cc.etapa, COUNT(*) AS total FROM crm_cartera cc
                WHERE {' AND '.join(where)} GROUP BY cc.vendedor, cc.etapa""",
            tuple(params),
        )
        actual = cursor.fetchall()
        movimientos = _movimientos_etapa(cursor, desde, hasta, vendedor, por_vendedor=False)
        resultado = crm_metricas.armar_embudo(actual, movimientos)
        resultado.update({"desde": desde.isoformat(), "hasta": hasta.isoformat()})
        return resultado
    except mysql.connector.Error as err:
        _error_db(conn, err, "embudo")
    finally:
        cursor.close()
        conn.close()
