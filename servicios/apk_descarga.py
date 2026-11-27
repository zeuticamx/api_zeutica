# Descarga del APK de la app móvil desde S3 (bucket privado) con URL prefirmada.
#
# Credenciales: la cadena por defecto de boto3. En producción salen del rol IAM de la instancia
# EC2 (`ec2-app`) vía IMDS; si AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY están en el entorno,
# esas ganan. Para que un contenedor Docker alcance IMDSv2, el EC2 necesita hop limit 2.
#
# Firmar la URL es un cálculo local (no conecta a S3); `head_object` sí consulta S3 para
# confirmar que el archivo existe y devolver tamaño y fecha antes de mandar al usuario.
import logging
import os
import posixpath
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, PartialCredentialsError

logger = logging.getLogger("zeutica.apk")

REGION_DEFECTO = "us-east-2"
BUCKET_DEFECTO = "bucket-prueban8n"
KEY_DEFECTO = "app-zeutica.apk"
NOMBRE_DEFECTO = "app-zeutica.apk"
EXPIRA_SEGUNDOS = 300
CONTENT_TYPE_APK = "application/vnd.android.package-archive"


class ApkNoConfigurado(Exception):
    """No hay credenciales de AWS (ni variables ni rol alcanzable)."""


class ApkNoEncontrado(Exception):
    """El objeto no existe en el bucket."""


class ApkError(Exception):
    """Cualquier otra falla de S3 / boto3."""


def configuracion() -> dict:
    region = (os.getenv("AWS_REGION") or "").strip() or REGION_DEFECTO
    bucket = (os.getenv("S3_APK_BUCKET") or "").strip() or BUCKET_DEFECTO
    key = (os.getenv("S3_APK_KEY") or "").strip().lstrip("/") or KEY_DEFECTO
    return {"region": region, "bucket": bucket, "key": key, "filename": nombre_archivo(key)}


def nombre_archivo(key: str) -> str:
    """Nombre del archivo descargado = última parte del key (decidido), sin comillas ni saltos
    que romperían la cabecera Content-Disposition."""
    base = posixpath.basename(str(key or "").strip())
    limpio = "".join(c for c in base if c not in '"\\\r\n' and c.isprintable()).strip()
    return limpio or NOMBRE_DEFECTO


@lru_cache(maxsize=4)
def _cliente(region: str):
    # s3v4 forzado (us-east-2 solo acepta SigV4). Endpoint regional explícito: sin él boto3 firma
    # contra s3.amazonaws.com (global) y S3 puede redirigir a otro host, que invalida la firma.
    # Timeouts cortos: el panel espera la respuesta.
    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "virtual"},
            connect_timeout=5,
            read_timeout=10,
            retries={"max_attempts": 2, "mode": "standard"},
        ),
    )


def generar_url_descarga() -> dict:
    cfg = configuracion()
    try:
        s3 = _cliente(cfg["region"])
        meta = s3.head_object(Bucket=cfg["bucket"], Key=cfg["key"])
        url = s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": cfg["bucket"],
                "Key": cfg["key"],
                "ResponseContentType": CONTENT_TYPE_APK,
                "ResponseContentDisposition": f'attachment; filename="{cfg["filename"]}"',
            },
            ExpiresIn=EXPIRA_SEGUNDOS,
        )
    except (NoCredentialsError, PartialCredentialsError) as err:
        logger.error(
            "APK: sin credenciales de AWS (rol del EC2 inalcanzable desde el contenedor —¿hop limit 2?— "
            "o faltan AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY): %s", err,
        )
        raise ApkNoConfigurado() from err
    except ClientError as err:
        codigo = str(err.response.get("Error", {}).get("Code", ""))
        if codigo in ("404", "NoSuchKey", "NotFound"):
            logger.warning("APK: no existe s3://%s/%s", cfg["bucket"], cfg["key"])
            raise ApkNoEncontrado() from err
        # Ojo: sin s3:ListBucket, S3 responde 403 (no 404) también cuando el objeto no existe.
        logger.error("APK: error de S3 (%s) sobre s3://%s/%s: %s", codigo, cfg["bucket"], cfg["key"], err)
        raise ApkError() from err
    except BotoCoreError as err:
        logger.error("APK: error de boto3: %s", err, exc_info=err)
        raise ApkError() from err

    fecha = meta.get("LastModified")
    return {
        "download_url": url,
        "filename": cfg["filename"],
        "expira_en": EXPIRA_SEGUNDOS,
        "tamano_bytes": meta.get("ContentLength"),
        "ultima_modificacion": fecha.isoformat() if hasattr(fecha, "isoformat") else None,
    }
