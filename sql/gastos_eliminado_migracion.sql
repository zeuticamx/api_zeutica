-- Borrado lógico de gastos operativos. La API (gastos.asegurar_columnas_eliminado)
-- lo aplica sola al arrancar; este script es para correrlo a mano si se prefiere.
ALTER TABLE gastos
  ADD COLUMN eliminado TINYINT(1) NOT NULL DEFAULT 0,
  ADD COLUMN eliminado_por VARCHAR(100) NULL,
  ADD COLUMN fecha_eliminado DATETIME NULL;
