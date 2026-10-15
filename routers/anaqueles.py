# fichero api de anaqueles: disposición física de la bodega (plano + IDs de celda)
# El ID (codigo) de cada celda ES el warehouse_id de las tarjetas de stock_ubicacion:
# la posición de una tarjeta se deduce cruzando ambos (sin mayúsculas ni espacios
# en los extremos). Varias tarjetas, de cualquier SKU, pueden compartir celda.
# planear_disposicion() es ESPEJO de planearDisposicion() en
# panel_gerencia/src/anaqueles-logica.js: si cambia una regla, cambia en los dos.
import os
import mysql.connector, mov_reg
from dotenv import load_dotenv
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

router = APIRouter(tags=["/anaqueles"], responses={404: {"Mensaje": "No encontrado"}})

load_dotenv()  # Carga de credenciales .env


def get_db_connection():
    return mysql.connector.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        database=os.getenv("DB_NAME")
    )


# Límites del editor (mismos valores en anaqueles-logica.js).
MAX_COLUMNAS = 10
MAX_NIVELES = 4
PLANO_ANCHO = 24
PLANO_ALTO = 12


class CeldaSchema(BaseModel):
    nivel: int = Field(ge=0)
    columna: int = Field(ge=0)
    codigo: str = Field(max_length=100)


class AnaquelSchema(BaseModel):
    id: Optional[int] = None  # None = anaquel nuevo
    codigo: str = Field(min_length=1, max_length=50)
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    columnas: int = Field(ge=1, le=MAX_COLUMNAS)
    niveles: int = Field(ge=1, le=MAX_NIVELES)
    # H: las columnas avanzan a lo ancho del plano; V: hacia el fondo.
    orientacion: str = Field("H", pattern="^[HV]$")
    celdas: list[CeldaSchema] = []


class DisposicionSchema(BaseModel):
    """Disposición completa: lo que no venga se borra (sus tarjetas quedan sin posición).
    `version` es la leída en GET /anaqueles; si otro usuario guardó antes, 409."""
    version: int = Field(ge=0)
    usuario: str = Field(min_length=1)
    anaqueles: list[AnaquelSchema]


def normalizar(codigo) -> str:
    return str(codigo or "").strip().lower()


def huella(a):
    """(ancho, fondo) que ocupa el anaquel en el plano, en celdas de rejilla."""
    return (1, a["columnas"]) if a.get("orientacion") == "V" else (a["columnas"], 1)


def se_enciman(a, b) -> bool:
    aw, ah = huella(a)
    bw, bh = huella(b)
    return a["x"] < b["x"] + bw and b["x"] < a["x"] + aw and a["y"] < b["y"] + bh and b["y"] < a["y"] + ah


def planear_disposicion(actual, nueva, ocupacion):
    """
    Valida la disposición nueva contra la actual sin tocar la BD.
    actual/nueva: [{id, codigo, x, y, columnas, niveles, celdas: [{nivel, columna, codigo}]}]
    ocupacion: {codigo normalizado: número de tarjetas}
    Devuelve {errores, renombres: [{de, a, tarjetas}], desasignadas: [{codigo, tarjetas}]}.
    Reglas (las cajas no se mueven al editar el plano, así que manda la posición):
    - cambiar el ID de una celda arrastra a sus tarjetas al ID nuevo (renombre);
    - una celda con tarjetas no se puede quitar de un anaquel que sigue (bloquear);
    - si se borra el anaquel entero, sus tarjetas quedan sin posición (desasignar),
      salvo que su ID se reuse en otra celda: ahí aparecerían en un lugar falso.
    """
    errores = []
    ids_actuales = {a["id"] for a in actual}
    nombres = set()
    for a in nueva:
        nombre = str(a.get("codigo") or "").strip()
        if not nombre:
            errores.append("Cada anaquel necesita un código")
        elif normalizar(nombre) in nombres:
            errores.append(f"El código de anaquel {nombre} está repetido")
        nombres.add(normalizar(nombre))
        if a.get("id") is not None and a["id"] not in ids_actuales:
            errores.append(f"El anaquel #{a['id']} ya no existe; recarga la disposición")
        if not (1 <= a["columnas"] <= MAX_COLUMNAS and 1 <= a["niveles"] <= MAX_NIVELES):
            errores.append(f"El anaquel {nombre} excede {MAX_COLUMNAS} columnas × {MAX_NIVELES} niveles")
        ancho, fondo = huella(a)
        if a["x"] < 0 or a["y"] < 0 or a["x"] + ancho > PLANO_ANCHO or a["y"] + fondo > PLANO_ALTO:
            errores.append(f"El anaquel {nombre} sale del plano")

    # En planta cada anaquel ocupa un rectángulo (horizontal: columnas × 1; vertical: 1 × columnas).
    for i, a in enumerate(nueva):
        for b in nueva[i + 1:]:
            if se_enciman(a, b):
                errores.append(f"Los anaqueles {a['codigo']} y {b['codigo']} se enciman")

    vistos = set()
    por_posicion = {}
    for a in nueva:
        posiciones = set()
        for c in a.get("celdas") or []:
            cod = str(c.get("codigo") or "").strip()
            if not cod:
                continue
            if not (0 <= c["nivel"] < a["niveles"] and 0 <= c["columna"] < a["columnas"]):
                errores.append(f"La celda {cod} queda fuera del anaquel {a['codigo']}")
            if (c["nivel"], c["columna"]) in posiciones:
                errores.append(f"La celda {cod} está duplicada en el anaquel {a['codigo']}")
            posiciones.add((c["nivel"], c["columna"]))
            if normalizar(cod) in vistos:
                errores.append(f"El ID de celda {cod} está repetido")
            vistos.add(normalizar(cod))
            if a.get("id") is not None:
                por_posicion[(a["id"], c["nivel"], c["columna"])] = cod

    ids_nuevos = {a.get("id") for a in nueva if a.get("id") is not None}
    renombres, desasignadas = [], []
    for a in actual:
        sigue = a["id"] in ids_nuevos
        for c in a.get("celdas") or []:
            viejo = str(c.get("codigo") or "").strip()
            clave = normalizar(viejo)
            if not clave:
                continue
            tarjetas = ocupacion.get(clave, 0)
            nuevo = por_posicion.get((a["id"], c["nivel"], c["columna"]))
            if nuevo:
                if normalizar(nuevo) != clave:
                    renombres.append({"de": viejo, "a": nuevo, "tarjetas": tarjetas})
            elif not tarjetas:
                continue
            elif sigue:
                errores.append(f"La celda {viejo} tiene {tarjetas} tarjeta(s): muévelas antes de quitarla")
            elif clave in vistos:
                errores.append(f"Las tarjetas de {viejo} aparecerían en otra celda con el mismo ID: cámbialo o muévelas antes")
            else:
                desasignadas.append({"codigo": viejo, "tarjetas": tarjetas})

    return {"errores": errores, "renombres": renombres, "desasignadas": desasignadas}


def asegurar_tablas_anaqueles(cursor):
    """Tablas del plano de bodega. Idempotente (CREATE IF NOT EXISTS)."""
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS anaqueles (
          id INT NOT NULL AUTO_INCREMENT PRIMARY KEY,
          codigo VARCHAR(50) NOT NULL,
          x INT NOT NULL DEFAULT 0,
          y INT NOT NULL DEFAULT 0,
          columnas INT NOT NULL,
          niveles INT NOT NULL,
          orientacion CHAR(1) NOT NULL DEFAULT 'H',
          actualizado DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        )"""
    )
    # Bases que ya crearon la tabla antes de existir la orientación.
    cursor.execute(
        "SELECT COUNT(*) AS n FROM information_schema.COLUMNS"
        " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'anaqueles' AND COLUMN_NAME = 'orientacion'"
    )
    if not (cursor.fetchone() or {}).get("n"):
        cursor.execute("ALTER TABLE anaqueles ADD COLUMN orientacion CHAR(1) NOT NULL DEFAULT 'H' AFTER niveles")
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS anaqueles_celdas (
          anaquel_id INT NOT NULL,
          nivel INT NOT NULL,
          columna INT NOT NULL,
          codigo VARCHAR(100) NOT NULL,
          PRIMARY KEY (anaquel_id, nivel, columna),
          UNIQUE KEY uq_anaqueles_celdas_codigo (codigo)
        )"""
    )
    # Una sola fila con la versión de la disposición (control de concurrencia).
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS bodega_disposicion (
          id TINYINT NOT NULL PRIMARY KEY,
          version INT NOT NULL DEFAULT 0,
          usuario VARCHAR(50) NULL,
          actualizado DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        )"""
    )
    cursor.execute("INSERT IGNORE INTO bodega_disposicion (id, version) VALUES (1, 0)")


def leer_anaqueles(cursor):
    cursor.execute("SELECT id, codigo, x, y, columnas, niveles, orientacion FROM anaqueles ORDER BY y, x, id")
    anaqueles = [dict(a, orientacion=a.get("orientacion") or "H", celdas=[]) for a in cursor.fetchall()]
    por_id = {a["id"]: a for a in anaqueles}
    cursor.execute("SELECT anaquel_id, nivel, columna, codigo FROM anaqueles_celdas ORDER BY anaquel_id, nivel, columna")
    for c in cursor.fetchall():
        if c["anaquel_id"] in por_id:
            por_id[c["anaquel_id"]]["celdas"].append(
                {"nivel": c["nivel"], "columna": c["columna"], "codigo": c["codigo"]})
    return anaqueles


def leer_ocupacion(cursor):
    """{warehouse_id normalizado: {tarjetas, skus}} de TODOS los SKUs."""
    cursor.execute(
        "SELECT LOWER(TRIM(warehouse_id)) AS clave, COUNT(*) AS tarjetas, COUNT(DISTINCT sku) AS skus"
        " FROM stock_ubicacion GROUP BY LOWER(TRIM(warehouse_id))"
    )
    return {f["clave"]: {"tarjetas": f["tarjetas"], "skus": f["skus"]}
            for f in cursor.fetchall() if f["clave"]}


@router.get("/anaqueles")
async def consultar_disposicion():
    """Plano de la bodega + ocupación por celda (todas las tarjetas, todos los SKUs)."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        asegurar_tablas_anaqueles(cursor)
        conn.commit()
        cursor.execute("SELECT version FROM bodega_disposicion WHERE id = 1")
        fila = cursor.fetchone()
        return {
            "version": fila["version"] if fila else 0,
            "anaqueles": leer_anaqueles(cursor),
            "ocupacion": leer_ocupacion(cursor),
        }

    except mysql.connector.Error as err:
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")

    finally:
        cursor.close()
        conn.close()


@router.put("/anaqueles/disposicion")
async def guardar_disposicion(datos: DisposicionSchema):
    """
    Reemplaza la disposición completa en una transacción. Si cambia el ID de
    una celda, las tarjetas de esa celda (de todos los SKUs) cambian su
    warehouse_id al nuevo. Exige la versión leída: 409 si alguien guardó antes.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    try:
        asegurar_tablas_anaqueles(cursor)
        conn.commit()
        cursor.execute("SELECT version FROM bodega_disposicion WHERE id = 1 FOR UPDATE")
        fila = cursor.fetchone()
        version_actual = fila["version"] if fila else 0
        if datos.version != version_actual:
            conn.rollback()
            raise HTTPException(status_code=409, detail="Otro usuario cambió la disposición de la bodega; recárgala antes de guardar")

        actual = leer_anaqueles(cursor)
        ocupacion = {k: v["tarjetas"] for k, v in leer_ocupacion(cursor).items()}
        nueva = [a.model_dump() for a in datos.anaqueles]
        plan = planear_disposicion(actual, nueva, ocupacion)
        if plan["errores"]:
            conn.rollback()
            raise HTTPException(status_code=422, detail=" · ".join(plan["errores"]))

        ids_nuevos = {a["id"] for a in nueva if a["id"] is not None}
        for a in actual:
            if a["id"] not in ids_nuevos:
                cursor.execute("DELETE FROM anaqueles WHERE id = %s", (a["id"],))
        # Las celdas se reescriben completas: así el UNIQUE de codigo no choca a medio guardar.
        cursor.execute("DELETE FROM anaqueles_celdas")
        for a in nueva:
            if a["id"] is None:
                cursor.execute(
                    "INSERT INTO anaqueles (codigo, x, y, columnas, niveles, orientacion) VALUES (%s, %s, %s, %s, %s, %s)",
                    (a["codigo"].strip(), a["x"], a["y"], a["columnas"], a["niveles"], a["orientacion"]),
                )
                anaquel_id = cursor.lastrowid
            else:
                cursor.execute(
                    "UPDATE anaqueles SET codigo = %s, x = %s, y = %s, columnas = %s, niveles = %s, orientacion = %s WHERE id = %s",
                    (a["codigo"].strip(), a["x"], a["y"], a["columnas"], a["niveles"], a["orientacion"], a["id"]),
                )
                anaquel_id = a["id"]
            for c in a["celdas"]:
                cod = c["codigo"].strip()
                if cod:
                    cursor.execute(
                        "INSERT INTO anaqueles_celdas (anaquel_id, nivel, columna, codigo) VALUES (%s, %s, %s, %s)",
                        (anaquel_id, c["nivel"], c["columna"], cod),
                    )

        # Renombres en dos fases para que un intercambio (A↔B) no mezcle tarjetas.
        temporales = []
        for i, r in enumerate(plan["renombres"]):
            tmp = f"~ren{i}~"
            cursor.execute(
                "UPDATE stock_ubicacion SET warehouse_id = %s WHERE LOWER(TRIM(warehouse_id)) = %s",
                (tmp, normalizar(r["de"])),
            )
            temporales.append((tmp, r["a"]))
        for tmp, final in temporales:
            cursor.execute("UPDATE stock_ubicacion SET warehouse_id = %s WHERE warehouse_id = %s", (final, tmp))

        cursor.execute(
            "UPDATE bodega_disposicion SET version = %s, usuario = %s WHERE id = 1",
            (version_actual + 1, datos.usuario),
        )
        conn.commit()

        detalle = f"Guardó la disposición de bodega ({len(nueva)} anaqueles)"
        con_tarjetas = [r for r in plan["renombres"] if r["tarjetas"]]
        if con_tarjetas:
            detalle += "; renombró " + ", ".join(f"{r['de']}→{r['a']}" for r in con_tarjetas)
        if plan["desasignadas"]:
            detalle += "; sin posición: " + ", ".join(d["codigo"] for d in plan["desasignadas"])
        mov_reg.registrar_movimiento(datos.usuario, detalle, "Productos")
        return {
            "mensaje": "Disposición guardada",
            "version": version_actual + 1,
            "renombres": plan["renombres"],
            "desasignadas": plan["desasignadas"],
        }

    except mysql.connector.Error as err:
        conn.rollback()
        raise HTTPException(status_code=500, detail=f"Error de base de datos: {err}")

    finally:
        cursor.close()
        conn.close()
