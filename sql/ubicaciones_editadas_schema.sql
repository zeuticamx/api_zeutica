-- Bitácora de ediciones/movimientos de ubicación: la escribe mov_reg.registrar_edicionUbi
-- y la lee GET /ubicaciones/registro/{sku} («Ver registro» en el panel).
-- mov_reg.asegurar_tabla_ubicaciones_editadas() la crea sola si falta; este script es la
-- referencia / creación manual. En producción ya existe: IF NOT EXISTS no la modifica.
CREATE TABLE IF NOT EXISTS ubicaciones_editadas (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  sku VARCHAR(100) NOT NULL,
  warehouse_id VARCHAR(100) NOT NULL,
  cama INT NULL,
  cantidad INT NULL,
  fecha DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_ubi_edit_sku (sku)
);
