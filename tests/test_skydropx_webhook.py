# Webhook de Skydropx: lectura del evento, guarda de «cancelada», rastro crudo y router.
# Sin MySQL ni red: una BD falsa registra las consultas y responde los SELECT de forma guionada.
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import skydropx_envios
import skydropx_service
from routers import skydropx as router

# ---------------------------------------------------------------------------
# extraer_evento_webhook
# ---------------------------------------------------------------------------

EVENTO_PAQUETE = {
    "data": {
        "id": "PKG-1", "type": "packages",
        "attributes": {"status": "created", "tracking_number": "TRK-1", "label_url": "https://l/1",
                       "tracking_url_provider": "https://t/1", "event_description": "Guía creada"},
        "relationships": {"shipment": {"data": {"id": "SHP-1"}}},
    }
}


def test_extrae_evento_de_paquete():
    d = skydropx_service.extraer_evento_webhook(EVENTO_PAQUETE)
    assert d["tipo"] == "packages"
    assert d["package_id"] == "PKG-1" and d["shipment_id"] == "SHP-1"
    assert d["tracking_number"] == "TRK-1" and d["estatus"] == "created"
    assert d["etiqueta_url"] == "https://l/1" and d["descripcion"] == "Guía creada"


def test_evento_de_shipment_toma_data_id_como_shipment_y_no_como_paquete():
    evento = {"data": {"id": "SHP-9", "type": "shipments", "attributes": {"status": "canceled"}}}
    d = skydropx_service.extraer_evento_webhook(evento)
    assert d["tipo"] == "shipments"
    assert d["shipment_id"] == "SHP-9"
    assert d["package_id"] is None
    assert d["estatus"] == "canceled"


def test_evento_de_shipment_puede_traer_package_id_en_atributos():
    evento = {"data": {"id": "SHP-9", "type": "shipment", "attributes": {"status": "canceled", "package_id": "PKG-3"}}}
    d = skydropx_service.extraer_evento_webhook(evento)
    assert d["shipment_id"] == "SHP-9" and d["package_id"] == "PKG-3"


def test_evento_plano_y_con_data_en_arreglo():
    plano = skydropx_service.extraer_evento_webhook({"status": "canceled", "tracking_number": "TRK-2", "shipment_id": "SHP-2"})
    assert plano["estatus"] == "canceled" and plano["tracking_number"] == "TRK-2" and plano["shipment_id"] == "SHP-2"
    arreglo = skydropx_service.extraer_evento_webhook({"data": [EVENTO_PAQUETE["data"]]})
    assert arreglo["package_id"] == "PKG-1" and arreglo["estatus"] == "created"


def test_evento_que_no_es_dict_da_vacio():
    assert skydropx_service.extraer_evento_webhook(["x"]) == {}


# ---------------------------------------------------------------------------
# registrar_evento_webhook con BD falsa
# ---------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = 0
        self._fila = None

    def execute(self, query, params=None):
        q = " ".join(query.split())
        self.db.ejecutados.append((q, params))
        self._fila = None
        self.rowcount = 0
        if q.startswith("SELECT usuario, codigo_cotizacion, estatus FROM skydropx_envios WHERE"):
            columna = q.split("WHERE ")[1].split(" =")[0]
            cola = self.db.selects.get(columna, [])
            self._fila = cola.pop(0) if len(cola) > 1 else (cola[0] if cola else None)
        elif q.startswith("INSERT IGNORE INTO skydropx_envio_eventos"):
            self.rowcount = 1

    def fetchone(self):
        return self._fila

    def close(self):
        pass


class FakeDB:
    def __init__(self, selects=None):
        self.selects = selects or {}   # columna -> [fila antes, fila despues]
        self.ejecutados = []
        self.commits = 0

    def cursor(self, dictionary=False):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass

    def de(self, prefijo):
        return [(q, p) for q, p in self.ejecutados if q.startswith(prefijo)]


@pytest.fixture
def conectar(monkeypatch):
    def _conectar(selects=None):
        db = FakeDB(selects)
        monkeypatch.setattr(skydropx_envios, "get_db_connection", lambda: db)
        return db
    return _conectar


def evento(**extra):
    base = {"tipo": "packages", "package_id": None, "shipment_id": "SHP-1", "tracking_number": None,
            "estatus": "canceled", "descripcion": "Cancelada por el carrier", "etiqueta_url": None, "tracking_url": None}
    base.update(extra)
    return base


def test_evento_sin_ninguna_llave_no_toca_la_bd(monkeypatch):
    def no_debe_conectar():
        raise AssertionError("no debía abrir conexión")
    monkeypatch.setattr(skydropx_envios, "get_db_connection", no_debe_conectar)
    r = skydropx_envios.registrar_evento_webhook(evento(shipment_id=None))
    assert r["aplicado"] is False and r["motivo"] == "sin_llave"


def test_cancelacion_de_shipment_actualiza_por_shipment_id(conectar):
    db = conectar({"shipment_id": [("ana", "ZTC-1", "created"), ("ana", "ZTC-1", "canceled")]})
    r = skydropx_envios.registrar_evento_webhook(evento())
    assert r["aplicado"] and r["envio_encontrado"] and r["evento_nuevo"]
    assert r["usuario"] == "ana" and r["codigo_cotizacion"] == "ZTC-1"
    assert r["estatus_protegido"] is False and r["motivo"] is None
    (q, p), = db.de("UPDATE skydropx_envios")
    assert "WHERE shipment_id = %s" in q and p[1] == "canceled" and p[-1] == "SHP-1"
    assert not db.de("INSERT INTO skydropx_envios")
    assert db.commits == 1


def test_el_set_asigna_la_descripcion_antes_que_el_estatus(conectar):
    """MySQL asigna de izquierda a derecha: si el estatus fuera primero, la guarda de la
    descripción vería «canceled» (ya nuevo) y conservaría la descripción vieja."""
    db = conectar({"shipment_id": [("ana", "ZTC-1", "created"), ("ana", "ZTC-1", "canceled")]})
    skydropx_envios.registrar_evento_webhook(evento())
    (q, _), = db.de("UPDATE skydropx_envios")
    assert q.index("estatus_descripcion =") < q.index("estatus = IF")
    assert "LOWER(estatus) IN ('canceled', 'cancelled')" in q


def test_evento_tardio_sobre_guia_cancelada_queda_protegido_y_no_crea_fila(conectar):
    db = conectar({"package_id": [("ana", "ZTC-1", "canceled")]})
    r = skydropx_envios.registrar_evento_webhook(evento(package_id="PKG-1", shipment_id=None, estatus="in_transit"))
    assert r["aplicado"] and r["envio_encontrado"] is True
    assert r["estatus_protegido"] is True
    # Aunque el UPDATE no cambie nada (filas afectadas = 0) NO debe crearse una fila huérfana.
    assert not db.de("INSERT INTO skydropx_envios")


def test_cancelacion_sobre_guia_ya_cancelada_no_es_protegido(conectar):
    conectar({"package_id": [("ana", "ZTC-1", "canceled")]})
    r = skydropx_envios.registrar_evento_webhook(evento(package_id="PKG-1", shipment_id=None, estatus="cancelled"))
    assert r["estatus_protegido"] is False


def test_prefiere_package_id_sobre_tracking_y_shipment(conectar):
    db = conectar({"package_id": [("ana", "ZTC-1", "created"), ("ana", "ZTC-1", "canceled")]})
    skydropx_envios.registrar_evento_webhook(evento(package_id="PKG-1", tracking_number="TRK-1"))
    (q, _), = db.de("UPDATE skydropx_envios")
    assert "WHERE package_id = %s" in q
    # Ya apareció por package_id: no se consulta por las llaves menos precisas.
    assert not [c for c, _ in db.ejecutados if c.startswith("SELECT") and "WHERE tracking_number" in c]
    assert not [c for c, _ in db.ejecutados if c.startswith("SELECT") and "WHERE shipment_id" in c]


def test_guia_desconocida_se_crea_con_el_estatus_recibido(conectar):
    db = conectar({"shipment_id": [None, ("", None, "canceled")]})
    r = skydropx_envios.registrar_evento_webhook(evento())
    assert r["aplicado"] and r["envio_encontrado"] is False
    (q, p), = db.de("INSERT INTO skydropx_envios")
    assert "ON DUPLICATE KEY UPDATE" in q and "canceled" in p
    assert not db.de("UPDATE skydropx_envios")


def test_evento_sin_estatus_se_guarda_en_la_linea_de_tiempo_pero_no_toca_la_guia(conectar):
    db = conectar()
    r = skydropx_envios.registrar_evento_webhook(evento(estatus=None))
    assert r["aplicado"] and r["motivo"] == "sin_estatus"
    assert not db.de("UPDATE skydropx_envios") and not db.de("INSERT INTO skydropx_envios")


def test_es_cancelado_acepta_las_dos_grafias():
    assert skydropx_envios.es_cancelado("canceled") and skydropx_envios.es_cancelado(" Cancelled ")
    assert not skydropx_envios.es_cancelado("created") and not skydropx_envios.es_cancelado(None)


def test_describir_estatus_canceled():
    assert skydropx_envios.describir_estatus("canceled") == {"estatus_texto": "Cancelado", "estatus_tono": "danger"}


# ---------------------------------------------------------------------------
# Rastro crudo
# ---------------------------------------------------------------------------

def test_guardar_webhook_recibido_inserta_y_purga(conectar):
    db = conectar()
    skydropx_envios.guardar_webhook_recibido(evento(tracking_number="  "), '{"x": 1}', "sin_llave")
    (q, p), = db.de("INSERT INTO skydropx_webhook_recibidos")
    assert p == ("packages", "canceled", None, "SHP-1", None, "sin_llave", '{"x": 1}')
    assert db.de("DELETE FROM skydropx_webhook_recibidos") and db.commits == 1


def test_guardar_webhook_recibido_nunca_lanza(monkeypatch):
    def falla():
        raise RuntimeError("BD caída")
    monkeypatch.setattr(skydropx_envios, "get_db_connection", falla)
    skydropx_envios.guardar_webhook_recibido({}, "x", "error")  # no debe propagar


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class FakeRequest:
    def __init__(self, cuerpo: bytes, firma="HMAC ok"):
        self._cuerpo = cuerpo
        self.headers = {"authorization": firma} if firma else {}

    async def body(self):
        return self._cuerpo

    async def json(self):
        return json.loads(self._cuerpo)


@pytest.fixture
def entorno(monkeypatch):
    guardados, notificados = [], []
    monkeypatch.setattr(skydropx_service, "_config", lambda: {"cabecera_firma": "authorization"})
    monkeypatch.setattr(skydropx_service, "verificar_firma_webhook", lambda cuerpo, firma: None)
    monkeypatch.setattr(skydropx_envios, "guardar_webhook_recibido", lambda d, p, r: guardados.append((d, p, r)))
    monkeypatch.setattr(router.notificaciones_service, "id_de_usuario", lambda n: 1)
    monkeypatch.setattr(router.notificaciones_service, "crear_y_notificar",
                        AsyncMock(side_effect=lambda *a, **k: notificados.append(a)))
    return guardados, notificados


def correr(coro):
    return asyncio.run(coro)


def test_webhook_guarda_el_evento_crudo_aunque_no_se_pueda_ligar(monkeypatch, entorno):
    guardados, notificados = entorno
    monkeypatch.setattr(skydropx_envios, "registrar_evento_webhook",
                        lambda datos, payload_crudo=None: {"aplicado": False, "motivo": "sin_llave"})
    cuerpo = json.dumps({"data": {"type": "shipments", "attributes": {"status": "canceled"}}}).encode()
    r = correr(router.recibir_webhook(FakeRequest(cuerpo)))
    assert r["recibido"] is True
    (datos, payload, resultado), = guardados
    assert resultado == "sin_llave" and datos["estatus"] == "canceled" and "canceled" in payload
    assert notificados == []


def test_webhook_de_cancelacion_aplicada_notifica(monkeypatch, entorno):
    guardados, notificados = entorno
    monkeypatch.setattr(skydropx_envios, "registrar_evento_webhook", lambda datos, payload_crudo=None: {
        "aplicado": True, "envio_encontrado": True, "evento_nuevo": True, "usuario": "ana",
        "codigo_cotizacion": "ZTC-1", "estatus_protegido": False, "motivo": None})
    cuerpo = json.dumps(EVENTO_PAQUETE).replace("created", "canceled").encode()
    correr(router.recibir_webhook(FakeRequest(cuerpo)))
    assert guardados[0][2] == "aplicado"
    assert notificados and "Cancelado" in notificados[0][1]


def test_webhook_protegido_no_notifica(monkeypatch, entorno):
    guardados, notificados = entorno
    monkeypatch.setattr(skydropx_envios, "registrar_evento_webhook", lambda datos, payload_crudo=None: {
        "aplicado": True, "envio_encontrado": True, "evento_nuevo": True, "usuario": "ana",
        "codigo_cotizacion": "ZTC-1", "estatus_protegido": True, "motivo": None})
    correr(router.recibir_webhook(FakeRequest(json.dumps(EVENTO_PAQUETE).encode())))
    assert guardados[0][2] == "estatus_protegido" and notificados == []


def test_webhook_con_json_invalido_deja_rastro_y_da_400(monkeypatch, entorno):
    guardados, _ = entorno
    with pytest.raises(HTTPException) as err:
        correr(router.recibir_webhook(FakeRequest(b"no es json")))
    assert err.value.status_code == 400
    assert guardados[0][2] == "json_invalido"


def test_webhook_con_firma_invalida_da_401_y_no_guarda(monkeypatch, entorno):
    guardados, _ = entorno

    def rechaza(cuerpo, firma):
        raise skydropx_service.SkydropxServiceError("Firma del webhook de Skydropx invalida.", status=401)
    monkeypatch.setattr(skydropx_service, "verificar_firma_webhook", rechaza)
    with pytest.raises(HTTPException) as err:
        correr(router.recibir_webhook(FakeRequest(b"{}", firma="HMAC mala")))
    assert err.value.status_code == 401 and guardados == []


@pytest.mark.parametrize("resultado,esperado", [
    ({"aplicado": False, "motivo": "sin_llave"}, "sin_llave"),
    ({"aplicado": False}, "no_aplicado"),
    ({"aplicado": True, "motivo": "sin_estatus"}, "sin_estatus"),
    ({"aplicado": True, "estatus_protegido": True}, "estatus_protegido"),
    ({"aplicado": True, "envio_encontrado": False}, "fila_nueva"),
    ({"aplicado": True, "envio_encontrado": True}, "aplicado"),
])
def test_etiqueta_del_resultado(resultado, esperado):
    assert router._resultado_webhook(resultado) == esperado
