# coding: utf-8
"""
Config y constantes globales de Fran 3.8
- Variables de entorno
- Logger unificado
- Límites/umbrales de negocio
- Rutas por defecto y headers
- Flags y parámetros de comportamiento
"""

import os
import logging
from decimal import Decimal
from dotenv import load_dotenv

# =========================================================
# CARGA DE ENTORNO
# =========================================================

# Carga variables desde .env si existe
load_dotenv()


# =========================================================
# LOGGER GLOBAL
# =========================================================

def _build_logger() -> logging.Logger:
    logger = logging.getLogger("fran38")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
        logger.addHandler(handler)
    return logger

logger = _build_logger()


# =========================================================
# VARIABLES DE ENTORNO (con defaults seguros)
# =========================================================

OPENAI_API_KEY = (os.environ.get("OPENAI_API_KEY") or "").strip()
if not OPENAI_API_KEY:
    # No levantamos excepción acá para permitir import del módulo en tareas offline (ej: scripts de mantenimiento).
    logger.warning("⚠️  Falta OPENAI_API_KEY. Algunas funciones de IA no estarán disponibles.")

MODEL_NAME = (os.environ.get("MODEL_NAME") or "gpt-4o").strip()

CATALOG_URL = (
    os.environ.get("CATALOG_URL")
    or "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/main/catalogo_tercom_faiss.csv"
).strip()

EXCHANGE_API_URL = (os.environ.get("EXCHANGE_API_URL") or "https://dolarapi.com/v1/dolares/oficial").strip()
DEFAULT_EXCHANGE = Decimal(os.environ.get("DEFAULT_EXCHANGE", "1600.0"))
REQUESTS_TIMEOUT = int(os.environ.get("REQUESTS_TIMEOUT", "30"))

# Twilio (envío WhatsApp; el cliente se crea en el módulo whatsapp.py)
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "").strip()
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()

# Persistencia local
DB_PATH = os.environ.get("DB_PATH", "tercom.db")
FAISS_INDEX_PATH = os.environ.get("FAISS_INDEX_PATH", "catalog.faiss")
FAISS_MAPPING_PATH = os.environ.get("FAISS_MAPPING_PATH", "catalog_mapping.pkl")
EMBEDDINGS_CACHE_PATH = os.environ.get("EMBEDDINGS_CACHE_PATH", "embeddings_cache.pkl")

# Puerto del servidor (Railway setea PORT; local default 8080)
PORT = int(os.environ.get("PORT", "8080"))


# =========================================================
# PARÁMETROS DE NEGOCIO Y RENDIMIENTO
# =========================================================

# Resultados de búsqueda
MAX_SEARCH_RESULTS = int(os.environ.get("MAX_SEARCH_RESULTS", "500"))
MAX_PRODUCTS_FOR_LLM = int(os.environ.get("MAX_PRODUCTS_FOR_LLM", "120"))  # límite razonable para prompt

# Límite de cotización masiva
MAX_BULK_ITEMS = int(os.environ.get("MAX_BULK_ITEMS", "500"))
INSTANT_THRESHOLD = int(os.environ.get("INSTANT_THRESHOLD", "40"))  # bajo este umbral cotizamos sync

# Deduplicación de mensajes similares en ventana corta
DEDUP_WINDOW = int(os.environ.get("DEDUP_WINDOW", "5"))  # segundos

# Rate limiting por usuario
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "30"))   # máx requests
RATE_WINDOW = int(os.environ.get("RATE_WINDOW", "60")) # por segundos

# Cache de tipo de cambio
EXCHANGE_CACHE_TTL = int(os.environ.get("EXCHANGE_CACHE_TTL", "3600"))  # 1 hora

# Config de embeddings
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_BATCH = int(os.environ.get("EMBEDDING_BATCH", "512"))
EMBEDDING_MAX_RETRIES = int(os.environ.get("EMBEDDING_MAX_RETRIES", "3"))

# Headers para requests HTTP (evitar bloqueos por default UA)
REQUEST_HEADERS = {
    "User-Agent": os.environ.get("HTTP_USER_AGENT", "FranBot/3.8 (+https://github.com/)"),
    "Accept": "text/csv,application/json;q=0.9,*/*;q=0.8",
}


# =========================================================
# FLAGS DE COMPORTAMIENTO
# =========================================================

# Control de logging verboso
DEBUG_SQL = os.environ.get("DEBUG_SQL", "0") == "1"
DEBUG_WEBHOOK = os.environ.get("DEBUG_WEBHOOK", "0") == "1"
DEBUG_SEARCH = os.environ.get("DEBUG_SEARCH", "0") == "1"
DEBUG_EMBEDDINGS = os.environ.get("DEBUG_EMBEDDINGS", "0") == "1"

# Modo “solo USD” para pruebas (si tu CSV tiene USD y ARS mezclados)
ONLY_USD = os.environ.get("ONLY_USD", "0") == "1"

# Modo “estricto catálogo”: nunca responder algo que no esté
STRICT_CATALOG = os.environ.get("STRICT_CATALOG", "1") == "1"


# =========================================================
# LOG DE ARRANQUE
# =========================================================

logger.info("=" * 60)
logger.info("Cargando configuración de Fran 3.8")
logger.info(f"MODEL_NAME: {MODEL_NAME}")
logger.info(f"CATALOG_URL: {CATALOG_URL}")
logger.info(f"DB_PATH: {DB_PATH}")
logger.info(f"FAISS_INDEX_PATH: {FAISS_INDEX_PATH}")
logger.info(f"EMBEDDINGS_CACHE_PATH: {EMBEDDINGS_CACHE_PATH}")
logger.info(f"REQUESTS_TIMEOUT: {REQUESTS_TIMEOUT}s")
logger.info(f"RATE: {RATE_LIMIT}/{RATE_WINDOW}s  | DEDUP_WINDOW: {DEDUP_WINDOW}s")
logger.info(f"MAX_SEARCH_RESULTS: {MAX_SEARCH_RESULTS} | MAX_PRODUCTS_FOR_LLM: {MAX_PRODUCTS_FOR_LLM}")
logger.info(f"STRICT_CATALOG: {STRICT_CATALOG} | ONLY_USD: {ONLY_USD}")
logger.info("=" * 60)
