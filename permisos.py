import os
import mysql.connector
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

load_dotenv()

# Usuarios con nivel gerencia. Mismo criterio que GERENCIA_USERS en el panel
# (shell.jsx); si se agrega uno, hay que agregarlo en ambos lados.
# Se puede sobreescribir con la variable de entorno USUARIOS_GERENCIA ("a,b,c").
_POR_DEFECTO = "gerencia,fparra"

security = HTTPBearer()


def usuarios_gerencia() -> set:
    crudo = os.getenv("USUARIOS_GERENCIA") or _POR_DEFECTO
    return {u.strip().lower() for u in crudo.split(",") if u.strip()}


def es_gerencia(usuario) -> bool:
    return bool(usuario) and str(usuario).strip().lower() in usuarios_gerencia()


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )


def usuario_autenticado(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """Usuario dueño del token (misma consulta que obtener_usuario_actual de main.py)."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT nombre_usuario FROM usuarios WHERE token = %s", (credentials.credentials,))
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


def requerir_gerencia(usuario: str = Depends(usuario_autenticado)) -> str:
    """Dependencia para endpoints exclusivos de gerencia: 403 si el token no es de gerencia.
    El usuario sale del token, nunca de un parámetro que mande el cliente."""
    if not es_gerencia(usuario):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Se requiere nivel de gerencia para esta acción")
    return usuario
