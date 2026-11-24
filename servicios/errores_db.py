# Errores internos hacia el cliente: el detalle técnico (tablas, columnas, fragmentos de SQL)
# se queda en el log del servidor y a la respuesta solo va un mensaje genérico.
import logging

logger = logging.getLogger("zeutica.errores")


def detalle_error_db(err, contexto: str = "Error de base de datos") -> str:
    """Registra `err` completo en el log y devuelve `contexto` para usarlo como `detail`.

        except mysql.connector.Error as err:
            raise HTTPException(status_code=500, detail=detalle_error_db(err))
    """
    logger.error("%s: %s", contexto, err, exc_info=err if isinstance(err, BaseException) else False)
    return contexto
