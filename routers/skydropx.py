# Endpoints del modulo de envios (Skydropx Pro).
# La logica de Skydropx vive en skydropx_service.py; aqui solo va el contrato HTTP.
#
# Este router es aditivo: no lee ni escribe la base de datos del sistema, salvo la
# bitacora de movimientos al generar una guia. No comparte estado con embarques.py
# ni con ningun otro modulo.
from typing import Any, Dict, List, Optional

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
    a asegurar en MXN (default: 2000).
    """
    model_config = ConfigDict(extra="allow")

    length: float = Field(gt=0, description="Largo en cm")
    width: float = Field(gt=0, description="Ancho en cm")
    height: float = Field(gt=0, description="Alto en cm")
    weight: float = Field(gt=0, description="Peso en kg")
    consignment_note: Optional[str] = Field(default=None, description="Contenido del paquete (carta porte). Requerido para generar guia.")
    package_type: Optional[str] = Field(default=None, description="Codigo de embalaje del catalogo de Skydropx. Requerido para generar guia.")
    package_protected: Optional[bool] = Field(default=True, description="Habilitar seguro en el paquete")
    declared_value: Optional[float] = Field(default=2500.0, description="Monto a asegurar en MXN")


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
    # Mismo campo y mismo criterio que en CotizacionRequest: con 1 (default) el
    # flujo es identico al de siempre (un solo parcel, sin `packages`). Con mas
    # de 1, se arma el arreglo `packages` con `package_number` correlativo --
    # ver crear_envio() en este archivo y en skydropx_service.py.
    cantidad_bultos: int = Field(default=1, ge=1, le=20, description="Cantidad de bultos del envio (multipaquete)")


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
    Genera la guia a partir de un rate_id.

    OJO: en ambiente de produccion esto contrata el envio con el carrier y se
    cobra. No hay cancelacion desde este endpoint.

    Multipaquete (cantidad_bultos > 1): el envio se manda con `packages` en
    vez de `parcels` (ver skydropx_service.crear_envio) y la respuesta puede
    traer varias guias (una por bulto). El flujo de 1 bulto (default) usa
    exactamente el mismo camino que antes de este feature -- no pasa por
    _leer_paquetes_envio ni por `packages`, para no arriesgar el comportamiento
    ya confirmado contra el sandbox.
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
    multipaquete = cantidad_bultos > 1

    packages_dicts = None
    if multipaquete:
        packages_dicts = [{**p, "package_number": i + 1} for i, p in enumerate(parcels_dicts)]
        parcels_dicts = None  # multipaquete va por `packages`, no por `parcels`
    elif not parcels_dicts:
        parcels_dicts = None

    try:
        envio = await skydropx_service.crear_envio(
            rate_id=datos.rate_id,
            address_from=_a_dict(datos.address_from),
            address_to=_a_dict(datos.address_to),
            parcels=parcels_dicts,
            packages=packages_dicts,
            consignment_note=consignment_note,
            package_type=package_type,
            extras=datos.extras,
        )
    except SkydropxServiceError as err:
        raise HTTPException(status_code=err.status, detail=err.detalle)

    shipment_id = _leer_id(envio)

    def _paquete_unico(env: Dict[str, Any]) -> List[Dict[str, Any]]:
        # Mismo dato y mismas funciones que el flujo original de un solo
        # paquete -- no se toca para no cambiar su comportamiento.
        return [{
            "package_number": 1,
            "tracking_number": _leer_tracking(env),
            "etiqueta_url": _leer_campo_envio(env, "label_url", "label", "pdf_url"),
            "shipment_id": _leer_id(env) or shipment_id,
        }]

    def _completos(pqs: List[Dict[str, Any]]) -> bool:
        return len(pqs) >= cantidad_bultos and all(p.get("tracking_number") for p in pqs)

    paquetes = _leer_paquetes_envio(envio) if multipaquete else _paquete_unico(envio)
    if not paquetes:
        paquetes = _paquete_unico(envio)  # red de seguridad si Skydropx no trajo `included`/`data` reconocibles

    # El POST inicial casi siempre contesta antes de que Skydropx termine de
    # generar la guia con el carrier: tracking_number y label_url llegan vacios
    # (confirmado en su propia documentacion del webhook, que muestra
    # "label_url": "" en el primer evento). Se reconsulta un par de veces -mismo
    # patron que esperar_tarifas() para cotizaciones- para no devolverle al panel
    # una guia sin numero de rastreo cuando Skydropx la resuelve en pocos segundos.
    # Si sigue sin llegar, la guia ya se genero (y se cobro) de todos modos: el
    # webhook la completara despues.
    if not _completos(paquetes) and shipment_id:
        for _ in range(3):
            await asyncio.sleep(1.5)
            try:
                envio = await skydropx_service.obtener_envio(shipment_id)
            except SkydropxServiceError:
                break  # no tumbar la respuesta: la guia ya se genero y se cobro
            paquetes = _leer_paquetes_envio(envio) if multipaquete else _paquete_unico(envio)
            if not paquetes:
                paquetes = _paquete_unico(envio)
            if _completos(paquetes):
                break

    tracking = paquetes[0].get("tracking_number") if paquetes else None
    codigo = datos.codigo_cotizacion or datos.referencia
    carrier = _leer_carrier(envio)
    orden_detalle_url = _leer_campo_envio(envio, "order_detail_url", "order_detail")
    tracking_url_general = _leer_campo_envio(envio, "tracking_url_provider", "tracking_url")

    # Un renglon en skydropx_envios por bulto generado. La tabla ya soportaba
    # varias guias por cotizacion (para cuando el webhook llegaba antes de que
    # guardaramos la guia), asi que no hizo falta tocar el esquema para esto.
    # costo/servicio son los de la tarifa elegida, que cubre el envio completo
    # (Skydropx no los desglosa por bulto): se repiten igual en cada renglon. Si
    # algun reporte llega a sumar esta columna por cotizacion, debe dedupear por
    # shipment_id en vez de sumarla tal cual.
    #
    # Nunca lanza: la guia ya se contrato y se cobro, y fallar aqui haria que el
    # usuario la generara de nuevo.
    guardado = False
    for paquete_resp in paquetes:
        try:
            if skydropx_envios.guardar_envio(
                codigo_cotizacion=codigo,
                tracking_number=paquete_resp.get("tracking_number"),
                shipment_id=paquete_resp.get("shipment_id") or shipment_id,
                carrier=carrier,
                servicio=datos.servicio,
                costo=datos.costo,
                etiqueta_url=paquete_resp.get("etiqueta_url"),
                # PDF de remision / packing slip. Viene junto a label_url, a
                # nivel general del envio (no por bulto).
                orden_detalle_url=orden_detalle_url,
                tracking_url=tracking_url_general,
                usuario=datos.usuario or "sistema",
            ):
                guardado = True
        except Exception as err:
            print(f"Error al guardar envio Skydropx en BD: {err}")

    # Bitacora. Si el registro falla no se tumba la respuesta: la guia ya se genero
    # y ocultarla con un 500 haria que el usuario la generara de nuevo (y pagara doble).
    try:
        referencia = codigo or tracking or datos.rate_id
        sufijo = f" multipaquete x{cantidad_bultos}" if multipaquete else ""
        mov_reg.registrar_movimiento(
            datos.usuario or "sistema",
            f"Genero guia Skydropx{sufijo} ({referencia})",
            "Envios Skydropx",
        )
    except Exception as err:
        print(f"Error al registrar movimiento de guia Skydropx: {err}")

    return {"status": "success", "envio": envio, "guardado": guardado, "paquetes": paquetes}


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


def _leer_campo_envio(envio: Dict[str, Any], *claves: str) -> Optional[str]:
    """
    Busca un campo en la respuesta del envio sin casarse con la forma exacta:
    puede venir en la raiz, bajo `data`, bajo `shipment`, dentro de `attributes`
    de cualquiera de esos, o como recurso relacionado en `included`.
    """
    if not isinstance(envio, dict):
        return None

    candidatos = [envio]
    for llave in ("data", "shipment"):
        anidado = envio.get(llave)
        if isinstance(anidado, dict):
            candidatos.append(anidado)

    for candidato in list(candidatos):
        atributos = candidato.get("attributes")
        if isinstance(atributos, dict):
            candidatos.append(atributos)

    incluidos = envio.get("included")
    if isinstance(incluidos, list):
        for item in incluidos:
            if isinstance(item, dict):
                candidatos.append(item)
                if isinstance(item.get("attributes"), dict):
                    candidatos.append(item["attributes"])

    for clave in claves:
        for candidato in candidatos:
            valor = candidato.get(clave)
            if valor:
                return str(valor)
    return None


def _leer_tracking(envio: Dict[str, Any]) -> Optional[str]:
    """Numero de rastreo de la guia recien generada."""
    return _leer_campo_envio(envio, "tracking_number")


def _leer_paquetes_envio(envio: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extrae una entrada por bulto de la respuesta de POST/GET shipments, para
    envios MULTIPAQUETE donde cada paquete trae su propio tracking_number y
    label_url. Solo se usa cuando cantidad_bultos > 1 -- el flujo de 1 bulto
    sigue usando _leer_tracking/_leer_campo_envio tal cual estaban.

    No confirmado contra esta cuenta (el sandbox no se probo todavia con mas
    de un bulto). Formas que Skydropx documenta segun la version del endpoint:
      - V1: el shipment va en `data`/raiz y los paquetes como recursos
        relacionados tipo "packages" en `included` (JSON:API), igual que en
        extraer_evento_webhook() de skydropx_service.py.
      - V2: `data` llega como arreglo, un recurso por paquete/guia generada.
    Si ninguna de las dos formas aparece, se devuelve una lista vacia y el
    llamador cae de vuelta a _paquete_unico() como red de seguridad.
    """
    if not isinstance(envio, dict):
        return []

    paquetes: List[Dict[str, Any]] = []
    shipment_id = _leer_id(envio)

    incluidos = envio.get("included")
    if isinstance(incluidos, list):
        for item in incluidos:
            if not isinstance(item, dict) or not str(item.get("type", "")).startswith("package"):
                continue
            atributos = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
            paquetes.append({
                "package_number": atributos.get("package_number") or item.get("package_number"),
                "tracking_number": atributos.get("tracking_number") or item.get("tracking_number"),
                "etiqueta_url": atributos.get("label_url") or item.get("label_url"),
                "shipment_id": shipment_id,
            })

    if not paquetes and isinstance(envio.get("data"), list):
        for item in envio["data"]:
            if not isinstance(item, dict):
                continue
            atributos = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
            paquetes.append({
                "package_number": atributos.get("package_number") or item.get("package_number"),
                "tracking_number": atributos.get("tracking_number") or item.get("tracking_number"),
                "etiqueta_url": atributos.get("label_url") or item.get("label_url"),
                "shipment_id": item.get("id") or shipment_id,
            })

    return paquetes


def _leer_carrier(envio: Dict[str, Any]) -> Optional[str]:
    """Carrier que quedo en la guia. Hace falta para poder rastrearla despues."""
    return _leer_campo_envio(envio, "provider", "carrier", "carrier_name")
