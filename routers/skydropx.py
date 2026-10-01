# Endpoints del modulo de envios (Skydropx Pro).
# La logica de Skydropx vive en skydropx_service.py; aqui solo va el contrato HTTP.
#
# Este router es aditivo: solo escribe sus propias tablas (skydropx_envios.py:
# guias y recolecciones) y la bitacora de movimientos. No comparte estado con
# embarques.py ni con ningun otro modulo.
from datetime import date
from typing import Any, Dict, List, Literal, Optional

import asyncio

import mov_reg
import notificaciones_service
import skydropx_envios
import skydropx_service
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from skydropx_service import SkydropxServiceError

router = APIRouter(tags=["skydropx-envios"], responses={404: {"Mensaje": "No encontrado"}})

# Router aparte para el webhook: lo llama Skydropx, no el panel, asi que no puede
# ir detras de obtener_usuario_actual. Se autentica con la firma HMAC del cuerpo.
router_webhook = APIRouter(tags=["skydropx-webhook"], responses={404: {"Mensaje": "No encontrado"}})

# Destinatarios que deben enterarse de todo cambio de estatus de guia, sea quien
# sea que la haya generado. Mismo patron que USUARIO_COBRANZA en abonos.py.
USUARIOS_ENVIO_SIEMPRE = ("gerencia", "fparra", "ventas")

# Monto asegurado por bulto (MXN) cuando el panel no manda otro. El panel lo
# precarga con este mismo valor y deja editarlo (skydropx-logica.js).
VALOR_DECLARADO_DEFAULT = 2500.0


# ─────────────────────────────────────────────────────────────────────────────
# Esquemas
#
# Todos permiten campos extra (extra="allow"): el contrato de Skydropx tiene
# campos opcionales por carrier (seguro, recoleccion, carta porte) que cambian sin
# avisar. Se validan los obligatorios y lo demas se deja pasar tal cual, para no
# tener que tocar este archivo cada vez que Skydropx agregue una opcion.
# ─────────────────────────────────────────────────────────────────────────────

class Direccion(BaseModel):
    """
    Origen o destino. Para cotizar basta con country_code + postal_code; para
    generar la guia el carrier ya exige calle, nombre y telefono.
    """
    model_config = ConfigDict(extra="allow") 

    country_code: str = Field(default="MX", description="Codigo ISO del pais, ej. MX")
    postal_code: str = Field(description="Codigo postal, 5 digitos en Mexico")
    area_level1: Optional[str] = Field(default=None, description="Estado")
    area_level2: Optional[str] = Field(default=None, description="Municipio o delegacion")
    area_level3: Optional[str] = Field(default=None, description="Colonia")
    street1: Optional[str] = Field(default=None, description="Calle y numero")
    name: Optional[str] = Field(default=None, description="Nombre de quien envia o recibe")
    company: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    # Opcional para cotizar; Skydropx lo exige (no puede venir en blanco) al
    # generar la guia, tanto en address_from como en address_to. Confirmado en
    # sandbox: sin el, 422 "reference: no puede estar en blanco".
    reference: Optional[str] = Field(default=None, description="Referencia para el repartidor. Requerida para generar guia.")


class Paquete(BaseModel):
    """
    Medidas en centimetros y peso en kilogramos, que es lo que espera Skydropx.

    consignment_note y package_type quedan opcionales aqui porque la cotizacion
    no los exige (Skydropx cotiza sin ellos); la generacion de guia si los exige
    -- confirmado en sandbox: sin ellos responde 422 "consignment_note es
    requerido en todos los paquetes" / "package_type es requerido en todos los
    paquetes". El frontend los manda siempre con default al generar la guia.

    package_protected y declared_value activan seguro en el paquete con el monto
    a asegurar en MXN (default: VALOR_DECLARADO_DEFAULT). Viajan tambien en
    `packages` al generar la guia (ver _armar_packages).
    """
    model_config = ConfigDict(extra="allow")

    length: float = Field(gt=0, description="Largo en cm")
    width: float = Field(gt=0, description="Ancho en cm")
    height: float = Field(gt=0, description="Alto en cm")
    weight: float = Field(gt=0, description="Peso en kg")
    consignment_note: Optional[str] = Field(default=None, description="Contenido del paquete (carta porte). Requerido para generar guia.")
    package_type: Optional[str] = Field(default=None, description="Codigo de embalaje del catalogo de Skydropx. Requerido para generar guia.")
    package_protected: Optional[bool] = Field(default=True, description="Habilitar seguro en el paquete")
    declared_value: Optional[float] = Field(default=VALOR_DECLARADO_DEFAULT, ge=0, description="Monto a asegurar en MXN")


class CotizacionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    address_from: Direccion
    address_to: Direccion
    parcels: List[Paquete] = Field(min_length=1)
    # Opcionales del contrato de Skydropx (ej. requested_carriers) van aqui.
    extras: Optional[Dict[str, Any]] = None
    # Si es True, se reconsulta la cotizacion hasta que lleguen tarifas.
    esperar_tarifas: bool = True
    # Envios multipaquete: cuantos bultos va a llevar el envio. Si el panel
    # manda un solo elemento en `parcels` (el caso comun: el usuario captura
    # una sola medida), se clona esa medida `cantidad_bultos` veces -- ver
    # _clonar_parcelas(). Si ya vinieran varios parcels detallados, se
    # respetan tal cual. El limite de 20 es un tope de seguridad, no un valor
    # confirmado contra Skydropx.
    cantidad_bultos: int = Field(default=1, ge=1, le=20, description="Cantidad de bultos del envio (multipaquete)")


class EnvioRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    rate_id: str = Field(description="rate_id tomado de las tarifas de la cotizacion")
    # Opcionales: el rate ya trae las direcciones de la cotizacion. Se mandan solo
    # para completar o corregir datos de contacto antes de imprimir la guia.
    address_from: Optional[Direccion] = None
    address_to: Optional[Direccion] = None
    parcels: Optional[List[Paquete]] = None
    extras: Optional[Dict[str, Any]] = None
    usuario: Optional[str] = Field(default=None, description="Solo para la bitacora de movimientos")
    referencia: Optional[str] = Field(default=None, description="Referencia interna para la bitacora")
    # Liga la guia con la cotizacion que la origino. Es lo que permite pintar el
    # estatus en el renglon correcto del panel cuando llega el webhook.
    codigo_cotizacion: Optional[str] = Field(default=None, description="Cotizacion que origina el envio")
    # Datos de la tarifa elegida, solo para guardarlos junto a la guia: Skydropx
    # no los repite en la respuesta del envio. Van aparte de `extras` a proposito,
    # porque `extras` se reenvia tal cual al API de Skydropx.
    servicio: Optional[str] = Field(default=None, description="Nombre del servicio de la tarifa elegida")
    costo: Optional[float] = Field(default=None, description="Costo de la tarifa elegida")
    # Skydropx valida estos dos a nivel shipment (no por paquete). Si el panel
    # no los manda aqui, se toman del primer parcel, que es donde los captura
    # el modal. Ver nota en skydropx_service.crear_envio().
    consignment_note: Optional[str] = Field(default=None, description="Contenido del envio (carta porte)")
    package_type: Optional[str] = Field(default=None, description="Codigo de embalaje del catalogo de Skydropx")
    # Mismo campo y mismo criterio que en CotizacionRequest. Siempre se arma el
    # arreglo `packages` de V2 con `package_number` correlativo, aunque sea 1.
    cantidad_bultos: int = Field(default=1, ge=1, le=20, description="Cantidad de bultos del envio (multipaquete)")


class RecoleccionRequest(BaseModel):
    """
    Paso 2 de la recoleccion: el horario elegido de entre los que devolvio
    GET /skydropx/recolecciones/cobertura. Se vuelve a validar contra la
    cobertura antes de agendar, porque el horario pudo cerrarse mientras el
    usuario decidia.
    """
    shipment_id: str = Field(description="Shipment de Skydropx generado por este sistema")
    fecha: date = Field(description="Fecha de recoleccion (YYYY-MM-DD)")
    hora_inicio: str = Field(pattern=r"^\d{2}:\d{2}$", description="Inicio de la ventana, HH:MM")
    hora_fin: str = Field(pattern=r"^\d{2}:\d{2}$", description="Fin de la ventana, HH:MM")
    peso_total: float = Field(gt=0, description="Peso total a recolectar en kg")
    # Si no viene, se usa cuantas guias (paquetes) tiene guardadas ese shipment.
    paquetes: Optional[int] = Field(default=None, ge=1, le=99)
    usuario: Optional[str] = None


class DireccionPlantilla(BaseModel):
    """
    Direccion para guardar en la libreta de Skydropx. Los obligatorios son los
    que Skydropx exige para dar de alta la plantilla (medido en sandbox: sin
    ellos responde 400 "X no puede estar en blanco"); company es opcional.
    """
    model_config = ConfigDict(extra="ignore")

    country_code: str = Field(default="MX")
    postal_code: str = Field(pattern=r"^\d{5}$", description="Codigo postal, 5 digitos")
    area_level1: str = Field(min_length=1, description="Estado")
    area_level2: str = Field(min_length=1, description="Municipio o alcaldia")
    area_level3: str = Field(min_length=1, description="Colonia")
    street1: str = Field(min_length=1, description="Calle y numero")
    name: str = Field(min_length=1, description="Nombre de contacto")
    phone: str = Field(min_length=1)
    email: str = Field(min_length=3)
    reference: str = Field(min_length=1, description="Referencia visual para el repartidor")
    company: Optional[str] = None
    street_number: Optional[str] = None
    apartment_number: Optional[str] = None
    rfc: Optional[str] = None


class PlantillaDireccionRequest(BaseModel):
    """Alta de una direccion en la libreta de la cuenta de Skydropx (address_templates)."""
    alias_name: str = Field(min_length=1, max_length=60, description="Alias con el que aparece en el selector")
    address_type: Literal["from", "to"] = Field(description="from = origen/remitente, to = destino")
    default: bool = False
    address: DireccionPlantilla
    usuario: Optional[str] = Field(default=None, description="Solo para la bitacora de movimientos")


class CajaRequest(BaseModel):
    """Medida de caja para los botones de presets del modal (catalogo propio, en MySQL)."""
    nombre: str = Field(min_length=1, max_length=60, description="Texto del boton, ej. Caja chica")
    length: float = Field(gt=0, le=500, description="Largo en cm")
    width: float = Field(gt=0, le=500, description="Ancho en cm")
    height: float = Field(gt=0, le=500, description="Alto en cm")
    weight: float = Field(gt=0, le=1000, description="Peso estimado en kg")
    package_type: str = Field(default="4G", min_length=1, max_length=10,
                              description="Codigo de embalaje de Skydropx (4G = caja de carton)")
    usuario: Optional[str] = None


def _a_dict(modelo: Optional[BaseModel]) -> Optional[Dict[str, Any]]:
    """Quita los None para no mandarle a Skydropx campos vacios que rechaza en validacion."""
    if modelo is None:
        return None
    return modelo.model_dump(exclude_none=True)


def _clonar_parcelas(parcelas: List[Dict[str, Any]], cantidad: int) -> List[Dict[str, Any]]:
    """
    Envios multipaquete: si el llamador mando un solo paquete (el caso comun
    del panel, que solo captura una medida) pero pidio varios bultos, clona
    ese paquete `cantidad` veces -- mismas medidas para todos los bultos.

    Si ya vinieran varios parcels detallados de forma individual, se respetan
    tal cual: este helper solo resuelve el atajo de "una medida + un numero",
    no reemplaza una lista que el llamador ya arme a mano.
    """
    if cantidad <= 1 or len(parcelas) != 1:
        return parcelas
    return [dict(parcelas[0]) for _ in range(cantidad)]


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/skydropx/configuracion")
async def obtener_configuracion():
    """
    Que credenciales de Skydropx estan cargadas y contra que ambiente apunta.
    No expone ningun valor secreto, solo si existe. Util para confirmar de un
    vistazo que no se esta generando guias reales cuando se pensaba probar.
    """
    return skydropx_service.configuracion_lista()


@router.post("/skydropx/cotizaciones")
async def crear_cotizacion(datos: CotizacionRequest):
    """
    Cotiza un envio con los carriers disponibles.

    Skydropx cotiza de forma asincrona, por eso con `esperar_tarifas` en true
    (default) se reconsulta hasta que lleguen las tarifas. Devuelve la cotizacion
    completa mas `tarifas` ya extraidas, que es de donde sale el rate_id.

    No genera guia ni cobra nada: cotizar es gratis.
    """
    parcels = _clonar_parcelas(
        [p.model_dump(exclude_none=True) for p in datos.parcels],
        datos.cantidad_bultos,
    )
    try:
        cotizacion = await skydropx_service.crear_cotizacion(
            address_from=datos.address_from.model_dump(exclude_none=True),
            address_to=datos.address_to.model_dump(exclude_none=True),
            parcels=parcels,
            extras=datos.extras,
        )
    except SkydropxServiceError as err:
        raise HTTPException(status_code=err.status, detail=err.detalle)

    tarifas = skydropx_service.extraer_tarifas(cotizacion)
    cotizacion_id = _leer_id(cotizacion)

    if datos.esperar_tarifas and not tarifas and cotizacion_id:
        try:
            cotizacion = await skydropx_service.esperar_tarifas(str(cotizacion_id))
            tarifas = skydropx_service.extraer_tarifas(cotizacion)
        except SkydropxServiceError as err:
            raise HTTPException(status_code=err.status, detail=err.detalle)

    return {
        "status": "success",
        "cotizacion_id": cotizacion_id,
        "tarifas": tarifas,
        "cotizacion": cotizacion,
    }


@router.get("/skydropx/cotizaciones/{cotizacion_id}")
async def obtener_cotizacion(cotizacion_id: str):
    """
    Tarifas de una cotizacion ya creada. Sirve para volver a consultarla cuando la
    primera respuesta llego sin `rates` porque los carriers seguian contestando.
    """
    try:
        cotizacion = await skydropx_service.obtener_cotizacion(cotizacion_id)
    except SkydropxServiceError as err:
        raise HTTPException(status_code=err.status, detail=err.detalle)

    return {
        "status": "success",
        "cotizacion_id": _leer_id(cotizacion) or cotizacion_id,
        "tarifas": skydropx_service.extraer_tarifas(cotizacion),
        "cotizacion": cotizacion,
    }


@router.post("/skydropx/envios")
async def crear_envio(datos: EnvioRequest):
    """
    Genera la(s) guia(s) a partir de un rate_id, siempre contra
    POST /api/v2/shipments (ver skydropx_service.crear_envio).

    OJO: en ambiente de produccion esto contrata el envio con el carrier y se
    cobra. No hay cancelacion desde este endpoint.

    V2 responde `data` como arreglo: con cantidad_bultos > 1 puede traer un
    shipment por bulto ("multishipment") o uno solo con varios paquetes
    ("multipackage"). En los dos casos `paquetes` trae una entrada por guia,
    cada una con su shipment_id, y se guarda un renglon por guia.
    """
    # Nivel shipment: si no vienen explicitos, se heredan del primer paquete
    # (el modal los captura ahi). Skydropx los exige a este nivel.
    primer_paquete = datos.parcels[0] if datos.parcels else None
    consignment_note = datos.consignment_note or (primer_paquete.consignment_note if primer_paquete else None)
    package_type = datos.package_type or (primer_paquete.package_type if primer_paquete else None)

    parcels_dicts = _clonar_parcelas(
        [p.model_dump(exclude_none=True) for p in datos.parcels] if datos.parcels else [],
        datos.cantidad_bultos,
    )
    cantidad_bultos = max(datos.cantidad_bultos or 1, len(parcels_dicts))
    packages = _armar_packages(parcels_dicts, cantidad_bultos, consignment_note, package_type)

    try:
        envio = await skydropx_service.crear_envio(
            rate_id=datos.rate_id,
            packages=packages,
            address_from=_a_dict(datos.address_from),
            address_to=_a_dict(datos.address_to),
            consignment_note=consignment_note,
            package_type=package_type,
            extras=datos.extras,
        )
    except SkydropxServiceError as err:
        raise HTTPException(status_code=err.status, detail=err.detalle)

    guias = skydropx_service.extraer_guias(envio)
    if not guias:
        # 2xx sin shipments reconocibles: la guia pudo haberse cobrado, asi que
        # no se contesta error (el usuario la generaria otra vez); se devuelve
        # el cuerpo crudo para que se vea que paso.
        print(f"Respuesta V2 de Skydropx sin shipments reconocibles: {str(envio)[:500]}")

    # El POST casi siempre contesta antes de que el carrier asigne tracking y
    # etiqueta (sandbox: "in_creation" con tracking_number null). Se reconsulta
    # cada shipment pendiente un par de veces; si sigue sin llegar, el webhook
    # lo completara despues.
    for _ in range(3):
        pendientes = _shipments_sin_tracking(guias)
        if not pendientes:
            break
        await asyncio.sleep(1.5)
        for sid in pendientes:
            try:
                actualizado = await skydropx_service.obtener_envio(sid)
            except SkydropxServiceError:
                continue  # no tumbar la respuesta: la guia ya se genero y se cobro
            guias = _reemplazar_guias_de(guias, sid, skydropx_service.extraer_guias(actualizado))

    codigo = datos.codigo_cotizacion or datos.referencia

    # Un renglon en skydropx_envios por guia. costo/servicio son los de la
    # tarifa elegida, que cubre el envio completo (Skydropx no los desglosa por
    # bulto): se repiten igual en cada renglon. Si algun reporte llega a sumar
    # esta columna por cotizacion, debe dedupear por shipment_id.
    #
    # Nunca lanza: la guia ya se contrato y se cobro, y fallar aqui haria que el
    # usuario la generara de nuevo.
    guardado = False
    for guia in guias:
        try:
            if skydropx_envios.guardar_envio(
                codigo_cotizacion=codigo,
                tracking_number=guia.get("tracking_number"),
                shipment_id=guia.get("shipment_id"),
                package_id=guia.get("package_id"),
                carrier=guia.get("carrier"),
                servicio=datos.servicio or guia.get("servicio"),
                costo=datos.costo,
                etiqueta_url=guia.get("etiqueta_url"),
                orden_detalle_url=guia.get("orden_detalle_url"),
                tracking_url=guia.get("tracking_url"),
                usuario=datos.usuario or "sistema",
            ):
                guardado = True
        except Exception as err:
            print(f"Error al guardar envio Skydropx en BD: {err}")

    # Bitacora. Si el registro falla no se tumba la respuesta: la guia ya se genero
    # y ocultarla con un 500 haria que el usuario la generara de nuevo (y pagara doble).
    try:
        tracking = next((g["tracking_number"] for g in guias if g.get("tracking_number")), None)
        referencia = codigo or tracking or datos.rate_id
        sufijo = f" x{len(guias)} guias" if len(guias) > 1 else ""
        mov_reg.registrar_movimiento(
            datos.usuario or "sistema",
            f"Genero guia Skydropx{sufijo} ({referencia})",
            "Envios Skydropx",
        )
    except Exception as err:
        print(f"Error al registrar movimiento de guia Skydropx: {err}")

    return {
        "status": "success",
        "envio": envio,
        "guardado": guardado,
        # Mismo contrato que antes para el panel, mas package_id y carrier.
        "paquetes": guias,
        "shipment_ids": list(dict.fromkeys(g["shipment_id"] for g in guias if g.get("shipment_id"))),
    }


@router.get("/skydropx/envios")
async def listar_envios(
    codigo_cotizacion: Optional[str] = Query(default=None, description="Filtra por cotizacion"),
):
    """
    Envios registrados con su ultimo estatus (el que dejo el webhook).

    Es lo que el panel consulta al abrir Cotizaciones para pintar el estatus de
    cada renglon. Devuelve `estatus_texto` y `estatus_tono` ya traducidos para
    que el front no tenga que conocer el catalogo de Skydropx.
    """
    return {"status": "success", "envios": skydropx_envios.listar_envios(codigo_cotizacion)}


@router.get("/skydropx/envios/{tracking_number}/eventos")
async def listar_eventos(tracking_number: str):
    """Linea de tiempo de una guia, armada con los eventos que mando el webhook."""
    return {"status": "success", "eventos": skydropx_envios.eventos_de(tracking_number)}


@router.get("/skydropx/saldo")
async def consultar_saldo():
    """
    Saldo disponible en la cuenta de Skydropx. De aqui se descuenta cada guia.

    Devuelve el monto ya normalizado mas la respuesta cruda: si Skydropx cambia
    el nombre de la llave, `saldo` llega en null y el panel puede mostrar el
    cuerpo tal cual en vez de pintar un cero que se leeria como "sin saldo".
    """
    try:
        respuesta = await skydropx_service.obtener_saldo()
    except SkydropxServiceError as err:
        raise HTTPException(status_code=err.status, detail=err.detalle)

    return {"status": "success", **skydropx_service.extraer_saldo(respuesta), "crudo": respuesta}


@router.get("/skydropx/rastreo")
async def rastrear_envio(
    tracking_number: str = Query(description="Numero de rastreo de la guia"),
    carrier_name: str = Query(description="Nombre del carrier, ej. fedex, estafeta"),
):
    """Estado actual de una guia ya generada, directo del carrier via Skydropx."""
    try:
        rastreo = await skydropx_service.rastrear(tracking_number, carrier_name)
    except SkydropxServiceError as err:
        raise HTTPException(status_code=err.status, detail=err.detalle)

    return {"status": "success", "rastreo": rastreo}


@router.get("/skydropx/direcciones")
async def listar_direcciones(
    address_type: Optional[Literal["from", "to"]] = Query(default=None, description="Solo origen (from) o destino (to)"),
):
    """
    Libreta de direcciones guardada en la cuenta de Skydropx (address_templates),
    ya en el mismo formato que address_from/address_to. Cache de 5 min.
    """
    try:
        direcciones = await skydropx_service.listar_direcciones(address_type)
    except SkydropxServiceError as err:
        raise HTTPException(status_code=err.status, detail=err.detalle)
    return {"status": "success", "direcciones": direcciones}


@router.post("/skydropx/direcciones")
async def guardar_direccion(datos: PlantillaDireccionRequest):
    """
    Guarda una direccion en la libreta de la cuenta de Skydropx para reusarla
    despues desde el selector del modal. Devuelve la plantilla ya normalizada.
    """
    try:
        plantilla = await skydropx_service.crear_direccion(
            alias=datos.alias_name.strip(),
            tipo=datos.address_type,
            direccion=datos.address.model_dump(exclude_none=True),
            por_defecto=datos.default,
        )
    except SkydropxServiceError as err:
        raise HTTPException(status_code=err.status, detail=err.detalle)

    try:
        mov_reg.registrar_movimiento(
            datos.usuario or "sistema",
            f"Guardo direccion Skydropx '{plantilla['alias']}' ({datos.address_type})",
            "Envios Skydropx",
        )
    except Exception as err:
        print(f"Error al registrar movimiento de direccion Skydropx: {err}")

    return {"status": "success", "direccion": plantilla}


@router.get("/skydropx/cajas")
async def listar_cajas():
    """Medidas de caja predefinidas (catalogo propio) para los botones del modal."""
    try:
        cajas = skydropx_envios.listar_cajas()
    except Exception as err:
        print(f"Error consultando cajas Skydropx: {err}")
        raise HTTPException(status_code=503, detail="No se pudo leer el catalogo de cajas.")
    return {"status": "success", "cajas": cajas}


@router.post("/skydropx/cajas")
async def guardar_caja(datos: CajaRequest):
    """Da de alta una medida de caja nueva. 409 si ya existe una con ese nombre."""
    nombre = " ".join(datos.nombre.split())
    try:
        caja = skydropx_envios.guardar_caja(
            nombre=nombre,
            length=datos.length,
            width=datos.width,
            height=datos.height,
            weight=datos.weight,
            package_type=datos.package_type.strip().upper(),
            usuario=datos.usuario,
        )
    except skydropx_envios.CajaDuplicada:
        raise HTTPException(status_code=409, detail=f"Ya existe una caja llamada '{nombre}'.")
    except Exception as err:
        print(f"Error guardando caja Skydropx: {err}")
        raise HTTPException(status_code=503, detail="No se pudo guardar la caja.")

    try:
        mov_reg.registrar_movimiento(datos.usuario or "sistema", f"Registro caja Skydropx '{nombre}'", "Envios Skydropx")
    except Exception as err:
        print(f"Error al registrar movimiento de caja Skydropx: {err}")

    return {"status": "success", "caja": caja}


@router.get("/skydropx/embalajes")
async def listar_embalajes():
    """Tipos de embalaje estandar (codigo para package_type + nombre). Cache de 30 min."""
    try:
        embalajes = await skydropx_service.listar_embalajes()
    except SkydropxServiceError as err:
        raise HTTPException(status_code=err.status, detail=err.detalle)
    return {"status": "success", "embalajes": embalajes}


@router.get("/skydropx/recolecciones")
async def listar_recolecciones(
    codigo_cotizacion: str = Query(description="Cotizacion cuyas recolecciones se consultan"),
):
    """Recolecciones ya agendadas para los envios de una cotizacion."""
    return {"status": "success", "recolecciones": skydropx_envios.listar_recolecciones(codigo_cotizacion)}


@router.get("/skydropx/recolecciones/cobertura")
async def cobertura_recoleccion(
    shipment_id: str = Query(description="Shipment de Skydropx generado por este sistema"),
):
    """
    Paso 1 de la recoleccion: fechas y ventanas horarias que ofrece el carrier
    para ese shipment. No agenda nada.
    """
    if not skydropx_envios.envios_de_shipment(shipment_id):
        raise HTTPException(status_code=404, detail="Ese envio no se genero desde este sistema.")
    try:
        cobertura = await skydropx_service.consultar_cobertura_recoleccion(shipment_id)
    except SkydropxServiceError as err:
        raise HTTPException(status_code=err.status, detail=err.detalle)
    return {
        "status": "success",
        "shipment_id": shipment_id,
        "carrier": cobertura.get("carrier") if isinstance(cobertura, dict) else None,
        "horarios": skydropx_service.extraer_horarios_recoleccion(cobertura),
        "recoleccion": skydropx_envios.recoleccion_de(shipment_id),
    }


@router.post("/skydropx/recolecciones")
async def agendar_recoleccion(datos: RecoleccionRequest):
    """
    Paso 2: agenda la recoleccion ligada a un shipment ya generado.

    Antes de llamar a Skydropx:
      - el shipment debe existir en skydropx_envios (lo genero este sistema);
      - no debe tener ya una recoleccion agendada (409, evita recolecciones dobles);
      - el horario debe seguir dentro de la cobertura (se vuelve a consultar).
    Skydropx rechaza con 422 si la guia todavia no termina de crearse con el
    carrier ("El estado del envio no es exitoso"); ese mensaje llega tal cual.
    """
    guias = skydropx_envios.envios_de_shipment(datos.shipment_id)
    if not guias:
        raise HTTPException(status_code=404, detail="Ese envio no se genero desde este sistema.")
    existente = skydropx_envios.recoleccion_de(datos.shipment_id)
    if existente:
        raise HTTPException(
            status_code=409,
            detail=f"Ese envio ya tiene recoleccion agendada para {existente.get('fecha')} "
                   f"{existente.get('hora_inicio')}-{existente.get('hora_fin')}.",
        )

    fecha = datos.fecha.isoformat()
    try:
        cobertura = await skydropx_service.consultar_cobertura_recoleccion(datos.shipment_id)
    except SkydropxServiceError as err:
        raise HTTPException(status_code=err.status, detail=err.detalle)
    horarios = skydropx_service.extraer_horarios_recoleccion(cobertura)
    if not skydropx_service.ventana_en_cobertura(horarios, fecha, datos.hora_inicio, datos.hora_fin):
        disponibles = ", ".join(f"{h['fecha']} {h['hora_inicio']}-{h['hora_fin']}" for h in horarios) or "ninguno"
        raise HTTPException(
            status_code=422,
            detail=f"El horario {fecha} {datos.hora_inicio}-{datos.hora_fin} no esta en la cobertura "
                   f"del carrier. Disponibles: {disponibles}.",
        )

    paquetes = datos.paquetes or len(guias)
    try:
        respuesta = await skydropx_service.crear_recoleccion(
            shipment_id=datos.shipment_id,
            paquetes=paquetes,
            peso_total=datos.peso_total,
            fecha=fecha,
            hora_inicio=datos.hora_inicio,
            hora_fin=datos.hora_fin,
        )
    except SkydropxServiceError as err:
        raise HTTPException(status_code=err.status, detail=err.detalle)

    info = skydropx_service.extraer_recoleccion(respuesta)
    codigo = guias[0].get("codigo_cotizacion")
    carrier = (cobertura.get("carrier") if isinstance(cobertura, dict) else None) or guias[0].get("carrier")
    guardado = skydropx_envios.guardar_recoleccion(
        shipment_id=datos.shipment_id,
        codigo_cotizacion=codigo,
        pickup_id=info["pickup_id"],
        estatus=info["estatus"],
        confirmacion=info["confirmacion"],
        carrier=carrier,
        fecha=fecha,
        hora_inicio=datos.hora_inicio,
        hora_fin=datos.hora_fin,
        paquetes=paquetes,
        peso_total=datos.peso_total,
        usuario=datos.usuario or "sistema",
        respuesta=respuesta,
    )

    try:
        mov_reg.registrar_movimiento(
            datos.usuario or "sistema",
            f"Agendo recoleccion Skydropx {fecha} {datos.hora_inicio}-{datos.hora_fin} ({codigo or datos.shipment_id})",
            "Envios Skydropx",
        )
    except Exception as err:
        print(f"Error al registrar movimiento de recoleccion Skydropx: {err}")

    return {
        "status": "success",
        "guardado": guardado,
        "recoleccion": {
            **info,
            "shipment_id": datos.shipment_id,
            "codigo_cotizacion": codigo,
            "carrier": carrier,
            "fecha": fecha,
            "hora_inicio": datos.hora_inicio,
            "hora_fin": datos.hora_fin,
            "paquetes": paquetes,
            "peso_total": datos.peso_total,
        },
        "respuesta": respuesta,
    }


@router_webhook.post("/skydropx/webhook")
async def recibir_webhook(request: Request):
    """
    Receptor de eventos de Skydropx (cambios de estatus de guias).

    Va sin obtener_usuario_actual a proposito: quien llama es Skydropx, no un
    usuario del panel. La autenticacion es la firma HMAC-SHA512 del cuerpo crudo
    contra SKYDROPX_WEBHOOK_SECRET; sin ese secreto configurado el endpoint
    responde 503 y no acepta nada.

    El evento actualiza el estatus del envio en skydropx_envios y se agrega a la
    bitacora skydropx_envio_eventos, que es de donde el panel arma la linea de
    tiempo de cada guia.
    """
    cuerpo_crudo = await request.body()
    cabecera = skydropx_service._config()["cabecera_firma"]

    try:
        skydropx_service.verificar_firma_webhook(cuerpo_crudo, request.headers.get(cabecera))
    except SkydropxServiceError as err:
        raise HTTPException(status_code=err.status, detail=err.detalle)

    try:
        evento = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="El webhook de Skydropx no trae JSON valido.")

    datos = skydropx_service.extraer_evento_webhook(evento)

    # Guardar nunca debe tumbar la respuesta: si contestamos error, Skydropx
    # reintenta el mismo evento y se acumulan reintentos por una falla nuestra
    # de base de datos. Se responde 200 y queda el rastro en el log.
    resultado = {"aplicado": False}
    try:
        resultado = skydropx_envios.registrar_evento_webhook(
            datos,
            payload_crudo=cuerpo_crudo.decode("utf-8", errors="replace")[:4000],
        )
    except Exception as err:
        print(f"Error procesando webhook Skydropx: {err}")

    print(
        f"Webhook Skydropx: tracking={datos.get('tracking_number')} "
        f"estatus={datos.get('estatus')} aplicado={resultado.get('aplicado')} "
        f"envio_encontrado={resultado.get('envio_encontrado')}"
    )

    # Avisa en vivo al usuario que genero la guia y a los destinatarios fijos
    # (gerencia, fparra), reutilizando el mismo canal de notificaciones
    # (WebSocket /ws/notificaciones) que ya usan abonos.py, etc. Solo en
    # evento_nuevo=True: la huella en skydropx_envios evita que un reintento de
    # Skydropx (mismo tracking+estatus) duplique el aviso. Si algo aqui falla no
    # debe tumbar la respuesta al webhook.
    if resultado.get("aplicado") and resultado.get("evento_nuevo"):
        try:
            destinatarios = []
            for nombre in (resultado.get("usuario"), *USUARIOS_ENVIO_SIEMPRE):
                if not nombre:
                    continue
                empleado_id = notificaciones_service.id_de_usuario(nombre)
                if empleado_id is not None and empleado_id not in destinatarios:
                    destinatarios.append(empleado_id)

            if destinatarios:
                info_estatus = skydropx_envios.describir_estatus(datos.get("estatus"))
                referencia = (
                    resultado.get("codigo_cotizacion")
                    or datos.get("tracking_number")
                    or ""
                )
                titulo = f"Guía Skydropx: {info_estatus['estatus_texto']}"
                mensaje = f"{referencia} — {datos.get('descripcion') or info_estatus['estatus_texto']}"
                for empleado_id in destinatarios:
                    await notificaciones_service.crear_y_notificar(empleado_id, titulo, mensaje, "envio")
        except Exception as err:
            print(f"Error notificando webhook Skydropx: {err}")

    # 200 rapido: Skydropx reintenta si tarda o si responde error.
    return {"status": "success", "recibido": True, **resultado}


def _leer_id(cotizacion: Dict[str, Any]) -> Optional[str]:
    """El id puede venir en la raiz o bajo `data`, segun el endpoint que respondio."""
    if not isinstance(cotizacion, dict):
        return None
    valor = cotizacion.get("id")
    if valor is None:
        datos = cotizacion.get("data")
        if isinstance(datos, dict):
            valor = datos.get("id")
    return str(valor) if valor is not None else None


def _armar_packages(
    parcels: List[Dict[str, Any]],
    cantidad: int,
    consignment_note: Optional[str],
    package_type: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Arreglo `packages` para POST /api/v2/shipments: un objeto por bulto con
    package_number correlativo, consignment_note, package_type y el seguro
    (package_protected + declared_value). Medidas y peso no se mandan: ya
    vienen de la cotizacion del rate.

    El seguro se tiene que repetir aqui: el que va en `parcels` de la
    cotizacion no se hereda a la guia (las guias salian sin proteccion).
    Si el paquete no trae declared_value se asegura con el default; solo un
    package_protected=False explicito o declared_value=0 lo apagan.
    """
    packages = []
    for numero in range(1, max(cantidad, 1) + 1):
        parcela = parcels[numero - 1] if numero <= len(parcels) else (parcels[0] if parcels else {})
        paquete = {"package_number": numero}
        nota = parcela.get("consignment_note") or consignment_note
        tipo = parcela.get("package_type") or package_type
        if nota:
            paquete["consignment_note"] = nota
        if tipo:
            paquete["package_type"] = tipo
        valor = parcela.get("declared_value", VALOR_DECLARADO_DEFAULT)
        if parcela.get("package_protected", True) and valor and float(valor) > 0:
            paquete["package_protected"] = True
            paquete["declared_value"] = float(valor)
        packages.append(paquete)
    return packages


def _shipments_sin_tracking(guias: List[Dict[str, Any]]) -> List[str]:
    """shipment_id de las guias que todavia no traen tracking_number, sin repetir."""
    return list(dict.fromkeys(
        g["shipment_id"] for g in guias if g.get("shipment_id") and not g.get("tracking_number")
    ))


def _reemplazar_guias_de(
    guias: List[Dict[str, Any]],
    shipment_id: str,
    nuevas: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Sustituye las guias de un shipment por las de su reconsulta, conservando el
    orden y renumerando package_number. Si la reconsulta no trae nada
    reconocible, se quedan las que habia.
    """
    nuevas = [g for g in nuevas if g.get("shipment_id") == shipment_id]
    if not nuevas:
        return guias
    resultado: List[Dict[str, Any]] = []
    insertado = False
    for g in guias:
        if g.get("shipment_id") == shipment_id:
            if not insertado:
                resultado.extend(dict(n) for n in nuevas)
                insertado = True
            continue
        resultado.append(g)
    for numero, g in enumerate(resultado, start=1):
        g["package_number"] = numero
    return resultado
