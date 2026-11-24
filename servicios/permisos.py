import os
import mysql.connector
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

load_dotenv()

# Niveles de usuario:
#   gerencia  -> lo definen los usuarios de la variable de entorno USUARIOS_GERENCIA ("a,b,c"); no hay
#                lista en el código. Sin la variable, nadie es gerencia.
#   developer -> ve todo (también las pantallas y GET exclusivos de gerencia) pero no cambia nada
#                exclusivo de gerencia (cualquier método que no sea de lectura da 403).
#   ventas    -> usuario normal (el que ya existía antes de los niveles).
# developer y ventas se guardan en usuarios.nivel y los elige gerencia al crear la cuenta.
NIVEL_GERENCIA = "gerencia"
NIVEL_DEVELOPER = "developer"
NIVEL_VENTAS = "ventas"
NIVELES_ASIGNABLES = (NIVEL_VENTAS, NIVEL_DEVELOPER)
METODOS_LECTURA = ("GET", "HEAD", "OPTIONS")

security = HTTPBearer()

# Vigencia de la sesión: el token de usuarios.token deja de servir pasadas estas horas
# (usuarios.token_expira, se renueva en cada login). Sobreescribible con TOKEN_HORAS.
TOKEN_HORAS_POR_DEFECTO = 12


def token_horas() -> int:
    try:
        return max(1, int(os.getenv("TOKEN_HORAS") or TOKEN_HORAS_POR_DEFECTO))
    except ValueError:
        return TOKEN_HORAS_POR_DEFECTO


def usuarios_gerencia() -> set:
    crudo = os.getenv("USUARIOS_GERENCIA") or ""
    return {u.strip().lower() for u in crudo.split(",") if u.strip()}


def es_gerencia(usuario) -> bool:
    return bool(usuario) and str(usuario).strip().lower() in usuarios_gerencia()


def nivel_usuario(usuario) -> str:
    """Nivel efectivo: gerencia (por entorno), si no el guardado en usuarios.nivel; ventas si no hay dato
    o la consulta falla (el nivel más bajo: fallar cerrado)."""
    if es_gerencia(usuario):
        return NIVEL_GERENCIA
    if not usuario:
        return NIVEL_VENTAS
    conn = cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT nivel FROM usuarios WHERE nombre_usuario = %s", (usuario,))
        fila = cursor.fetchone()
        nivel = str(fila[0]).strip().lower() if fila and fila[0] else NIVEL_VENTAS
        return nivel if nivel in NIVELES_ASIGNABLES else NIVEL_VENTAS
    except Exception as err:
        print(f"Error leyendo nivel de {usuario}: {err}")
        return NIVEL_VENTAS
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def puede_ver_todo(usuario) -> bool:
    """Lectura de los datos de todo el equipo (listas «solo lo mío»): gerencia o developer.
    SOLO para decidir qué se LEE; los permisos de escritura siguen por es_gerencia."""
    return es_gerencia(usuario) or nivel_usuario(usuario) == NIVEL_DEVELOPER


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )


def asegurar_columna_token_expira():
    """Agrega usuarios.token_expira si falta (se llama en el lifespan). Los tokens emitidos
    antes de la migración quedan con NULL = vencidos: cada usuario inicia sesión una vez."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.COLUMNS"
            " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'usuarios' AND COLUMN_NAME = 'token_expira'"
        )
        if (cursor.fetchone() or [0])[0] == 0:
            cursor.execute("ALTER TABLE usuarios ADD COLUMN token_expira DATETIME NULL")
            conn.commit()
    finally:
        cursor.close()
        conn.close()


def asegurar_columna_nivel():
    """Agrega usuarios.nivel ('ventas' por defecto, así los usuarios actuales quedan como ventas)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT COUNT(*) FROM information_schema.COLUMNS"
            " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'usuarios' AND COLUMN_NAME = 'nivel'"
        )
        if (cursor.fetchone() or [0])[0] == 0:
            cursor.execute("ALTER TABLE usuarios ADD COLUMN nivel VARCHAR(20) NOT NULL DEFAULT 'ventas'")
            conn.commit()
    finally:
        cursor.close()
        conn.close()


def usuario_autenticado(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """Usuario dueño del token vigente (main.obtener_usuario_actual es este mismo)."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT nombre_usuario FROM usuarios WHERE token = %s AND token_expira > NOW()",
            (credentials.credentials,),
        )
        fila = cursor.fetchone()
        if not fila:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token inválido o expirado",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return fila["nombre_usuario"]
    except mysql.connector.Error as err:
        print(f"Error DB token: {err}")
        raise HTTPException(status_code=500, detail="Error interno en DB")
    finally:
        cursor.close()
        conn.close()


def requerir_gerencia(request: Request, usuario: str = Depends(usuario_autenticado)) -> str:
    """Dependencia para endpoints exclusivos de gerencia: 403 si el token no es de gerencia.
    Excepción: un developer puede LEER (GET/HEAD/OPTIONS), nunca cambiar.
    El usuario sale del token, nunca de un parámetro que mande el cliente."""
    if es_gerencia(usuario):
        return usuario
    if request.method in METODOS_LECTURA and nivel_usuario(usuario) == NIVEL_DEVELOPER:
        return usuario
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Se requiere nivel de gerencia para esta acción")
