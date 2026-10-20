-- Parámetros de reabastecimiento por SKU (página Punto de reorden). Sin FK con productos:
-- un SKU sin fila usa los valores por defecto de reorden_logica.py (LT 3 meses, Z 1.65).
-- La crea routers/reorden.py::crear_tabla_reorden() en el lifespan.
CREATE TABLE IF NOT EXISTS reorden_parametros (
    sku VARCHAR(100) NOT NULL PRIMARY KEY,
    lead_time_meses DECIMAL(5,2) NOT NULL,
    nivel_servicio_z DECIMAL(4,2) NOT NULL,
    actualizado_por VARCHAR(100) NULL,
    actualizado_en TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;
