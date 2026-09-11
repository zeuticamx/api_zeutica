-- Modulo de Envios (Skydropx Pro)
-- Ejecutar contra la misma DB de api_zeutica1 (DB_NAME en .env)
-- Sin FOREIGN KEY: el usuario de DB no tiene privilegio REFERENCES
-- (mismo criterio que sql/embarques_schema.sql).

-- Una fila por guia generada. La llave natural es tracking_number, que es lo
-- unico que trae el webhook de Skydropx para identificar el envio.
--
-- tracking_number es UNIQUE pero acepta NULL: MySQL permite varios NULL en un
-- indice unico, asi que una guia que todavia no tiene numero de rastreo no
-- bloquea a las demas. El webhook y la generacion de guia hacen UPSERT sobre
-- esta llave, de modo que no importa cual de los dos llegue primero.
CREATE TABLE IF NOT EXISTS skydropx_envios (
    id INT AUTO_INCREMENT PRIMARY KEY,
    codigo_cotizacion VARCHAR(100) NULL,
    tracking_number VARCHAR(100) NULL,
    shipment_id VARCHAR(100) NULL,
    carrier VARCHAR(100) NULL,
    servicio VARCHAR(150) NULL,
    costo DECIMAL(10,2) NULL,
    etiqueta_url TEXT NULL,
    -- PDF del detalle de la orden (packing slip / remision). Skydropx lo
    -- devuelve como order_detail_url junto a label_url al crear el envio.
    orden_detalle_url TEXT NULL,
    tracking_url TEXT NULL,
    estatus VARCHAR(60) NOT NULL DEFAULT 'created',
    estatus_descripcion VARCHAR(255) NULL,
    usuario VARCHAR(100) NULL,
    creado_en TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    actualizado_en TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_sky_tracking (tracking_number),
    KEY idx_sky_codigo (codigo_cotizacion),
    KEY idx_sky_shipment (shipment_id)
) ENGINE=InnoDB;

-- Bitacora de eventos del webhook: alimenta la linea de tiempo del panel.
--
-- `huella` es sha1(tracking|estatus|descripcion) con indice unico. Skydropx
-- reintenta el webhook cuando no recibe 200 a tiempo, y sin esto la linea de
-- tiempo se llenaria de eventos repetidos. Se inserta con INSERT IGNORE.
CREATE TABLE IF NOT EXISTS skydropx_envio_eventos (
    id INT AUTO_INCREMENT PRIMARY KEY,
    tracking_number VARCHAR(100) NULL,
    shipment_id VARCHAR(100) NULL,
    estatus VARCHAR(60) NULL,
    descripcion VARCHAR(255) NULL,
    huella CHAR(40) NOT NULL,
    payload TEXT NULL,
    recibido_en TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_sky_evento (huella),
    KEY idx_sky_evento_tracking (tracking_number)
) ENGINE=InnoDB;
