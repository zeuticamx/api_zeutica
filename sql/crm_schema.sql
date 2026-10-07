-- CRM ligero: cartera (dueño + etapa comercial), bitácora de interacciones e
-- historial de etapas. Sin FK, igual que embarques. Lo ejecuta
-- routers/crm.py:crear_tablas_crm() en el lifespan (idempotente).
-- Etapas: contacto_inicial | en_seguimiento | cotizado | ganado | perdido
-- Tipos:  llamada | correo | whatsapp | reunion

-- Estado comercial y vendedor dueño de cada cliente (1:1 con clientes.id).
-- Un cliente sin fila aquí aún no entra al CRM: su dueño provisional es
-- clientes.usuario (quien lo dio de alta).
CREATE TABLE IF NOT EXISTS crm_cartera (
  cliente_id        INT          NOT NULL PRIMARY KEY,
  vendedor          VARCHAR(50)  NOT NULL,
  etapa             VARCHAR(20)  NOT NULL DEFAULT 'contacto_inicial',
  motivo_perdida    VARCHAR(255) NULL,
  etapa_actualizada DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  creado            DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_crm_cartera_vendedor_etapa (vendedor, etapa)
);

-- Bitácora: una fila por llamada / correo / WhatsApp / reunión.
-- proxima_fecha + seguimiento_cerrado = 0 es lo que aparece en "Mis seguimientos".
CREATE TABLE IF NOT EXISTS crm_interacciones (
  id                  INT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
  cliente_id          INT          NOT NULL,
  vendedor            VARCHAR(50)  NOT NULL,
  tipo                VARCHAR(12)  NOT NULL,
  fecha               DATETIME     NOT NULL,
  resultado           VARCHAR(20)  NULL,
  notas               TEXT         NULL,
  proxima_accion      VARCHAR(255) NULL,
  proxima_fecha       DATE         NULL,
  seguimiento_cerrado TINYINT(1)   NOT NULL DEFAULT 0,
  eliminado           TINYINT(1)   NOT NULL DEFAULT 0,
  creado              DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_crm_int_vendedor_fecha (vendedor, fecha),
  INDEX idx_crm_int_cliente_fecha (cliente_id, fecha),
  INDEX idx_crm_int_pendientes (seguimiento_cerrado, proxima_fecha)
);

-- Cada cambio de etapa, para medir el avance del embudo por periodo.
CREATE TABLE IF NOT EXISTS crm_etapas_historial (
  id             INT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
  cliente_id     INT          NOT NULL,
  etapa_anterior VARCHAR(20)  NULL,
  etapa_nueva    VARCHAR(20)  NOT NULL,
  usuario        VARCHAR(50)  NOT NULL,
  fecha          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_crm_hist_fecha (fecha),
  INDEX idx_crm_hist_cliente (cliente_id)
);

-- Agenda / calendario: citas y tareas con hora, con o sin cliente.
-- cliente_id NULL = tarea interna (dueño = vendedor, sin validación de cartera).
-- cliente_id con valor = se valida contra la cartera con puede_gestionar().
-- origen_seguimiento_id liga opcional con crm_interacciones (convertir seguimiento en cita).
-- Tipos: cita | tarea | llamada | reunion | whatsapp | correo
-- Estados: pendiente | hecho | cancelado
CREATE TABLE IF NOT EXISTS crm_eventos (
  id                     INT          NOT NULL AUTO_INCREMENT PRIMARY KEY,
  cliente_id             INT          NULL,
  vendedor               VARCHAR(50)  NOT NULL,
  tipo                   VARCHAR(12)  NOT NULL,
  titulo                 VARCHAR(255) NOT NULL,
  descripcion            TEXT         NULL,
  inicio                 DATETIME     NOT NULL,
  fin                    DATETIME     NULL,
  todo_dia               TINYINT(1)   NOT NULL DEFAULT 0,
  estado                 VARCHAR(12)  NOT NULL DEFAULT 'pendiente',
  origen_seguimiento_id  INT          NULL,
  eliminado              TINYINT(1)   NOT NULL DEFAULT 0,
  creado                 DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_crm_ev_vendedor_inicio (vendedor, inicio),
  INDEX idx_crm_ev_cliente (cliente_id),
  INDEX idx_crm_ev_estado_inicio (estado, inicio)
);
