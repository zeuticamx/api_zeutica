-- Historial del Análisis de Competencia ML (también sirve de cupo semanal y de caché de 24 h).
-- La crea routers/competencia.py::crear_tabla_competencia() en el lifespan. Fechas en hora de México
-- (las escribe Python, no el servidor MySQL). `resultado` es el JSON completo que ve el panel.
-- `origen_cache_id` = análisis reutilizado de otro (no gasta cupo ni llamadas).
CREATE TABLE IF NOT EXISTS competencia_analisis (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    usuario VARCHAR(100) NOT NULL,
    url VARCHAR(600) NOT NULL,
    enfoque VARCHAR(20) NOT NULL,
    sku VARCHAR(100) NULL,
    margen_objetivo DECIMAL(5,2) NULL,
    comision_pct DECIMAL(5,2) NULL,
    estado VARCHAR(12) NOT NULL DEFAULT 'en_proceso',
    costo_base DECIMAL(14,4) NULL,
    precio_sugerido DECIMAL(14,2) NULL,
    precio_promedio DECIMAL(14,2) NULL,
    resultado LONGTEXT NULL,
    truncado TINYINT(1) NOT NULL DEFAULT 0,
    origen_cache_id BIGINT NULL,
    error_origen VARCHAR(20) NULL,
    error_detalle VARCHAR(300) NULL,
    creado_en DATETIME NOT NULL,
    finalizado_en DATETIME NULL,
    KEY idx_comp_usuario (usuario, creado_en),
    KEY idx_comp_url (url(190), enfoque)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
