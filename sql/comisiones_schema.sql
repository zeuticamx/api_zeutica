-- Comisiones por SKU para vendedores. Sin FK, igual que el CRM. Lo ejecuta
-- routers/comisiones.py:crear_tablas_comisiones() en el lifespan (idempotente).

-- Matriz de porcentajes: una fila por vendedor y SKU. sku = '*' es la tasa base
-- del vendedor para los SKU sin porcentaje propio.
CREATE TABLE IF NOT EXISTS comisiones_config (
  id             INT           NOT NULL AUTO_INCREMENT PRIMARY KEY,
  vendedor       VARCHAR(50)   NOT NULL,
  sku            VARCHAR(60)   NOT NULL,
  porcentaje     DECIMAL(5,2)  NOT NULL,
  actualizado_por VARCHAR(50)  NULL,
  actualizado    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uq_comisiones_config (vendedor, sku)
);

-- Una fila por partida de una venta directa elegible. El porcentaje queda
-- congelado al momento de la venta: cambiar la matriz no altera lo ya vendido.
CREATE TABLE IF NOT EXISTS comisiones_ventas (
  id           INT            NOT NULL AUTO_INCREMENT PRIMARY KEY,
  id_ventas    VARCHAR(40)    NOT NULL,
  sku          VARCHAR(60)    NOT NULL,
  producto     VARCHAR(255)   NULL,
  cantidad     INT            NOT NULL,
  vendedor     VARCHAR(50)    NOT NULL,
  comprador    VARCHAR(255)   NULL,
  plataforma   VARCHAR(60)    NULL,
  fecha_venta  DATE           NOT NULL,
  precio_neto  DECIMAL(12,2)  NOT NULL,
  base_sin_iva DECIMAL(12,2)  NOT NULL,
  porcentaje   DECIMAL(5,2)   NOT NULL,
  comision     DECIMAL(12,2)  NOT NULL,
  origen_tasa  VARCHAR(10)    NOT NULL,
  creado       DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE KEY uq_comisiones_venta_sku (id_ventas, sku),
  INDEX idx_comisiones_vendedor_fecha (vendedor, fecha_venta)
);

-- Vínculo de la venta con el CRM (una fila por venta): seguimiento y/o folio de cotización.
-- modo (del seguimiento): 'auto' (cliente con seguimiento abierto del vendedor) | 'manual'.
CREATE TABLE IF NOT EXISTS venta_seguimiento (
  id_ventas      VARCHAR(40)  NOT NULL PRIMARY KEY,
  cliente_id     INT          NULL,
  seguimiento_id INT          NULL,
  cotizacion     VARCHAR(40)  NULL,
  vendedor       VARCHAR(50)  NOT NULL,
  modo           VARCHAR(10)  NOT NULL,
  creado         DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_venta_seguimiento_seg (seguimiento_id),
  INDEX idx_venta_seguimiento_cot (cotizacion)
);
