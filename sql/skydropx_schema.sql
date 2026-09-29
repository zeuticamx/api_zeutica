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
    -- Id del paquete en Skydropx (una guia = un paquete). Con V2 la guia nace
    -- sin tracking_number, y en multipaquete varios paquetes comparten
    -- shipment_id: es la unica llave que distingue cada guia desde el inicio.
    -- El webhook la trae como data.id.
    package_id VARCHAR(100) NULL,
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
    UNIQUE KEY uq_sky_package (package_id),
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

-- Recolecciones agendadas. Una por shipment: se liga por shipment_id con
-- skydropx_envios (sin FK, mismo criterio que el resto del modulo).
CREATE TABLE IF NOT EXISTS skydropx_recolecciones (
    id INT AUTO_INCREMENT PRIMARY KEY,
    shipment_id VARCHAR(100) NOT NULL,
    codigo_cotizacion VARCHAR(100) NULL,
    pickup_id VARCHAR(100) NULL,
    estatus VARCHAR(60) NULL,
    confirmacion VARCHAR(100) NULL,
    carrier VARCHAR(100) NULL,
    fecha DATE NOT NULL,
    hora_inicio CHAR(5) NOT NULL,
    hora_fin CHAR(5) NOT NULL,
    paquetes INT NOT NULL,
    peso_total DECIMAL(10,2) NOT NULL,
    usuario VARCHAR(100) NULL,
    respuesta TEXT NULL,
    creado_en TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_sky_recoleccion_shipment (shipment_id),
    KEY idx_sky_recoleccion_codigo (codigo_cotizacion)
) ENGINE=InnoDB;

-- Catalogo propio de medidas de caja (presets del modal de envio). Skydropx no
-- tiene API para registrar cajas personalizadas, asi que viven aqui.
-- `nombre` es UNIQUE: el panel identifica el boton por su nombre.
CREATE TABLE IF NOT EXISTS skydropx_cajas (
    id INT AUTO_INCREMENT PRIMARY KEY,
    nombre VARCHAR(60) NOT NULL,
    length DECIMAL(8,2) NOT NULL,
    width DECIMAL(8,2) NOT NULL,
    height DECIMAL(8,2) NOT NULL,
    weight DECIMAL(8,2) NOT NULL,
    package_type VARCHAR(10) NOT NULL DEFAULT '4G',
    usuario VARCHAR(100) NULL,
    creado_en TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_sky_caja_nombre (nombre)
) ENGINE=InnoDB;

-- Las 4 medidas que el modal traia fijas (SKY_PRESETS). INSERT IGNORE: corre en
-- cada arranque y no duplica ni pisa lo que ya exista con ese nombre.
INSERT IGNORE INTO skydropx_cajas (nombre, length, width, height, weight, package_type) VALUES
    ('Sobre', 30, 25, 2, 0.5, '4G'),
    ('Caja chica', 25, 20, 15, 2, '4G'),
    ('Caja mediana', 40, 30, 25, 5, '4G'),
    ('Caja grande', 60, 40, 40, 12, '4G');
