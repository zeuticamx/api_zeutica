-- Plano de la bodega: anaqueles en planta + ID de cada celda.
-- routers/anaqueles.py crea estas tablas solo (asegurar_tablas_anaqueles);
-- este script es la referencia / creación manual.
-- El codigo de la celda ES el warehouse_id de stock_ubicacion: no hay FK,
-- la tarjeta se liga a la celda comparando LOWER(TRIM(warehouse_id)).

CREATE TABLE IF NOT EXISTS anaqueles (
  id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
  codigo VARCHAR(50) NOT NULL,
  x INT NOT NULL DEFAULT 0,            -- columna en el plano (0..23)
  y INT NOT NULL DEFAULT 0,            -- fila en el plano (0 = frente)
  columnas INT NOT NULL,               -- 1..10
  niveles INT NOT NULL,                -- 1..4
  orientacion CHAR(1) NOT NULL DEFAULT 'H',  -- H: columnas a lo ancho del plano; V: hacia el fondo
  actualizado DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);

-- Si `anaqueles` ya existía sin orientación (asegurar_tablas_anaqueles lo hace solo):
-- ALTER TABLE anaqueles ADD COLUMN orientacion CHAR(1) NOT NULL DEFAULT 'H' AFTER niveles;

CREATE TABLE IF NOT EXISTS anaqueles_celdas (
  anaquel_id INT NOT NULL,
  nivel INT NOT NULL,                  -- 0 = abajo
  columna INT NOT NULL,                -- 0 = izquierda
  codigo VARCHAR(100) NOT NULL,        -- = stock_ubicacion.warehouse_id
  PRIMARY KEY (anaquel_id, nivel, columna),
  UNIQUE KEY uq_anaqueles_celdas_codigo (codigo)
);

-- Una sola fila: versión para control de concurrencia (409 si cambió).
CREATE TABLE IF NOT EXISTS bodega_disposicion (
  id TINYINT NOT NULL PRIMARY KEY,
  version INT NOT NULL DEFAULT 0,
  usuario VARCHAR(50) NULL,
  actualizado DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
);
INSERT IGNORE INTO bodega_disposicion (id, version) VALUES (1, 0);
