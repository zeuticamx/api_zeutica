-- Prospectos: perfiles con los mismos datos que un cliente + etapa comercial propia.
-- Flujo: nuevo -> contactado (con medio_contacto: whatsapp|correo|llamada) -> cotizacion -> convertido.
-- `convertido` solo lo pone POST /prospectos/{id}/convertir, nunca a mano.
-- Sin FK, igual que CRM. Lo ejecuta routers/prospectos.py:crear_tablas_prospectos() en el lifespan.
CREATE TABLE IF NOT EXISTS prospectos (
  id              INT           NOT NULL AUTO_INCREMENT PRIMARY KEY,
  nombre          VARCHAR(255)  NOT NULL,
  email           VARCHAR(255)  NULL,
  empresa         VARCHAR(255)  NULL,
  contacto        VARCHAR(255)  NULL,
  telefono        BIGINT        NULL DEFAULT 0,
  direccion       VARCHAR(500)  NULL,
  rfc             VARCHAR(20)   NULL,
  cp              INT           NULL DEFAULT 0,
  regimen         VARCHAR(100)  NULL,
  usocfdi         VARCHAR(100)  NULL,
  frecuencia      VARCHAR(100)  NULL,
  credito         TINYINT(1)    NOT NULL DEFAULT 0,
  monto_credito   INT           NULL DEFAULT 0,
  dias_credito    INT           NULL DEFAULT 0,
  etapa           VARCHAR(20)   NOT NULL DEFAULT 'nuevo',
  medio_contacto  VARCHAR(12)   NULL,
  vendedor        VARCHAR(50)   NOT NULL,
  registrado_por  VARCHAR(50)   NULL,
  cliente_id      INT           NULL,
  eliminado       TINYINT(1)    NOT NULL DEFAULT 0,
  creado          DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  etapa_actualizada DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_prospectos_vendedor_etapa (vendedor, etapa),
  INDEX idx_prospectos_nombre (nombre)
);

-- Bitácora de seguimiento por prospecto (espejo mínimo de crm_interacciones).
CREATE TABLE IF NOT EXISTS prospectos_seguimientos (
  id                  INT           NOT NULL AUTO_INCREMENT PRIMARY KEY,
  prospecto_id        INT           NOT NULL,
  vendedor            VARCHAR(50)   NOT NULL,
  tipo                VARCHAR(12)   NOT NULL,
  fecha               DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resultado           VARCHAR(20)   NULL,
  notas               TEXT          NULL,
  proxima_accion      VARCHAR(255)  NULL,
  proxima_fecha       DATE          NULL,
  seguimiento_cerrado TINYINT(1)    NOT NULL DEFAULT 0,
  creado              DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_prosp_seg_prospecto (prospecto_id),
  INDEX idx_prosp_seg_vendedor_fecha (vendedor, fecha)
);
