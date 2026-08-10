# fichero para backend
import bcrypt, asyncpg
from contextlib import asynccontextmanager  # <-- Añadido para el lifespan
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from routers import cotizacionesBack, productos, ventas, clientes, traspaso, gastos, compras, cleanest, cuentas_pendientes,\
      abonos, estadisticas, inventario, empleados, notificaciones, cuentas_pagar, consulta_registros, pendientes, proveedores, genera_cotizacion, sofi_conversaciones, embarques
import mysql.connector
from fastapi.middleware.cors import CORSMiddleware
import os, secrets
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

# ---  CREDENCIALES POSTGRESQL (NUEVO) ---
PG_USER = os.getenv("PG_USER")
PG_PASSWORD = os.getenv("PG_PASSWORD")
PG_HOST = os.getenv("PG_HOST")
PG_PORT = os.getenv("PG_PORT")
PG_NAME = os.getenv("PG_NAME")

# --- CICLO DE VIDA PARA INICIAR POSTGRESQL ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Intentar conectar al pool de PostgreSQL al arrancar
    try:
        if PG_USER and PG_PASSWORD: # Pequeña validación
            DATABASE_URL = f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{PG_NAME}"
            app.state.db_pool = await asyncpg.create_pool(
                dsn=DATABASE_URL,
                min_size=1,   # un solo modulo, no necesito muchos
                max_size=5,
                timeout=10.0
            )
            print("✅ Pool de conexiones a PostgreSQL (Sofia) creado.")
        else:
            print("⚠️ Faltan credenciales de PostgreSQL en el .env")
            app.state.db_pool = None
    except Exception as e:
        print(f"❌ Error al conectar con PostgreSQL: {e}")
        app.state.db_pool = None

    # Crea tablas del modulo de embarques si no existen (MySQL, sin FK)
    embarques.crear_tablas_embarques()

    yield  # Aquí corre la aplicación normal

    # Apagar el pool al cerrar la API
    if getattr(app.state, "db_pool", None):
        await app.state.db_pool.close()
        print("🔒 Pool de PostgreSQL cerrado.")

# ---  CONFIGURACIÓN DE SEGURIDAD Y ESTADO ---
security = HTTPBearer()

# ---  DEPENDENCIA PARA VALIDAR EL TOKEN EN LAS RUTAS ---
def obtener_usuario_actual(credentials: HTTPAuthorizationCredentials = Depends(security)):
    token = credentials.credentials
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        query = "SELECT nombre_usuario FROM usuarios WHERE token = %s"
        cursor.execute(query, (token,))
        resultado = cursor.fetchone()
        if not resultado:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token inválido o expirado",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return resultado['nombre_usuario']
    except mysql.connector.Error as err:
        print(f"Error DB token: {err}")
        raise HTTPException(status_code=500, detail="Error interno en DB")
    finally:
        cursor.close()
        conn.close()

# Corregido: FastAPI usa root_path, no prefix. 
app = FastAPI(root_path="/zeutica", tags=["login"], responses={404: {"Mensaje":"No encontrado"}}, lifespan=lifespan)

# Paginas
app.include_router(productos.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(ventas.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(clientes.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(traspaso.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(cotizacionesBack.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(gastos.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(compras.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(cleanest.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(cuentas_pendientes.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(abonos.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(estadisticas.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(inventario.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(empleados.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(notificaciones.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(cuentas_pagar.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(consulta_registros.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(pendientes.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(proveedores.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(genera_cotizacion.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(sofi_conversaciones.router, dependencies=[Depends(obtener_usuario_actual)])  # <-- Añadido para la ruta de conversaciones
app.include_router(embarques.router, dependencies=[Depends(obtener_usuario_actual)])

app.add_middleware( 
    CORSMiddleware,
    allow_origins=["*"],    
    allow_methods=["*"],
    allow_headers=["*"],
)


# Configuración de la conexión
def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )

class LoginSchema(BaseModel): #molde para usuario
    usuario: str
    password: str

class CambioPasswSchema(BaseModel): #molde para cambio de contraseña
    usuario: str
    password_nueva: str

# // AUTENTICACION DE USUARIOS PARA INGRESO AL SOFTWARE // 
def hash_password(password: str) -> str:
    pwd_bytes = password.encode('utf-8')
    salt = bcrypt.gensalt()
    hashed = bcrypt.hashpw(pwd_bytes, salt)
    return hashed.decode('utf-8')

def verify_password(plano_password: str, hashed_password: str) -> bool:
    pwd_bytes = plano_password.encode('utf-8')
    hashed_bytes = hashed_password.encode('utf-8')
    return bcrypt.checkpw(pwd_bytes, hashed_bytes)   

@app.get("/")
async def test_server():
    return {"Servidor Conectado..."}

@app.post("/login")
async def login(datos: LoginSchema):
    """
    Consulta credenciales para ingreso a sistema.
    """
    usuario = datos.usuario
    password_ingresado = datos.password
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        query = """
            SELECT u.password_hash, u.id, e.estatus
            FROM usuarios u
            LEFT JOIN empleados e ON e.usuario = u.nombre_usuario
            WHERE u.nombre_usuario = %s
        """
        cursor.execute(query, (usuario,))
        resultado = cursor.fetchone()

        # Primero verifico que exista, luego reviso estatus
        if not resultado:
            raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")

        if resultado['estatus'] == 0:
            raise HTTPException(status_code=403, detail="Usuario inactivo. Contacta al administrador.")  
        
        if resultado['estatus'] == 1:
            cursor.execute(
                "INSERT INTO registro_login (nombre_usuario, nombre) VALUES (%s, %s)",
                (datos.usuario, 'Datos de empleado no disponible')
            )
            conn.commit()

        if verify_password(password_ingresado, resultado['password_hash']):
            nuevo_token = secrets.token_urlsafe(32)
            update_query = "UPDATE usuarios SET token = %s WHERE nombre_usuario = %s"
            cursor.execute(update_query, (nuevo_token, usuario))
            conn.commit()
                        
            return {
                "auth": True,
                "mensaje": "Acceso exitoso",
                "access_token": nuevo_token,
                "token_type": "bearer",
                "id_usuario": resultado['id']
            }       
           

        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")

    except mysql.connector.Error as err:
        print(f"Error DB login: {err}")
        raise HTTPException(status_code=500, detail="Error interno en DB")

    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()


@app.put("/cambio-passw")
async def cambio_passw(datos: CambioPasswSchema):
    """
    Cambia la contraseña del usuario. Encripto antes de guardar.
    """
    usuario = datos.usuario
    password_nueva = datos.password_nueva
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Primero checo que el usuario exista, si no ni le muevo
        cursor.execute("SELECT nombre_usuario FROM usuarios WHERE nombre_usuario = %s", (usuario,))
        if not cursor.fetchone():
            raise HTTPException(status_code=404, detail="Usuario no encontrado")

        # Encripto la nueva contraseña antes de meterla a la DB
        hash_nuevo = hash_password(password_nueva)
        update_query = "UPDATE usuarios SET password_hash = %s WHERE nombre_usuario = %s"
        cursor.execute(update_query, (hash_nuevo, usuario))
        conn.commit()

        return {"mensaje": "Contraseña actualizada"}

    except mysql.connector.Error as err:
        print(f"Error DB cambio passw: {err}")
        raise HTTPException(status_code=500, detail="Error interno en DB")

    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()


# Documentacion ip.server/docs (swagger)
# Docuementacion ip.server/redoc (redocly)