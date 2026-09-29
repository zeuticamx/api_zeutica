-- Índices de ventasRegistro para reportes y validación de duplicados.
-- Correr a mano una sola vez (no lo aplica el lifespan).
--
-- Índices existentes al 2026-09-29 (SHOW INDEX FROM ventasRegistro):
--   PRIMARY (id), uq_meli_venta_sku (meli_key), uq_amazon_venta_sku (amazon_key),
--   id_ventas (id_ventas, NO único).
-- id_ventas no puede ser UNIQUE: una venta de varios productos tiene una fila por
-- partida con el mismo id. El backend valida el duplicado antes de insertar
-- (routers/ventas.py: existe_id_venta) usando el índice id_ventas existente.

-- Reportes: GET /ventas/{f1}/{f2} filtra por rango de fecha_registro.
CREATE INDEX idx_ventasregistro_fecha_registro ON ventasRegistro (fecha_registro);
