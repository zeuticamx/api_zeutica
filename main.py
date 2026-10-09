# fichero para backend
import bcrypt, asyncpg
import sys
from contextlib import asynccontextmanager  # <-- Añadido para el lifespan

# Consola Windows (cp1252) truena con los emojis de los print del lifespan.
# Se fuerza UTF-8 solo para salida de texto; en Linux no cambia nada.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from routers import cotizacionesBack, productos, ventas, clientes, traspaso, gastos, compras, cleanest, cuentas_pendientes,\
      abonos, estadisticas, inventario, empleados, notificaciones, cuentas_pagar, consulta_registros, pendientes, proveedores, genera_cotizacion, sofi_conversaciones, embarques, sofi_notificaciones, whatsapp_plantillas, skydropx
from routers import crm, comisiones, prospectos, jobs, meli_webhook
import mysql.connector
import skydropx_envios
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

    # Tablas de envios Skydropx: guardan el estatus que manda el webhook.
    skydropx_envios.crear_tablas_skydropx()

    # Columnas del borrado lógico de gastos (eliminado, eliminado_por, fecha_eliminado)
    try:
        gastos.asegurar_columnas_eliminado()
    except Exception as e:
        print(f"❌ No se pudieron asegurar las columnas de gastos: {e}")

    # Columnas del borrado lógico de clientes (eliminado, eliminado_por, fecha_eliminado)
    try:
        clientes.asegurar_columnas_eliminado()
    except Exception as e:
        print(f"❌ No se pudieron asegurar las columnas de clientes: {e}")

    # Tablas del CRM (cartera, interacciones, historial de etapas)
    try:
        crm.crear_tablas_crm()
    except Exception as e:
        print(f"❌ No se pudieron crear las tablas del CRM: {e}")

    # Tablas de prospectos (prospectos + seguimientos)
    try:
        prospectos.crear_tablas_prospectos()
    except Exception as e:
        print(f"❌ No se pudieron crear las tablas de prospectos: {e}")

    # Tablas de comisiones (matriz por vendedor/SKU, comisiones calculadas, vínculo venta-seguimiento)
    try:
        comisiones.crear_tablas_comisiones()
    except Exception as e:
        print(f"❌ No se pudieron crear las tablas de comisiones: {e}")

    # Columna usuario en devoluciones (auditoría de inventario).
    try:
        from routers.productos import asegurar_columnas_devolucion
        asegurar_columnas_devolucion()
    except Exception as e:
        print(f"❌ No se pudo migrar devoluciones: {e}")

    # Índices por fecha para /inventario/movimientos (sin full-scan).
    try:
        from routers.inventario import asegurar_indices_inventario
        asegurar_indices_inventario()
    except Exception as e:
        print(f"❌ No se pudieron crear índices de inventario: {e}")

    # Columnas + backfill de ventasRegistro para los jobs de marketplaces
    # (inventario_descontado=1 en lo ya registrado: hasta hoy sí se descontaba).
    try:
        from jobs import schema_ventas
        rep = schema_ventas.asegurar_columnas_ventas()
        if rep["columnas_agregadas"] or rep["indice_creado"] or rep["backfill"]:
            print(f"📦 Migración ventasRegistro: {rep}")
    except Exception as e:
        print(f"❌ No se pudo migrar ventasRegistro: {e}")

    # Scheduler del job de Amazon (mismo proceso; Easypanel no necesita otro contenedor).
    # AMAZON_JOB_ENABLED=0 lo apaga (útil en local). Requiere `apscheduler` en requirements.
    app.state.amazon_scheduler = None
    if os.getenv("AMAZON_JOB_ENABLED", "1") == "1":
        try:
            from apscheduler.schedulers.asyncio import AsyncIOScheduler
            from apscheduler.triggers.cron import CronTrigger
            from zoneinfo import ZoneInfo
            from jobs import amazon_ventas

            hora = os.getenv("AMAZON_JOB_HORA", "12:22")
            hh, mm = (hora.split(":") + ["0"])[:2]
            scheduler = AsyncIOScheduler(timezone=ZoneInfo("America/Mexico_City"))
            scheduler.add_job(amazon_ventas.run_job, CronTrigger(hour=int(hh), minute=int(mm)),
                              kwargs={"motivo": "scheduler"}, id="amazon_ventas", replace_existing=True,
                              misfire_grace_time=600, coalesce=True, max_instances=1)
            scheduler.start()
            app.state.amazon_scheduler = scheduler
            print(f"⏰ Job Amazon programado a las {int(hh):02d}:{int(mm):02d} America/Mexico_City.")
            try:
                from jobs import meli_ventas
                if os.getenv("MELI_JOB_ENABLED", "1") == "1":
                    mh = os.getenv("MELI_JOB_HORA", "12:05")
                    mhh, mmm = (mh.split(":") + ["0"])[:2]
                    scheduler.add_job(meli_ventas.run_job, CronTrigger(hour=int(mhh), minute=int(mmm)),
                                      kwargs={"motivo": "scheduler"}, id="meli_ventas", replace_existing=True,
                                      misfire_grace_time=600, coalesce=True, max_instances=1)
                    print(f"⏰ Job MeLi programado a las {int(mhh):02d}:{int(mmm):02d} America/Mexico_City.")
            except Exception as e:
                print(f"❌ No se pudo programar el job MeLi: {e}")
        except ImportError:
            print("⚠️ apscheduler no instalado: job Amazon solo manual (pip install apscheduler).")
        except Exception as e:
            print(f"❌ No se pudo programar el job Amazon: {e}")

    yield  # Aquí corre la aplicación normal

    # Apagar scheduler + pool al cerrar la API
    try:
        if getattr(app.state, "amazon_scheduler", None):
            app.state.amazon_scheduler.shutdown(wait=False)
            print("🔒 Scheduler Amazon apagado.")
    except Exception:
        pass
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
app.include_router(whatsapp_plantillas.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(skydropx.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(crm.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(comisiones.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(prospectos.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(jobs.router, dependencies=[Depends(obtener_usuario_actual)])
# Sin obtener_usuario_actual a proposito: el WebSocket valida el token por query
# param y el POST de escalacion valida X-API-Key (n8n no tiene sesion de usuario).
app.include_router(sofi_notificaciones.router)
# Mismo caso: el webhook lo llama Skydropx, no el panel. Valida firma HMAC-SHA512.
app.include_router(skydropx.router_webhook)
# Webhook de MeLi/MercadoPago (pagadas y cancelaciones). Lo llaman ellos, sin
# sesión: valida por GET del recurso + x-signature opcional. Siempre 200.
app.include_router(meli_webhook.router)

app.add_middleware( 
    CORSMiddleware,
    allow_origins=["*"],    
    allow_methods=["*"],
    allow_headers=["*"],
    # El panel lee el folio y el nombre del PDF que devuelve /genera-cotizacion;
    # sin esto el navegador oculta esos headers en peticiones cross-origin.
    expose_headers=["X-Codigo-Cotizacion", "X-Cotizacion-Id", "Content-Disposition"],
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

@app.get("/health")
async def health():
    # Chequeo de vida para el deploy (Easypanel/Docker): sin auth porque el
    # healthcheck no manda token. Siempre 200 si el proceso responde; la DB
    # se valida por request, no aquí, para que un parpadeo de MySQL no mate
    # el contenedor.
    return {"status": "ok", "servicio": "api_zeutica1"}

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