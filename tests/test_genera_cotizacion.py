# Tests de POST /genera-cotizacion: folio asignado por el backend, PDF guardado en BD
# (base64) y devuelto como binario, y la fila de descuento en el PDF.
# MySQL, Telegram y bitácora van simulados.
import base64
from unittest.mock import AsyncMock

import mysql.connector
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from servicios import pdf_cotizacion
from routers import genera_cotizacion


class FakeDB:
    def __init__(self):
        self.guardadas = []
        self.commits = 0
        self.rollbacks = 0
        self.error = None

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


@pytest.fixture
def db(monkeypatch):
    base = FakeDB()

    def guardar(conn, cot):
        if base.error:
            raise base.error
        base.guardadas.append(cot.model_copy(deep=True))
        base.commits += 1
        return 77

    monkeypatch.setattr(genera_cotizacion, "get_db_connection", lambda: base)
    monkeypatch.setattr(genera_cotizacion, "generar_nuevo_codigo", lambda conn: "ZTC-301")
    monkeypatch.setattr(genera_cotizacion, "guardar_cotizacion_db", guardar)
    monkeypatch.setattr(genera_cotizacion.mov_reg, "registrar_movimiento", lambda *a, **k: None)
    monkeypatch.setattr(genera_cotizacion, "send_telegram_alert", AsyncMock())
    return base


class _CursorAsesor:
    def __init__(self, fila=None, falla=False):
        self.fila, self.falla = fila, falla

    def execute(self, sql, params=None):
        if self.falla:
            raise mysql.connector.Error("boom")

    def fetchone(self):
        return self.fila

    def close(self):
        pass


class _ConnAsesor:
    def __init__(self, **kw):
        self.kw = kw

    def cursor(self):
        return _CursorAsesor(**self.kw)


def test_armar_nombre_asesor_une_nombres_y_apellido_paterno():
    assert genera_cotizacion.armar_nombre_asesor("Juan  Carlos", " Pérez ") == "Juan Carlos Pérez"
    assert genera_cotizacion.armar_nombre_asesor("Ana", None) == "Ana"
    assert genera_cotizacion.armar_nombre_asesor("", "Pérez") is None
    assert genera_cotizacion.armar_nombre_asesor(None, None) is None


def test_obtener_nombre_asesor_lee_empleado_y_tolera_fallos():
    f = genera_cotizacion.obtener_nombre_asesor
    assert f(_ConnAsesor(fila=("Ana", "López", "Ruiz")[:2]), "ana") == "Ana López"
    assert f(_ConnAsesor(fila=None), "sin_empleado") is None
    assert f(_ConnAsesor(falla=True), "ana") is None


def test_pdf_usa_nombre_formal_del_asesor_y_conserva_usuario(client, db, monkeypatch):
    monkeypatch.setattr(genera_cotizacion, "obtener_nombre_asesor", lambda conn, u: "Ana López")
    textos = []
    original = pdf_cotizacion._CotizacionPDF.cell

    def cell(self, w, h=0, txt="", *a, **k):
        textos.append(str(txt))
        return original(self, w, h, txt, *a, **k)

    monkeypatch.setattr(pdf_cotizacion._CotizacionPDF, "cell", cell)
    assert client.post("/genera-cotizacion", json=payload()).status_code == 200
    assert "Asesor: Ana López" in textos
    assert db.guardadas[0].usuario == "tester"  # lo guardado/bitácora sigue siendo el usuario


def test_pdf_sin_empleado_cae_al_usuario(client, db, monkeypatch):
    monkeypatch.setattr(genera_cotizacion, "obtener_nombre_asesor", lambda conn, u: None)
    textos = []
    original = pdf_cotizacion._CotizacionPDF.cell

    def cell(self, w, h=0, txt="", *a, **k):
        textos.append(str(txt))
        return original(self, w, h, txt, *a, **k)

    monkeypatch.setattr(pdf_cotizacion._CotizacionPDF, "cell", cell)
    client.post("/genera-cotizacion", json=payload())
    assert "Asesor: tester" in textos


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(genera_cotizacion.router)
    return TestClient(app)


def payload(**extra):
    datos = {
        "empresa": "Ferretería Uno", "atencion": "Ana", "email": "a@b.mx",
        "domicilio": "Calle 1", "telefono": "3312345678",
        "subtotal": 225.0, "iva": 76.0, "total": 551.0, "costo_envio": 250.0,
        "forma_pago": "03 - Transferencia", "metodo_pago": "PUE - Pago en Una sola Exhibición",
        "comentarios": "Entrega en bodega", "usuario": "tester",
        "items": [
            {"sku": "A1", "nombre_producto": "Martillo", "cantidad": 2, "precio_unitario": 90, "total_linea": 180},
            {"sku": "B2", "nombre_producto": "Pinzas", "cantidad": 1, "precio_unitario": 45, "total_linea": 45},
        ],
    }
    datos.update(extra)
    return datos


def test_devuelve_pdf_con_folio_del_backend_en_headers(client, db):
    r = client.post("/genera-cotizacion", json=payload(codigo_cotizacion="ZTC-999"))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    # Se ignora el folio que mande el cliente: manda el consecutivo del backend.
    assert r.headers["x-codigo-cotizacion"] == "ZTC-301"
    assert r.headers["x-cotizacion-id"] == "77"
    assert "cotizacion_ZTC-301.pdf" in r.headers["content-disposition"]


def test_guarda_en_bd_el_mismo_pdf_que_devuelve(client, db):
    r = client.post("/genera-cotizacion", json=payload())
    (guardada,) = db.guardadas
    assert guardada.codigo_cotizacion == "ZTC-301"
    # /cotizaciones/base64 sirve este valor para "Ver": debe ser idéntico a la descarga.
    assert base64.b64decode(guardada.pdf) == r.content


def test_campos_de_descuento_son_opcionales(client, db):
    assert client.post("/genera-cotizacion", json=payload()).status_code == 200
    assert db.guardadas[0].descuento_porcentaje == 0


@pytest.mark.parametrize("campo", ["empresa", "telefono", "subtotal", "total", "forma_pago", "usuario", "items"])
def test_campos_obligatorios_faltantes_dan_422(client, db, campo):
    datos = payload()
    datos.pop(campo)
    assert client.post("/genera-cotizacion", json=datos).status_code == 422
    assert db.guardadas == []


def test_telefono_numerico_da_422(client, db):
    # El panel debe mandarlo como texto (Pydantic 2 no convierte int -> str).
    assert client.post("/genera-cotizacion", json=payload(telefono=3312345678)).status_code == 422


def test_error_de_mysql_da_500_con_rollback(client, db):
    db.error = mysql.connector.Error("boom")
    r = client.post("/genera-cotizacion", json=payload())
    assert r.status_code == 500
    assert db.rollbacks == 1


# ---------- Generador de PDF ----------

class _Cot:
    def __init__(self, **kw):
        datos = payload()
        datos.update(codigo_cotizacion="ZTC-301", descuento_porcentaje=0, descuento_monto=0)
        datos.update(kw)
        datos["items"] = [genera_cotizacion.ItemCotizacion(**i) for i in datos["items"]]
        self.__dict__.update(datos)


def _filas_totales(monkeypatch, cot):
    """Captura las celdas que el generador escribe para revisar el bloque de totales."""
    textos = []
    original = pdf_cotizacion._CotizacionPDF.cell

    def cell(self, w, h=0, txt="", *a, **k):
        textos.append(str(txt))
        return original(self, w, h, txt, *a, **k)

    monkeypatch.setattr(pdf_cotizacion._CotizacionPDF, "cell", cell)
    pdf_cotizacion.generar_pdf_cotizacion(cot)
    return textos


def test_pdf_sin_descuento_no_imprime_fila_de_descuento(monkeypatch):
    textos = _filas_totales(monkeypatch, _Cot())
    assert not any(t.startswith("Descuento") for t in textos)
    i = textos.index("Sub-Total:")
    assert textos[i + 1] == "$225.00"


def test_pdf_con_descuento_imprime_subtotal_original_y_descuento(monkeypatch):
    textos = _filas_totales(monkeypatch, _Cot(descuento_porcentaje=10, descuento_monto=25))
    i = textos.index("Sub-Total:")
    assert textos[i + 1] == "$250.00"  # 225 con descuento + 25 de descuento
    assert textos[i + 2] == "Descuento (10%):"
    assert textos[i + 3] == "-$25.00"


def test_pdf_soporta_acentos_y_descripciones_largas():
    largo = "TORNILLO AUTORROSCANTE CABEZA PLANA ACERO INOXIDABLE " * 4
    cot = _Cot(atencion="José Núñez", items=[
        {"sku": "Ñ1", "nombre_producto": largo, "cantidad": 1, "precio_unitario": 1, "total_linea": 1},
    ] * 40)
    contenido = pdf_cotizacion.generar_pdf_cotizacion(cot)
    assert contenido.startswith(b"%PDF")


# ---- T2: cliente_id opcional y validado antes de folio y PDF
from servicios import cotizacion_service as _cs
from fastapi import HTTPException as _HTTPException


@pytest.fixture
def validacion(monkeypatch):
    llamadas = {"codigo": 0, "pdf": 0}
    monkeypatch.setattr(genera_cotizacion, "generar_nuevo_codigo",
                        lambda conn: llamadas.__setitem__("codigo", llamadas["codigo"] + 1) or "ZTC-301")
    original = genera_cotizacion.generar_pdf_cotizacion

    def pdf(c):
        llamadas["pdf"] += 1
        return original(c)

    monkeypatch.setattr(genera_cotizacion, "generar_pdf_cotizacion", pdf)

    def validar(conn, cid):
        if cid is not None and cid != 5:
            raise _HTTPException(status_code=422, detail="El cliente indicado no existe o fue eliminado")

    monkeypatch.setattr(genera_cotizacion, "validar_cliente_id", validar)
    return llamadas


def test_cliente_id_valido_se_guarda_y_conserva_con_otra_empresa(client, db, validacion):
    r = client.post("/genera-cotizacion", json=payload(cliente_id=5, empresa="Nombre distinto"))
    assert r.status_code == 200
    assert db.guardadas[0].cliente_id == 5 and db.guardadas[0].empresa == "Nombre distinto"


def test_sin_cliente_id_guarda_none(client, db, validacion):
    assert client.post("/genera-cotizacion", json=payload()).status_code == 200
    assert db.guardadas[0].cliente_id is None


@pytest.mark.parametrize("valor", [0, -1, "abc", 1.5, "5", True])
def test_cliente_id_invalido_422(client, db, validacion, valor):
    assert client.post("/genera-cotizacion", json=payload(cliente_id=valor)).status_code == 422
    assert validacion == {"codigo": 0, "pdf": 0} and db.guardadas == []


def test_cliente_inexistente_422_antes_de_folio_y_pdf(client, db, validacion):
    r = client.post("/genera-cotizacion", json=payload(cliente_id=99))
    assert r.status_code == 422  # no se vuelve 500
    assert validacion == {"codigo": 0, "pdf": 0} and db.guardadas == []
