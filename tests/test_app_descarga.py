# Descarga del APK (routers/app_movil.py + servicios/apk_descarga.py). Sin red: boto3 real con
# llaves falsas (firmar es local) y Stubber para head_object. No se ha probado contra el bucket
# real ni con el rol del EC2.
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from botocore.exceptions import NoCredentialsError, EndpointConnectionError
from botocore.stub import Stubber
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import app_movil
from servicios import apk_descarga, mov_reg
from servicios.permisos import usuario_autenticado


_CLIENTE_REAL = apk_descarga._cliente


@pytest.fixture(autouse=True)
def entorno(monkeypatch):
    for var in ("AWS_REGION", "S3_APK_BUCKET", "S3_APK_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    # Llaves falsas: las variables de entorno ganan en la cadena de boto3 (no se toca IMDS ni ~/.aws).
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAFALSA")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secreto-falso")
    _CLIENTE_REAL.cache_clear()
    yield
    _CLIENTE_REAL.cache_clear()


def _stub_head(monkeypatch, *, bucket="bucket-prueban8n", key="app-zeutica.apk", error=None, region="us-east-2"):
    s3 = _CLIENTE_REAL(region)  # el cliente real del servicio (s3v4 + endpoint regional)
    stub = Stubber(s3)
    esperado = {"Bucket": bucket, "Key": key}
    if error:
        stub.add_client_error("head_object", service_error_code=error, http_status_code=404 if error == "404" else 403,
                              expected_params=esperado)
    else:
        stub.add_response("head_object", {"ContentLength": 45_000_000,
                                          "LastModified": datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc)},
                          esperado)
    stub.activate()
    regiones = []
    monkeypatch.setattr(apk_descarga, "_cliente", lambda r: regiones.append(r) or s3)
    return regiones


def _app(monkeypatch, usuario="ventas1"):
    bitacora = []
    monkeypatch.setattr(mov_reg, "registrar_movimiento", lambda *a, **k: bitacora.append(a))
    app = FastAPI()
    app.include_router(app_movil.router)
    app.dependency_overrides[usuario_autenticado] = lambda: usuario
    return TestClient(app), bitacora


# ---------- Configuración ----------

def test_defaults_sin_variables():
    assert apk_descarga.configuracion() == {
        "region": "us-east-2", "bucket": "bucket-prueban8n", "key": "app-zeutica.apk", "filename": "app-zeutica.apk",
    }


def test_variables_y_nombre_derivado_del_key(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-1")
    monkeypatch.setenv("S3_APK_BUCKET", "otro")
    monkeypatch.setenv("S3_APK_KEY", "/releases/zeutica-1.2.apk")
    cfg = apk_descarga.configuracion()
    assert cfg == {"region": "us-west-1", "bucket": "otro", "key": "releases/zeutica-1.2.apk", "filename": "zeutica-1.2.apk"}


def test_variables_vacias_usan_default(monkeypatch):
    monkeypatch.setenv("S3_APK_KEY", "  ")
    monkeypatch.setenv("AWS_REGION", "")
    cfg = apk_descarga.configuracion()
    assert cfg["key"] == "app-zeutica.apk" and cfg["region"] == "us-east-2"


@pytest.mark.parametrize("key,esperado", [
    ('app"malo.apk', "appmalo.apk"), ("carpeta/", "app-zeutica.apk"), ("a\r\nb.apk", "ab.apk"), ("", "app-zeutica.apk"),
])
def test_nombre_archivo_limpio(key, esperado):
    assert apk_descarga.nombre_archivo(key) == esperado


# ---------- Servicio ----------

def test_url_prefirmada_con_parametros_de_android(monkeypatch):
    regiones = _stub_head(monkeypatch)
    datos = apk_descarga.generar_url_descarga()
    assert regiones == ["us-east-2"]
    url = urlparse(datos["download_url"])
    q = parse_qs(url.query)
    assert url.scheme == "https"
    assert url.hostname == "bucket-prueban8n.s3.us-east-2.amazonaws.com" and url.path == "/app-zeutica.apk"
    assert q["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert q["X-Amz-Expires"] == ["300"]
    assert q["response-content-type"] == ["application/vnd.android.package-archive"]
    assert q["response-content-disposition"] == ['attachment; filename="app-zeutica.apk"']
    assert datos["filename"] == "app-zeutica.apk"
    assert datos["expira_en"] == 300
    assert datos["tamano_bytes"] == 45_000_000
    assert datos["ultima_modificacion"].startswith("2026-11-20T18:00:00")


def test_apk_inexistente(monkeypatch):
    _stub_head(monkeypatch, error="404")
    with pytest.raises(apk_descarga.ApkNoEncontrado):
        apk_descarga.generar_url_descarga()


def test_acceso_denegado_es_error_generico(monkeypatch):
    _stub_head(monkeypatch, error="403")
    with pytest.raises(apk_descarga.ApkError):
        apk_descarga.generar_url_descarga()


class _ClienteQueFalla:
    def __init__(self, err):
        self.err = err

    def head_object(self, **_):
        raise self.err


def test_sin_credenciales(monkeypatch):
    monkeypatch.setattr(apk_descarga, "_cliente", lambda r: _ClienteQueFalla(NoCredentialsError()))
    with pytest.raises(apk_descarga.ApkNoConfigurado):
        apk_descarga.generar_url_descarga()


def test_sin_red_es_error_generico(monkeypatch):
    monkeypatch.setattr(apk_descarga, "_cliente",
                        lambda r: _ClienteQueFalla(EndpointConnectionError(endpoint_url="https://s3")))
    with pytest.raises(apk_descarga.ApkError):
        apk_descarga.generar_url_descarga()


def test_cliente_real_fuerza_s3v4():
    s3 = apk_descarga._cliente("us-east-2")
    assert s3.meta.config.signature_version == "s3v4"
    assert s3.meta.region_name == "us-east-2"


# ---------- Endpoint ----------

def test_endpoint_ok_y_bitacora(monkeypatch):
    _stub_head(monkeypatch)
    cliente, bitacora = _app(monkeypatch, usuario="ventas1")
    r = cliente.get("/app/download-url")
    assert r.status_code == 200
    cuerpo = r.json()
    assert cuerpo["filename"] == "app-zeutica.apk" and cuerpo["download_url"].startswith("https://")
    assert bitacora == [("ventas1", "Generó enlace de descarga del APK (app-zeutica.apk)", "App móvil")]


def test_endpoint_no_falla_si_la_bitacora_falla(monkeypatch):
    _stub_head(monkeypatch)
    cliente, _ = _app(monkeypatch)

    def revienta(*a, **k):
        raise RuntimeError("BD caída")
    monkeypatch.setattr(mov_reg, "registrar_movimiento", revienta)
    assert cliente.get("/app/download-url").status_code == 200


@pytest.mark.parametrize("excepcion,status", [
    (apk_descarga.ApkNoConfigurado, 503), (apk_descarga.ApkNoEncontrado, 404), (apk_descarga.ApkError, 500),
])
def test_endpoint_errores_limpios(monkeypatch, excepcion, status):
    def falla():
        raise excepcion()
    monkeypatch.setattr(apk_descarga, "generar_url_descarga", falla)
    cliente, bitacora = _app(monkeypatch)
    r = cliente.get("/app/download-url")
    assert r.status_code == status
    assert "detail" in r.json() and "s3" not in r.json()["detail"].lower()
    assert bitacora == []


def test_endpoint_exige_sesion():
    app = FastAPI()
    app.include_router(app_movil.router)
    r = TestClient(app).get("/app/download-url")
    assert r.status_code in (401, 403)
