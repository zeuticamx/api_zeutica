# Endpoints del modulo de envios (Skydropx Pro).
# La logica de Skydropx vive en skydropx_service.py; aqui solo va el contrato HTTP.
#
# Este router es aditivo: no lee ni escribe la base de datos del sistema, salvo la
# bitacora de movimientos al generar una guia. No comparte estado con embarques.py
# ni con ningun otro modulo.
from typing import Any, Dict, List, Optional

import mov_reg
import skydropx_service
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from skydropx_service import SkydropxServiceError

router = APIRouter(tags=["skydropx-envios"], responses={404: {"Mensaje": "No encontrado"}})

# Router aparte para el webhook: lo llama Skydropx, no el panel, asi que no puede
# ir detras de obtener_usuario_actual. Se autentica con la firma HMAC del cuerpo.
router_webhook = APIRouter(tags=["skydropx-webhook"], responses={404: {"Mensaje": "No encontrado"}})


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
    reference: Optional[str] = Field(default=None, description="Referencia para el repartidor")


class Paquete(BaseModel):
    """Medidas en centimetros y peso en kilogramos, que es lo que espera Skydropx."""
    model_config = ConfigDict(extra="allow")

    length: float = Field(gt=0, description="Largo en cm")
    width: float = Field(gt=0, description="Ancho en cm")
    height: float = Field(gt=0, description="Alto en cm")
    weight: float = Field(gt=0, description="Peso en kg")


class CotizacionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    address_from: Direccion
    address_to: Direccion
    parcels: List[Paquete] = Field(min_length=1)
    # Opcionales del contrato de Skydropx (ej. requested_carriers) van aqui.
    extras: Optional[Dict[str, Any]] = None
    # Si es True, se reconsulta la cotizacion hasta que lleguen tarifas.
    esperar_tarifas: bool = True


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


def _a_dict(modelo: Optional[BaseModel]) -> Optional[Dict[str, Any]]:
    """Quita los None para no mandarle a Skydropx campos vacios que rechaza en validacion."""
    if modelo is None:
        return None
    return modelo.model_dump(exclude_none=True)


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
    try:
        cotizacion = await skydropx_service.crear_cotizacion(
            address_from=datos.address_from.model_dump(exclude_none=True),
            address_to=datos.address_to.model_dump(exclude_none=True),
            parcels=[p.model_dump(exclude_none=True) for p in datos.parcels],
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
    Genera la guia a partir de un rate_id.

    OJO: en ambiente de produccion esto contrata el envio con el carrier y se
    cobra. No hay cancelacion desde este endpoint.
    """
    try:
        envio = await skydropx_service.crear_envio(
            rate_id=datos.rate_id,
            address_from=_a_dict(datos.address_from),
            address_to=_a_dict(datos.address_to),
            parcels=[p.model_dump(exclude_none=True) for p in datos.parcels] if datos.parcels else None,
            extras=datos.extras,
        )
    except SkydropxServiceError as err:
        raise HTTPException(status_code=err.status, detail=err.detalle)

    # Bitacora. Si el registro falla no se tumba la respuesta: la guia ya se genero
    # y ocultarla con un 500 haria que el usuario la generara de nuevo (y pagara doble).
    try:
        referencia = datos.referencia or _leer_tracking(envio) or datos.rate_id
        mov_reg.registrar_movimiento(
            datos.usuario or "sistema",
            f"Genero guia Skydropx ({referencia})",
            "Envios Skydropx",
        )
    except Exception as err:
        print(f"Error al registrar movimiento de guia Skydropx: {err}")

    return {"status": "success", "envio": envio}


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


@router_webhook.post("/skydropx/webhook")
async def recibir_webhook(request: Request):
    """
    Receptor de eventos de Skydropx (cambios de estatus de guias).

    Va sin obtener_usuario_actual a proposito: quien llama es Skydropx, no un
    usuario del panel. La autenticacion es la firma HMAC-SHA512 del cuerpo crudo
    contra SKYDROPX_WEBHOOK_SECRET; sin ese secreto configurado el endpoint
    responde 503 y no acepta nada.

    Hoy solo valida y deja rastro en log. Cuando se decida que hacer con el evento
    (actualizar una tabla, notificar por WebSocket), el enganche va aqui abajo.
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

    tipo = evento.get("type") or evento.get("event") if isinstance(evento, dict) else None
    print(f"Webhook Skydropx recibido: tipo={tipo} cuerpo={evento}")

    # 200 rapido: Skydropx reintenta si tarda o si responde error.
    return {"status": "success", "recibido": True}


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


def _leer_tracking(envio: Dict[str, Any]) -> Optional[str]:
    """Numero de rastreo para la bitacora. Best effort: si no aparece, se usa el rate_id."""
    if not isinstance(envio, dict):
        return None
    for candidato in (envio, envio.get("data") if isinstance(envio.get("data"), dict) else {},
                      envio.get("shipment") if isinstance(envio.get("shipment"), dict) else {}):
        if not isinstance(candidato, dict):
            continue
        valor = candidato.get("tracking_number")
        if valor:
            return str(valor)
        atributos = candidato.get("attributes")
        if isinstance(atributos, dict) and atributos.get("tracking_number"):
            return str(atributos["tracking_number"])
    return None
