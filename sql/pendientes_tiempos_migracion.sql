-- Cronómetro de tareas de pendientes (hoy solo `fparra`) y revisión de gerencia («Intercambiado»).
-- Referencia: la crea sola routers/pendientes.asegurar_columnas_tiempos() en el lifespan (solo agrega lo que falte).
ALTER TABLE pendientes ADD COLUMN inicio_atencion DATETIME NULL;
ALTER TABLE pendientes ADD COLUMN fin_atencion DATETIME NULL;
ALTER TABLE pendientes ADD COLUMN intercambiado TINYINT(1) NOT NULL DEFAULT 0;
ALTER TABLE pendientes ADD COLUMN intercambiado_por VARCHAR(100) NULL;
ALTER TABLE pendientes ADD COLUMN fecha_intercambiado DATETIME NULL;
