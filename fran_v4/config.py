"""Configuración centralizada para Fran 4.0.

Todas las constantes y valores de entorno se definen aquí para que el resto
del código solo importe este módulo y no lea variables de entorno
directamente. Esto facilita la migración y los tests.
"""
from __future__ import annotations

import logging
import os
from typing import Final
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

logger = logging.getLogger(__name__)


def _mask_credentials(url: str) -> str:
    """Evita filtrar contraseñas en logs."""
    parsed = urlparse(url)
    if parsed.password is None:
        return url
    redacted_netloc = parsed.netloc.replace(parsed.password, "***")
    return urlunparse(
        (
            parsed.scheme,
            redacted_netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def mask_database_url(url: str) -> str:
    """Wrapper público para enmascarar credenciales al loguear URLs."""
    return _mask_credentials(url)


OPENAI_API_KEY: Final[str] = os.getenv("OPENAI_API_KEY", "test-key")
MODEL_NAME: Final[str] = os.getenv("MODEL_NAME", "gpt-4o-mini")
OPENAI_EMBEDDING_MODEL: Final[str] = os.getenv(
    "OPENAI_EMBEDDING_MODEL", "text-embedding-3-large"
).strip() or "text-embedding-3-large"


def _fix_database_url(url: str) -> str:
    """Convierte postgresql:// a postgresql+asyncpg:// para SQLAlchemy async.
    Railway y otros proveedores usan postgresql:// pero SQLAlchemy async
    necesita el driver explícito postgresql+asyncpg://. También convierte
    el parámetro sslmode (usado por psycopg2) a ssl (usado por asyncpg).
    Añade ssl=require si la base de datos no es local.
    """
    logger.info("Fixing database URL: %s", _mask_credentials(url))
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    # Parsear la URL para manejar parámetros
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)

    allowed_sslmodes = {
        "disable",
        "allow",
        "prefer",
        "require",
        "verify-ca",
        "verify-full",
    }

    # Normalizar parámetro de SSL para asyncpg, que usa "ssl" en lugar
    # del "sslmode" de psycopg2.
    ssl_value = None
    if "sslmode" in params:
        ssl_value = params.pop("sslmode")[0]
    elif "ssl" in params:
        # Aceptar "ssl" como booleano ("true") o como un sslmode válido.
        raw_ssl = params.pop("ssl")[0]
        if raw_ssl.lower() in ("true", "1", "on"):
            ssl_value = "require"
        elif raw_ssl.lower() in ("false", "0", "off", "disable"):
            ssl_value = "disable"
        else:
            ssl_value = raw_ssl  # Asumir que es un sslmode válido

    if ssl_value:
        if ssl_value in allowed_sslmodes:
            if ssl_value != "disable":
                params["ssl"] = ssl_value
        else:
            logger.warning(
                "Valor ssl/sslmode inválido '%s'; usando 'require' para compatibilidad",
                ssl_value,
            )
            params["ssl"] = "require"
    elif parsed.hostname not in ("localhost", "127.0.0.1"):
        # Para entornos cloud, asegurar TLS explícitamente si no se ha definido.
        params["ssl"] = "require"

    # Reconstruir la query string
    new_query = urlencode(params, doseq=True)

    # Reconstruir la URL
    new_url = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment,
        )
    )
    logger.info("Fixed database URL for asyncpg: %s", _mask_credentials(new_url))
    return new_url


DATABASE_URL: Final[str] = _fix_database_url(
    os.getenv(
        "DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/fran",
    )
)

SESSION_TTL_SECONDS: Final[int] = int(os.getenv("SESSION_TTL_SECONDS", "86400"))

QDRANT_URL: Final[str] = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_COLLECTION: Final[str] = os.getenv("QDRANT_COLLECTION", "fran_catalog")
QDRANT_API_KEY: Final[str | None] = os.getenv("QDRANT_API_KEY")

CATALOG_URL: Final[str | None] = os.getenv("CATALOG_URL")

MAX_SEARCH_RESULTS: Final[int] = int(os.getenv("MAX_SEARCH_RESULTS", "60"))
MAX_ITEMS: Final[int] = int(os.getenv("MAX_ITEMS", "150"))
RELEVANCE_MIN_SCORE: Final[float] = float(os.getenv("RELEVANCE_MIN_SCORE", "65.0"))
RATE_LIMIT_PER_MINUTE: Final[int] = int(os.getenv("RATE_LIMIT_PER_MINUTE", "30"))

DB_INIT_MAX_RETRIES: Final[int | None] = (
    int(os.getenv("DB_INIT_MAX_RETRIES", "10"))
    if os.getenv("DB_INIT_MAX_RETRIES", "").strip() != "" else None
)
DB_INIT_BASE_DELAY: Final[float] = float(os.getenv("DB_INIT_BASE_DELAY", "1.0"))
DB_INIT_MAX_DELAY: Final[float] = float(os.getenv("DB_INIT_MAX_DELAY", "10.0"))

REQUEST_TIMEOUT: Final[int] = int(os.getenv("REQUEST_TIMEOUT", "15"))

UVICORN_HOST: Final[str] = os.getenv("UVICORN_HOST", "0.0.0.0")
UVICORN_PORT: Final[int] = int(os.getenv("UVICORN_PORT", "8000"))

