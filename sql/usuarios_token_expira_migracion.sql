-- Vigencia de las sesiones. La API (permisos.asegurar_columna_token_expira) la agrega sola al
-- arrancar; este script es para correrlo a mano si se prefiere.
-- Los tokens emitidos antes quedan con NULL = vencidos: cada usuario inicia sesion una vez.
ALTER TABLE usuarios ADD COLUMN token_expira DATETIME NULL;
