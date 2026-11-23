"""Alta de cuentas de acceso y niveles de usuario.

Niveles (ver permisos.py): gerencia (variable de entorno USUARIOS_GERENCIA), developer y ventas.
Gerencia elige developer o ventas al crear la cuenta; gerencia no se asigna desde aquí porque
sale del entorno (no hay lista en el código ni en la BD).
"""
import re
import bcrypt
import mysql.connector
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import mov_reg
from errores_db import detalle_error_db
from permisos import (
    get_db_connection, requerir_gerencia, usuario_autenticado, nivel_usuario,
    es_gerencia, NIVELES_ASIGNABLES, NIVEL_VENTAS, NIVEL_GERENCIA,
)

router = APIRouter(tags=["/usuarios"])

PATRON_USUARIO = re.compile(r"^[A-Za-z0-9._-]{3,40}$")
PASSWORD_MIN = 8


class NivelCambio(BaseModel):
    nivel: str


class UsuarioNuevo(BaseModel):
    nombre_usuario: str
    password: str
    nivel: str = NIVEL_VENTAS


def validar_usuario_nuevo(datos: UsuarioNuevo) -> tuple:
    """Devuelve (nombre, nivel) normalizados o lanza ValueError con el motivo."""
    nombre = (datos.nombre_usuario or "").strip()
    if not PATRON_USUARIO.match(nombre):
        raise ValueError("El usuario debe tener de 3 a 40 caracteres: letras, números, punto, guion o guion bajo")
    if es_gerencia(nombre):
        raise ValueError("Ese nombre está reservado para un usuario de gerencia")
    if len(datos.password or "") < PASSWORD_MIN:
        raise ValueError(f"La contraseña debe tener al menos {PASSWORD_MIN} caracteres")
    nivel = (datos.nivel or "").strip().lower()
    if nivel not in NIVELES_ASIGNABLES:
        raise ValueError(f"Nivel inválido. Elige uno de: {', '.join(NIVELES_ASIGNABLES)}")
    return nombre, nivel


@router.get("/usuarios/niveles")
async def listar_niveles(usuario: str = Depends(usuario_autenticado)):
    """Niveles que gerencia puede asignar y el nivel efectivo de quien consulta."""
    return {"niveles": list(NIVELES_ASIGNABLES), "mi_nivel": nivel_usuario(usuario)}


@router.post("/usuarios/nuevo", status_code=201)
async def crear_usuario(datos: UsuarioNuevo, quien: str = Depends(requerir_gerencia)):
    try:
        nombre, nivel = validar_usuario_nuevo(datos)
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err))

    hash_pw = bcrypt.hashpw(datos.password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT 1 FROM usuarios WHERE nombre_usuario = %s", (nombre,))
        if cursor.fetchone():
            raise HTTPException(status_code=409, detail=f"El usuario «{nombre}» ya existe")
        # `rol` ya existía en la tabla (se muestra en Usuarios): se llena con el mismo nivel.
        cursor.execute(
            "INSERT INTO usuarios (nombre_usuario, password_hash, rol, nivel) VALUES (%s, %s, %s, %s)",
            (nombre, hash_pw, nivel, nivel),
        )
        conn.commit()
        nuevo_id = cursor.lastrowid
        try:
            mov_reg.registrar_movimiento(quien, f"Creó la cuenta {nombre} (nivel {nivel})", "Usuarios")
        except Exception as err:  # la bitácora no debe tumbar el alta
            print(f"Bitácora usuarios: {err}")
        return {"status": "success", "id": nuevo_id, "nombre_usuario": nombre, "nivel": nivel}
    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error al crear el usuario"))
    finally:
        cursor.close()
        conn.close()


@router.get("/usuarios/lista")
async def listar_cuentas(_: str = Depends(requerir_gerencia)):
    """Cuentas con su nivel efectivo (gerencia sale del entorno). Developer puede verla, solo gerencia cambia niveles."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, nombre_usuario, nivel FROM usuarios ORDER BY nombre_usuario")
        filas = cursor.fetchall()
    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error al listar usuarios"))
    finally:
        cursor.close()
        conn.close()
    salida = []
    for f in filas:
        nivel = str(f.get("nivel") or NIVEL_VENTAS).strip().lower()
        if es_gerencia(f["nombre_usuario"]):
            nivel = NIVEL_GERENCIA
        elif nivel not in NIVELES_ASIGNABLES:
            nivel = NIVEL_VENTAS
        salida.append({"id": f["id"], "nombre_usuario": f["nombre_usuario"], "nivel": nivel,
                       "editable": nivel != NIVEL_GERENCIA})
    return salida


@router.put("/usuarios/{nombre_usuario}/nivel")
async def cambiar_nivel(nombre_usuario: str, datos: NivelCambio, quien: str = Depends(requerir_gerencia)):
    nivel = (datos.nivel or "").strip().lower()
    if nivel not in NIVELES_ASIGNABLES:
        raise HTTPException(status_code=422, detail=f"Nivel inválido. Elige uno de: {', '.join(NIVELES_ASIGNABLES)}")
    if es_gerencia(nombre_usuario):
        raise HTTPException(status_code=409, detail="Los usuarios de gerencia se definen en USUARIOS_GERENCIA del servidor, no desde aquí")
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT nombre_usuario, nivel FROM usuarios WHERE nombre_usuario = %s FOR UPDATE", (nombre_usuario,))
        fila = cursor.fetchone()
        if not fila:
            conn.rollback()
            raise HTTPException(status_code=404, detail="Usuario no encontrado")
        anterior = str(fila.get("nivel") or NIVEL_VENTAS).strip().lower()
        if anterior == nivel:
            conn.rollback()
            return {"status": "sin_cambios", "nombre_usuario": fila["nombre_usuario"], "nivel": nivel}
        cursor.execute("UPDATE usuarios SET nivel = %s WHERE nombre_usuario = %s", (nivel, nombre_usuario))
        conn.commit()
        try:
            mov_reg.registrar_movimiento(quien, f"Cambió el nivel de {fila['nombre_usuario']}: {anterior} -> {nivel}", "Usuarios")
        except Exception as err:
            print(f"Bitácora usuarios: {err}")
        return {"status": "success", "nombre_usuario": fila["nombre_usuario"], "nivel": nivel}
    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=detalle_error_db(err, "Error al cambiar el nivel"))
    finally:
        cursor.close()
        conn.close()
