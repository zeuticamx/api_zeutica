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
