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
    logger.info("Fixing database URL: %s", url)
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)

    # Parsear la URL para manejar parámetros
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)

    # Añadir logs de depuración para verificar la configuración en el entorno de despliegue.
    # No se loguea la contraseña.
    logger.info(
        "Database connection details: host=%s, port=%s, user=%s",
        parsed.hostname,
        parsed.port,
        parsed.username,
    )

    # Forzar SSL en conexiones no locales para seguridad y compatibilidad
    # con proveedores cloud como Railway.
    if parsed.hostname not in ("localhost", "127.0.0.1"):
        params["ssl"] = "require"

    # Convertir sslmode a ssl para asyncpg. Railway necesita 'require'.
    if "sslmode" in params:
        if params.get("sslmode") == ["require"]:
            params["ssl"] = "require"
        # Eliminar el parámetro original que no es soportado por asyncpg
        del params["sslmode"]

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
    logger.info("Fixed database URL for asyncpg: %s", new_url)
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

