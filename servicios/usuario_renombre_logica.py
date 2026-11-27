"""Lógica pura para renombrar una cuenta de `usuarios` (sin SQL de negocio).

El nombre de usuario se guarda como TEXTO en muchas tablas (pendientes, CRM, ventas, bitácora…), casi todas
sin llave foránea. Renombrar la cuenta exige arrastrar ese texto: las columnas se descubren en
`information_schema` por el nombre de la columna (`columna_guarda_usuario`) y solo las de tipo texto.
"""
import re

PATRON_USUARIO = re.compile(r"^[A-Za-z0-9._-]{3,40}$")

# Columnas que guardan «quién» (nombre de cuenta) como texto.
_NOMBRES_EXACTOS = {"usuario", "vendedor", "autor", "nombre_usuario"}
_PREFIJOS = ("usuario_", "vendedor_")
_SUFIJOS = ("_por",)  # editado_por, eliminado_por, cancelada_por, registrado_por…

TIPOS_TEXTO = ("varchar", "char")

# Tablas que no se tocan con el barrido: `usuarios` se actualiza aparte (es la cuenta misma).
TABLAS_EXCLUIDAS = {"usuarios"}


def columna_guarda_usuario(columna: str) -> bool:
    c = str(columna or "").strip().lower()
    if not c:
        return False
    return c in _NOMBRES_EXACTOS or c.startswith(_PREFIJOS) or c.endswith(_SUFIJOS)


def citar(identificador: str) -> str:
    """Identificador de MySQL entre backticks (los nombres salen de information_schema, no del cliente)."""
    return "`" + str(identificador).replace("`", "``") + "`"


def validar_nuevo_nombre(actual: str, nuevo: str, es_gerencia) -> str:
    """Nombre nuevo normalizado, o ValueError con el motivo. `es_gerencia(nombre)` dice si es de USUARIOS_GERENCIA."""
    nombre = (nuevo or "").strip()
    if not PATRON_USUARIO.match(nombre):
        raise ValueError("El usuario debe tener de 3 a 40 caracteres: letras, números, punto, guion o guion bajo")
    if es_gerencia(actual):
        raise ValueError("Las cuentas de gerencia se definen en USUARIOS_GERENCIA del servidor: no se renombran desde aquí")
    if es_gerencia(nombre):
        raise ValueError("Ese nombre está reservado para un usuario de gerencia")
    return nombre


def resumir(afectados: list) -> dict:
    """{ total, tablas } de [{tabla, columna, filas}] contando solo lo que tiene filas."""
    con_filas = [a for a in afectados if a.get("filas")]
    return {"total": sum(a["filas"] for a in con_filas), "tablas": len({a["tabla"] for a in con_filas})}
