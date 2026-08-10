-- Modulo Rastreo de Importaciones
-- Ejecutar contra la misma DB de api_zeutica1 (DB_NAME en .env)
-- Sin FOREIGN KEY: el usuario de DB no tiene privilegio REFERENCES.
-- Cascade de borrado se maneja a nivel app (ver routers/embarques.py).

CREATE TABLE IF NOT EXISTS embarques (
    id INT AUTO_INCREMENT PRIMARY KEY,
    numero_contenedor VARCHAR(100) NULL,
    invoice_orders VARCHAR(255) NOT NULL,
    proveedor VARCHAR(255) NULL,
    llegada_manzanillo_tentativa DATE NULL,
    fecha_llegada_real DATE NULL,
    fecha_de_recibido DATE NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS embarque_etapas (
    id INT AUTO_INCREMENT PRIMARY KEY,
    embarque_id INT NOT NULL,
    tipo ENUM('ANTICIPO_CHINA', 'LIQUIDADO_CHINA', 'HL_LIQUIDADA') NOT NULL,
    completado BOOLEAN NOT NULL DEFAULT FALSE,
    fecha_pago DATE NULL,
    monto_mxn DECIMAL(12,2) NULL,
    tipo_cambio_referencia DECIMAL(10,4) NULL,
    fecha_captura TIMESTAMP NULL,
    nota VARCHAR(255) NULL,
    UNIQUE KEY uq_embarque_etapa (embarque_id, tipo),
    KEY idx_etapa_embarque (embarque_id)
) ENGINE=InnoDB;

-- Migracion in-place para instalaciones ya existentes (columnas viejas
-- fecha_registro / tipo_cambio_usd_mxn -> fecha_pago / monto_mxn / tipo_cambio_referencia):
-- ver _migrar_columnas_embarque_etapas() en routers/embarques.py. No se hace aqui
-- con "ADD COLUMN IF NOT EXISTS" porque requiere MySQL 8.0.29+ y no todos los
-- servidores lo soportan (1064 en versiones viejas).

CREATE TABLE IF NOT EXISTS embarque_estatus (
    id INT AUTO_INCREMENT PRIMARY KEY,
    embarque_id INT NOT NULL,
    tipo ENUM('CON_FORWARDER', 'SALIO_DE_CHINA') NOT NULL,
    activo BOOLEAN NOT NULL DEFAULT FALSE,
    fecha_registro DATE NULL,
    UNIQUE KEY uq_embarque_estatus (embarque_id, tipo),
    KEY idx_estatus_embarque (embarque_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS embarque_items (
    id INT AUTO_INCREMENT PRIMARY KEY,
    embarque_id INT NOT NULL,
    sku VARCHAR(100) NOT NULL,
    qty INT NOT NULL,
    cbm DECIMAL(10,4) NULL,
    pct_contenedor DECIMAL(5,2) NULL,
    KEY idx_item_embarque (embarque_id)
) ENGINE=InnoDB;

-- Cache diaria de tipo de cambio Banxico (fallback sin Redis)
CREATE TABLE IF NOT EXISTS tipo_cambio_cache (
    fecha_consulta DATE PRIMARY KEY,
    valor DECIMAL(10,4) NOT NULL,
    fecha_dato DATE NOT NULL,
    consultado_en TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;

-- Cache de tipo de cambio por fecha especifica (referencia/auditoria de pagos
-- de etapas; un valor historico ya publicado no cambia, se cachea indefinido).
CREATE TABLE IF NOT EXISTS tipo_cambio_historico (
    fecha_solicitada DATE PRIMARY KEY,
    valor DECIMAL(10,4) NOT NULL,
    fecha_dato DATE NOT NULL,
    consultado_en TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB;
