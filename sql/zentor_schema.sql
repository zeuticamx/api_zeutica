-- Reglas del Motor Zentor (página Mercado Ads): una sola fila (id = 1) con los umbrales,
-- el piloto automático y el estado del motor. `version` evita que dos personas se pisen (409).
-- La crea routers/mercado_ads.py::crear_tabla_zentor() en el lifespan.
CREATE TABLE IF NOT EXISTS zentor_reglas (
    id TINYINT NOT NULL PRIMARY KEY,
    target_acos DECIMAL(5,2) NOT NULL DEFAULT 20.00,
    max_clicks_without_sale INT NOT NULL DEFAULT 30,
    min_spend_threshold DECIMAL(12,2) NOT NULL DEFAULT 100.00,
    auto_pilot TINYINT(1) NOT NULL DEFAULT 0,
    estado_motor VARCHAR(10) NOT NULL DEFAULT 'activo',
    version INT NOT NULL DEFAULT 1,
    actualizado_por VARCHAR(100) NULL,
    actualizado_en TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;

INSERT IGNORE INTO zentor_reglas (id) VALUES (1);
