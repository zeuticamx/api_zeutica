# Tests del modulo de envios Skydropx: guias V2 multipaquete, recoleccion en
# dos pasos y catalogos (direcciones y embalajes).
#
# Las respuestas de Skydropx son las reales del sandbox (tests/fixtures/skydropx),
# capturadas el 2026-09-29. No se llama a Skydropx ni a MySQL: se reemplaza
# skydropx_service._pedir y las funciones de skydropx_envios.
import copy
import json
import os
from datetime import date
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

import skydropx_service
from routers import skydropx as router

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "skydropx")

SHIPMENT_A = "8d373fa2-db02-4e58-9ccd-628b1534c7dd"
SHIPMENT_B = "1ce54847-bae4-47d7-ae65-8aa0e8029936"


def fixture(nombre):
    with open(os.path.join(FIXTURES, f"{nombre}.json"), encoding="utf-8") as f:
        return json.load(f)


def con_tracking(respuesta, tracking, etiqueta):
    """Copia de una respuesta de shipment con el tracking ya asignado a sus paquetes."""
    r = copy.deepcopy(respuesta)
    for item in r.get("included", []):
        if item.get("type") == "package":
            item["attributes"]["tracking_number"] = tracking
            item["attributes"]["label_url"] = etiqueta
    return r


@pytest.fixture(autouse=True)
def sin_efectos(monkeypatch):
    """Nada de BD, bitacora, esperas ni caches compartidas entre tests."""
    guardados = []
    monkeypatch.setattr(router.skydropx_envios, "guardar_envio", lambda **k: guardados.append(k) or True)
    monkeypatch.setattr(router.mov_reg, "registrar_movimiento", lambda *a, **k: None)
    monkeypatch.setattr(router.asyncio, "sleep", AsyncMock())
    skydropx_service.invalidar_catalogos()
    return guardados


def pedir_mock(monkeypatch, *respuestas):
    mock = AsyncMock(side_effect=list(respuestas))
    monkeypatch.setattr(skydropx_service, "_pedir", mock)
    return mock


def envio_request(**extra):
    datos = {
        "rate_id": "a6021a31-7718-40cf-8d37-9e4b4850c0fc",
        "parcels": [{"length": 25, "width": 20, "height": 15, "weight": 2,
                     "consignment_note": "53103200", "package_type": "4G"}],
        "cantidad_bultos": 2,
        "codigo_cotizacion": "COT-100",
        "servicio": "Standard",
        "costo": 70.95,
        "usuario": "ventas",
    }
    datos.update(extra)
    return router.EnvioRequest(**datos)


# ─────────────────────────────────────────────────────────────────────────────
# Guias V2
# ─────────────────────────────────────────────────────────────────────────────

def test_extraer_guias_v2_multishipment_da_una_guia_por_shipment():
    guias = skydropx_service.extraer_guias(fixture("shipment_v2_post"))

    assert [g["shipment_id"] for g in guias] == [SHIPMENT_A, SHIPMENT_B]
    assert [g["package_number"] for g in guias] == [1, 2]
    assert all(g["package_id"] for g in guias)
    assert guias[0]["package_id"] != guias[1]["package_id"]
    assert all(g["carrier"] == "ampm" for g in guias)
    # Recien creada: el carrier todavia no asigna tracking.
    assert all(g["tracking_number"] is None for g in guias)


def test_extraer_guias_multipackage_un_shipment_con_varios_paquetes():
    respuesta = {
        "data": [{
            "id": "S1", "type": "shipment", "attributes": {"carrier_name": "dhl"},
            "relationships": {"packages": {"data": [{"id": "P1", "type": "package"},
                                                    {"id": "P2", "type": "package"}]}},
        }],
        "included": [
            {"id": "P1", "type": "package", "attributes": {"tracking_number": "T1", "label_url": "https://l/1"}},
            {"id": "P2", "type": "package", "attributes": {"tracking_number": "T2", "label_url": "https://l/2"}},
        ],
    }
    guias = skydropx_service.extraer_guias(respuesta)

    assert [(g["shipment_id"], g["package_id"], g["tracking_number"]) for g in guias] == [
        ("S1", "P1", "T1"), ("S1", "P2", "T2"),
    ]


def test_extraer_guias_lee_tambien_la_respuesta_v1_de_reconsulta():
    guias = skydropx_service.extraer_guias(fixture("shipment_v1_get"))
    assert len(guias) == 1
    assert guias[0]["shipment_id"] == SHIPMENT_A


@pytest.mark.asyncio
async def test_crear_envio_usa_endpoint_v2_con_packages(monkeypatch):
    listo_a = con_tracking(fixture("shipment_v1_get"), "TRK-A", "https://label/a")
    listo_b = copy.deepcopy(listo_a)
    listo_b["data"]["id"] = SHIPMENT_B
    mock = pedir_mock(monkeypatch, fixture("shipment_v2_post"), listo_a, listo_b)

    await router.crear_envio(envio_request())

    metodo, ruta = mock.await_args_list[0].args
    cuerpo = mock.await_args_list[0].kwargs["json"]["shipment"]
    assert (metodo, ruta) == ("POST", "/api/v2/shipments")
    seguro = {"package_protected": True, "declared_value": 2500.0}
    assert cuerpo["packages"] == [
        {"package_number": 1, "consignment_note": "53103200", "package_type": "4G", **seguro},
        {"package_number": 2, "consignment_note": "53103200", "package_type": "4G", **seguro},
    ]
    assert "parcels" not in cuerpo
    assert cuerpo["consignment_note"] == "53103200" and cuerpo["package_type"] == "4G"


@pytest.mark.asyncio
async def test_crear_envio_un_bulto_tambien_va_por_v2(monkeypatch):
    un_shipment = fixture("shipment_v2_post")
    un_shipment["data"] = un_shipment["data"][:1]
    mock = pedir_mock(monkeypatch, un_shipment, con_tracking(fixture("shipment_v1_get"), "TRK-A", "https://l"))

    r = await router.crear_envio(envio_request(cantidad_bultos=1))

    assert mock.await_args_list[0].args == ("POST", "/api/v2/shipments")
    assert mock.await_args_list[0].kwargs["json"]["shipment"]["packages"] == [
        {"package_number": 1, "consignment_note": "53103200", "package_type": "4G",
         "package_protected": True, "declared_value": 2500.0},
    ]
    assert [p["tracking_number"] for p in r["paquetes"]] == ["TRK-A"]


def test_armar_packages_respeta_valor_declarado_del_panel():
    parcels = [{"consignment_note": "53103200", "package_type": "4G",
                "package_protected": True, "declared_value": 8000}]
    packages = router._armar_packages(parcels, 2, None, None)
    assert [p["declared_value"] for p in packages] == [8000.0, 8000.0]
    assert all(p["package_protected"] is True for p in packages)


def test_armar_packages_asegura_con_default_si_no_viene_valor():
    packages = router._armar_packages([{"consignment_note": "53103200"}], 1, None, "4G")
    assert packages[0]["package_protected"] is True
    assert packages[0]["declared_value"] == router.VALOR_DECLARADO_DEFAULT
    # Sin parcels (solo rate_id) tambien sale asegurada.
    assert router._armar_packages([], 1, "53103200", "4G")[0]["declared_value"] == 2500.0


def test_armar_packages_sin_seguro_si_se_apaga():
    sin_valor = router._armar_packages([{"declared_value": 0}], 1, None, None)[0]
    apagado = router._armar_packages([{"package_protected": False, "declared_value": 2500}], 1, None, None)[0]
    for paquete in (sin_valor, apagado):
        assert "package_protected" not in paquete and "declared_value" not in paquete


@pytest.mark.asyncio
async def test_crear_envio_dos_guias_mismo_articulo_reconsulta_y_guarda_cada_una(monkeypatch, sin_efectos):
    listo_a = con_tracking(fixture("shipment_v1_get"), "TRK-A", "https://label/a")
    listo_b = copy.deepcopy(listo_a)
    listo_b["data"]["id"] = SHIPMENT_B
    for item in listo_b["included"]:
        if item["type"] == "package":
            item["id"] = "PKG-B"
            item["attributes"]["tracking_number"] = "TRK-B"
            item["attributes"]["label_url"] = "https://label/b"
    listo_b["data"]["relationships"]["packages"]["data"] = [{"id": "PKG-B", "type": "package"}]
    mock = pedir_mock(monkeypatch, fixture("shipment_v2_post"), listo_a, listo_b)

    r = await router.crear_envio(envio_request())

    # Una reconsulta V1 por cada shipment sin tracking.
    assert [c.args for c in mock.await_args_list[1:]] == [
        ("GET", f"/api/v1/shipments/{SHIPMENT_A}"),
        ("GET", f"/api/v1/shipments/{SHIPMENT_B}"),
    ]
    assert [(p["package_number"], p["tracking_number"], p["shipment_id"]) for p in r["paquetes"]] == [
        (1, "TRK-A", SHIPMENT_A), (2, "TRK-B", SHIPMENT_B),
    ]
    assert r["shipment_ids"] == [SHIPMENT_A, SHIPMENT_B]
    # Un renglon por guia, cada uno con su propia llave de paquete.
    assert [g["tracking_number"] for g in sin_efectos] == ["TRK-A", "TRK-B"]
    assert len({g["package_id"] for g in sin_efectos}) == 2
    assert all(g["codigo_cotizacion"] == "COT-100" for g in sin_efectos)


@pytest.mark.asyncio
async def test_crear_envio_sin_tracking_todavia_guarda_las_guias_igual(monkeypatch, sin_efectos):
    pendiente = fixture("shipment_v1_get")  # sigue "in_creation"
    pedir_mock(monkeypatch, fixture("shipment_v2_post"), *([pendiente] * 6))

    r = await router.crear_envio(envio_request())

    assert len(r["paquetes"]) == 2
    assert len(sin_efectos) == 2
    assert all(g["tracking_number"] is None and g["package_id"] for g in sin_efectos)


# ─────────────────────────────────────────────────────────────────────────────
# Recoleccion en dos pasos
# ─────────────────────────────────────────────────────────────────────────────

GUIA_GUARDADA = [{"shipment_id": SHIPMENT_A, "codigo_cotizacion": "COT-100", "carrier": "ampm"}]


def recoleccion_request(**extra):
    datos = {"shipment_id": SHIPMENT_A, "fecha": date(2026, 9, 30),
             "hora_inicio": "10:00", "hora_fin": "14:00", "peso_total": 2, "usuario": "ventas"}
    datos.update(extra)
    return router.RecoleccionRequest(**datos)


def test_extraer_horarios_de_la_cobertura_real():
    horarios = skydropx_service.extraer_horarios_recoleccion(fixture("pickup_coverage"))
    assert horarios == [{"fecha": "2026-09-30", "hora_inicio": "09:00", "hora_fin": "19:00"}]


def test_ventana_en_cobertura():
    horarios = [{"fecha": "2026-09-30", "hora_inicio": "09:00", "hora_fin": "19:00"}]
    assert skydropx_service.ventana_en_cobertura(horarios, "2026-09-30", "10:00", "14:00")
    assert not skydropx_service.ventana_en_cobertura(horarios, "2026-09-30", "08:00", "12:00")
    assert not skydropx_service.ventana_en_cobertura(horarios, "2026-10-01", "10:00", "14:00")
    assert not skydropx_service.ventana_en_cobertura(horarios, "2026-09-30", "14:00", "10:00")


@pytest.mark.asyncio
async def test_cobertura_consulta_por_shipment(monkeypatch):
    monkeypatch.setattr(router.skydropx_envios, "envios_de_shipment", lambda sid: GUIA_GUARDADA)
    monkeypatch.setattr(router.skydropx_envios, "recoleccion_de", lambda sid: None)
    mock = pedir_mock(monkeypatch, fixture("pickup_coverage"))

    r = await router.cobertura_recoleccion(SHIPMENT_A)

    assert mock.await_args.args == ("GET", "/api/v1/pickups/coverage")
    assert mock.await_args.kwargs["params"] == {"shipment_id": SHIPMENT_A}
    assert r["horarios"][0]["fecha"] == "2026-09-30"
    assert r["carrier"] == "AMPM"


@pytest.mark.asyncio
async def test_agendar_valida_cobertura_y_luego_crea_ligada_al_envio(monkeypatch):
    guardadas = []
    monkeypatch.setattr(router.skydropx_envios, "envios_de_shipment", lambda sid: GUIA_GUARDADA)
    monkeypatch.setattr(router.skydropx_envios, "recoleccion_de", lambda sid: None)
    monkeypatch.setattr(router.skydropx_envios, "guardar_recoleccion", lambda **k: guardadas.append(k) or True)
    mock = pedir_mock(monkeypatch, fixture("pickup_coverage"),
                      {"data": {"id": "PK-1", "attributes": {"status": "scheduled", "confirmation_number": "C-9"}}})

    r = await router.agendar_recoleccion(recoleccion_request())

    (m1, r1), (m2, r2) = [c.args for c in mock.await_args_list]
    assert (m1, r1) == ("GET", "/api/v1/pickups/coverage")
    assert (m2, r2) == ("POST", "/api/v1/pickups")
    assert mock.await_args_list[1].kwargs["json"] == {"pickup": {
        "reference_shipment_id": SHIPMENT_A,
        "packages": 1,
        "total_weight": 2.0,
        "scheduled_from": "2026-09-30 10:00",
        "scheduled_to": "2026-09-30 14:00",
    }}
    assert r["recoleccion"]["pickup_id"] == "PK-1"
    assert r["recoleccion"]["confirmacion"] == "C-9"
    assert guardadas[0]["shipment_id"] == SHIPMENT_A
    assert guardadas[0]["codigo_cotizacion"] == "COT-100"


@pytest.mark.asyncio
async def test_agendar_fuera_de_cobertura_no_llama_a_crear(monkeypatch):
    monkeypatch.setattr(router.skydropx_envios, "envios_de_shipment", lambda sid: GUIA_GUARDADA)
    monkeypatch.setattr(router.skydropx_envios, "recoleccion_de", lambda sid: None)
    mock = pedir_mock(monkeypatch, fixture("pickup_coverage"))

    with pytest.raises(HTTPException) as exc:
        await router.agendar_recoleccion(recoleccion_request(hora_inicio="07:00", hora_fin="08:00"))

    assert exc.value.status_code == 422
    assert "2026-09-30 09:00-19:00" in exc.value.detail
    assert mock.await_count == 1  # solo la cobertura


@pytest.mark.asyncio
async def test_agendar_shipment_ajeno_da_404_sin_llamar_a_skydropx(monkeypatch):
    monkeypatch.setattr(router.skydropx_envios, "envios_de_shipment", lambda sid: [])
    mock = pedir_mock(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await router.agendar_recoleccion(recoleccion_request())

    assert exc.value.status_code == 404
    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_agendar_dos_veces_el_mismo_envio_da_409(monkeypatch):
    monkeypatch.setattr(router.skydropx_envios, "envios_de_shipment", lambda sid: GUIA_GUARDADA)
    monkeypatch.setattr(router.skydropx_envios, "recoleccion_de",
                        lambda sid: {"fecha": "2026-09-30", "hora_inicio": "10:00", "hora_fin": "14:00"})
    mock = pedir_mock(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await router.agendar_recoleccion(recoleccion_request())

    assert exc.value.status_code == 409
    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_agendar_propaga_el_rechazo_de_skydropx_por_guia_en_creacion(monkeypatch):
    monkeypatch.setattr(router.skydropx_envios, "envios_de_shipment", lambda sid: GUIA_GUARDADA)
    monkeypatch.setattr(router.skydropx_envios, "recoleccion_de", lambda sid: None)
    rechazo = skydropx_service.SkydropxServiceError(
        "Skydropx rechazo los datos del envio (422): reference_shipment: El estado del envío no es exitoso",
        status=422,
    )
    pedir_mock(monkeypatch, fixture("pickup_coverage"), rechazo)

    with pytest.raises(HTTPException) as exc:
        await router.agendar_recoleccion(recoleccion_request())

    assert exc.value.status_code == 422
    assert "no es exitoso" in exc.value.detail


# ─────────────────────────────────────────────────────────────────────────────
# Catalogos
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_embalajes_normaliza_y_usa_cache(monkeypatch):
    mock = pedir_mock(monkeypatch, fixture("packagings"))

    primera = await router.listar_embalajes()
    segunda = await router.listar_embalajes()

    assert mock.await_count == 1
    assert mock.await_args.args == ("GET", "/api/v1/shipments/packagings")
    assert primera["embalajes"][0] == {"code": "4G", "name": "Caja de cartón"}
    assert segunda == primera


@pytest.mark.asyncio
async def test_direcciones_vacias(monkeypatch):
    mock = pedir_mock(monkeypatch, fixture("address_templates"))

    r = await router.listar_direcciones(address_type=None)

    assert mock.await_args.args == ("GET", "/api/v1/address_templates")
    assert r["direcciones"] == []


@pytest.mark.asyncio
async def test_direcciones_con_la_forma_real_de_skydropx(monkeypatch):
    pedir_mock(monkeypatch, fixture("address_templates_con_datos"))

    r = await router.listar_direcciones(address_type=None)

    destino, bodega = r["direcciones"]
    assert (destino["alias"], destino["tipo"], destino["default"]) == ("cliente prueba", "to", False)
    assert (bodega["alias"], bodega["tipo"], bodega["default"]) == ("bodega", "from", True)
    # El numero exterior quedo en apartment_number en las plantillas reales: se conserva aparte.
    assert bodega["direccion"] == {
        "country_code": "MX", "postal_code": "45145", "area_level1": "Jalisco", "area_level2": "Zapopan",
        "area_level3": "Parque Industrial Belenes Norte", "street1": "Boulevard de los charros",
        "apartment_number": "1629", "name": "Contacto Bodega", "company": "zeutica",
        "phone": "3300000002", "email": "bodega@example.com", "reference": "porton negro",
    }


@pytest.mark.asyncio
async def test_direcciones_filtra_por_tipo_localmente(monkeypatch):
    # El sandbox ignora address_type en la consulta: el filtro es nuestro, sobre la cache.
    mock = pedir_mock(monkeypatch, fixture("address_templates_con_datos"))

    origenes = await router.listar_direcciones(address_type="from")
    destinos = await router.listar_direcciones(address_type="to")

    assert [d["alias"] for d in origenes["direcciones"]] == ["bodega"]
    assert [d["alias"] for d in destinos["direcciones"]] == ["cliente prueba"]
    assert mock.await_count == 1


@pytest.mark.asyncio
async def test_direcciones_recorre_paginas(monkeypatch):
    pagina1 = {"data": [{"id": "D1", "alias_name": "a", "address": {"postal_code": "45145"}}], "meta": {"next_page": 2}}
    pagina2 = {"data": [{"id": "D2", "alias_name": "b", "address": {"postal_code": "44100"}},
                        {"id": "D3", "alias_name": "sin cp", "address": {"name": "x"}}],
               "meta": {"next_page": None}}
    mock = pedir_mock(monkeypatch, pagina1, pagina2)

    r = await router.listar_direcciones(address_type=None)

    assert [c.kwargs["params"] for c in mock.await_args_list] == [{"page": 1}, {"page": 2}]
    assert [d["id"] for d in r["direcciones"]] == ["D1", "D2"]  # la que no tiene CP se descarta


def test_street_number_se_une_a_la_calle():
    plantilla = skydropx_service.extraer_direcciones({"data": [fixture("address_template_post")["data"]]})[0]
    assert plantilla["direccion"]["street1"] == "Av. Juarez 100"
    assert plantilla["direccion"]["apartment_number"] == "4B"


DIRECCION_COMPLETA = {
    "postal_code": "44100", "area_level1": "Jalisco", "area_level2": "Guadalajara", "area_level3": "Centro",
    "street1": "Av. Juarez", "street_number": "100", "apartment_number": "4B", "name": "Prueba",
    "company": "Prueba SA", "phone": "3312345678", "email": "prueba@example.com", "reference": "Porton verde",
}


@pytest.mark.asyncio
async def test_guardar_direccion_manda_el_payload_de_skydropx_e_invalida_cache(monkeypatch):
    mock = pedir_mock(monkeypatch,
                      fixture("address_templates"),          # listado antes (vacio, queda en cache)
                      fixture("address_template_post"),       # alta
                      fixture("address_templates_con_datos"))  # listado despues: ya no sale de cache
    await router.listar_direcciones(address_type=None)

    r = await router.guardar_direccion(router.PlantillaDireccionRequest(
        alias_name="  PRUEBA ", address_type="to", address=DIRECCION_COMPLETA, usuario="ventas"))

    metodo, ruta = mock.await_args_list[1].args
    assert (metodo, ruta) == ("POST", "/api/v1/address_templates")
    assert mock.await_args_list[1].kwargs["json"] == {"address_template": {
        "alias_name": "PRUEBA", "address_type": "to", "default": False,
        "address_attributes": {"country_code": "MX", **DIRECCION_COMPLETA},
    }}
    assert r["direccion"]["alias"] == "PRUEBA"
    assert r["direccion"]["direccion"]["street1"] == "Av. Juarez 100"

    despues = await router.listar_direcciones(address_type=None)
    assert len(despues["direcciones"]) == 2
    assert mock.await_count == 3


def test_guardar_direccion_valida_obligatorios_antes_de_llamar_a_skydropx():
    incompleta = {k: v for k, v in DIRECCION_COMPLETA.items() if k not in ("reference", "area_level3")}
    with pytest.raises(ValidationError) as exc:
        router.PlantillaDireccionRequest(alias_name="x", address_type="to", address=incompleta)
    faltantes = {e["loc"][-1] for e in exc.value.errors()}
    assert faltantes == {"reference", "area_level3"}

    with pytest.raises(ValidationError):
        router.PlantillaDireccionRequest(alias_name="x", address_type="otro", address=DIRECCION_COMPLETA)


# ─────────────────────────────────────────────────────────────────────────────
# Catalogo propio de cajas
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_listar_cajas(monkeypatch):
    cajas = [{"id": 1, "nombre": "Sobre", "length": 30.0, "width": 25.0, "height": 2.0, "weight": 0.5, "package_type": "4G"}]
    monkeypatch.setattr(router.skydropx_envios, "listar_cajas", lambda: cajas)

    assert (await router.listar_cajas())["cajas"] == cajas


@pytest.mark.asyncio
async def test_listar_cajas_si_falla_la_bd_da_503(monkeypatch):
    def falla():
        raise RuntimeError("sin conexion")
    monkeypatch.setattr(router.skydropx_envios, "listar_cajas", falla)

    with pytest.raises(HTTPException) as exc:
        await router.listar_cajas()
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_guardar_caja_normaliza_nombre_y_codigo(monkeypatch):
    recibido = {}
    monkeypatch.setattr(router.skydropx_envios, "guardar_caja", lambda **k: recibido.update(k) or {"id": 5, **k})

    r = await router.guardar_caja(router.CajaRequest(
        nombre="  Caja   tapetes ", length=70, width=50, height=10, weight=3.5, package_type="4g", usuario="ventas"))

    assert recibido["nombre"] == "Caja tapetes"
    assert recibido["package_type"] == "4G"
    assert r["caja"]["id"] == 5


@pytest.mark.asyncio
async def test_guardar_caja_duplicada_da_409(monkeypatch):
    def duplicada(**k):
        raise router.skydropx_envios.CajaDuplicada(k["nombre"])
    monkeypatch.setattr(router.skydropx_envios, "guardar_caja", duplicada)

    with pytest.raises(HTTPException) as exc:
        await router.guardar_caja(router.CajaRequest(nombre="Sobre", length=30, width=25, height=2, weight=0.5))
    assert exc.value.status_code == 409


def test_caja_rechaza_medidas_invalidas():
    with pytest.raises(ValidationError):
        router.CajaRequest(nombre="x", length=0, width=10, height=10, weight=1)
    with pytest.raises(ValidationError):
        router.CajaRequest(nombre="", length=10, width=10, height=10, weight=1)
