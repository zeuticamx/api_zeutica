"""Domicilio estructurado del cliente y del prospecto (lógica pura, sin SQL de negocio).

Campos: calle, numero, interior (opcional), colonia, cp, municipio, estado.
Espejo de panel_gerencia/src/domicilio-logica.js (el backend manda; el panel solo avisa antes).
El catálogo de estados y municipios (SEPOMEX) vive en catalogo_domicilio.json, el mismo que
incrusta el panel: si se actualiza uno hay que actualizar el otro.

Reglas:
- Domicilio vacío por completo: válido (clientes de mostrador, prospectos sin datos).
- Con cualquiera de CAMPOS_ESTRUCTURA capturado: calle, número, colonia, CP, estado y municipio
  son obligatorios (el interior no) y el municipio debe pertenecer al estado.
- El CP solo (dato fiscal que ya existía) no dispara esa exigencia, pero si viene debe tener 5 dígitos.
- `direccion` (texto de una línea) se compone con los campos nuevos para los consumidores que solo la
  leen (PDF de cotización, Skydropx); si no hay campos nuevos se conserva el texto libre anterior.
"""
import json
import os
import re
import unicodedata
from typing import Any, Dict, List, Optional

_RUTA_CATALOGO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalogo_domicilio.json")
with open(_RUTA_CATALOGO, encoding="utf-8") as _f:
    CATALOGO: Dict[str, List[str]] = json.load(_f)

# Si se captura alguno de estos, el domicilio debe estar completo. El CP no cuenta.
CAMPOS_ESTRUCTURA = ("calle", "numero", "interior", "colonia", "municipio", "estado")
CAMPOS_DOMICILIO = ("calle", "numero", "interior", "colonia", "cp", "municipio", "estado")
LIMITES = {"calle": 150, "numero": 20, "interior": 20, "colonia": 120}

# Columnas nuevas (clientes y prospectos). `cp` ya existía como INT y se convierte a texto.
COLUMNAS_DOMICILIO = {
    "calle": "VARCHAR(150) NULL",
    "numero": "VARCHAR(20) NULL",
    "interior": "VARCHAR(20) NULL",
    "colonia": "VARCHAR(120) NULL",
    "municipio": "VARCHAR(100) NULL",
    "estado": "VARCHAR(60) NULL",
}
_TIPOS_NUMERICOS = {"tinyint", "smallint", "mediumint", "int", "bigint", "decimal", "numeric"}


def _texto(valor: Any) -> str:
    return " ".join(str(valor if valor is not None else "").split())


def normalizar(valor: Any) -> str:
    """Minúsculas, sin acentos y sin espacios de más (para comparar con el catálogo)."""
    base = unicodedata.normalize("NFD", _texto(valor))
    return "".join(c for c in base if unicodedata.category(c) != "Mn").lower()


_ESTADOS = {normalizar(e): e for e in CATALOGO}
_MUNICIPIOS = {e: {normalizar(m): m for m in ms} for e, ms in CATALOGO.items()}


def estado_canonico(estado: Any) -> Optional[str]:
    return _ESTADOS.get(normalizar(estado))


def municipio_canonico(estado: Any, municipio: Any) -> Optional[str]:
    e = estado_canonico(estado)
    return _MUNICIPIOS[e].get(normalizar(municipio)) if e else None


def cp_a_texto(valor: Any) -> Optional[str]:
    """CP como texto de 5 dígitos. None, '', 0 y '0' son «sin CP»; un entero viejo se rellena con ceros
    (un INT pierde el cero inicial: 01000 se guardaba como 1000)."""
    if valor is None or isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        n = int(valor)
        if n <= 0:
            return None
        return str(n).zfill(5)
    s = _texto(valor)
    return None if s in ("", "0") else s


def validar_domicilio(datos: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Valida y normaliza el domicilio. Devuelve calle..estado, cp y `direccion` (compuesta, o None si no hay
    campos estructurados). Lanza ValueError con el mensaje para el usuario."""
    cp = cp_a_texto(datos.get("cp"))
    if cp is not None and not re.fullmatch(r"\d{5}", cp):
        raise ValueError("El código postal debe tener 5 dígitos")

    limpio = {k: (_texto(datos.get(k)) or None) for k in CAMPOS_ESTRUCTURA}
    salida: Dict[str, Optional[str]] = {**limpio, "cp": cp, "direccion": None}
    if not any(limpio.values()):
        return salida

    etiquetas = (("calle", "calle"), ("numero", "número"), ("colonia", "colonia"),
                 ("cp", "código postal"), ("estado", "estado"), ("municipio", "municipio"))
    valores = {**limpio, "cp": cp}
    faltan = [etq for campo, etq in etiquetas if not valores.get(campo)]
    if faltan:
        raise ValueError(f"Completa el domicilio: falta {', '.join(faltan)}")
    for campo, tope in LIMITES.items():
        if limpio[campo] and len(limpio[campo]) > tope:
            raise ValueError(f"El campo {campo} no puede pasar de {tope} caracteres")

    estado = estado_canonico(limpio["estado"])
    if not estado:
        raise ValueError(f'El estado "{limpio["estado"]}" no es válido')
    municipio = municipio_canonico(estado, limpio["municipio"])
    if not municipio:
        raise ValueError(f'El municipio "{limpio["municipio"]}" no pertenece a {estado}')

    salida["estado"] = estado
    salida["municipio"] = municipio
    salida["direccion"] = armar_direccion(salida)
    return salida


def armar_direccion(datos: Dict[str, Any]) -> str:
    """«Av. Juárez 100 Int. 4, Col. Centro, CP 44100, Guadalajara, Jalisco»."""
    calle_num = " ".join(p for p in (_texto(datos.get("calle")), _texto(datos.get("numero"))) if p)
    interior = _texto(datos.get("interior"))
    cp = cp_a_texto(datos.get("cp"))
    colonia = _texto(datos.get("colonia"))
    partes = [
        f"{calle_num} Int. {interior}" if interior else calle_num,
        f"Col. {colonia}" if colonia else "",
        f"CP {cp}" if cp else "",
        _texto(datos.get("municipio")),
        _texto(datos.get("estado")),
    ]
    return ", ".join(p for p in partes if p)


def sentencias_cp_a_texto(tabla: str) -> List[str]:
    """SQL para pasar `cp` de INT a texto: 0 → NULL y los de 4 dígitos recuperan su cero inicial.
    `tabla` es una constante interna (clientes | prospectos), nunca un dato del usuario."""
    return [
        f"ALTER TABLE {tabla} MODIFY cp VARCHAR(10) NULL",
        f"UPDATE {tabla} SET cp = NULL WHERE cp IN ('0', '')",
        f"UPDATE {tabla} SET cp = LPAD(cp, 5, '0') WHERE cp REGEXP '^[0-9]{{1,4}}$'",
    ]


def cp_es_numerico(tipo_columna: Optional[str]) -> bool:
    """True si el DATA_TYPE de information_schema indica que `cp` sigue siendo numérico."""
    return str(tipo_columna or "").lower() in _TIPOS_NUMERICOS


def asegurar_columnas_domicilio(cursor, tabla: str) -> None:
    """Agrega las columnas del domicilio si faltan y convierte `cp` a texto si aún es numérico.
    Idempotente. `tabla` es una constante interna (clientes | prospectos)."""
    cursor.execute(
        "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
        (tabla,),
    )
    existentes = {str(f[0]).lower(): f[1] for f in cursor.fetchall()}
    for columna, definicion in COLUMNAS_DOMICILIO.items():
        if columna not in existentes:
            cursor.execute(f"ALTER TABLE {tabla} ADD COLUMN {columna} {definicion}")
    if "cp" in existentes and cp_es_numerico(existentes["cp"]):
        for sentencia in sentencias_cp_a_texto(tabla):
            cursor.execute(sentencia)
