"""Configuración centralizada para Fran 4.0.

Todas las constantes y valores de entorno se definen aquí para que el resto
del código solo importe este módulo y no lea variables de entorno
directamente. Esto facilita la migración y los tests.
"""
from __future__ import annotations

import os
from typing import Final


OPENAI_API_KEY: Final[str] = os.getenv("OPENAI_API_KEY", "test-key")
MODEL_NAME: Final[str] = os.getenv("MODEL_NAME", "gpt-4o-mini")
OPENAI_EMBEDDING_MODEL: Final[str] = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")

DATABASE_URL: Final[str] = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/fran",
)

REDIS_URL: Final[str] = os.getenv("REDIS_URL", "redis://localhost:6379/0")
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

