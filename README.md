# api_zeutica1

Backend de cálculos e integraciones (FastAPI) del sistema Zeutica. Ver
[CLAUDE.md](CLAUDE.MD) para arquitectura y convenciones completas.

## Setup rápido

```bash
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
copy .env.example .env       # y llenar los valores reales
uvicorn main:app --reload --port 8000
```

## Tests

```bash
pytest -v
```

## Módulo Comisiones (`/comisiones`)

Router: [`routers/comisiones.py`](routers/comisiones.py) · Cálculo puro:
[`comisiones_calc.py`](comisiones_calc.py) · Esquema:
[`sql/comisiones_schema.sql`](sql/comisiones_schema.sql) (se crea solo en el lifespan).

- **Fórmula** por partida: `(precio con IVA × cantidad ÷ 1.16) × porcentaje ÷ 100`, en `Decimal`,
  redondeando solo el resultado final a centavos.
- **Matriz** `comisiones_config` (vendedor × SKU). `sku = '*'` es la tasa base del vendedor; sin
  porcentaje ni tasa base la partida comisiona 0% y se marca `sin_tasa`. Solo gerencia la edita.
- **Sin comisión para nadie:** Cleanest, Mercado Libre y Amazon. La lista vive en
  `comisiones_calc.PLATAFORMAS_EXCLUIDAS` / `COMPRADORES_EXCLUIDOS`.
- **Captura:** `routers/ventas.py` llama a `registrar_comisiones_seguro` tras confirmar la venta
  (un fallo ahí nunca la tumba). El porcentaje queda congelado en `comisiones_ventas`.
  `POST /comisiones/recalcular` (gerencia) rellena ventas anteriores sin tocar el CRM.
- **Venta ↔ seguimiento:** si el cliente (por nombre exacto) tiene un seguimiento abierto del
  vendedor, se enlaza al más reciente en `venta_seguimiento`, se cierra y el cliente pasa a `ganado`.
  También se puede vincular a mano (`POST /comisiones/vinculos`) por seguimiento, por **folio de
  cotización** (`codigo_cotizacion`) o por ambos. Si la venta se registra cargando una cotización en
  el formulario, el folio se liga solo (`cotizacion` en `POST /ventas/registrar`). Un vendedor solo
  liga cotizaciones y seguimientos suyos; ligar un folio no marca la cotización como vendida.

## Módulo Rastreo de Importaciones (`/embarques`)

Router: [`routers/embarques.py`](routers/embarques.py) · Esquema BD:
[`sql/embarques_schema.sql`](sql/embarques_schema.sql) (ejecutar contra la
misma BD MySQL de `DB_NAME`).

Cada embarque tiene 3 etapas (`ANTICIPO_CHINA`, `LIQUIDADO_CHINA`,
`HL_LIQUIDADA`) y 2 estatus (`CON_FORWARDER`, `SALIO_DE_CHINA`).

Al completar una etapa (`PATCH /embarques/{id}/etapas/{tipo}`) el usuario
captura `fecha_pago` y `monto_mxn` **directo en pesos, sin conversión de
USD**. El backend solo agrega `tipo_cambio_referencia`: el tipo de cambio
FIX de esa fecha de pago, guardado puramente como dato de auditoría — nunca
se usa para calcular el monto. Si falta `fecha_pago` o `monto_mxn` con
`completado=true`, responde `400`. Editar el monto no vuelve a consultar
Banxico; editar la fecha de pago sí (`tipo_cambio_referencia` se recalcula
para la nueva fecha). `fecha_captura` registra cuándo se guardó en el
sistema y no se toca al desmarcar una etapa.

### Costeo de importación (Landed Cost)

Router: [`routers/landed_cost.py`](routers/landed_cost.py) · lógica pura:
[`landed_cost_logica.py`](landed_cost_logica.py) · pruebas:
[`tests/test_landed_cost.py`](tests/test_landed_cost.py). Reproduce la plantilla
«PRECIOS PRODUCTOS FELIPE FER.xlsx» (`Tabla6102`). **Todo es solo gerencia.**

- **Entradas por item** (columnas de `embarque_items` que agrega la migración):
  `invoice` (número, debe ser una de `embarque_invoices`), `fecha_compra`,
  `paquete_venta`, `unit_price_fob_usd`, `ctns`, `arancel_porc` (fracción: 0.35 =
  35 %) y `tipo_cambio_china` (manual por item, no sale de Banxico). `sku`, `qty`
  y `cbm` son los del item.
- **Entradas del embarque** (`embarque_liquidacion`, 1:1): `total_cbm_manual`
  (vacío = suma de los CBM de los items), `no_hoja_liquidacion`,
  `tipo_cambio_log`, `incrementales_usd`, `igi_total_embarque`,
  `iva_total_aduana`, `total_hoja_liquidacion` (MXN) y los informativos
  `total_consolidado_porc`, `total_pagado_calculo_excel`, `pagado_china_mxn`.
- **Fórmulas** (Decimal; `null` + advertencia si falta un dato o se divide entre 0):
  - `total_paquetes_venta = qty / paquete_venta`
  - `costo_paq_venta_china_mxn_sin_igi = fob × qty × tc_china / total_paquetes`
    (la columna O del Excel, «total pagado material China en MXN», es en realidad
    USD por paquete: aquí se llama `costo_paq_venta_china_usd`)
  - `porc_cbm = cbm / total_cbm`; valor aduana USD = `incrementales × porc_cbm + fob × qty`
  - `igi_item = valor aduana × tc_log × arancel`; `iva_item = (valor aduana × tc_log + igi_item) × 0.16`
  - envío = `(hoja − igi_total − iva_total) × porc_cbm / total_paquetes`, más IGI+IVA por paquete
  - `costo_por_paquete_c_iva_bodega`, `costo_por_paquete_sin_iva_bodega`, `zeutica_paga_total`
- **Regla de la plantilla:** la suma de `comprobacion` del embarque debe ser igual
  a `total_hoja_liquidacion` → `resumen.cuadra` (tolerancia $1). Solo cuadra si
  los % de CBM suman 1 y el IGI/IVA estimado coincide con el del pedimento.
- **Cierre:** completar la etapa `HL_LIQUIDADA` guarda la foto del cálculo en
  `embarque_costeo_cierre` (en la misma transacción del `PATCH`); desde ahí el
  `GET` devuelve la foto y editar da `409`. Desmarcar la etapa la borra y reabre.
- Rutas: `GET /embarques/{id}/landed-cost`, `PUT /embarques/{id}/landed-cost/liquidacion`,
  `PATCH /embarques/{id}/items/{item_id}/costos` (solo lo enviado),
  `POST /landed-cost/simular` (calcula sin guardar). Es informativo: no toca
  `productos.costo_total`.

### Configurar `BANXICO_API_TOKEN`

El tipo de cambio se obtiene de la serie **SF43718** (FIX USD/MXN) del
Sistema de Información Económica de Banxico, vía
[`banxico_service.py`](banxico_service.py). Hay dos consultas distintas,
cada una con su propia caché para no golpear la API en cada request:

- `obtener_tipo_cambio_dia()` — tipo de cambio de hoy, cache diaria en
  `tipo_cambio_cache` (usada por `GET /tipo-cambio/hoy`).
- `obtener_tipo_cambio_fecha_especifica(fecha)` — tipo de cambio de una
  fecha pasada (o el dato disponible más reciente antes de ella), cache
  indefinida en `tipo_cambio_historico` ya que un valor histórico publicado
  no cambia. Es la que usa el `PATCH` de etapas y `GET /tipo-cambio/{fecha}`.

1. Entra a <https://www.banxico.org.mx/SieAPIRest/service/v1/> y
   regístrate (gratis) con tu correo.
2. Banxico te manda el token por correo en un par de minutos.
3. Pégalo en tu `.env` local (nunca se versiona):

   ```env
   BANXICO_API_TOKEN=tu_token_aqui
   ```

4. Sin este token, cualquier intento de completar una etapa o consultar
   `GET /tipo-cambio/hoy` o `GET /tipo-cambio/{fecha}` responde `502` con
   `"No se pudo obtener tipo de cambio...: Falta BANXICO_API_TOKEN en el .env"`.

El resto de variables de entorno (MySQL, PostgreSQL) están documentadas en
[`.env.example`](.env.example).
