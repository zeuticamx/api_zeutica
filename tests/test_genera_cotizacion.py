# Tests de POST /genera-cotizacion: folio asignado por el backend, PDF guardado en BD
# (base64) y devuelto como binario, y la fila de descuento en el PDF.
# MySQL, Telegram y bitácora van simulados.
import base64
from unittest.mock import AsyncMock

import mysql.connector
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import pdf_cotizacion
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
