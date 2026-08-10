-- Correr UNA VEZ con un usuario que tenga privilegio ALTER (root/admin),
-- no con el usuario de la app (fparra_pruebas no tiene ALTER).
-- Idempotente-manual: si una columna ya existe, comenta esa linea y reintenta.

ALTER TABLE embarque_etapas ADD COLUMN fecha_pago DATE NULL AFTER completado;
ALTER TABLE embarque_etapas ADD COLUMN monto_mxn DECIMAL(12,2) NULL AFTER fecha_pago;
ALTER TABLE embarque_etapas ADD COLUMN tipo_cambio_referencia DECIMAL(10,4) NULL AFTER monto_mxn;
ALTER TABLE embarque_etapas ADD COLUMN fecha_captura TIMESTAMP NULL AFTER tipo_cambio_referencia;
ALTER TABLE embarque_etapas DROP COLUMN fecha_registro;
ALTER TABLE embarque_etapas DROP COLUMN tipo_cambio_usd_mxn;
