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
from fastapi import FastAPI, HTTPException, Depends, status, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from permisos import usuario_autenticado, asegurar_columna_token_expira, token_horas, es_gerencia
from routers import cotizacionesBack, productos, ventas, clientes, traspaso, gastos, compras, cleanest, cuentas_pendientes,\
      abonos, estadisticas, inventario, empleados, notificaciones, cuentas_pagar, consulta_registros, pendientes, proveedores, genera_cotizacion, sofi_conversaciones, embarques, sofi_notificaciones, whatsapp_plantillas, skydropx
from routers import crm, comisiones, prospectos, jobs, meli_webhook, meli_promos, anaqueles
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

    # Vigencia de los tokens de sesión (usuarios.token_expira). Sin la columna nadie podría
    # validar su token, así que si falla se avisa fuerte en los logs.
    try:
        asegurar_columna_token_expira()
    except Exception as e:
        print(f"❌ No se pudo asegurar usuarios.token_expira (las sesiones no validarán): {e}")

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

    # Dedupe + llave única de clientes_potenciales (el INSERT IGNORE no servía sin ella).
    try:
        from jobs import schema_potenciales
        rep = schema_potenciales.asegurar_tabla_potenciales()
        if rep["tabla_creada"] or rep["duplicados_eliminados"] or rep["indice_creado"]:
            print(f"📦 Migración clientes_potenciales: {rep}")
    except Exception as e:
        print(f"❌ No se pudo migrar clientes_potenciales: {e}")

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
            from jobs import amazon_ventas, _ejecucion

            hora = os.getenv("AMAZON_JOB_HORA", "12:22")
            hh, mm = (hora.split(":") + ["0"])[:2]
            scheduler = AsyncIOScheduler(timezone=ZoneInfo("America/Mexico_City"))
            # Zona explícita también en cada trigger: sin esto APScheduler 3.x
            # evalúa en la zona local del contenedor (UTC en Easypanel = -6h).
            TZ_CDMX = ZoneInfo("America/Mexico_City")
            scheduler.add_job(_ejecucion.ejecutar, CronTrigger(hour=int(hh), minute=int(mm), timezone=TZ_CDMX),
                              kwargs={"modulo": amazon_ventas, "motivo": "scheduler"}, id="amazon_ventas", replace_existing=True,
                              misfire_grace_time=600, coalesce=True, max_instances=1)
            scheduler.start()
            app.state.amazon_scheduler = scheduler
            print(f"⏰ Job Amazon programado a las {int(hh):02d}:{int(mm):02d} America/Mexico_City.")
            try:
                from jobs import meli_ventas
                if os.getenv("MELI_JOB_ENABLED", "1") == "1":
                    mh = os.getenv("MELI_JOB_HORA", "12:05")
                    mhh, mmm = (mh.split(":") + ["0"])[:2]
                    scheduler.add_job(_ejecucion.ejecutar, CronTrigger(hour=int(mhh), minute=int(mmm), timezone=TZ_CDMX),
                                      kwargs={"modulo": meli_ventas, "motivo": "scheduler"}, id="meli_ventas", replace_existing=True,
                                      misfire_grace_time=600, coalesce=True, max_instances=1)
                    print(f"⏰ Job MeLi programado a las {int(mhh):02d}:{int(mmm):02d} America/Mexico_City.")
            except Exception as e:
                print(f"❌ No se pudo programar el job MeLi: {e}")
            try:
                from jobs import meli_stock
                meli_stock.asegurar_tabla_publicaciones()
                if os.getenv("MELI_STOCK_ENABLED", "1") == "1":
                    sh = os.getenv("MELI_STOCK_HORA", "20:00")
                    shh, smm = (sh.split(":") + ["0"])[:2]
                    scheduler.add_job(_ejecucion.ejecutar, CronTrigger(hour=int(shh), minute=int(smm), timezone=TZ_CDMX),
                                      kwargs={"modulo": meli_stock, "motivo": "scheduler"}, id="meli_stock", replace_existing=True,
                                      misfire_grace_time=600, coalesce=True, max_instances=1)
                    print(f"⏰ Job stock MeLi programado a las {int(shh):02d}:{int(smm):02d} America/Mexico_City.")
            except Exception as e:
                print(f"❌ No se pudo programar el job stock MeLi: {e}")
            try:
                from jobs import cleanest_recordatorios
                cleanest_recordatorios.asegurar_tabla_avisos()
                if os.getenv("CLEANEST_JOB_ENABLED", "1") == "1":
                    ch = os.getenv("CLEANEST_JOB_HORA", "11:00")
                    chh, cmm = (ch.split(":") + ["0"])[:2]
                    scheduler.add_job(_ejecucion.ejecutar, CronTrigger(hour=int(chh), minute=int(cmm), timezone=TZ_CDMX),
                                      kwargs={"modulo": cleanest_recordatorios, "motivo": "scheduler"}, id="cleanest", replace_existing=True,
                                      misfire_grace_time=600, coalesce=True, max_instances=1)
                    print(f"⏰ Job Cleanest programado a las {int(chh):02d}:{int(cmm):02d} America/Mexico_City.")
            except Exception as e:
                print(f"❌ No se pudo programar el job Cleanest: {e}")
            try:
                from jobs import meli_full_stock
                meli_full_stock.asegurar_tabla_estado()
                if os.getenv("MELI_FULL_ENABLED", "1") == "1":
                    fh = os.getenv("MELI_FULL_HORA", "10:30")
                    fhh, fmm = (fh.split(":") + ["0"])[:2]
                    scheduler.add_job(_ejecucion.ejecutar, CronTrigger(hour=int(fhh), minute=int(fmm), timezone=TZ_CDMX),
                                      kwargs={"modulo": meli_full_stock, "motivo": "scheduler"}, id="meli_full", replace_existing=True,
                                      misfire_grace_time=600, coalesce=True, max_instances=1)
                    print(f"⏰ Job Full programado a las {int(fhh):02d}:{int(fmm):02d} America/Mexico_City.")
            except Exception as e:
                print(f"❌ No se pudo programar el job Full: {e}")
            try:
                from jobs import cotizaciones_vencimiento
                if os.getenv("COTIZ_JOB_ENABLED", "1") == "1":
                    th = os.getenv("COTIZ_JOB_HORA", "09:30")
                    thh, tmm = (th.split(":") + ["0"])[:2]
                    scheduler.add_job(_ejecucion.ejecutar, CronTrigger(hour=int(thh), minute=int(tmm), timezone=TZ_CDMX),
                                      kwargs={"modulo": cotizaciones_vencimiento, "motivo": "scheduler"}, id="cotizaciones", replace_existing=True,
                                      misfire_grace_time=600, coalesce=True, max_instances=1)
                    print(f"⏰ Job cotizaciones programado a las {int(thh):02d}:{int(tmm):02d} America/Mexico_City.")
            except Exception as e:
                print(f"❌ No se pudo programar el job cotizaciones: {e}")
            try:
                from jobs import cotizaciones_por_vender
                if os.getenv("COTIZV_JOB_ENABLED", "1") == "1":
                    vh = os.getenv("COTIZV_JOB_HORA", "13:00")
                    vhh, vmm = (vh.split(":") + ["0"])[:2]
                    scheduler.add_job(_ejecucion.ejecutar, CronTrigger(hour=int(vhh), minute=int(vmm), timezone=TZ_CDMX),
                                      kwargs={"modulo": cotizaciones_por_vender, "motivo": "scheduler"}, id="cotizaciones_vender", replace_existing=True,
                                      misfire_grace_time=600, coalesce=True, max_instances=1)
                    print(f"⏰ Job por-vender programado a las {int(vhh):02d}:{int(vmm):02d} America/Mexico_City.")
            except Exception as e:
                print(f"❌ No se pudo programar el job por-vender: {e}")
            # Verificación visible en logs: próxima corrida de cada job en hora CDMX.
            try:
                for jb in scheduler.get_jobs():
                    nxt = jb.next_run_time
                    print(f"⏰ {jb.id}: próxima corrida "
                          f"{nxt.astimezone(TZ_CDMX).isoformat() if nxt else 'sin programar'}")
            except Exception as e:
                print(f"⚠️ No se pudo listar próximas corridas: {e}")
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

# ---  DEPENDENCIA PARA VALIDAR EL TOKEN EN LAS RUTAS ---
# Es la misma de permisos.py (token vigente: usuarios.token_expira > NOW()), para que los
# routers y main.py nunca validen distinto.
obtener_usuario_actual = usuario_autenticado

# ---  LÍMITE DE INTENTOS (slowapi) ---
def _ip_cliente(request: Request) -> str:
    """IP del cliente detrás del proxy de EasyPanel. El proxy AGREGA la IP real al final de
    X-Forwarded-For, así que se toma el último valor (el primero lo puede inventar el cliente).
    Si la API se expone sin proxy, ese header no se toma de nadie y se usa la IP del socket."""
    reenviada = request.headers.get("x-forwarded-for", "")
    if reenviada:
        return reenviada.split(",")[-1].strip() or "desconocida"
    return request.client.host if request.client else "desconocida"

limiter = Limiter(key_func=_ip_cliente)
LOGIN_LIMITE = os.getenv("LOGIN_RATE_LIMIT", "10/minute;60/hour")
PASSW_LIMITE = os.getenv("PASSW_RATE_LIMIT", "5/minute")

# bcrypt de relleno: se verifica cuando el usuario no existe, para que el tiempo no lo delate.
_HASH_FALSO = bcrypt.hashpw(b"zeutica-hash-falso", bcrypt.gensalt()).decode("utf-8")

# Corregido: FastAPI usa root_path, no prefix.
app = FastAPI(root_path="/zeutica", tags=["login"], responses={404: {"Mensaje":"No encontrado"}}, lifespan=lifespan)
app.state.limiter = limiter


@app.exception_handler(RateLimitExceeded)
async def _limite_excedido(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Demasiados intentos. Espera un minuto antes de volver a intentar."},
        headers={"Retry-After": "60"},
    )

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
app.include_router(meli_promos.router, dependencies=[Depends(obtener_usuario_actual)])
app.include_router(anaqueles.router, dependencies=[Depends(obtener_usuario_actual)])
# Sin obtener_usuario_actual a proposito: el WebSocket valida el token por query
# param y el POST de escalacion valida X-API-Key (n8n no tiene sesion de usuario).
app.include_router(sofi_notificaciones.router)
# Mismo caso: el webhook lo llama Skydropx, no el panel. Valida firma HMAC-SHA512.
app.include_router(skydropx.router_webhook)
# Webhook de MeLi/MercadoPago (pagadas y cancelaciones). Lo llaman ellos, sin
# sesión: valida por GET del recurso + x-signature opcional. Siempre 200.
app.include_router(meli_webhook.router)

# CORS: solo el panel. CORS_ORIGINS (coma-separado) sobreescribe la lista; localhost/127.0.0.1
# (cualquier puerto) quedan permitidos para desarrollo local. La autenticación va en el header
# Authorization (no en cookies), así que no se habilitan credenciales.
_CORS_POR_DEFECTO = "https://postgresqldb-panel-administrativo.i4mjht.easypanel.host"
ORIGENES_CORS = [o.strip().rstrip("/") for o in (os.getenv("CORS_ORIGINS") or _CORS_POR_DEFECTO).split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGENES_CORS,
    allow_origin_regex=r"^http://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key"],
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
    password_actual: str  # la vigente de quien hace la petición (dueño del token)
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
    try:
        return bcrypt.checkpw(pwd_bytes, hashed_bytes)
    except ValueError:  # hash guardado con formato inválido: se trata como contraseña incorrecta
        return False

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
@limiter.limit(LOGIN_LIMITE)
async def login(request: Request, datos: LoginSchema):
    """
    Consulta credenciales para ingreso a sistema.
    El token que devuelve vence a las TOKEN_HORAS horas (usuarios.token_expira).
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

        # Siempre se corre un bcrypt (aunque el usuario no exista) para que el tiempo de respuesta
        # no delate qué usuarios existen, y todo rechazo da el mismo mensaje. El estatus del
        # empleado solo se revela a quien ya dio la contraseña correcta.
        hash_guardado = resultado["password_hash"] if resultado and resultado["password_hash"] else None
        password_ok = verify_password(password_ingresado, hash_guardado or _HASH_FALSO) and hash_guardado is not None
        if not password_ok:
            raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")

        if resultado['estatus'] == 0:
            raise HTTPException(status_code=403, detail="Usuario inactivo. Contacta al administrador.")

        if resultado['estatus'] == 1:
            # Solo ingresos exitosos: antes se registraba también el intento con contraseña errónea.
            cursor.execute(
                "INSERT INTO registro_login (nombre_usuario, nombre) VALUES (%s, %s)",
                (datos.usuario, 'Datos de empleado no disponible')
            )

        nuevo_token = secrets.token_urlsafe(32)
        update_query = "UPDATE usuarios SET token = %s, token_expira = DATE_ADD(NOW(), INTERVAL %s HOUR) WHERE nombre_usuario = %s"
        cursor.execute(update_query, (nuevo_token, token_horas(), usuario))
        conn.commit()

        return {
            "auth": True,
            "mensaje": "Acceso exitoso",
            "access_token": nuevo_token,
            "token_type": "bearer",
            "expira_en_horas": token_horas(),
            "id_usuario": resultado['id']
        }

    except mysql.connector.Error as err:
        print(f"Error DB login: {err}")
        raise HTTPException(status_code=500, detail="Error interno en DB")

    finally:
        if cursor:
            cursor.close()
        if conn and conn.is_connected():
            conn.close()


@app.put("/cambio-passw")
@limiter.limit(PASSW_LIMITE)
async def cambio_passw(request: Request, datos: CambioPasswSchema, actual: str = Depends(obtener_usuario_actual)):
    """
    Cambia la contraseña. Exige sesión y la contraseña vigente; solo se puede cambiar la propia
    (gerencia puede cambiar la de otro usuario, pero también debe dar su propia contraseña actual).
    Encripto antes de guardar.
    """
    usuario = datos.usuario
    password_nueva = datos.password_nueva
    if usuario.strip().lower() != actual.strip().lower() and not es_gerencia(actual):
        raise HTTPException(status_code=403, detail="Solo puedes cambiar tu propia contraseña")
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # La contraseña actual que se verifica es la de quien hace la petición (el dueño del token).
        cursor.execute("SELECT password_hash FROM usuarios WHERE nombre_usuario = %s", (actual,))
        quien = cursor.fetchone()
        if not quien or not quien["password_hash"] or not verify_password(datos.password_actual, quien["password_hash"]):
            # 400 y no 401: el panel cierra la sesión ante un 401, y esto es un dato mal capturado.
            raise HTTPException(status_code=400, detail="La contraseña actual es incorrecta")

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