# =========================================================
# Fran 3.16 – Bot Mayorista Inteligente (Arquitectura Híbrida)
# =========================================================
# Combina lo mejor de Fran 3.14 y 3.15 en una arquitectura unificada:
#
# ARQUITECTURA HÍBRIDA:
# - Templates estructurados (3.15) para Understanding y Response
# - Intent detection temprano con skip logic para intents sociales
# - Razonamiento explícito con re-query capability (3.14)
# - Structured Outputs nativos de OpenAI
# - Fallbacks automáticos por fase
#
# VERSIONES DISPONIBLES (A/B/C Testing):
# - Fran 3.17: Orquestador dinámico (router + búsqueda híbrida v3.17) (40% de usuarios)
# - Fran 3.16: Arquitectura híbrida (30% de usuarios)
# - Fran 3.15: Templates estructurados (15% de usuarios)
# - Fran 3.14: Doble LLM con reasoning (15% de usuarios)
#
# INFRAESTRUCTURA COMPARTIDA:
# - Búsqueda híbrida FAISS+BM25 con RRF
# - Memory enrichment y contexto de conversación
# - Validación anti-alucinación en dos capas
# - Circuit breakers y observabilidad
# - Manejo de listas masivas y chunks para WhatsApp
# =========================================================

import os, json, csv, io, sqlite3, logging, re, unicodedata, time, threading, pickle, random, hashlib, math
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from collections import defaultdict, Counter
from contextlib import contextmanager
from threading import Lock, Semaphore
from queue import Queue, Empty
from pathlib import Path

import requests
from flask import Flask, request, Response, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI, RateLimitError
from rapidfuzz import process, fuzz
import faiss
import numpy as np
try:  # pragma: no cover - fallback para entornos sin rank_bm25
    from rank_bm25 import BM25Okapi
except ImportError:  # pragma: no cover - fallback liviano
    class BM25Okapi:  # type: ignore
        def __init__(self, corpus):
            self.corpus = corpus or []

        def get_scores(self, tokens):
            token_set = set(tokens or [])
            scores = []
            for doc in self.corpus:
                scores.append(float(sum(1 for t in doc if t in token_set)))
            return np.array(scores, dtype=float)
try:  # pragma: no cover - fallback cuando dotenv no está disponible
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - fallback liviano
    def load_dotenv(*args, **kwargs):
        return False
try:  # pragma: no cover - fallback cuando cachetools no está disponible
    from cachetools import LRUCache
except ImportError:  # pragma: no cover - fallback sencillo
    class LRUCache(dict):  # type: ignore
        def __init__(self, maxsize=128):
            super().__init__()
            self.maxsize = maxsize

        def __setitem__(self, key, value):
            if len(self) >= self.maxsize:
                first_key = next(iter(self))
                super().pop(first_key, None)
            super().__setitem__(key, value)
try:  # pragma: no cover - fallback cuando jsonschema no está disponible
    from jsonschema import validate, ValidationError
except ImportError:  # pragma: no cover - fallback liviano
    class ValidationError(Exception):
        ...

    def validate(instance, schema):
        required = schema.get("required", [])
        for field in required:
            if field not in instance:
                raise ValidationError(f"Missing required field: {field}")

from fran.clients import HttpClient, LLMClient
from fran.observability import CircuitBreaker, METRICS_REGISTRY, track_step
from pipeline.llm_classifier_dynamic import build_classifier_schema
from pipeline.orquestador_v317 import orquestar_v317
from pipeline.router_dynamic import build_catalog_centroid

load_dotenv()
app = Flask(__name__)

REQUESTS_HEADERS = {
    "User-Agent": "Safari/605.1.15",
    "Accept": "text/plain"
}

# =========================================================
# CATALOGO (Fran 3.12 – versión ultra normalizada FAISS)
# =========================================================

CSV_URL = "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/refs/heads/Fran-3.13.2/catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv"
LOCAL_CSV_FALLBACK = Path("catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv")
CATALOG_URL = (os.environ.get("CATALOG_URL") or CSV_URL).strip()

print(f"[Fran] Catálogo cargado desde: {CATALOG_URL}")

# ------------------------------------------------------------
# LOGGER
# ------------------------------------------------------------
logger = logging.getLogger("fran313")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)

logger.info("✅ Imports completos")

FRAN_DEBUG = (os.environ.get("FRAN_DEBUG") or "").strip().lower() in {"1", "true", "yes", "on"}
LAST_SEARCH_DEBUG = {}

# ------------------------------------------------------------
# CONFIGURACIÓN FRAN 3.16 (Pipeline JSON-first)
# ------------------------------------------------------------
STRICT_MODE = False
ALLOW_MISSING_MOTO_DATA = True
LLM_REASONING_ENABLED = True
CONFIDENCE_THRESHOLD = 0.75
MAX_REQUERY_ATTEMPTS = 2
RESPONSE_TIMEOUT_MS = 5000

# CONSTANTS
SEARCH_TOP_K = 15
FAISS_THRESHOLD = 0.6
BM25_THRESHOLD = 0.4
RRF_BM25_WEIGHT = 1.0
RRF_FAISS_WEIGHT = 1.2
LAST_FILTER_CATALOG_DEBUG = {}
LAST_RELEVANCE_DEBUG = {}

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------
OPENAI_API_KEY = (os.environ.get("OPENAI_API_KEY") or "test-key").strip()
if os.environ.get("OPENAI_API_KEY") is None:
    logger.warning("OPENAI_API_KEY no configurada, usando clave dummy solo para tests")

EMBEDDING_MODEL = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large").strip()
MODEL_NAME = (os.environ.get("MODEL_NAME") or "gpt-4o-mini").strip()
# Usar modelo más barato para reasoning
MODEL_REASONING = "gpt-4o-mini"  # más barato, rápido
MODEL_RESPONSE = "gpt-4o-mini"   # mantener calidad conversacional
MODEL_OUTPUT = os.environ.get("MODEL_OUTPUT", MODEL_RESPONSE)

EXCHANGE_API_URL = (
    os.environ.get("EXCHANGE_API_URL") or "https://dolarapi.com/v1/dolares/oficial"
).strip()

DEFAULT_EXCHANGE = Decimal(os.environ.get("DEFAULT_EXCHANGE", "1600.0"))
REQUESTS_TIMEOUT = int(os.environ.get("REQUESTS_TIMEOUT", "15"))
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "tercom.db")
FAISS_INDEX_PATH = os.environ.get("FAISS_INDEX_PATH", "catalog.faiss")
FAISS_MAPPING_PATH = os.environ.get("FAISS_MAPPING_PATH", "catalog_mapping.pkl")

_safe_catalog_hash = CATALOG_URL.replace("/", "_").replace(":", "_").replace(".", "_")[-40:]
EMBEDDINGS_CACHE_PATH = f"embeddings_cache_{_safe_catalog_hash}.pkl"

MAX_SEARCH_RESULTS = int(os.environ.get("MAX_SEARCH_RESULTS", "60"))
MAX_PRODUCTS_FOR_LLM = int(os.environ.get("MAX_PRODUCTS_FOR_LLM", "15"))
WHATSAPP_MSG_LIMIT = int(os.environ.get("WHATSAPP_MSG_LIMIT", "1600"))
PRODUCTS_PER_CHUNK = int(os.environ.get("PRODUCTS_PER_CHUNK", "30"))

# Nuevos parámetros de calidad (ajustados)
RELEVANCE_MIN_SCORE = float(os.environ.get("RELEVANCE_MIN_SCORE", "65.0"))
QUALITY_HIGH_THRESHOLD = float(os.environ.get("QUALITY_HIGH_THRESHOLD", "70.0"))
QUALITY_MEDIUM_THRESHOLD = float(os.environ.get("QUALITY_MEDIUM_THRESHOLD", "60.0"))

INSTANT_THRESHOLD = 15
ASYNC_QUICK = 40
ASYNC_MEDIUM = 80
MAX_ITEMS = 150
BULK_TIMEOUT = 240
MAX_BULK_ITEMS = 150

# ============================================================
# TEMPLATE SCHEMAS - FRAN 3.15
# ============================================================

QUERY_UNDERSTANDING_SCHEMA = {
    "task": "understand_query",
    "description": """
    Sos un experto en motos argentinas. Analiza el mensaje del cliente y normaliza errores.
    
    MARCAS COMUNES (pueden estar mal escritas):
    - Honda, Yamaha, Zanella, Motomel, Corven, Gilera, Guerrero, Bajaj, Keeway
    
    CATEGORÍAS COMUNES:
    - Batería (ytx, gel, litio), Amortiguador, Filtro, Aceite, Cadena, Bujía, Pastillas
    
    CORRECCIONES TÍPICAS:
    - "gonda" → "Honda"
    - "iamaha" → "Yamaha"
    - "sanella" → "Zanella"
    - "bateria" → "batería"

    Si el mensaje del cliente tiene intención social, humana o relacional (saludo, agradecimiento, conversación ligera, humor leve, follow-up, cierre, rapport), clasificá la intención como intent = "social". Este intent es distinto de "product_search" y debe priorizar lo humano por sobre lo técnico. No inventes datos de productos en este nivel.
    """,
    "output_schema": {
        "type": "object",
        "required": ["normalized_query", "entities", "intent", "confidence"],
        "properties": {
            "original_query": {
                "type": "string",
                "description": "Query original del usuario"
            },
            "normalized_query": {
                "type": "string", 
                "description": "Query corregida y lista para búsqueda"
            },
            "entities": {
                "type": "object",
                "properties": {
                    "brand": {
                        "type": "string",
                        "description": "Marca de moto detectada (nombre correcto)"
                    },
                    "model": {
                        "type": "string",
                        "description": "Modelo de moto detectado (nombre correcto)"
                    },
                    "category": {
                        "type": "string",
                        "description": "Categoría de repuesto detectada"
                    }
                }
            },
            "corrections": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Lista de correcciones aplicadas (ej: 'gonda→Honda')"
            },
            "intent": {
                "type": "string",
                "enum": ["product_search", "cart_action", "social", "tech_question", "order_flow"],
                "description": "Intención detectada"
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Confianza en la normalización (0.0-1.0)"
            },
            "needs_clarification": {
                "type": "boolean",
                "description": "True si falta info crítica"
            },
            "clarification_question": {
                "type": "string",
                "description": "Pregunta para el cliente si needs_clarification=true"
            }
        }
    }
}

PRODUCT_SELECTION_SCHEMA = {
    "task": "select_products",
    "description": """
    Elegí los mejores productos de la lista para el cliente.
    
    REGLAS CRÍTICAS:
    - SOLO productos de allowed_products (NO inventes códigos)
    - Prioriza compatibilidad exacta de marca/modelo
    - Si hay múltiples opciones, explicá diferencias clave
    - Máximo 5 productos (3 si es primer mensaje)
    """,
    "output_schema": {
        "type": "object",
        "required": ["selected_products", "analysis", "action"],
        "properties": {
            "selected_products": {
                "type": "array",
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "required": ["code", "reason", "rank"],
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "Código TERCOM del producto"
                        },
                        "reason": {
                            "type": "string",
                            "description": "Por qué elegiste este producto (1 línea)"
                        },
                        "rank": {
                            "type": "string",
                            "enum": ["primary", "alternative"],
                            "description": "Recomendación principal o alternativa"
                        },
                        "compatibility": {
                            "type": "number",
                            "description": "Score de compatibilidad 0.0-1.0"
                        }
                    }
                }
            },
            "analysis": {
                "type": "object",
                "properties": {
                    "customer_type": {
                        "type": "string",
                        "enum": ["nuevo", "recurrente", "comparador", "urgente"],
                        "description": "Tipo de cliente detectado"
                    },
                    "interest_level": {
                        "type": "string",
                        "enum": ["bajo", "medio", "alto"],
                        "description": "Nivel de interés de compra"
                    },
                    "key_arguments": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Argumentos comerciales (precio, calidad, marca)"
                    }
                }
            },
            "action": {
                "type": "string",
                "enum": ["show_products", "ask_clarification", "suggest_alternatives"],
                "description": "Acción recomendada"
            },
            "clarification_needed": {
                "type": "string",
                "description": "Si action=ask_clarification, qué preguntar"
            }
        }
    }
}

RESPONSE_GENERATION_SCHEMA = {
    "task": "generate_response",
    "description": """
    Generá la respuesta final para WhatsApp como Fran.

    SI EL INTENT ES "social":
    - Ignorá allowed_products por completo.
    - No generes listados ni pidas marca/modelo/año.
    - Respondé en tono humano, cálido, vendedor mayorista real.
    - La respuesta debe ser breve (1–3 líneas).
    - Podés mantener continuidad (“¡Me alegra que te haya servido!”, “¿Todo tranqui por ahí?”).
    - No menciones sistemas, búsquedas, catálogos ni procesos internos.
    - products_cited debe ser siempre [].
    - La respuesta debe ser 100% independiente del catálogo.

    REGLAS PARA RESPUESTA:
    1. Validación de coherencia entre lo que pidió el cliente y los productos (brand, model, cylinder, part_category, normalized_query, intent, corrections). Si allowed_products trae productos no coherentes, ignoralos. Si ninguno es coherente, devolvé un mensaje breve pidiendo aclaración. Si allowed_products está vacío o incoherente, devolvé: "No encontré coincidencias claras con lo que pediste. ¿Me pasás más detalles (marca/modelo/año) así lo afino?"
    2. Manejo de large list (mayorista): si allowed_products tiene más de 10 elementos, no limites el total. Dividí la respuesta en bloques aptos para WhatsApp con 8–12 productos ordenados por relevancia, sin repetir. Tono formal mayorista.
    3. Límites de Twilio / WhatsApp: cada mensaje < ~3500 caracteres. Ajustá dinámicamente el tamaño de los bloques manteniendo el máximo posible sin exceder el límite. Si hay varios mensajes, generá cada uno por separado manteniendo coherencia y continuidad.
    4. Estructura de los productos en cada mensaje: cada producto debe listar código TERCOM, descripción limpia y precio. Nunca inventes precios, códigos ni descripciones.
    5. products_cited: en cada mensaje listar solo los códigos incluidos en ese mensaje. No mezclar códigos de otros bloques. Si el intent NO es product_search, entonces products_cited = [].
    6. Mensajes sociales, saludos, agradecimientos o conversación ligera (social, small_talk, rapport, etc.): ignorá allowed_products. No generes listados ni pidas marca/modelo/año. Respondé en máximo 1–3 líneas, tono humano. products_cited = [].
    7. Mensaje final: en el último bloque de productos (o en el único mensaje) agregá: "Decime si querés que compare opciones o te arme el carrito."
    8. Restricciones generales: nunca inventes productos, ni derivados, ni modifiques códigos. Nunca respondas fuera de la estructura JSON del schema.

    ESTILO GENERAL:
    - Humano, directo, cercano
    - Máximo 3-4 líneas
    - Nunca digas “soy Fran”, “soy un asistente”, ni menciones sistemas
    - Si el intent es product_search, ahí sí incluir productos y códigos

    ESTRUCTURA PARA RESPUESTAS NORMALES:
    1. Confirmación breve
    2. Productos con código TERCOM (máx 3)
    3. Call-to-action suave
    """,
    "output_schema": {
        "type": "object",
        "required": ["message", "products_cited"],
        "properties": {
            "message": {
                "type": "string",
                "description": "Texto final para WhatsApp (máx 350 caracteres)"
            },
            "products_cited": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Códigos TERCOM mencionados en el mensaje"
            },
            "tone": {
                "type": "string",
                "enum": ["friendly", "expert", "urgent", "advisory"],
                "description": "Tono usado en el mensaje"
            },
            "next_expected_action": {
                "type": "string",
                "description": "Qué esperás que haga el cliente ahora"
            }
        }
    }
}

TEMPLATES = {
    "query_understanding": QUERY_UNDERSTANDING_SCHEMA,
    "product_selection": PRODUCT_SELECTION_SCHEMA,
    "response_generation": RESPONSE_GENERATION_SCHEMA
}

# ------------------------------------------------------------
# TWILIO
# ------------------------------------------------------------
try:
    from twilio.rest import Client as TwilioClient
    from twilio.request_validator import RequestValidator
except Exception:
    TwilioClient = None
    RequestValidator = None

twilio_rest_available = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and TwilioClient)
twilio_rest_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if twilio_rest_available else None
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN) if (RequestValidator and TWILIO_AUTH_TOKEN) else None

client = OpenAI(api_key=OPENAI_API_KEY)
http_client = HttpClient(
    timeout=REQUESTS_TIMEOUT,
    headers=REQUESTS_HEADERS,
    logger=logger,
    breaker=CircuitBreaker(failure_threshold=3, recovery_time=120),
)
llm_client = LLMClient(
    client,
    logger=logger,
    breaker=CircuitBreaker(failure_threshold=2, recovery_time=90),
)
cart_lock = Lock()
exchange_lock = Lock()
bulk_queue = Queue()

openai_sem = Semaphore(3)


class SessionMemory:
    """In-memory session context with TTL to persist recent searches per usuario."""

    _store: dict[str, dict] = {}
    _lock = threading.Lock()
    DEFAULT_TTL = 1800

    @classmethod
    def get(cls, user_id: str | None) -> dict | None:
        if not user_id:
            return None

        with cls._lock:
            data = cls._store.get(user_id)
            if not data:
                # Hydrate desde storage persistente para mantener continuidad
                try:
                    last_search = get_last_search(user_id)
                    if last_search:
                        ts = last_search.get("metadata", {}).get("timestamp") or last_search.get("age_minutes")
                        timestamp = None
                        if isinstance(ts, (int, float)):
                            timestamp = time.time() - float(ts) * 60
                        else:
                            try:
                                timestamp = datetime.fromisoformat(ts).timestamp() if ts else None
                            except Exception:
                                timestamp = None

                        data = {
                            "last_search": {
                                "query": last_search.get("query", ""),
                                "results": last_search.get("products", []),
                                "metadata": last_search.get("metadata", {}),
                                "timestamp": timestamp or time.time(),
                                "confidence": 0.5,
                            },
                            "conversation_turns": 0,
                            "context_ttl": cls.DEFAULT_TTL,
                        }
                        cls._store[user_id] = data
                except Exception:
                    data = None
            if not data:
                return None

            last_ts = data.get("last_search", {}).get("timestamp")
            ttl = data.get("context_ttl") or cls.DEFAULT_TTL
            if last_ts and (time.time() - last_ts) > ttl:
                cls._store.pop(user_id, None)
                return None

            return data

    @classmethod
    def update_last_search(
        cls,
        user_id: str,
        *,
        query: str,
        results: list,
        metadata: dict,
        confidence: float,
        context_ttl: int | None = None,
    ) -> None:
        if not user_id:
            return

        payload = {
            "last_search": {
                "query": query,
                "results": results,
                "metadata": metadata or {},
                "timestamp": time.time(),
                "confidence": _normalize_confidence(confidence, minimum=0.0),
            },
            "conversation_turns": 1,
            "context_ttl": context_ttl or cls.DEFAULT_TTL,
        }

        with cls._lock:
            existing = cls._store.get(user_id) or {}
            if existing.get("conversation_turns"):
                payload["conversation_turns"] = existing.get("conversation_turns", 0) + 1
            if existing.get("last_cart_action"):
                payload["last_cart_action"] = existing.get("last_cart_action")
            cls._store[user_id] = payload


def _build_cached_search_payload(understanding: dict, last_search: dict) -> dict | None:
    cached_results = (last_search or {}).get("results") or []
    if not cached_results:
        return None

    target_family = None
    has_structured_compatibility = False

    for res in cached_results:
        fam = res.get("family") or (res.get("catalog_data") or {}).get("familia")
        if fam and not target_family:
            target_family = fam
        if res.get("has_structured_compatibility"):
            has_structured_compatibility = True

    payload = {
        "phase": "search",
        "query_params": {
            "text_query": understanding.get("raw_query", ""),
            "filters": {
                "brand": understanding.get("brand"),
                "model": understanding.get("model"),
                "displacement_cc": understanding.get("displacement_cc"),
            },
        },
        "results": cached_results[:SEARCH_TOP_K],
        "total_results": len(cached_results),
        "target_family": target_family,
        "has_structured_compatibility": has_structured_compatibility,
        "needs_llm_compatibility": any(r.get("needs_llm_compatibility") for r in cached_results),
        "source": "session_cache",
    }

    if not _validate_phase(payload, SEARCH_PHASE_SCHEMA, "FASE2_CACHE"):
        return None

    return payload

exchange_cache = {"rate": None, "timestamp": None}
EXCHANGE_CACHE_TTL = 3600

user_requests = defaultdict(list)
RATE_LIMIT = 20
RATE_WINDOW = 60

message_dedup_cache = defaultdict(list)
DEDUP_WINDOW = 5

_catalog_and_index_cache = {"catalog": None, "index": None, "bm25": None, "bm25_corpus": None, "built_at": None}
_catalog_lock = Lock()
_raw_csv_cache = {"text": None}

_v317_cache = {"catalog": None, "centroid": None, "schema": None}
_v317_lock = Lock()

_embeddings_cache_lock = Lock()

# Cache de fuzzy matching para post-validation
_fuzzy_match_cache = LRUCache(maxsize=20000)

# Índice de familias (global)
FAMILIES_INDEX = []
FAMILIES_TOKEN_IDF = {}
FAMILIES_TOKEN_IDF_DEFAULT = 1.0
FAMILY_COMPATIBILITY_PROFILE = {}

TEMPLATE_FALLBACKS = {
    "query_understanding": {
        "original_query": "",
        "normalized_query": "",
        "entities": {},
        "corrections": [],
        "intent": "product_search",
        "confidence": 0.5,
        "needs_clarification": False,
        "clarification_question": ""
    },
    "product_selection": {
        "selected_products": [],
        "analysis": {
            "customer_type": "nuevo",
            "interest_level": "medio",
            "key_arguments": []
        },
        "action": "ask_clarification",
        "clarification_needed": "Tuve un problema técnico, ¿me repetís qué necesitás?"
    },
    "response_generation": {
        "message": "Disculpá, tuve un problema. ¿Me repetís qué estabas buscando?",
        "products_cited": [],
        "tone": "friendly",
        "next_expected_action": "retry"
    }
}


def validate_schema(data: dict, schema: dict) -> bool:
    if data is None:
        raise ValueError("Schema validation failed: data is None")

    if not isinstance(schema, dict):
        raise ValueError("Schema validation failed: invalid schema")

    if schema.get("type") == "object":
        if not isinstance(data, dict):
            raise ValueError("Schema validation failed: expected object")
        for key in schema.get("required", []):
            if key not in data:
                raise ValueError(f"Schema validation failed: missing '{key}'")

        properties = schema.get("properties", {})
        for key, value in data.items():
            if key not in properties:
                continue
            expected = properties[key]
            expected_type = expected.get("type")
            if expected_type == "object" and value is not None:
                validate_schema(value, expected)
            elif expected_type == "array" and value is not None:
                if not isinstance(value, list):
                    raise ValueError(f"Schema validation failed: '{key}' should be array")
                item_schema = expected.get("items")
                if item_schema:
                    for item in value:
                        if item_schema.get("type") == "object" and isinstance(item, dict):
                            validate_schema(item, item_schema)
                        elif item_schema.get("type") == "string" and not isinstance(item, str):
                            raise ValueError(f"Schema validation failed: '{key}' items should be string")
            elif expected_type == "string" and value is not None and not isinstance(value, str):
                raise ValueError(f"Schema validation failed: '{key}' should be string")
            elif expected_type == "number" and value is not None and not isinstance(value, (int, float)):
                raise ValueError(f"Schema validation failed: '{key}' should be number")
            elif expected_type == "boolean" and not isinstance(value, bool):
                raise ValueError(f"Schema validation failed: '{key}' should be boolean")

            enum_values = expected.get("enum")
            if enum_values and value not in enum_values:
                raise ValueError(f"Schema validation failed: '{key}' not in enum")

    return True


def log_template_execution(template_name: str, input_data: dict, output_data: dict, duration: float):
    """Log de ejecución de templates para debugging"""
    try:
        with get_db_connection() as conn:
            conn.execute(
                """INSERT INTO template_logs 
                   (phone, template_name, input_json, output_json, duration_ms, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    input_data.get("phone"),
                    template_name,
                    json.dumps(input_data, ensure_ascii=False),
                    json.dumps(output_data, ensure_ascii=False),
                    int(duration * 1000),
                    datetime.now().isoformat()
                )
            )
    except Exception as e:
        logger.error(f"Error logging template execution: {e}")


def complete_template(template_name: str, context: dict, model: str = MODEL_REASONING) -> dict:
    """
    Completa un template usando el LLM con structured output.
    """
    template = TEMPLATES.get(template_name)
    if not template:
        raise ValueError(f"Template '{template_name}' no encontrado")

    system_prompt = f"""
{template['description']}

Tu respuesta DEBE seguir este JSON schema EXACTO:
{json.dumps(template['output_schema'], indent=2)}

REGLAS:
- Devolvé SOLO JSON válido
- NO agregues explicaciones fuera del JSON
- Respetá todos los campos required
"""

    user_prompt = json.dumps(context, ensure_ascii=False)
    start_time = time.time()

    try:
        with openai_sem:
            resp = llm_client.completion(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.1,
                response_format={"type": "json_object"}
            )

        result = json.loads(resp.choices[0].message.content)
        validate_schema(result, template["output_schema"])
        log_template_execution(template_name, context, result, time.time() - start_time)
        return result

    except Exception as e:
        logger.error(f"Template completion failed: {e}", exc_info=True)
        fallback = TEMPLATE_FALLBACKS.get(template_name, template.get("fallback", {}))
        try:
            log_template_execution(template_name, context, fallback, time.time() - start_time)
        except Exception:
            pass
        return fallback


def should_use_v315(phone: str) -> bool:
    """
    DEPRECATED: Usar get_orchestrator_version() en su lugar.
    Mantenido para compatibilidad hacia atrás.
    """
    version = get_orchestrator_version(phone)
    return version == "3.15"


def get_orchestrator_version(phone: str) -> str:
    """
    Determina qué versión del orquestador usar: "3.14", "3.15", "3.16" o "3.17"

    Estrategia de rollout:
    - USE_FRAN_317=true → todos a 3.17
    - USE_FRAN_316=true → todos a 3.16
    - USE_FRAN_315=true → todos a 3.15
    - BETA_PHONES → 3.17
    - Hash-based split: 40% → 3.17, 30% → 3.16, 15% → 3.15, 15% → 3.14

    Returns:
        str: "3.14", "3.15", "3.16" o "3.17"
    """
    # Force v3.17 globally
    if os.environ.get("USE_FRAN_317", "false").lower() == "true":
        return "3.17"

    # Force v3.16 globally
    if os.environ.get("USE_FRAN_316", "false").lower() == "true":
        return "3.16"

    # Force v3.15 globally
    if os.environ.get("USE_FRAN_315", "false").lower() == "true":
        return "3.15"

    # Beta phones get v3.17
    beta_phones = [p.strip() for p in os.environ.get("BETA_PHONES", "").split(",") if p.strip()]
    if beta_phones and phone in beta_phones:
        return "3.17"

    # Hash-based A/B/C split (40% v3.17, 30% v3.16, 15% v3.15, 15% v3.14)
    phone_hash = int(hashlib.md5(phone.encode()).hexdigest(), 16) % 100

    if phone_hash < 40:
        return "3.17"  # 40% get dynamic hybrid with router
    elif phone_hash < 70:
        return "3.16"  # 30% get hybrid architecture
    elif phone_hash < 85:
        return "3.15"  # 15% get templates
    else:
        return "3.14"  # 15% get dual LLM

# ------------------------------------------------------------
# UTILS
# ------------------------------------------------------------
def strip_accents(s):
    if not s:
        return ""
    try:
        return "".join(
            ch for ch in unicodedata.normalize("NFKD", str(s))
            if not unicodedata.combining(ch)
        ).lower()
    except Exception:
        return str(s).lower()


def _tokenize_text(text):
    try:
        normalized = normalize_search_query(text or "")
        return normalized.split()
    except Exception as e:
        logger.warning(f"Error tokenizando texto: {e}")
        return (text or "").lower().split()


def to_decimal_money(x):
    if x is None or x == "":
        return Decimal("0")
    try:
        s = str(x).replace("USD", "").replace("ARS", "").replace("$", "").replace(" ", "").strip()
        if not s:
            return Decimal("0")
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        d = Decimal(s)
        return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError) as e:
        logger.warning(f"Error convirtiendo a decimal: {x} - {e}")
        return Decimal("0")


def format_price(price):
    try:
        if not isinstance(price, Decimal):
            price = Decimal(str(price))
        return f"${price:,.0f}".replace(",", ".")
    except Exception:
        return "$0"


def validate_tercom_code(code):
    pattern = r"^\d{4}/\d{5}-\d{3}$"
    s = str(code).strip()
    if re.match(pattern, s):
        return True, s
    code_clean = re.sub(r"[^0-9]", "", s)
    if len(code_clean) == 12:
        normalized = f"{code_clean[:4]}/{code_clean[4:9]}-{code_clean[9:12]}"
        return True, normalized
    return False, s


def sanitize_input(text, max_length=1500):
    if not text:
        return ""
    text = text[:max_length]
    text = re.sub(r'[^\w\s\-.,;:()/áéíóúñÁÉÍÓÚÑ]', '', text, flags=re.UNICODE)
    return text.strip()


def is_duplicate_message(phone, message, window=DEDUP_WINDOW):
    now = datetime.now().timestamp()
    message_dedup_cache[phone] = [
        (msg, ts) for msg, ts in message_dedup_cache[phone]
        if now - ts < window
    ]
    for cached_msg, cached_ts in message_dedup_cache[phone]:
        if cached_msg == message and (now - cached_ts) < window:
            return True
    message_dedup_cache[phone].append((message, now))
    return False


def normalize_search_query(query):
    if not query:
        return ""

    normalized = strip_accents(query)
    normalized = re.sub(r"[^\w\s/.-]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized

# ------------------------------------------------------------
# NUEVO: RELEVANCE SCORING GENERAL
# ------------------------------------------------------------
def calculate_relevance_score(query: str, product: dict) -> float:
    """
    Calcula qué tan relevante es un producto para el query.
    Retorna score 0-100.
    - 40% overlap de palabras clave
    - 40% fuzzy match del nombre completo
    - 20% match de categoría
    """
    q_norm = normalize_search_query(query)
    q_words = set(q_norm.split())

    p_text = normalize_search_query(
        f"{product.get('name', '')} {product.get('category', '')} "
        f"{product.get('keywords', '')} {product.get('brand', '')} {product.get('model', '')}"
    )
    p_words = set(p_text.split())

    overlap = len(q_words & p_words) / len(q_words) if q_words else 0
    overlap_score = overlap * 40

    product_name = normalize_search_query(product.get('name', ''))
    try:
        fuzzy_ratio = fuzz.partial_ratio(q_norm, product_name)
        fuzzy_score = fuzzy_ratio * 0.4
    except Exception:
        fuzzy_score = 0

    category_score = 0
    product_cat = normalize_search_query(product.get('category', ''))
    for q_word in q_words:
        if len(q_word) >= 4:
            if q_word in product_cat or product_cat in q_word:
                category_score = 20
                break

    total = overlap_score + fuzzy_score + category_score
    return min(total, 100)


def filter_by_relevance(query: str, products: list, min_score: float = RELEVANCE_MIN_SCORE) -> list:
    global LAST_RELEVANCE_DEBUG
    if not products or not query:
        LAST_RELEVANCE_DEBUG = {"scores": [], "query": query, "min_score": min_score}
        return []
    scored = []
    for p in products:
        score = calculate_relevance_score(query, p)
        if score >= min_score:
            scored.append((p, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    LAST_RELEVANCE_DEBUG = {
        "query": query,
        "min_score": min_score,
        "scores": [{"code": p.get("code"), "score": s} for p, s in scored],
    }
    logger.info("[DEBUG][Relevancia] %s candidatos", len(scored))
    return [p for p, score in scored]

# ------------------------------------------------------------
# NUEVO: CONTEXT QUALITY ASSESSMENT
# ------------------------------------------------------------
def assess_context_quality(query: str, products: list) -> dict:
    """
    Evalúa si el contexto recuperado es suficiente para responder.
    """
    if not products:
        return {
            "sufficient": False,
            "reason": "no_results",
            "action": "ask_clarification",
            "confidence": "none",
            "message": "No encontré coincidencias claras con lo que pediste. ¿Me pasás más detalles (marca/modelo/año) así lo afinamos?"
        }

    scores = [calculate_relevance_score(query, p) for p in products[:10]]
    avg_score = sum(scores) / len(scores) if scores else 0
    max_score = max(scores) if scores else 0
    relevant_count = sum(1 for s in scores if s >= RELEVANCE_MIN_SCORE)

    logger.info(f"Quality assessment - Avg: {avg_score:.1f}, Max: {max_score:.1f}, Relevant: {relevant_count}/{len(products[:10])}")

    if max_score < QUALITY_MEDIUM_THRESHOLD:
        return {
            "sufficient": False,
            "reason": "low_relevance",
            "action": "suggest_alternatives",
            "confidence": "low",
            "top_products": products[:3],
            "avg_score": avg_score,
            "max_score": max_score
        }

    if relevant_count < 3 and max_score >= RELEVANCE_MIN_SCORE:
        return {
            "sufficient": True,
            "reason": "limited_but-valid",
            "action": "show_with_caveat",
            "confidence": "medium",
            "relevant_count": relevant_count,
            "avg_score": avg_score
        }

    if avg_score >= QUALITY_HIGH_THRESHOLD:
        confidence = "high"
    elif avg_score >= QUALITY_MEDIUM_THRESHOLD:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "sufficient": True,
        "reason": "high_quality" if confidence == "high" else "acceptable",
        "action": "proceed",
        "confidence": confidence,
        "avg_score": avg_score,
        "relevant_count": relevant_count
    }

# ------------------------------------------------------------
# NUEVO: CODE EXTRACTION & VALIDATION
# ------------------------------------------------------------
def extract_mentioned_codes(response_text: str) -> set:
    codes = set()
    pattern1 = r'\((?:código\s+|codigo\s+|cod\s+)?(\d{4}/\d{5}-\d{3})\)'
    codes.update(re.findall(pattern1, response_text, re.IGNORECASE))
    pattern2 = r'\b(\d{4}/\d{5}-\d{3})\b'
    codes.update(re.findall(pattern2, response_text))
    return codes


def validate_response_codes(response_text: str, allowed_products: list) -> dict:
    mentioned = extract_mentioned_codes(response_text)
    allowed = {p.get('code', '') for p in allowed_products if p.get('code')}

    hallucinated = mentioned - allowed

    if hallucinated:
        logger.error(f"⚠️ LLM mencionó códigos inexistentes: {hallucinated}")
        return {
            "valid": False,
            "hallucinated_codes": list(hallucinated),
            "mentioned_codes": list(mentioned),
            "allowed_codes": list(allowed),
            "action": "regenerate_or_fallback"
        }

    if not mentioned and allowed:
        logger.warning("LLM no citó códigos específicos")
        return {
            "valid": True,
            "cited_products": 0,
            "warning": "no_citations"
        }

    return {
        "valid": True,
        "cited_products": len(mentioned),
        "codes": list(mentioned)
    }


def validate_mentioned_names(response_text: str, allowed_products: list) -> dict:
    if not allowed_products:
        return {"valid": True}

    mentioned_names = []
    quoted = re.findall(r'"([^"]{5,})"', response_text)
    mentioned_names.extend(quoted)

    patterns = [
        r'(?:tengo|tenemos|hay|encontré)\s+(?:el\s+|la\s+|los\s+|las\s+)?([A-ZÁÉÍÓÚÑ][a-záéíóúñA-ZÁÉÍÓÚÑ\s]{5,50})(?:\s+(?:para|de|en|cod|código|\()|\.|\,|$)',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, response_text, re.IGNORECASE)
        mentioned_names.extend(matches)

    if not mentioned_names:
        return {"valid": True}

    allowed_names_normalized = [
        normalize_search_query(p.get("name", ""))
        for p in allowed_products if p.get("name")
    ]

    hallucinated_names = []
    for name in mentioned_names:
        name_norm = normalize_search_query(name)
        cache_key = (name_norm, tuple(sorted(allowed_names_normalized)))

        if cache_key in _fuzzy_match_cache:
            is_valid = _fuzzy_match_cache[cache_key]
        else:
            is_valid = any(
                fuzz.partial_ratio(name_norm, allowed_name) > 70
                for allowed_name in allowed_names_normalized
            )
            _fuzzy_match_cache[cache_key] = is_valid

        if not is_valid:
            hallucinated_names.append(name)

    if hallucinated_names:
        logger.warning(f"⚠️ LLM mencionó productos dudosos: {hallucinated_names}")
        return {
            "valid": False,
            "hallucinated_names": hallucinated_names,
            "action": "fallback"
        }

    return {"valid": True}


def validate_and_fix_response(reply: str, allowed_products: list, phone: str, execution_context: dict) -> str:
    """
    Valida la respuesta y la regenera si tiene alucinaciones.
    """
    code_validation = validate_response_codes(reply, allowed_products)
    name_validation = validate_mentioned_names(reply, allowed_products)

    execution_context["validation"] = {
        "codes": code_validation,
        "names": name_validation
    }

    if not code_validation.get("valid") or not name_validation.get("valid"):
        logger.warning("Respuesta con posibles alucinaciones detectadas; sugiero reintentar el razonamiento.")
        execution_context["validation"]["needs_retry"] = True
        note = "Detecté un código o nombre raro. ¿Me repetís marca/modelo o querés que lo vuelva a calcular?"
        return f"{reply}\n\n_{note}_"

    return reply

# ------------------------------------------------------------
# AUTOCORRECTOR
# ------------------------------------------------------------
CATEGORY_MAP = {
    "amortiguador": ["amort", "amortiguador", "shock", "suspension", "suspensión"],
    "bateria": [
        "bateria", "batería", "battery", "baterias", "baterías",
        "ytx", "yb", "yt", "agm", "gel", "litio",
        "12v", "14a", "16l", "fp"
    ],
    "aceite": ["aceite", "oil", "lubricante"],
    "filtro": ["filtro", "filter", "filtros"],
    "cadena": ["cadena", "chain"],
    "bujia": ["bujia", "bujía", "spark"],
}
BRAND_LIST = [
    "appia", "bajaj", "benelli", "beta", "brava", "corven", "gilera", "guerrero", "hero", "honda",
    "husqvarna", "kawasaki", "keeway", "keller", "kymco", "mondial", "motomel", "moto guzzi",
    "nsu", "suzuki", "tvs", "yamaha", "zanella"
]
MODEL_LIST = [
    "50", "70", "80", "90", "100", "110", "125", "135", "150", "160", "180", "200", "220", "250", "300",
    "ax100", "biz", "blade", "blitz", "boxer", "c50", "c70", "c90", "cb", "cg", "crypton", "dominar",
    "dakar", "due", "eco", "en125", "energy", "falcon", "fazer", "fire", "flash", "fly", "fz", "gixxer",
    "gn125", "go", "hd", "hunter", "jet", "jog", "k1", "k2", "k3", "k4", "kmx", "liberty", "luxe", "magic",
    "monkey", "motard", "navi", "ns", "pulsar", "rc", "rks", "road", "rocket", "rouser", "rs", "rx",
    "sahel", "sempre", "sma", "sol", "sonic", "sprinter", "starken", "storm", "styler", "super cub",
    "tiburon", "tiger", "titan", "tornado", "triax", "tricolor", "twister", "vc", "vento", "viggo", "vr",
    "wave", "x3m", "xr", "xtz", "zb", "ztt"
]

KNOWN_BRANDS = BRAND_LIST
KNOWN_MODELS = MODEL_LIST


def _build_autocorrect_vocab():
    base_tokens = []
    for cat, variants in CATEGORY_MAP.items():
        base_tokens.append(cat)
        base_tokens.extend(variants)
    base_tokens.extend(BRAND_LIST)
    base_tokens.extend(MODEL_LIST)
    base_tokens.extend([
        "pastilla", "disco", "embrague", "regulador", "rectificador",
        "carburador", "inyector", "tanque", "asiento", "manubrio",
        "espejo", "cubierta", "neumatico", "neumático", "ruleman", "rodamiento",
        "corona", "piñon", "piñón", "kit transmision", "kit freno", "amortiguador"
    ])
    vocab = {normalize_search_query(t) for t in base_tokens if t}
    return sorted(vocab)


AUTOCORRECT_VOCAB = _build_autocorrect_vocab()

_BRANDS_NORMALIZED = {normalize_search_query(b) for b in BRAND_LIST}
_MODELS_NORMALIZED = {normalize_search_query(m) for m in MODEL_LIST}

_BRAND_NORMALIZED_MAP = {normalize_search_query(b): b for b in BRAND_LIST}
_MODEL_NORMALIZED_MAP = {normalize_search_query(m): m for m in MODEL_LIST}
_CATEGORY_VARIANT_TOKENS = {
    normalize_search_query(v)
    for variants in CATEGORY_MAP.values()
    for v in variants
}


def _looks_like_code_or_number(token: str) -> bool:
    if not token:
        return False
    if token.isdigit():
        return True
    if re.match(r"^\d{4}/\d{5}-\d{3}$", token):
        return True
    if re.match(r"^\d{3,}[/-]\d+", token):
        return True
    return False


def autocorrect_keywords(text: str):
    if not text:
        return "", []

    tokens = text.split()
    corrections = []
    new_tokens = []

    for tok in tokens:
        raw = tok
        base = normalize_search_query(tok)

        if _looks_like_code_or_number(raw):
            new_tokens.append(raw)
            continue

        if base in _BRANDS_NORMALIZED or base in _MODELS_NORMALIZED:
            new_tokens.append(raw)
            continue

        if base in _CATEGORY_VARIANT_TOKENS:
            new_tokens.append(raw)
            continue

        if len(base) <= 3:
            new_tokens.append(raw)
            continue

        # Correcciones específicas para marcas/modelos mal tipeados (p.ej. "gonda" → "honda")
        brand_suggestion = None
        model_suggestion = None
        try:
            brand_suggestion = process.extractOne(base, _BRANDS_NORMALIZED, scorer=fuzz.ratio)
            model_suggestion = process.extractOne(base, _MODELS_NORMALIZED, scorer=fuzz.ratio)
        except Exception:
            brand_suggestion = None
            model_suggestion = None

        if brand_suggestion and brand_suggestion[1] >= 82:
            corrected = _BRAND_NORMALIZED_MAP.get(brand_suggestion[0], brand_suggestion[0])
            new_tokens.append(corrected)
            corrections.append(f"{raw}→{corrected}")
            continue

        if model_suggestion and model_suggestion[1] >= 85:
            corrected = _MODEL_NORMALIZED_MAP.get(model_suggestion[0], model_suggestion[0])
            new_tokens.append(corrected)
            corrections.append(f"{raw}→{corrected}")
            continue

        try:
            best = process.extractOne(base, AUTOCORRECT_VOCAB, scorer=fuzz.ratio)
        except Exception:
            best = None

        if best and best[1] >= 94 and best[0] != base:
            corrected = best[0]
            new_tokens.append(corrected)
            corrections.append(f"{raw}→{corrected}")
        else:
            new_tokens.append(raw)

    corrected_text = " ".join(new_tokens)
    return corrected_text, corrections

# ------------------------------------------------------------
# DETECCIÓN DE FAMILIAS EN QUERY
# ------------------------------------------------------------
def _ensure_family_idf():
    global FAMILIES_TOKEN_IDF, FAMILIES_TOKEN_IDF_DEFAULT

    if FAMILIES_TOKEN_IDF and FAMILIES_TOKEN_IDF_DEFAULT:
        return

    if not FAMILIES_INDEX:
        FAMILIES_TOKEN_IDF = {}
        FAMILIES_TOKEN_IDF_DEFAULT = 1.0
        return

    total_families = len(FAMILIES_INDEX)
    df = Counter()

    for fam in FAMILIES_INDEX:
        tokens = fam.get("tokens") or []
        for t in set(tokens):
            df[t] += 1

    if not df:
        FAMILIES_TOKEN_IDF = {}
        FAMILIES_TOKEN_IDF_DEFAULT = 1.0
        return

    FAMILIES_TOKEN_IDF = {
        token: max(0.0, math.log((total_families + 1) / (freq + 1)))
        for token, freq in df.items()
    }

    values = list(FAMILIES_TOKEN_IDF.values())
    FAMILIES_TOKEN_IDF_DEFAULT = float(np.median(values)) if values else 1.0


def detect_families_in_query(query: str):
    """
    Usa FAMILIES_INDEX para detectar familias mencionadas en el texto.
    - Pondera tokens por IDF dinámico construido desde las familias del catálogo
    - Evita descartar palabras manualmente y privilegia términos distintivos
    """
    if not query or not FAMILIES_INDEX:
        return []

    _ensure_family_idf()

    q_norm = normalize_search_query(query)
    if not q_norm:
        return []

    query_tokens = {w for w in q_norm.split() if len(w) >= 3}

    results = []
    for fam in FAMILIES_INDEX:
        fam_name_norm = fam.get("family_name_norm", "")
        fam_tokens = fam.get("tokens") or [w for w in fam_name_norm.split() if len(w) >= 3]

        if not fam_name_norm or not fam_tokens:
            continue

        matching_tokens = [w for w in fam_tokens if (w in query_tokens or w in q_norm)]
        if not matching_tokens:
            continue

        token_score = sum(FAMILIES_TOKEN_IDF.get(w, FAMILIES_TOKEN_IDF_DEFAULT) for w in matching_tokens)
        popularity_bonus = min(fam.get("count", 0), 50) / 20.0

        if fam_name_norm in q_norm:
            token_score *= 1.25

        score = token_score + popularity_bonus

        if score > 0:
            results.append((fam_name_norm, score))

    if not results:
        return []

    results.sort(key=lambda x: x[1], reverse=True)
    dynamic_threshold = max(np.percentile([s for _, s in results], 60), 0.5)
    selected = [name for name, score in results if score >= dynamic_threshold]
    return selected[:5]

# ------------------------------------------------------------
# SEMANTIC INTENT OVERRIDE (LINGUISTIC + HEURISTIC LAYER)
# ------------------------------------------------------------
PURCHASE_VERBS = {
    "necesito",
    "busco",
    "quiero",
    "tenes",
    "tenés",
    "vendes",
    "vendés",
    "vende",
    "conseguis",
    "consigues",
    "recomendame",
    "recomendas",
    "recomendás",
}

SOCIAL_MARKERS = {
    "hola",
    "buenas",
    "gracias",
    "cómo estás",
    "como estas",
    "buen dia",
    "buen día",
    "que tal",
    "qué tal",
    "buenas tardes",
    "buenas noches",
}

FOLLOW_UP_MARKERS = {
    "de nuevo",
    "otra vez",
    "lo de antes",
    "como te decia",
    "como te decía",
    "sobre las",
    "sobre los",
    "ahora",
    "ademas",
    "además",
}

# AUTOCORRECT_VOCAB is a list; convert to set for union operations.
TECH_LEXICAL_ROOTS = set(AUTOCORRECT_VOCAB) | {
    "repuesto",
    "respuesto",
    "repuestos",
    "pieza",
    "pieza",
    "piezas",
    "parte",
    "partes",
    "codigo",
    "código",
    "códigos",
    "codigos",
    "amortiguadores",
    "bujias",
    "pastillas",
}


def detect_semantic_entities(message: str) -> dict:
    """Detect technical entities and discourse markers without catalog hardcodes.

    Combines token similarity, fuzzy brand/model detection and purchase verbs to
    decide whether the query contains product-seeking evidence. Also surfaces
    social/follow-up cues so the orchestrator can build compound intents.
    """

    normalized = normalize_search_query(message)
    tokens = [t for t in re.split(r"[^\wáéíóúüñ]+", normalized) if t]

    purchase_hits = [v for v in PURCHASE_VERBS if re.search(rf"\b{v}\b", normalized)]
    social_hits = [s for s in SOCIAL_MARKERS if re.search(rf"\b{s}\b", normalized)]
    follow_up_hits = [s for s in FOLLOW_UP_MARKERS if re.search(rf"\b{s}\b", normalized)]

    technical_tokens = [t for t in tokens if t in TECH_LEXICAL_ROOTS]
    numeric_codes = [t for t in tokens if _looks_like_code_or_number(t)]

    # FIX: Si es un saludo simple (1-2 palabras con marcador social), no hacer fuzzy matching
    # para evitar falsos positivos que contaminen el flujo
    is_simple_greeting = bool(social_hits) and len(tokens) <= 2 and not purchase_hits and not technical_tokens

    fuzzy_brands = []
    fuzzy_models = []

    # Solo hacer fuzzy matching si NO es un saludo simple
    if not is_simple_greeting:
        for tok in tokens:
            if len(tok) < 3:
                continue
            try:
                brand_match = process.extractOne(tok, _BRANDS_NORMALIZED, scorer=fuzz.partial_ratio)
                model_match = process.extractOne(tok, _MODELS_NORMALIZED, scorer=fuzz.partial_ratio)
            except Exception:
                brand_match = None
                model_match = None

            if brand_match and brand_match[1] >= 88:
                fuzzy_brands.append(_BRAND_NORMALIZED_MAP.get(brand_match[0], brand_match[0]))
            if model_match and model_match[1] >= 88:
                fuzzy_models.append(_MODEL_NORMALIZED_MAP.get(model_match[0], model_match[0]))

    has_technical = bool(
        technical_tokens
        or purchase_hits
        or fuzzy_brands
        or fuzzy_models
        or numeric_codes
    )

    has_social = bool(social_hits)
    has_follow_up = bool(follow_up_hits)

    return {
        "tokens": tokens,
        "technical_tokens": list(set(technical_tokens)),
        "purchase_verbs": list(set(purchase_hits)),
        "brands": list(set(fuzzy_brands)),
        "models": list(set(fuzzy_models)),
        "codes": numeric_codes,
        "social_markers": list(set(social_hits)),
        "follow_up_markers": list(set(follow_up_hits)),
        "has_social": has_social,
        "has_follow_up": has_follow_up,
        "has_technical": has_technical,
        "is_simple_greeting": is_simple_greeting,
    }


def merge_intents_with_semantics(
    semantic_signals: dict, llm_intent: str | None, llm_semantic_intents: list[str] | None = None
) -> list[str]:
    """Fuse deterministic semantic evidence with the LLM intent guess.

    - If technical entities are present, product_search is mandatory and the
      social-only path is disabled.
    - Social/follow_up markers are preserved to build compound replies.
    - The LLM remains for disambiguation but cannot suppress product intents
      when evidence is strong.
    """

    intents: list[str] = []

    if semantic_signals.get("social_markers"):
        intents.append("social")
    if semantic_signals.get("follow_up_markers"):
        intents.append("follow_up")
    if semantic_signals.get("has_technical"):
        intents.append("product_search")

    llm_intents: list[str] = []
    if llm_intent:
        llm_intents.append(llm_intent)
    if llm_semantic_intents:
        for cand in llm_semantic_intents:
            if cand and cand not in llm_intents:
                llm_intents.append(cand)

    for candidate in llm_intents:
        if candidate == "social" and semantic_signals.get("has_technical"):
            if "social" not in intents:
                intents.append("social")
            if "product_search" not in intents:
                intents.append("product_search")
        elif candidate and candidate not in intents:
            intents.append(candidate)

    if not intents:
        intents.append(llm_intent or "product_search")

    return intents

# ------------------------------------------------------------
# QUERY PARSING
# ------------------------------------------------------------
def parse_query_v2(query: str, phone: str | None = None) -> dict:
    q = normalize_search_query(query)
    tokens = q.split()
    out = {
        "brands": [],
        "models": [],
        "category": None,
        "categories": [],
        "families": [],
        "raw": q,
        "moto_brands": [],
        "moto_models": [],
        "displacement": None,
        "final_category": None,
        "motos_detectadas": [],
    }

    for cat, variants in CATEGORY_MAP.items():
        if any(v in q for v in variants):
            out["categories"].append(cat)

    out["category"] = out["categories"][0] if out["categories"] else None

    for b in BRAND_LIST:
        if b in q:
            out["brands"].append(b)

    for m in MODEL_LIST:
        if m in q:
            out["models"].append(m)

    for brand in KNOWN_BRANDS:
        for model in KNOWN_MODELS:
            if brand in q and model in q:
                out["motos_detectadas"].append({
                    "brand": brand,
                    "model": model
                })

    if "esa moto" in q or "esa misma" in q:
        ctx = get_moto_context(phone)
        if ctx:
            out["motos_detectadas"].append(ctx)

    out["families"] = detect_families_in_query(q)

    if out["motos_detectadas"]:
        out["moto_brands"] = list({m["brand"] for m in out["motos_detectadas"] if m.get("brand")})
        out["moto_models"] = list({m["model"] for m in out["motos_detectadas"] if m.get("model")})

    for m in out["motos_detectadas"]:
        save_moto_context(phone, m.get("brand", ""), m.get("model", ""))

    return out


def filter_catalog(catalog, parsed):
    if not catalog:
        return []
    global LAST_FILTER_CATALOG_DEBUG
    brands = set(parsed.get("brands") or [])
    models = set(parsed.get("models") or [])
    cats = parsed.get("categories") or []
    families = set(parsed.get("families") or [])
    moto_brands = set(parsed.get("moto_brands") or [])
    moto_models = set(parsed.get("moto_models") or [])
    motos_detectadas = parsed.get("motos_detectadas") or []
    displacement = parsed.get("displacement")
    final_category = parsed.get("final_category")

    reject_reasons = Counter()
    filtered = []

    for p in catalog:
        reason = None

        if brands:
            p_brand = normalize_search_query(p.get("brand", ""))
            if not any(b in p_brand for b in brands):
                reason = "brand_mismatch"

        if reason is None and moto_brands:
            p_moto_brand = normalize_search_query(p.get("moto_brand", "") or p.get("brand", ""))
            if not any(b in p_moto_brand for b in moto_brands):
                reason = "moto_brand_mismatch"

        if reason is None and models:
            p_model = normalize_search_query(p.get("model", ""))
            if not any(m in p_model for m in models):
                reason = "model_mismatch"

        if reason is None and moto_models:
            p_moto_model = normalize_search_query(p.get("moto_model", "") or p.get("model", ""))
            if not any(m in p_moto_model for m in moto_models):
                reason = "moto_model_mismatch"

        if reason is None and motos_detectadas:
            p_moto_brand = normalize_search_query(p.get("moto_brand", "") or p.get("brand", ""))
            p_moto_model = normalize_search_query(p.get("moto_model", "") or p.get("model", ""))
            if not p_moto_brand or not p_moto_model:
                reason = "moto_data_missing"
            elif not any(
                normalize_search_query(m.get("brand", "")) in p_moto_brand and
                normalize_search_query(m.get("model", "")) in p_moto_model
                for m in motos_detectadas
            ):
                reason = "moto_mismatch"

        if reason is None and families:
            p_family = normalize_search_query(p.get("family_name", ""))
            if not p_family:
                reason = "family_missing"
            elif not any(f in p_family for f in families):
                reason = "family_mismatch"

        if reason is None and cats:
            p_cat = normalize_search_query(p.get("category", ""))

            # Batería tiene reglas especiales
            if "bateria" in cats:
                name_norm = normalize_search_query(p.get("name", ""))
                if not any(x in name_norm for x in ["ytx", "yb", "yt", "gel", "agm", "litio", "12v"]):
                    if not any(v in p_cat for v in CATEGORY_MAP.get("bateria", ["bateria"])):
                        reason = "category_mismatch"

            if reason is None:
                # Otras categorías
                other_cats = [c for c in cats if c != "bateria"]
                if other_cats:
                    if not any(
                        any(v in p_cat for v in CATEGORY_MAP.get(c, [c]))
                        for c in other_cats
                    ):
                        reason = "category_mismatch"

        if reason is None and final_category:
            p_final_cat = normalize_search_query(p.get("final_category", "") or p.get("category", ""))
            if not p_final_cat:
                reason = "final_category_missing"
            elif final_category not in p_final_cat:
                reason = "final_category_mismatch"

        if reason is None and displacement:
            p_disp = normalize_search_query(p.get("displacement", ""))
            if not p_disp:
                reason = "displacement_missing"
            elif displacement not in p_disp:
                reason = "displacement_mismatch"

        if reason:
            reject_reasons[reason] += 1
            continue

        filtered.append(p)

    LAST_FILTER_CATALOG_DEBUG = {
        "input_count": len(catalog),
        "output_count": len(filtered),
        "rejections": dict(reject_reasons),
        "parsed": parsed,
    }
    logger.info(
        "[DEBUG][Filtro] Rechazos: %s | Resultado: %s/%s",
        dict(reject_reasons),
        len(filtered),
        len(catalog),
    )

    return filtered

# ------------------------------------------------------------
# PENDING ACTIONS (MEJORADAS EN 3.13)
# ------------------------------------------------------------
def compute_cart_hash_from_items(items):
    """
    Crea un hash simple del carrito (code, qty) para asegurar que
    la confirmación se haga sobre el mismo estado.
    """
    try:
        simple = sorted([(i[0], int(i[1])) for i in items if len(i) >= 2])
        snapshot = json.dumps(simple, ensure_ascii=False)
        cart_hash = hashlib.md5(f"{snapshot}_{datetime.now().isoformat()}".encode()).hexdigest()
        return cart_hash
    except Exception as e:
        logger.error(f"Error computando cart_hash: {e}")
        return ""


def save_pending_action(phone, action_type, action_data, context="", ttl_minutes=30):
    if not phone:
        return
    try:
        items = cart_get(phone)
        cart_hash = compute_cart_hash_from_items(items)
        action_data["cart_hash"] = cart_hash
        expires_at = (datetime.now() + timedelta(minutes=ttl_minutes)).isoformat()
        with get_db_connection() as conn:
            conn.execute(
                """INSERT INTO pending_actions (phone, action_type, action_data, context, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                action_type=excluded.action_type,
                action_data=excluded.action_data,
                context=excluded.context,
                created_at=excluded.created_at,
                expires_at=excluded.expires_at""",
                (phone, action_type, json.dumps(action_data), context, datetime.now().isoformat(), expires_at)
            )
    except Exception as e:
        logger.error(f"Error guardando pending_action: {e}")


def get_pending_action(phone):
    if not phone:
        return None
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT action_type, action_data, context, created_at FROM pending_actions "
                "WHERE phone=? AND expires_at > ?",
                (phone, datetime.now().isoformat())
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "action_type": row[0],
                "action_data": json.loads(row[1]) if row[1] else {},
                "context": row[2],
                "created_at": row[3]
            }
    except Exception as e:
        logger.error(f"Error leyendo pending_action: {e}")
        return None


def clear_pending_action(phone):
    if not phone:
        return
    try:
        with get_db_connection() as conn:
            conn.execute("DELETE FROM pending_actions WHERE phone=?", (phone,))
    except Exception as e:
        logger.error(f"Error limpiando pending_action: {e}")


def get_sales_phase(phone):
    if not phone:
        return None
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT phase FROM conversation_phase WHERE phone=?", (phone,))
            row = cur.fetchone()
            return row[0] if row else None
    except Exception as e:
        logger.error(f"Error leyendo sales_phase: {e}")
        return None


def set_sales_phase(phone, phase):
    if not phone:
        return
    try:
        with get_db_connection() as conn:
            if phase:
                conn.execute(
                    """INSERT INTO conversation_phase (phone, phase, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(phone) DO UPDATE SET
                            phase=excluded.phase,
                            updated_at=excluded.updated_at""",
                    (phone, phase, datetime.now().isoformat())
                )
            else:
                conn.execute("DELETE FROM conversation_phase WHERE phone=?", (phone,))
    except Exception as e:
        logger.error(f"Error guardando sales_phase: {e}")


def update_sales_phase_from_intent(phone, intent):
    phase_map = {
        "product_search": "search",
        "cart_action": "cart",
        "view_cart": "cart",
        "order_flow": "checkout",
        "payment": "payment",
        "shipping": "shipping",
        "tech_expert": "advice",
        "empty_cart": "search",
        "negation": None,
        "small_talk": None,
        "confirmation": None
    }
    phase = phase_map.get(intent)
    if phase is None:
        if intent in {"negation"}:
            set_sales_phase(phone, None)
        return
    set_sales_phase(phone, phase)


def apply_add_each_quantity_pending(phone, pending):
    """
    Ejecuta la acción pendiente de "agregar N de cada uno" usando el carrito
    y la última búsqueda guardada al momento de crear la acción.
    """
    try:
        data = pending.get("action_data") or {}
        qty_each = int(data.get("qty", 1) or 1)
        products = data.get("products") or []
        pending_hash = data.get("cart_hash") or ""
        created_at_raw = pending.get("created_at")

        if created_at_raw:
            try:
                created_dt = datetime.fromisoformat(created_at_raw)
                if datetime.now() - created_dt > timedelta(minutes=5):
                    return "Tu carrito cambió desde que armé esa lista, repetíme el pedido así no le pifio"
            except Exception as e:
                logger.error(f"Error validando antigüedad de pending_action: {e}", exc_info=True)

        if not products or qty_each <= 0:
            return "No tengo lista la selección anterior, repetíme el pedido."

        current_items = cart_get(phone)
        current_hash = compute_cart_hash_from_items(current_items)

        if pending_hash and current_hash and current_hash != pending_hash:
            return "Tu carrito cambió desde que armé esa lista. Repetíme el pedido así no le pifio."

        added = 0
        total = Decimal("0")

        for p in products:
            code = p.get("code")
            name = p.get("name", "")
            price_ars = to_decimal_money(p.get("price_ars", 0))
            price_usd = to_decimal_money(p.get("price_usd", 0))
            if not code:
                continue
            ok = cart_add(phone, code, qty_each, name, price_ars, price_usd)
            if ok:
                added += 1
                total += price_ars * qty_each

        if added == 0:
            return "No pude agregar esos productos al carrito, repetíme el pedido."

        total = total.quantize(Decimal("0.01"))
        return (
            f"Listo, agregué {qty_each} unidad(es) de cada uno de los {added} productos al carrito.\n"
            f"Total estimado: {format_price(total)}"
        )
    except Exception as e:
        logger.error(f"Error aplicando pending_action: {e}")
        return "Tuve un problema al confirmar la lista, repetíme el pedido por favor."

# ------------------------------------------------------------------
# DATABASE
# ------------------------------------------------------------------
@contextmanager
def get_db_connection():
    conn = None
    try:
        db_dir = os.path.dirname(DB_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
    except Exception as e:
        logger.warning(f"No se pudo crear dir DB: {e}")

    for attempt in range(3):
        try:
            conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            yield conn
            conn.commit()
            return
        except sqlite3.OperationalError as e:
            if conn:
                conn.close()
                conn = None
            if "locked" in str(e) and attempt < 2:
                time.sleep(0.25 * (attempt + 1))
                continue
            logger.error(f"DB error: {e}")
            raise
        except Exception as e:
            if conn:
                conn.close()
            logger.error(f"DB error: {e}")
            raise
        finally:
            if conn:
                conn.close()


def get_db():
    try:
        db_dir = os.path.dirname(DB_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
    except Exception as e:
        logger.warning(f"No se pudo preparar el directorio de DB: {e}")

    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def save_moto_context(phone, brand, model):
    if not phone or not brand or not model:
        return
    try:
        conn = get_db()
        conn.execute(
            """
                INSERT INTO moto_context (phone, brand, model, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(phone)
                DO UPDATE SET brand=?, model=?, updated_at=CURRENT_TIMESTAMP
            """,
            (phone, brand, model, brand, model)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Error guardando contexto de moto: {e}")
    finally:
        try:
            conn.close()
        except Exception as e:
            logger.error(f"Error cerrando conexión de moto_context: {e}", exc_info=True)


def get_moto_context(phone):
    if not phone:
        return None
    try:
        conn = get_db()
        row = conn.execute("SELECT brand, model FROM moto_context WHERE phone=?", (phone,)).fetchone()
        if not row:
            return None
        return {"brand": row[0], "model": row[1]}
    except Exception as e:
        logger.error(f"Error obteniendo contexto de moto: {e}")
        return None
    finally:
        try:
            conn.close()
        except Exception as e:
            logger.error(f"Error cerrando conexión tras leer moto_context: {e}", exc_info=True)


def init_db():
    with get_db_connection() as conn:
        c = conn.cursor()
        try:
            c.execute("PRAGMA journal_mode=WAL;")
        except Exception as e:
            logger.warning(f"No se pudo activar WAL: {e}")

        c.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                phone TEXT, message TEXT, role TEXT, timestamp TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_conv_phone_timestamp ON conversations(phone, timestamp DESC)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS carts (
                phone TEXT, code TEXT, quantity INTEGER, name TEXT,
                price_ars TEXT, price_usd TEXT, created_at TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_cart_phone ON carts(phone)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS user_state (
                phone TEXT PRIMARY KEY, last_code TEXT, last_name TEXT,
                last_price_ars TEXT, updated_at TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT, products_json TEXT, query TEXT, timestamp TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_search_phone_timestamp ON search_history(phone, timestamp DESC)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS last_search (
                phone TEXT PRIMARY KEY, products_json TEXT, query TEXT, timestamp TEXT, metadata TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY, phone TEXT, customer_name TEXT,
                customer_address TEXT, items_json TEXT, total_ars TEXT,
                status TEXT, created_at TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS bulk_jobs (
                job_id TEXT PRIMARY KEY, phone TEXT, raw_list TEXT,
                total_items INTEGER, processed_items INTEGER, found_items INTEGER,
                results_json TEXT, status TEXT, created_at TEXT, completed_at TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT, message TEXT, intent_detected TEXT,
                products_count INTEGER, timestamp TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_interactions_phone_timestamp ON interactions(phone, timestamp DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_interactions_intent ON interactions(intent_detected)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS performance_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT, intent TEXT, duration_ms INTEGER,
                results_count INTEGER, timestamp TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS quality_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT,
                query TEXT,
                avg_score REAL,
                max_score REAL,
                relevant_count INTEGER,
                created_at TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS pending_actions (
                phone TEXT PRIMARY KEY,
                action_type TEXT,
                action_data TEXT,
                context TEXT,
                created_at TEXT,
                expires_at TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_pending_actions_expires ON pending_actions(expires_at)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS conversation_phase (
                phone TEXT PRIMARY KEY,
                phase TEXT,
                updated_at TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS moto_context (
                phone TEXT PRIMARY KEY,
                brand TEXT,
                model TEXT,
                updated_at TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS template_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT,
                template_name TEXT,
                input_json TEXT,
                output_json TEXT,
                duration_ms INTEGER,
                created_at TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_template_logs_phone_created ON template_logs(phone, created_at DESC)")

# ------------------------------------------------------------------
# ANALYTICS
# ------------------------------------------------------------------
def log_interaction(phone, message, intent, products_count=0):
    if not phone:
        return
    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO interactions (phone, message, intent_detected, products_count, timestamp) VALUES (?, ?, ?, ?, ?)",
                (phone, message[:200], intent, products_count, datetime.now().isoformat())
            )
    except Exception as e:
        logger.error(f"Error logging interaction: {e}")


def log_performance(phone, intent, duration, results_count):
    if not phone:
        return
    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO performance_metrics (phone, intent, duration_ms, results_count, timestamp) VALUES (?, ?, ?, ?, ?)",
                (phone, intent, int(duration * 1000), results_count, datetime.now().isoformat())
            )
    except Exception as e:
        logger.error(f"Error logging metrics: {e}")


def log_quality_metrics(phone, query, avg_score, max_score, relevant_count):
    if not phone:
        return
    try:
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO quality_metrics (phone, query, avg_score, max_score, relevant_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    phone,
                    (query or "")[:200],
                    float(avg_score),
                    float(max_score),
                    int(relevant_count),
                    datetime.now().isoformat(),
                )
            )
    except Exception as e:
        logger.error(f"Error guardando métricas de calidad: {e}")

# ------------------------------------------------------------------
# TIPO DE CAMBIO
# ------------------------------------------------------------------
def get_exchange_rate():
    with exchange_lock:
        now = datetime.now().timestamp()

        if exchange_cache["rate"] and exchange_cache["timestamp"]:
            age = now - exchange_cache["timestamp"]
            if age < EXCHANGE_CACHE_TTL:
                return exchange_cache["rate"]

    def _fetch_rate():
        response = http_client.get(requests, EXCHANGE_API_URL)
        response.raise_for_status()
        venta = response.json().get("venta", None)
        return to_decimal_money(venta) if venta is not None else DEFAULT_EXCHANGE

    try:
        with track_step("exchange.rate"):
            rate = _fetch_rate()
        with exchange_lock:
            exchange_cache["rate"] = rate
            exchange_cache["timestamp"] = datetime.now().timestamp()
        return rate
    except Exception as e:
        logger.warning(f"Fallo tasa cambio: {e}")
        with exchange_lock:
            if exchange_cache["rate"] is None:
                exchange_cache["rate"] = DEFAULT_EXCHANGE
            return exchange_cache["rate"]

# ------------------------------------------------------------------
# RATE LIMIT
# ------------------------------------------------------------------
def rate_limit_check(phone):
    if not phone:
        return True
    try:
        now = datetime.now().timestamp()
        user_requests[phone] = [t for t in user_requests[phone] if now - t < RATE_WINDOW]
        if len(user_requests[phone]) >= RATE_LIMIT:
            return False
        user_requests[phone].append(now)
        return True
    except Exception as e:
        logger.error(f"Error en rate_limit_check: {e}")
        return True

# ------------------------------------------------------------------
# PERSISTENCIA
# ------------------------------------------------------------------
def save_message(phone, msg, role):
    if not phone or not msg:
        return
    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO conversations VALUES (?, ?, ?, ?)",
                (phone, msg, role, datetime.now().isoformat())
            )
    except Exception as e:
        logger.error(f"Error guardando mensaje: {e}")


def get_history_since(phone, days=7, limit=2000):
    if not phone:
        return []
    try:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT message, role, timestamp FROM conversations "
                "WHERE phone = ? AND timestamp >= ? ORDER BY timestamp ASC LIMIT ?",
                (phone, since, limit)
            )
            rows = cur.fetchall()
            return [{"role": r[1], "content": r[0], "timestamp": r[2]} for r in rows]
    except Exception as e:
        logger.error(f"Error leyendo historial: {e}")
        return []


def save_to_search_history(phone, products, query):
    if not phone or not products:
        return
    try:
        serializable = [
            {
                "code": p.get("code", ""),
                "name": p.get("name", ""),
                "price_ars": float(p.get("price_ars", 0)),
                "price_usd": float(p.get("price_usd", 0)),
                "qty": int(p.get("qty", 1))
            }
            for p in products
        ]
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO search_history (phone, products_json, query, timestamp) VALUES (?, ?, ?, ?)",
                (phone, json.dumps(serializable, ensure_ascii=False), query or "", datetime.now().isoformat())
            )

            cur = conn.cursor()
            cur.execute(
                "SELECT id FROM search_history WHERE phone=? ORDER BY timestamp DESC LIMIT -1 OFFSET 5",
                (phone,)
            )
            old_ids = [r[0] for r in cur.fetchall()]
            if old_ids:
                placeholders = ",".join("?" * len(old_ids))
                conn.execute(f"DELETE FROM search_history WHERE id IN ({placeholders})", old_ids)
    except Exception as e:
        logger.error(f"Error guardando search_history: {e}")


def get_search_history(phone, limit=5):
    if not phone:
        return []
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT products_json, query, timestamp FROM search_history "
                "WHERE phone=? ORDER BY timestamp DESC LIMIT ?",
                (phone, limit)
            )
            rows = cur.fetchall()
            return [{"products": json.loads(r[0]), "query": r[1], "timestamp": r[2]} for r in rows]
    except Exception as e:
        logger.error(f"Error leyendo search_history: {e}")
        return []


def save_last_search(phone, products, query):
    if not phone or not products:
        return
    meta = {
        "products": products,
        "query": query,
        "timestamp": datetime.now().isoformat(),
        "summary": f"{len(products)} productos",
        "top_category": max(
            set(p.get("category", "") for p in products),
            key=lambda c: sum(1 for p in products if p.get("category") == c),
            default=""
        ),
        "total_value": sum(float(p.get("price_ars", 0)) for p in products)
    }
    try:
        with get_db_connection() as conn:
            conn.execute(
                """INSERT INTO last_search (phone, products_json, query, timestamp, metadata)
                VALUES (?,?,?,?,?)
                ON CONFLICT(phone) DO UPDATE SET
                  products_json=excluded.products_json,
                  query=excluded.query,
                  timestamp=excluded.timestamp,
                  metadata=excluded.metadata""",
                (phone, json.dumps(meta["products"], ensure_ascii=False), query, meta["timestamp"], json.dumps(meta, ensure_ascii=False))
            )
    except Exception as e:
        logger.error(f"save_last_search error: {e}")


def get_last_search(phone):
    if not phone:
        return None
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT products_json, query, metadata, timestamp FROM last_search WHERE phone=?", (phone,))
            row = cur.fetchone()
            if not row:
                return None

            timestamp = datetime.fromisoformat(row[3])
            age_minutes = (datetime.now() - timestamp).total_seconds() / 60

            # Si pasaron más de 10 minutos, no usar ese contexto
            if age_minutes > 10:
                logger.info(f"Last search for {phone} is {age_minutes:.1f} min old, ignoring")
                return None

            return {
                "products": json.loads(row[0]),
                "query": row[1],
                "metadata": json.loads(row[2]) if row[2] else {},
                "age_minutes": age_minutes
            }
    except Exception as e:
        logger.error(f"get_last_search error: {e}")
        return None


def create_order(phone, customer_name, customer_address, items, total_ars):
    if not phone:
        return None
    try:
        real_total, _ = cart_totals(phone)
        if abs(Decimal(total_ars) - real_total) > Decimal("0.01"):
            raise ValueError("Total manipulado")

        order_id = f"ORD-{int(time.time()*10)}"
        with get_db_connection() as conn:
            conn.execute(
                """INSERT INTO orders
                (order_id, phone, customer_name, customer_address, items_json, total_ars, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, phone, customer_name, customer_address, json.dumps(items), total_ars, "confirmed", datetime.now().isoformat())
            )
            conn.execute("DELETE FROM carts WHERE phone=?", (phone,))
        return order_id
    except Exception as e:
        logger.error(f"Error creando orden: {e}")
        return None

# ------------------------------------------------------------------
# CARRITO
# ------------------------------------------------------------------
def cart_add(phone, code, qty, name, price_ars, price_usd):
    if not phone or not code:
        return False
    try:
        qty = max(1, min(int(qty or 1), 1000))
        price_ars = price_ars.quantize(Decimal("0.01"))
        price_usd = price_usd.quantize(Decimal("0.01"))

        catalog, _idx, _, _ = get_catalog_and_index()
        prod = next((p for p in catalog if p["code"] == code), None)
        if prod is None:
            logger.warning(f"Producto {code} no existe en catalogo")
            return False

        with cart_lock:
            with get_db_connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT quantity FROM carts WHERE phone=? AND code=?", (phone, code))
                row = cur.fetchone()
                now = datetime.now().isoformat()

                if row:
                    new_qty = int(row[0]) + qty
                    cur.execute(
                        "UPDATE carts SET quantity=?, created_at=? WHERE phone=? AND code=?",
                        (new_qty, now, phone, code)
                    )
                else:
                    cur.execute(
                        """INSERT INTO carts (phone, code, quantity, name, price_ars, price_usd, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (phone, code, qty, name, str(price_ars), str(price_usd), now)
                    )
        return True
    except Exception as e:
        logger.error(f"Error en cart_add: {e}")
        return False


def cart_get(phone, max_age_hours=168):
    if not phone:
        return []
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat()
            cur.execute("DELETE FROM carts WHERE phone=? AND created_at < ?", (phone, cutoff))
            cur.execute("SELECT code, quantity, name, price_ars FROM carts WHERE phone=?", (phone,))
            rows = cur.fetchall()
            out = []
            for r in rows:
                code, q, name, price_str = r[0], int(r[1]), r[2], r[3]
                price_dec = to_decimal_money(price_str)
                out.append((code, q, name, price_dec))
            return out
    except Exception as e:
        logger.error(f"Error en cart_get: {e}")
        return []


def cart_update_qty(phone, code, qty):
    if not phone or not code:
        return
    try:
        qty = max(0, min(int(qty or 0), 999999))
        with cart_lock:
            with get_db_connection() as conn:
                if qty == 0:
                    conn.execute("DELETE FROM carts WHERE phone=? AND code=?", (phone, code))
                else:
                    now = datetime.now().isoformat()
                    conn.execute(
                        "UPDATE carts SET quantity=?, created_at=? WHERE phone=? AND code=?",
                        (qty, now, phone, code)
                    )
    except Exception as e:
        logger.error(f"Error en cart_update_qty: {e}")


def cart_clear(phone):
    if not phone:
        return
    try:
        with cart_lock:
            with get_db_connection() as conn:
                conn.execute("DELETE FROM carts WHERE phone=?", (phone,))
    except Exception as e:
        logger.error(f"Error en cart_clear: {e}")


def cart_totals(phone):
    items = cart_get(phone)
    total = sum(q * price for _, q, __, price in items)
    discount = min(Decimal("0.05") * total, Decimal("500000")) if total > Decimal("10000000") else Decimal("0.00")
    final = (total - discount).quantize(Decimal("0.01"))
    return final, discount.quantize(Decimal("0.01"))

# ------------------------------------------------------------------
# CATÁLOGO + FAMILIAS
# ------------------------------------------------------------------
def _load_raw_csv():
    if _raw_csv_cache.get("text"):
        return _raw_csv_cache["text"]

    text = ""

    try:
        if CATALOG_URL.startswith("http"):
            r = requests.get(CATALOG_URL, timeout=REQUESTS_TIMEOUT, headers=REQUESTS_HEADERS)
            r.raise_for_status()
            r.encoding = "utf-8"
            text = r.text
        else:
            local_path = Path(CATALOG_URL.replace("file://", ""))
            if local_path.exists():
                text = local_path.read_text(encoding="utf-8")
            else:
                logger.warning(f"Ruta de catálogo inválida: {CATALOG_URL}")
    except Exception as e:
        logger.error(f"Error descargando CSV: {e}")

    if not text and LOCAL_CSV_FALLBACK.exists():
        logger.info("Usando catálogo local de respaldo")
        text = LOCAL_CSV_FALLBACK.read_text(encoding="utf-8")

    if text:
        _raw_csv_cache["text"] = text

    return text


def _extract_column(header_row, key_variants):
    header_norm = [strip_accents(h) for h in header_row]
    for variant in key_variants:
        variant_norm = strip_accents(variant)
        for idx, col in enumerate(header_norm):
            if variant_norm == col or variant_norm in col:
                return idx
    return None


def load_catalog_enriched():
    try:
        text = _load_raw_csv()
        if not text:
            return []

        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return []

        header = rows[0]
        data_rows = rows[1:]

        idx_code = _extract_column(header, ["code", "codigo", "id"])
        idx_name = _extract_column(header, ["description", "descripcion", "producto", "nombre", "name"])
        idx_name_normalized = _extract_column(header, ["descripcion_normalizada", "description_normalized", "normalized_name"])
        idx_usd = _extract_column(header, ["price_importado", "precio_importado", "usd", "dolar", "precio en dolares", "price_usd"])
        idx_ars = _extract_column(header, ["price_nacional", "precio_nacional", "ars", "pesos", "precio en pesos", "price_ars"])
        idx_brand = _extract_column(header, ["marca_final", "marca", "brand", "marca_moto"])
        idx_moto_brand = _extract_column(header, ["marca_moto", "marca moto"])
        idx_model = _extract_column(header, ["modelo_final", "modelo", "model", "modelo_moto"])
        idx_moto_model = _extract_column(header, ["modelo_moto", "modelo moto"])
        idx_category = _extract_column(header, ["categoria_nueva", "categoria", "category", "rubro", "categoria_final"])
        idx_final_category = _extract_column(header, ["categoria_final", "categoria final"])
        idx_keywords = _extract_column(header, ["keywords", "palabras clave", "sinonimos"])
        idx_oem = _extract_column(header, ["oem", "codigo oem", "original"])
        idx_alt = _extract_column(header, ["alt_names", "nombres alternativos", "alias"])
        idx_vehicle = _extract_column(header, ["vehicle_type", "tipo de moto", "aplica a"])
        idx_family_name = _extract_column(header, ["familia_nombre", "familia", "family", "familia_final"])
        idx_family_code = _extract_column(header, ["familia_codigo", "codigo_familia", "family_code"])
        idx_provider_name = _extract_column(header, ["proveedor_nombre", "proveedor", "provider", "proveedor_final"])
        idx_displacement = _extract_column(header, ["cilindrada", "cc", "engine_cc"])

        exchange = get_exchange_rate()
        catalog = []

        for line in data_rows:
            if not line:
                continue
            try:
                code = line[idx_code].strip() if (idx_code is not None and idx_code < len(line)) else ""
                name = line[idx_name].strip() if (idx_name is not None and idx_name < len(line)) else ""
                normalized_name = (
                    line[idx_name_normalized].strip()
                    if (idx_name_normalized is not None and idx_name_normalized < len(line))
                    else ""
                )

                price_usd = to_decimal_money(line[idx_usd]) if (idx_usd is not None and idx_usd < len(line)) else Decimal("0")
                price_ars = to_decimal_money(line[idx_ars]) if (idx_ars is not None and idx_ars < len(line)) else Decimal("0")

                if price_ars == 0 and price_usd > 0:
                    price_ars = (price_usd * exchange).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

                brand = line[idx_brand].strip() if (idx_brand is not None and idx_brand < len(line)) else ""
                moto_brand = line[idx_moto_brand].strip() if (idx_moto_brand is not None and idx_moto_brand < len(line)) else ""
                model = line[idx_model].strip() if (idx_model is not None and idx_model < len(line)) else ""
                moto_model = line[idx_moto_model].strip() if (idx_moto_model is not None and idx_moto_model < len(line)) else ""
                category = line[idx_category].strip() if (idx_category is not None and idx_category < len(line)) else ""
                final_category = line[idx_final_category].strip() if (idx_final_category is not None and idx_final_category < len(line)) else ""
                keywords = line[idx_keywords].strip() if (idx_keywords is not None and idx_keywords < len(line)) else ""
                oem = line[idx_oem].strip() if (idx_oem is not None and idx_oem < len(line)) else ""
                alt_names = line[idx_alt].strip() if (idx_alt is not None and idx_alt < len(line)) else ""
                vehicle_type = line[idx_vehicle].strip() if (idx_vehicle is not None and idx_vehicle < len(line)) else ""
                family_name = line[idx_family_name].strip() if (idx_family_name is not None and idx_family_name < len(line)) else ""
                family_code = line[idx_family_code].strip() if (idx_family_code is not None and idx_family_code < len(line)) else ""
                provider_name = line[idx_provider_name].strip() if (idx_provider_name is not None and idx_provider_name < len(line)) else ""
                displacement = line[idx_displacement].strip() if (idx_displacement is not None and idx_displacement < len(line)) else ""

                effective_brand = brand or moto_brand
                effective_model = model or moto_model
                effective_category = category or final_category

                search_text_parts = [
                    normalized_name or name,
                    f"familia {family_name}" if family_name else "",
                    f"marca {effective_brand}" if effective_brand else "",
                    f"modelo {effective_model}" if effective_model else "",
                    f"categoria {effective_category}" if effective_category else "",
                    f"marca moto {moto_brand}" if moto_brand else "",
                    f"modelo moto {moto_model}" if moto_model else "",
                    f"categoria final {final_category}" if final_category else "",
                    f"aplica a {vehicle_type}" if vehicle_type else "",
                    f"equivalente oem {oem}" if oem else "",
                    f"tambien llamado {alt_names}" if alt_names else "",
                    f"palabras clave {keywords}" if keywords else "",
                    f"proveedor {provider_name}" if provider_name else "",
                    f"cilindrada {displacement}" if displacement else "",
                ]
                search_text = " ".join([p for p in search_text_parts if p]).strip()

                if not name and not search_text:
                    continue

                catalog.append({
                    "code": code,
                    "name": normalized_name or name,
                    "raw_name": name,
                    "price_usd": float(price_usd),
                    "price_ars": float(price_ars),
                    "brand": effective_brand,
                    "moto_brand": moto_brand,
                    "model": effective_model,
                    "moto_model": moto_model,
                    "category": effective_category,
                    "final_category": final_category,
                    "keywords": keywords,
                    "oem": oem,
                    "alt_names": alt_names,
                    "vehicle_type": vehicle_type,
                    "family_name": family_name,
                    "family_code": family_code,
                    "provider_name": provider_name,
                    "displacement": displacement,
                    "search_text": search_text or name,
                })
            except Exception as e:
                logger.warning(f"Error procesando linea CSV: {e}")
                continue

        logger.info(f"Catalogo enriquecido cargado: {len(catalog)} productos")
        return catalog
    except Exception as e:
        logger.error(f"Error cargando catalogo: {e}", exc_info=True)
        return []


def build_families_index_from_catalog(catalog):
    families = {}
    for p in catalog:
        fam_name_raw = p.get("family_name") or ""
        if not fam_name_raw:
            continue
        fam_name_norm = normalize_search_query(fam_name_raw)
        if not fam_name_norm.strip():
            continue

        fam_words = [w for w in fam_name_norm.split() if len(w) >= 3]
        if not fam_words:
            continue

        entry = families.get(fam_name_norm)
        if not entry:
            entry = {
                "family_name": fam_name_raw.strip(),
                "family_name_norm": fam_name_norm,
                "codes": set(),
                "count": 0,
                "tokens": set(fam_words),
            }
            families[fam_name_norm] = entry

        family_code = (p.get("family_code") or "").strip()
        if not family_code:
            code = p.get("code") or ""
            m = re.match(r"(\d{4})/", code)
            if m:
                family_code = m.group(1)
        if family_code:
            entry["codes"].add(family_code)

        entry["count"] += 1

    sorted_fams = sorted(families.values(), key=lambda x: x["count"], reverse=True)
    return sorted_fams


def build_family_token_idf_from_catalog(catalog):
    df = Counter()
    total_docs = len(catalog)

    for p in catalog:
        text = p.get("search_text") or p.get("name") or ""
        tokens = {w for w in normalize_search_query(text).split() if len(w) >= 3}
        for t in tokens:
            df[t] += 1

    if not df:
        return {}

    return {
        token: max(0.0, math.log((total_docs + 1) / (freq + 1)))
        for token, freq in df.items()
    }


def initialize_families_index(catalog):
    global FAMILIES_INDEX
    try:
        FAMILIES_INDEX = build_families_index_from_catalog(catalog) if catalog else []
        if FAMILIES_INDEX:
            top_names = [f["family_name"] for f in FAMILIES_INDEX[:20]]
            logger.info(f"Top 20 familias: {top_names}")
        else:
            logger.warning("FAMILIES_INDEX vacío: no se encontraron familias en el catálogo")

        family_idf = build_family_token_idf_from_catalog(catalog) if catalog else {}
        if family_idf:
            global FAMILIES_TOKEN_IDF, FAMILIES_TOKEN_IDF_DEFAULT
            FAMILIES_TOKEN_IDF = family_idf
            values = list(family_idf.values())
            FAMILIES_TOKEN_IDF_DEFAULT = float(np.median(values)) if values else 1.0
    except Exception as e:
        logger.error(f"Error construyendo FAMILIES_INDEX: {e}", exc_info=True)


def build_family_compatibility_profile(catalog):
    profile = {}
    for p in catalog:
        fam_raw = p.get("family_name") or ""
        fam_norm = normalize_search_query(fam_raw)
        if not fam_norm:
            continue

        entry = profile.setdefault(
            fam_norm,
            {"family_name": fam_raw.strip() or fam_norm, "total": 0, "with_structured": 0},
        )
        entry["total"] += 1

        moto_brand = p.get("moto_brand") or p.get("brand")
        moto_model = p.get("moto_model") or p.get("model")
        if moto_brand and moto_model:
            entry["with_structured"] += 1

    return profile


def initialize_family_compatibility_profile(catalog):
    global FAMILY_COMPATIBILITY_PROFILE
    try:
        FAMILY_COMPATIBILITY_PROFILE = build_family_compatibility_profile(catalog) if catalog else {}
        if FAMILY_COMPATIBILITY_PROFILE:
            logger.info(
                f"Perfil de compatibilidad por familia listo ({len(FAMILY_COMPATIBILITY_PROFILE)} familias)"
            )
    except Exception as e:
        logger.error(f"Error construyendo perfil de compatibilidad por familia: {e}", exc_info=True)


def needs_llm_compatibility(family: str | None) -> bool:
    if not family:
        return True

    fam_norm = normalize_search_query(family)
    if not fam_norm:
        return True

    llm_first_families = {
        "bujia",
        "pastilla",
        "filtro",
        "aceite",
        "junta",
        "bulbo",
        "rayo",
        "tornillo",
        "pinon",
        "corona",
        "universal",
    }

    if any(token in fam_norm for token in llm_first_families):
        return True

    stats = FAMILY_COMPATIBILITY_PROFILE.get(fam_norm)
    if not stats or not stats.get("total"):
        return True

    ratio = stats.get("with_structured", 0) / max(stats.get("total", 1), 1)
    return ratio < 0.05


def save_faiss_index(index, catalog):
    try:
        faiss.write_index(index, FAISS_INDEX_PATH)
        with open(FAISS_MAPPING_PATH, "wb") as f:
            pickle.dump(catalog, f)
        logger.info(f"FAISS guardado en disco: {len(catalog)} productos")
    except Exception as e:
        logger.error(f"Error guardando FAISS: {e}")


def load_faiss_index():
    try:
        if os.path.exists(FAISS_INDEX_PATH) and os.path.exists(FAISS_MAPPING_PATH):
            index = faiss.read_index(FAISS_INDEX_PATH)
            with open(FAISS_MAPPING_PATH, "rb") as f:
                catalog = pickle.load(f)

            if index.ntotal != len(catalog):
                logger.warning(
                    f"FAISS inconsistente (index.ntotal={index.ntotal}, catalog={len(catalog)}). "
                    "Se reconstruirá desde cero."
                )
                return None, None

            logger.info(f"FAISS cargado desde disco: {len(catalog)} productos")
            return catalog, index
        else:
            logger.warning("FAISS no encontrado en disco, se construira de cero.")
            return None, None
    except Exception as e:
        logger.warning(f"No se pudo cargar FAISS desde disco: {e}")
        return None, None


def generate_embeddings_with_cache(texts):
    with _embeddings_cache_lock:
        cache = {}

        if os.path.exists(EMBEDDINGS_CACHE_PATH):
            try:
                with open(EMBEDDINGS_CACHE_PATH, "rb") as f:
                    cache = pickle.load(f)
                    logger.info(f"Cache de embeddings cargado: {len(cache)} textos")
            except Exception as e:
                logger.error(f"Cache corrupto, recreando desde cero: {e}")
                cache = {}
                try:
                    os.remove(EMBEDDINGS_CACHE_PATH)
                    logger.info("Cache corrupto eliminado")
                except Exception as e2:
                    logger.warning(f"No se pudo borrar cache corrupto: {e2}")

        texts_to_embed = [text for text in texts if text not in cache]

        if texts_to_embed:
            logger.info(f"Generando embeddings para {len(texts_to_embed)} textos nuevos...")
            batch = 256
            max_retries = 3
            updated_cache = False

            for i in range(0, len(texts_to_embed), batch):
                chunk = texts_to_embed[i : i + batch]

                for retry in range(max_retries):
                    try:
                        with openai_sem:
                            resp = client.embeddings.create(
                                input=chunk,
                                model=EMBEDDING_MODEL,
                            )
                        chunk_vectors = [d.embedding for d in resp.data]

                        for text, vec in zip(chunk, chunk_vectors):
                            cache[text] = vec
                            updated_cache = True

                        break
                    except RateLimitError as e:
                        if retry < max_retries - 1:
                            wait_time = min((2**retry) * random.uniform(2, 5), 60)
                            logger.warning(
                                f"RateLimitError en embeddings, reintentando en {wait_time:.2f}s... (intento {retry+1}/{max_retries})"
                            )
                            time.sleep(wait_time)
                        else:
                            logger.error(f"RateLimitError persistente: {e}")
                            raise

            if updated_cache:
                try:
                    with open(EMBEDDINGS_CACHE_PATH, "wb") as f:
                        pickle.dump(cache, f)
                    logger.info(f"Cache de embeddings guardado: {len(cache)} textos")
                except Exception as e:
                    logger.warning(f"Error guardando cache de embeddings: {e}")

        final_vectors = []
        for text in texts:
            if text in cache and cache[text]:
                final_vectors.append(cache[text])
            else:
                logger.error(f"Texto sin embedding: {text[:50]}")
                final_vectors.append(np.random.normal(0, 0.01, 1536).astype("float32").tolist())

        return final_vectors


def _build_faiss_index_from_catalog(catalog):
    try:
        if not catalog:
            return None, 0

        texts = [c["search_text"] for c in catalog]
        if not texts:
            return None, 0

        vectors = generate_embeddings_with_cache(texts)

        if not vectors:
            return None, 0

        vecs = np.array(vectors).astype("float32")
        if vecs.ndim != 2 or vecs.shape[0] == 0 or vecs.shape[1] == 0:
            return None, 0

        faiss.normalize_L2(vecs)
        index = faiss.IndexFlatIP(vecs.shape[1])
        index.add(vecs)

        logger.info(f"Indice FAISS creado con {vecs.shape[0]} vectores")
        return index, vecs.shape[0]
    except Exception as e:
        logger.error(f"Error construyendo FAISS: {e}", exc_info=True)
        return None, 0


def _build_bm25_index_from_catalog(catalog):
    try:
        if not catalog:
            return None, []

        corpus = [c.get("search_text") or "" for c in catalog]
        tokenized_corpus = [_tokenize_text(text) for text in corpus]

        if not tokenized_corpus:
            return None, []

        bm25 = BM25Okapi(tokenized_corpus)
        logger.info(f"Indice BM25 creado con {len(tokenized_corpus)} documentos")
        return bm25, tokenized_corpus
    except Exception as e:
        logger.error(f"Error construyendo BM25: {e}", exc_info=True)
        return None, []


def get_catalog_and_index():
    with _catalog_lock:
        if _catalog_and_index_cache["catalog"] is not None:
            return (
                _catalog_and_index_cache["catalog"],
                _catalog_and_index_cache["index"],
                _catalog_and_index_cache.get("bm25"),
                _catalog_and_index_cache.get("bm25_corpus") or [],
            )

        catalog, index = load_faiss_index()

        if catalog and index:
            bm25_index, tokenized_corpus = _build_bm25_index_from_catalog(catalog)
            _catalog_and_index_cache["catalog"] = catalog
            _catalog_and_index_cache["index"] = index
            _catalog_and_index_cache["bm25"] = bm25_index
            _catalog_and_index_cache["bm25_corpus"] = tokenized_corpus
            _catalog_and_index_cache["built_at"] = datetime.utcnow().isoformat()
            initialize_families_index(catalog)
            initialize_family_compatibility_profile(catalog)
            return catalog, index, bm25_index, tokenized_corpus

        catalog = load_catalog_enriched()
        index, _ = _build_faiss_index_from_catalog(catalog)
        bm25_index, tokenized_corpus = _build_bm25_index_from_catalog(catalog)

        if index and catalog:
            save_faiss_index(index, catalog)

        _catalog_and_index_cache["catalog"] = catalog
        _catalog_and_index_cache["index"] = index
        _catalog_and_index_cache["bm25"] = bm25_index
        _catalog_and_index_cache["bm25_corpus"] = tokenized_corpus
        _catalog_and_index_cache["built_at"] = datetime.utcnow().isoformat()
        initialize_families_index(catalog)
        initialize_family_compatibility_profile(catalog)
        return catalog, index, bm25_index, tokenized_corpus


def get_v317_resources():
    with _v317_lock:
        catalog, _, _, _ = get_catalog_and_index()

        if not catalog:
            logger.warning("[v3.17][resources] Catálogo no disponible; se deshabilita temporalmente v3.17")
            return None, None, None

        if len(catalog) < 10:
            logger.warning(
                "[v3.17][resources] Catálogo cargado pero con pocos productos (%s).",
                len(catalog),
            )

        if _v317_cache["catalog"] is None:
            _v317_cache["catalog"] = catalog

        if _v317_cache["catalog"] and _v317_cache["centroid"] is None:
            descriptions = [
                row.get("descripcion_normalizada") or row.get("descripcion") or ""
                for row in _v317_cache["catalog"]
                if (row.get("descripcion_normalizada") or row.get("descripcion"))
            ]
            try:
                _v317_cache["centroid"] = build_catalog_centroid(descriptions)
            except Exception as exc:  # pragma: no cover - log only
                logger.exception("[v3.17][resources] Error generando centroid: %s", exc)
                _v317_cache["centroid"] = None

            if _v317_cache["centroid"] is None:
                logger.warning("[v3.17][resources] Centroid no generado; router usará fallback de keywords")

        if _v317_cache["catalog"] and _v317_cache["schema"] is None:
            _v317_cache["schema"] = build_classifier_schema(_v317_cache["catalog"])

        return _v317_cache["catalog"], _v317_cache["centroid"], _v317_cache["schema"]

# ------------------------------------------------------------------
# BÚSQUEDA HÍBRIDA (BM25 + FAISS con RRF)
# ------------------------------------------------------------------
def hybrid_search(
    query: str,
    phone: str | None = None,
    top_k: int = MAX_SEARCH_RESULTS,
    metadata_filters: dict | None = None,
    *,
    catalog=None,
    index=None,
    bm25=None,
    bm25_corpus=None,
    max_results: int | None = None,
) -> dict:
    global LAST_SEARCH_DEBUG

    if max_results is not None:
        top_k = max_results

    if catalog is None or index is None or bm25_corpus is None:
        catalog, index, bm25_index, bm25_corpus = get_catalog_and_index()
    else:
        bm25_index = bm25

    if not catalog or not query:
        LAST_SEARCH_DEBUG = {"query": query, "final_count": 0}
        return {"final_candidates": []}

    parsed = parse_query_v2(query, phone=phone)

    if metadata_filters:
        def _norm_list(val):
            if val is None:
                return []
            if isinstance(val, (list, tuple, set)):
                seq = val
            else:
                seq = [val]
            return [normalize_search_query(str(v)) for v in seq if str(v).strip()]

        def _norm_str(val):
            if val is None:
                return None
            s = str(val).strip()
            return normalize_search_query(s) if s else None

        meta = metadata_filters or {}
        parsed["final_category"] = _norm_str(meta.get("categoria_final") or meta.get("final_category") or parsed.get("final_category"))
        parsed["category"] = _norm_str(meta.get("categoria") or meta.get("category") or parsed.get("category")) or parsed.get("category")
        parsed["moto_brands"] = _norm_list(meta.get("marca_moto") or meta.get("moto_brand") or meta.get("moto_brands") or parsed.get("moto_brands"))
        parsed["moto_models"] = _norm_list(meta.get("modelo_moto") or meta.get("moto_model") or meta.get("moto_models") or parsed.get("moto_models"))
        displacement_val = _norm_str(meta.get("cilindrada") or meta.get("displacement") or parsed.get("displacement"))
        if displacement_val:
            parsed["displacement"] = displacement_val

    LAST_SEARCH_DEBUG = {
        "query": query,
        "top_k": top_k,
        "parsed": parsed,
        "metadata_filters": metadata_filters or {},
    }

    bm25_results = []
    bm25_cutoff = None
    if bm25_index:
        try:
            tokenized_query = _tokenize_text(query)
            scores = bm25_index.get_scores(tokenized_query)
            ranked_indices = np.argsort(scores)[::-1]
            if scores.size:
                max_score = float(np.max(scores))
                bm25_cutoff = max_score * BM25_THRESHOLD if max_score > 0 else None
            k_bm25 = min(max(top_k * 2, top_k), len(ranked_indices))
            for rank, idx in enumerate(ranked_indices[:k_bm25], 1):
                if 0 <= idx < len(catalog):
                    if bm25_cutoff is not None and scores[idx] < bm25_cutoff:
                        continue
                    bm25_results.append((catalog[idx], float(scores[idx]), rank))
            logger.info("[DEBUG][BM25] %s candidatos", len(bm25_results))
        except Exception as e:
            logger.error(f"Error en búsqueda BM25: {e}", exc_info=True)
    else:
        logger.warning("BM25 no disponible, usando solo FAISS")

    faiss_results = []
    faiss_cutoff = None
    if index:
        try:
            emb = generate_embeddings_with_cache([query])[0]
            q_vec = np.array([emb]).astype("float32")
            faiss.normalize_L2(q_vec)

            has_families = bool(parsed.get("families"))
            multiplier = 4 if has_families else 8
            k_for_index = min(max(top_k * multiplier, top_k), len(catalog))

            D, I = index.search(q_vec, k_for_index)
            if D.size:
                max_dist = float(np.max(D))
                faiss_cutoff = max_dist * FAISS_THRESHOLD if max_dist > 0 else None
            for rank, (dist, idx) in enumerate(zip(D[0], I[0]), 1):
                if 0 <= idx < len(catalog):
                    if faiss_cutoff is not None and dist < faiss_cutoff:
                        continue
                    faiss_results.append((catalog[idx], float(dist), rank))
            logger.info("[DEBUG][FAISS] %s candidatos", len(faiss_results))
        except Exception as e:
            logger.error(f"Error en búsqueda FAISS: {e}", exc_info=True)
    else:
        logger.warning("Índice FAISS no disponible, usando solo BM25")

    if not bm25_results and not faiss_results:
        LAST_SEARCH_DEBUG["final_count"] = 0
        return {"final_candidates": []}

    k_rrf = 60
    fused_scores = defaultdict(float)
    product_lookup = {}

    def add_rrf_scores(results, weight):
        for product, _score, rank in results:
            key = product.get("code") or product.get("name") or id(product)
            if key not in product_lookup:
                product_lookup[key] = dict(product)
            fused_scores[key] += (weight or 1.0) / (k_rrf + rank)

    add_rrf_scores(bm25_results, RRF_BM25_WEIGHT)
    add_rrf_scores(faiss_results, RRF_FAISS_WEIGHT)

    sorted_keys = sorted(fused_scores, key=lambda k: fused_scores[k], reverse=True)
    max_candidates = min(max(top_k * 2, top_k), len(sorted_keys))
    fused = []
    for k in sorted_keys[:max_candidates]:
        product_with_score = dict(product_lookup[k])
        product_with_score["_score"] = fused_scores[k]
        fused.append((product_with_score, fused_scores[k]))

    logger.info("[DEBUG][RRF] bm25=%s faiss=%s fused=%s", len(bm25_results), len(faiss_results), len(fused))

    products_only = [p for p, _ in fused]

    cats = parsed.get("categories") or []
    motos = parsed.get("motos_detectadas") or []

    if len(motos) > 1 and len(cats) > 1:
        if len(motos) * len(cats) > 6:
            return {
                "error": "too_many_combinations",
                "message": "Hay muchas combinaciones de moto y categoría. Decime una sola moto o categoría para buscar mejor."
            }
        combined = {}
        for m in motos:
            for c in cats:
                sub = parsed.copy()
                sub["motos_detectadas"] = [m]
                sub["categories"] = [c]
                sub["category"] = c
                sub_filtered = filter_catalog(products_only, sub)
                key = f"{m['brand']} {m['model']} – {c}"
                combined[key] = sub_filtered[:MAX_SEARCH_RESULTS]
        return {"multi_moto_multi_cat": True, "results": combined}

    if len(cats) > 1 and len(motos) <= 1:
        multi_results = {}
        for c in cats:
            sub_parsed = parsed.copy()
            sub_parsed["categories"] = [c]
            sub_parsed["category"] = c
            sub_filtered = filter_catalog(products_only, sub_parsed)
            multi_results[c] = sub_filtered[:MAX_SEARCH_RESULTS]
        return {"multisearch": True, "results": multi_results}

    if len(motos) > 1:
        results = {}
        for m in motos:
            sub_parsed = parsed.copy()
            sub_parsed["motos_detectadas"] = [m]
            sub_filtered = filter_catalog(products_only, sub_parsed)
            key = f"{m['brand']} {m['model']}"
            results[key] = sub_filtered[:MAX_SEARCH_RESULTS]
        return {"multi_moto": True, "results": results}

    filtered_products = filter_catalog(products_only, parsed)

    def _score_for(product):
        key = product.get("code") or product.get("name") or id(product)
        return fused_scores.get(key, 0.0)

    if filtered_products:
        results = [(p, _score_for(p)) for p in filtered_products]
    else:
        families = parsed.get("families") or []
        cat = parsed.get("category")
        brands = parsed.get("brands") or []
        models = parsed.get("models") or []

        results = []
        if (brands or models) and (families or cat):
            super_relaxed = {
                "families": families,
                "category": cat,
                "brands": [],
                "models": [],
                "raw": parsed.get("raw", "")
            }
            filtered_products = filter_catalog(products_only, super_relaxed)
            if filtered_products:
                results = [(p, _score_for(p)) for p in filtered_products]

        if not results:
            logger.warning("[DEBUG][Filtro] Moto filter empty, usando merged_results")
            results = fused

    results.sort(key=lambda x: x[1], reverse=True)
    final_candidates = [p for p, _ in results[:top_k]]

    LAST_SEARCH_DEBUG.update({
        "bm25_count": len(bm25_results),
        "faiss_count": len(faiss_results),
        "rrf_count": len(fused),
        "final_count": len(final_candidates),
    })

    return {
        "final_candidates": final_candidates,
        "bm25_candidates": [p for p, *_ in bm25_results],
        "faiss_candidates": [p for p, *_ in faiss_results],
        "fused": fused,
        "parsed": parsed,
    }


def run_allowed_products_search(normalized_query: str, phone: str | None = None, intent: str | None = None) -> dict:
    """
    Ejecuta la búsqueda híbrida y filtra por relevancia para generar allowed_products.
    """
    semantic_results = hybrid_search(normalized_query, phone=phone, top_k=MAX_SEARCH_RESULTS)

    if isinstance(semantic_results, dict):
        base_candidates = list(semantic_results.get("final_candidates") or [])
        payload = dict(semantic_results)
    else:
        base_candidates = [p for p, _ in semantic_results]
        payload = {"final_candidates": base_candidates, "raw_results": semantic_results}

    filtered_products = filter_by_relevance(normalized_query, base_candidates, min_score=RELEVANCE_MIN_SCORE)

    if not filtered_products and base_candidates:
        logger.info("[DEBUG][Relevancia] merged_results usados tras filtro vacío")
        filtered_products = base_candidates[:MAX_SEARCH_RESULTS]

    payload["final_candidates"] = filtered_products
    payload["relevance_debug"] = LAST_RELEVANCE_DEBUG
    return payload


def _order_products_for_llm(products: list[dict] | None) -> list[dict]:
    if not products:
        return []

    return sorted(
        products,
        key=lambda p: float(p.get("_score") or p.get("score") or 0.0),
        reverse=True,
    )


def _slice_products_for_llm(products: list[dict] | None) -> tuple[list[dict], list[list[dict]]]:
    ordered = _order_products_for_llm(products)
    top_products = ordered[:MAX_PRODUCTS_FOR_LLM]

    chunks: list[list[dict]] = []
    if ordered:
        chunk_source = ordered
        chunks = [
            chunk_source[i:i + PRODUCTS_PER_CHUNK]
            for i in range(0, len(chunk_source), PRODUCTS_PER_CHUNK)
        ]

    return top_products, chunks

# ------------------------------------------------------------------
# LISTAS MASIVAS
# ------------------------------------------------------------------
def parse_bulk_list(text):
    if not text:
        return []
    text = text.replace(",", "\n").replace(";", "\n")
    lines = text.strip().split("\n")
    parsed = []
    for line in lines:
        line = line.strip().lstrip("-").strip()
        if not line:
            continue
        match = re.match(r"^(\d+)\s+(.+)$", line)
        if match:
            qty = int(match.group(1))
            product_name = match.group(2).strip()
            parsed.append((qty, product_name))
        else:
            parsed.append((1, line))

    if len(parsed) > MAX_BULK_ITEMS:
        logger.warning(f"Lista truncada: {len(parsed)} -> {MAX_BULK_ITEMS}")
        return parsed[:MAX_BULK_ITEMS]

    return parsed


def is_bulk_list_request(text):
    if not text:
        return False, 0
    lower = text.lower()
    norm = text.replace(",", "\n").replace(";", "\n")
    lines = [l for l in norm.split("\n") if l.strip()]
    lines_with_qty = sum(1 for l in lines if re.match(r"^\d+\s+\w", l.strip()))
    has_quote_intent = any(kw in lower for kw in ["cotiz", "precio", "cuanto", "tenes", "tenés", "stock", "pedido", "lista", "presupuest"])
    is_multiline = len(lines) >= 3

    is_bulk = (lines_with_qty >= 3) or (has_quote_intent and is_multiline and lines_with_qty >= 1)
    count = len(lines) if is_bulk else 0

    return is_bulk, count


def process_bulk_sync(phone, raw_list):
    parsed_items = parse_bulk_list(raw_list)
    if not parsed_items:
        return {"success": False, "error": "No pude interpretar la lista"}

    results, not_found = [], []
    total_quoted = Decimal("0")

    for requested_qty, product_name in parsed_items:
        corrected_name, corr = autocorrect_keywords(product_name)
        if corr:
            logger.info(f"Autocorrect bulk sync: {product_name} -> {corrected_name} ({', '.join(corr)})")

        matches = hybrid_search(corrected_name, phone=phone, top_k=3)
        candidates = matches.get("final_candidates") if isinstance(matches, dict) else [p for p, _ in matches]
        if candidates:
            best = candidates[0]
            price_ars = to_decimal_money(best.get("price_ars", 0))
            subtotal = (price_ars * requested_qty).quantize(Decimal("0.01"))
            total_quoted += subtotal
            results.append({
                "requested": product_name,
                "corrected": corrected_name,
                "found": best.get("name", ""),
                "code": best.get("code", ""),
                "quantity": requested_qty,
                "price_unit": float(price_ars),
                "subtotal": float(subtotal)
            })
        else:
            not_found.append({"requested": product_name, "quantity": requested_qty})

    return {
        "success": True,
        "found_count": len(results),
        "not_found_count": len(not_found),
        "results": results,
        "not_found": not_found,
        "total_quoted": float(total_quoted)
    }


def create_bulk_job(phone, raw_list, item_count):
    if not phone:
        return None
    job_id = f"bulk_{int(time.time())}_{phone.replace(':', '_')}"
    try:
        with get_db_connection() as conn:
            conn.execute(
                """INSERT INTO bulk_jobs
                (job_id, phone, raw_list, total_items, processed_items, found_items, results_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (job_id, phone, raw_list, item_count, 0, 0, "[]", "processing", datetime.now().isoformat())
            )

        bulk_queue.put({
            "job_id": job_id,
            "phone": phone,
            "raw_list": raw_list,
            "total_items": item_count
        })

        return job_id
    except Exception as e:
        logger.error(f"Error creando bulk_job: {e}")
        return None


def process_bulk_async(job):
    try:
        job_id = job["job_id"]
        phone = job["phone"]
        raw_list = job["raw_list"]
        start_time = time.time()

        logger.info(f"Procesando job {job_id}")

        parsed_items = parse_bulk_list(raw_list)
        results, not_found = [], []
        total_quoted = Decimal("0")

        for i, (requested_qty, product_name) in enumerate(parsed_items):
            if time.time() - start_time > BULK_TIMEOUT:
                logger.warning(f"Job {job_id} timeout despues de {BULK_TIMEOUT}s")
                break

            corrected_name, corr = autocorrect_keywords(product_name)
            if corr:
                logger.info(f"Autocorrect bulk async: {product_name} -> {corrected_name} ({', '.join(corr)})")

            matches = hybrid_search(corrected_name, phone=phone, top_k=3)
            candidates = matches.get("final_candidates") if isinstance(matches, dict) else [p for p, _ in matches]
            if candidates:
                best = candidates[0]
                price_ars = to_decimal_money(best.get("price_ars", 0))
                subtotal = (price_ars * requested_qty).quantize(Decimal("0.01"))
                total_quoted += subtotal
                results.append({
                    "requested": product_name,
                    "corrected": corrected_name,
                    "found": best.get("name", ""),
                    "code": best.get("code", ""),
                    "quantity": requested_qty,
                    "price_unit": float(price_ars),
                    "subtotal": float(subtotal)
                })
            else:
                not_found.append({"requested": product_name, "quantity": requested_qty})

            if (i + 1) % 10 == 0:
                with get_db_connection() as conn:
                    conn.execute(
                        "UPDATE bulk_jobs SET processed_items=?, found_items=? WHERE job_id=?",
                        (i + 1, len(results), job_id)
                    )

        final_results = {
            "results": results,
            "not_found": not_found,
            "total_quoted": float(total_quoted),
            "found_count": len(results),
            "not_found_count": len(not_found)
        }

        with get_db_connection() as conn:
            conn.execute(
                """UPDATE bulk_jobs
                   SET processed_items=?, found_items=?, results_json=?, status=?, completed_at=?
                   WHERE job_id=?""",
                (len(parsed_items), len(results), json.dumps(final_results), "completed", datetime.now().isoformat(), job_id)
            )

        products_for_save = [
            {
                "code": r["code"],
                "name": r["found"],
                "price_ars": r["price_unit"],
                "price_usd": float(Decimal(str(r["price_unit"])) / get_exchange_rate()),
                "qty": int(r["quantity"])
            }
            for r in results
        ]
        save_last_search(phone, products_for_save, "Lista async")
        save_to_search_history(phone, products_for_save, "Lista async")

        send_bulk_completion(phone, final_results)

        logger.info(f"Job {job_id} completado: {len(results)}/{len(parsed_items)} encontrados")

    except Exception as e:
        logger.error(f"Error procesando job: {e}", exc_info=True)
        try:
            with get_db_connection() as conn:
                conn.execute("UPDATE bulk_jobs SET status=? WHERE job_id=?", ("failed", job["job_id"]))
        except Exception as e2:
            logger.error(f"Error marcando bulk_job como failed: {e2}", exc_info=True)


def bulk_worker():
    while True:
        try:
            job = bulk_queue.get(timeout=1)
            process_bulk_async(job)
            bulk_queue.task_done()
        except Empty:
            continue
        except Exception as e:
            logger.exception("Worker crashed, respawning...")
            time.sleep(5)


for _ in range(2):
    threading.Thread(target=bulk_worker, daemon=True).start()


def send_bulk_completion(phone, results):
    if not twilio_rest_client or not phone:
        return

    try:
        found = results.get("found_count", 0)
        not_found_count = results.get("not_found_count", 0)
        total = results.get("total_quoted", 0)

        message = f"""Listo! Procesé tu lista:

{found} productos encontrados
{not_found_count} sin coincidencia exacta

TOTAL: {format_price(Decimal(str(total)))}

¿Los agregamos al carrito? Decime: dale"""

        twilio_rest_client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            body=message,
            to=phone
        )

        logger.info(f"Notificacion enviada a {phone}")
    except Exception as e:
        logger.error(f"Error enviando notificacion: {e}")


# ------------------------------------------------------------------
# PEDIDOS IMPLÍCITOS (MEJORADOS)
# ------------------------------------------------------------------
def detect_implicit_cart_action(message, phone):
    """
    Detecta pedidos implícitos tipo:
    - "3 de cada una"
    - "2 de todos"
    - "todos x5"
    - "agregame todos"
    - "mandame los que me pasaste x3"
    Usa la última búsqueda guardada.
    """
    msg = (message or "").lower()
    last = get_last_search(phone)
    if not last or not last.get("products"):
        return None

    products = last["products"]

    # 3 de cada / 3 c/u / 3 c.u.
    m = re.search(r"(\d+)\s*(de\s*cada(\s+una|\s+uno|\s+una de esas|\s+uno de esos)?|c\/u|c\.u\.|c u)", msg)
    if m:
        qty = int(m.group(1))
        return {
            "action": "add_each_quantity",
            "quantity": qty,
            "products": products
        }

    # 3 de todos / 3 de todas
    m = re.search(r"(\d+)\s+de\s+(todos?|todas?)", msg)
    if m:
        qty = int(m.group(1))
        return {
            "action": "add_each_quantity",
            "quantity": qty,
            "products": products
        }

    # todos x5 / todos por 5
    m = re.search(r"(todos?|todas?)\s*(x|por)\s*(\d+)", msg)
    if m:
        qty = int(m.group(3))
        return {
            "action": "add_each_quantity",
            "quantity": qty,
            "products": products
        }

    # "agregame todos", "mandame todos", "poneme todos"
    if re.search(r"(agregame|sumame|mandame|poneme|dejame|cargame)\s+(todos?|todas?)", msg):
        return {
            "action": "add_each_quantity",
            "quantity": 1,
            "products": products
        }

    # "esos", "los de arriba", "los anteriores" + número (opcional)
    if re.search(r"(esos|esas|los que me pasaste|los de arriba|los anteriores)", msg):
        m_qty = re.search(r"(\d+)", msg)
        qty = int(m_qty.group(1)) if m_qty else 1
        return {
            "action": "add_each_quantity",
            "quantity": qty,
            "products": products
        }

    return None

# ------------------------------------------------------------------
# CART ACTION PARSER
# ------------------------------------------------------------------
SPANISH_NUMBER_WORDS = {
    "cero": 0,
    "un": 1,
    "uno": 1,
    "una": 1,
    "dos": 2,
    "tres": 3,
    "cuatro": 4,
    "cinco": 5,
    "seis": 6,
    "siete": 7,
    "ocho": 8,
    "nueve": 9,
    "diez": 10,
    "once": 11,
    "doce": 12
}

EXPLICIT_QTY_PATTERN = re.compile(r"\b(a|en)\s+(\d+|un|uno|una|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez|once|doce)\b")


def extract_number_from_text(text):
    if not text:
        return None
    match = re.search(r"(\d+)", text)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    normalized = strip_accents(text.lower())
    for word, value in SPANISH_NUMBER_WORDS.items():
        if re.search(rf"\b{word}\b", normalized):
            return value
    return None


def extract_code_from_text(text):
    if not text:
        return None
    tokens = re.findall(r"[0-9/\-]+", text)
    for token in tokens:
        ok, normalized = validate_tercom_code(token)
        if ok:
            return normalized
    digits_only = re.sub(r"\D", "", text)
    if len(digits_only) >= 12:
        ok, normalized = validate_tercom_code(digits_only[:12])
        if ok:
            return normalized
    return None


def match_product_from_list(message, products, key="name"):
    if not message or not products:
        return None
    query = strip_accents(message.lower())
    best = None
    best_score = 0
    for item in products:
        name = strip_accents(str(item.get(key, ""))).lower()
        if not name:
            continue
        try:
            score = fuzz.partial_ratio(query, name)
        except Exception:
            continue
        if score > best_score:
            best = item
            best_score = score
    if best_score < 60:
        return None
    return best


def handle_cart_action(phone, message):
    msg_norm = strip_accents((message or "")).lower()
    if not msg_norm:
        return "Necesito que me indiques qué producto toco del carrito."

    cart_items = cart_get(phone)
    cart_snapshot = [
        {"code": code, "qty": qty, "name": name, "price": price}
        for code, qty, name, price in cart_items
    ]

    last_search = get_last_search(phone) or {}
    last_products = last_search.get("products") or []

    code = extract_code_from_text(message)
    explicit_qty = bool(EXPLICIT_QTY_PATTERN.search(msg_norm))

    remove_keywords = ("saca", "sacame", "sacalo", "sacalos", "sacalas", "borra", "elimina", "quita", "retira")
    add_keywords = ("agrega", "agregá", "agregame", "agregalo", "sumame", "sumalo", "sumales", "mandame", "cargame", "poneme")
    increase_keywords = ("subi", "subile", "subilo", "aumenta", "aumentale", "sumale")
    decrease_keywords = ("baja", "bajame", "bajale", "restale", "sacale")
    set_keywords = ("dejalo", "dejala", "dejame", "ponelo", "ponela", "ponele")

    action = "unknown"
    if any(k in msg_norm for k in remove_keywords):
        action = "remove"
    elif explicit_qty and any(k in msg_norm for k in set_keywords):
        action = "set"
    elif explicit_qty and any(k in msg_norm for k in ("baja", "subi", "subilo", "bajalo")):
        action = "set"
    elif any(k in msg_norm for k in add_keywords):
        action = "add"
    elif any(k in msg_norm for k in increase_keywords):
        action = "increase"
    elif any(k in msg_norm for k in decrease_keywords):
        action = "decrease"

    if action == "unknown":
        return "Para tocar el carrito decime el código o nombre del producto y qué querés hacer."

    if action == "add" and not last_products:
        if code:
            last_products = [{"code": code, "name": message}]
        else:
            return "No tengo la última búsqueda a mano. Pasame el código o repetí qué producto querés agregar."

    target_cart = None
    if code:
        target_cart = next((item for item in cart_snapshot if item["code"] == code), None)

    if action in {"remove", "increase", "decrease", "set"} and not target_cart:
        target_cart = match_product_from_list(message, cart_snapshot, key="name")

    if action != "add" and not target_cart:
        return "No ubico ese producto en tu carrito. ¿Me pasás el código exacto?"

    if action == "add":
        candidate = None
        if code:
            candidate = next((p for p in last_products if p.get("code") == code), None)
        if not candidate:
            candidate = match_product_from_list(message, last_products, key="name")
        if not candidate:
            return "No encontré ese producto en lo último que te pasé. Repetíme el nombre o el código."

        qty_to_add = extract_number_from_text(message) or 1
        qty_to_add = max(1, qty_to_add)
        price_ars = candidate.get("price_ars")
        if isinstance(price_ars, Decimal):
            price_dec = price_ars
        else:
            price_dec = to_decimal_money(price_ars)
        price_usd = Decimal("0")
        if price_dec and price_dec > 0:
            try:
                price_usd = (price_dec / get_exchange_rate()).quantize(Decimal("0.01"))
            except Exception:
                price_usd = Decimal("0")

        ok = cart_add(
            phone,
            candidate.get("code", ""),
            qty_to_add,
            candidate.get("name", ""),
            price_dec or Decimal("0"),
            price_usd
        )
        if ok:
            return f"Listo, agregué {qty_to_add}x {candidate.get('name', '').strip()} al carrito."
        return "No pude agregar ese producto, pasame el código completo y lo cargo."

    current_qty = target_cart["qty"]
    if action == "remove":
        cart_update_qty(phone, target_cart["code"], 0)
        return f"Listo, saqué {target_cart['name']} del carrito."

    if action == "set":
        qty_target = extract_number_from_text(message)
        if qty_target is None:
            return "Decime cuántas unidades querés dejar."
        qty_target = max(0, qty_target)
        cart_update_qty(phone, target_cart["code"], qty_target)
        if qty_target == 0:
            return f"Listo, saqué {target_cart['name']} del carrito."
        return f"Dejé {target_cart['name']} en {qty_target}u."

    if action == "increase":
        delta = extract_number_from_text(message) or 1
        new_qty = current_qty + max(1, delta)
        cart_update_qty(phone, target_cart["code"], new_qty)
        return f"Subí {target_cart['name']} a {new_qty}u."

    if action == "decrease":
        delta = extract_number_from_text(message) or 1
        new_qty = max(0, current_qty - max(1, delta))
        cart_update_qty(phone, target_cart["code"], new_qty)
        if new_qty == 0:
            return f"Listo, saqué {target_cart['name']} del carrito."
        return f"Dejé {target_cart['name']} en {new_qty}u."

    return "No pude interpretar la acción sobre el carrito."

# ------------------------------------------------------------------
# PROMPTS LLM
# ------------------------------------------------------------------
BUSINESS_CONTEXT = """
TERCOM - Mayorista Motopartes Argentina

ENVIOS:
- CABA: 24-48hs
- Interior: 3-5 dias
- Gratis CABA >$100.000

PAGOS:
- Transferencia
- Efectivo (retiro local)
- Cheque (clientes habituales)

HORARIOS:
- Lun-Vie: 9-18hs
- Sab: 9-13hs
"""

CUSTOMER_OUTPUT_PROMPT = f"""
Eres el módulo de salida del sistema Fran. Tu objetivo es generar una respuesta
clara, útil, humana, segura y enfocada en ventas. Nunca inventes productos.
Nunca menciones que eres un modelo ni hagas frases genéricas como:
"Soy Fran de TERCOM" o "Estoy aquí para ayudarte".

REGLAS GENERALES:
- Mantén un tono humano, profesional y cálido.
- Limita la respuesta a lo relevante.
- No repitas información innecesaria.
- Usa sales_analysis.tono_sugerido para ajustar estilo.
- Usa meta_razonamiento para manejar dudas o riesgos.
- Jamás menciones allowed_products, reasoning, JSONs, etc.
- No promociones productos no presentes en productos_finales.
- Respeta el embudo propuesto en el plan.

SALUDO INICIAL (cuando primer_mensaje es true):
- No digas "soy Fran".
- No digas "soy un asistente".
- Saludá de forma natural y breve.
- Integra sales_analysis y meta_razonamiento para empatía y riesgos.
- Pedí lo mínimo necesario para avanzar (moto, pieza o código).
- No devuelvas más de 2 líneas en este saludo inicial.
- Ejemplos válidos:
  "Buenas! Decime qué moto tenés y te paso lo que mejor va."
  "Hola! ¿Qué estás buscando hoy?"
  "Contame qué pieza necesitás y te paso opciones rápidas."

EN RESPUESTAS NORMALES:
- Usa productos_finales para armar opciones ordenadas.
- Aplica señales de cierre si sales_analysis indica alto interés.
- Si meta_razonamiento.accion_sugerida = pedir_aclaracion:
    pedir una aclaración puntual.
- Si el plan propone acciones (carrito, cantidades, familias):
    ejecutar internamente y comunicarlo de forma breve y amable.
- Si el cliente pide algo imposible o fuera del catálogo:
    ofrecer alternativas seguras de productos_finales.

FORMATO:
- No más de 2–4 renglones por respuesta.
- Priorizar claridad sobre detalles técnicos.

{BUSINESS_CONTEXT}
"""

# Compatibilidad hacia atrás
CITATION_ENFORCED_PROMPT = CUSTOMER_OUTPUT_PROMPT

# Prompt de planificación unificada (versión actualizada)
PLANNING_UNIFIED_PROMPT = """
Eres FRAN, un vendedor mayorista de repuestos de moto para TERCOM.
Tu tarea es PENSAR en voz baja (razonamiento interno) y devolver SIEMPRE un JSON ESTRICTO
con el plan de respuesta. NO hables al cliente todavía, esto es solo tu plan interno.

Tienes la siguiente información de contexto:

- Mensaje del cliente (user_message)
- Historial corto de la conversación (short_history)
- Resultados de búsqueda de catálogo ya filtrados y validados (allowed_products)
- Análisis de calidad de contexto (context_quality)
- Estado del carrito, fase de venta, y acciones pendientes (cart_state, sales_phase, pending_action, last_search)
- Información de negocio (por ejemplo, reglas de precios, stock aproximado si se incluye)

TU OBJETIVO PRINCIPAL:
1) No inventar productos ni códigos.
2) No mezclar atributos de productos (marca/modelo/cilindrada).
3) Entender qué quiere el cliente y elegir los productos correctos dentro de allowed_products.
4) Preparar un plan claro para que otro modelo arme el texto final para el cliente.

Debes devolver SIEMPRE un JSON con la siguiente estructura EXACTA:

{
  "real_intent": "...",
  "query_interpretada": "...",
  "productos_elegidos": [
    {
      "code": "...",
      "name": "...",
      "reason": "..."
    }
  ],
  "razonamiento": "...",
  "sales_analysis": {
      "tipo_de_cliente": "...",
      "nivel_de_interes": "...",
      "senales_de_cierre": "...",
      "producto_recomendado": "",
      "argumentos_clave": [],
      "alternativas_seguras": [],
      "tono_sugerido": "...",
      "nivel_de_confianza": 0.0
  },
  "requery": {
      "NEED_REQUERY": false,
      "new_query": "",
      "razon": "",
      "tipo_de_error": "",
      "nivel_de_confianza": 0.0
  },
  "meta_razonamiento": {
      "confianza": 0.0,
      "riesgos": "",
      "datos_faltantes": "",
      "accion_sugerida": ""
  }
}

Definiciones:

- real_intent: intención real del cliente, por ejemplo:
  "product_search", "cart_action", "view_cart", "order_flow",
  "payment", "shipping", "tech_expert", "small_talk", "negation", "confirmation".
- query_interpretada: cómo entendiste la búsqueda del cliente, ya normalizada
  (por ejemplo: "amortiguadores traseros para Honda CG 150").
- productos_elegidos: lista de productos dentro de allowed_products que vas a usar
  para responder. Deben existir en allowed_products (no inventes).
- razonamiento: resumen de tu razonamiento interno, en castellano, sin adornos.
- sales_analysis: acá haces tu ANÁLISIS COMERCIAL:
  - tipo_de_cliente: "comparador", "fiel", "nuevo", "desconfiado", etc.
  - nivel_de_interes: "bajo", "medio", "alto".
  - senales_de_cierre: texto breve con señales de cierre que detectaste.
  - producto_recomendado: code del producto principal que sugerirías (si aplica).
  - argumentos_clave: lista de bullets con argumentos comerciales (precio, calidad, marca, etc.).
  - alternativas_seguras: lista de códigos de productos alternativos (solo de allowed_products).
  - tono_sugerido: "amigable", "experto", "directo", "asesor", etc.
  - nivel_de_confianza: número 0.0–1.0 de cuán seguro estás de tu análisis comercial.
- requery:
  - Si la búsqueda de catálogo fue pobre, ambigua o mal escrita, podés sugerir UNA sola nueva búsqueda.
  - NEED_REQUERY: true si crees que hay que reintentar la búsqueda.
  - new_query: texto de la nueva búsqueda sugerida (ya normalizada).
  - razon: por qué crees que hay que reintentar.
  - tipo_de_error: "ortografía", "ambigüedad", "categoría incorrecta", "compatibilidad".
  - nivel_de_confianza: 0.0–1.0 de cuán seguro estás de que el requery ayudará.
- meta_razonamiento:
  - confianza: 0.0–1.0 sobre tu respuesta global.
  - riesgos: texto con posibles riesgos de equivocación (por ejemplo "modelo de moto ambiguo").
  - datos_faltantes: qué datos le pedirías al cliente para estar seguro.
  - accion_sugerida: "pedir_aclaracion", "responder_con_cautela" o "requery".

Instrucciones críticas:
- Trabaja SIEMPRE solo con los productos que aparecen en allowed_products.
- No inventes códigos, ni nombres, ni marcas, ni modelos que no estén en allowed_products.
- Si no estás seguro, sé conservador e indica en meta_razonamiento qué falta.
- Si la calidad de contexto es mala (context_quality.sufficient==false), prioriza:
  - sugerir requery con buena new_query, o
  - pedir aclaraciones (accion_sugerida="pedir_aclaracion").
- Devuelve SIEMPRE un JSON válido. No envíes nunca texto fuera del JSON.
"""

SALES_ANALYSIS_PROMPT = """
Eres Fran, analista comercial de TERCOM.
Analiza la conversación reciente y el contexto del cliente.

Devuelve SIEMPRE un JSON con:
{
  "sales_analysis": {
    "tipo_de_cliente": "comparador|fiel|nuevo|desconfiado|price_sensitive|impulsivo|indefinido",
    "nivel_de_interes": "bajo|medio|alto",
    "senales_de_cierre": ["texto breve con señales claras"],
    "producto_recomendado": "codigo_principal" (si aplica),
    "argumentos_clave": ["bullets comerciales clave"],
    "alternativas_seguras": ["codigos_alternativos_seguro"] ,
    "tono_sugerido": "amigable|experto|directo|asesor|concise",
    "nivel_de_confianza": 0.0-1.0
  },
  "confianza": "alta|media|baja",
  "alertas": ["strings de alerta si faltan datos o hay ambigüedades"]
}

No expliques nada fuera del JSON.
"""


def run_sales_analysis_llm(conversacion_completa, productos_disponibles, contexto_cliente, perfil_cliente, phone: str | None = None):
    """
    Ejecuta el análisis comercial previo al planning.
    Devuelve el JSON con sales_analysis.
    """
    history_block = conversacion_completa
    if not history_block and phone:
        history_block = [h.get("content", "") for h in _recent_history_for_prompt(phone, limit=8)]

    payload = {
        "conversacion_completa": history_block,
        "productos_disponibles": productos_disponibles,
        "contexto_cliente": contexto_cliente,
        "perfil_cliente": perfil_cliente,
    }

    messages = [
        {"role": "system", "content": SALES_ANALYSIS_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

    resp = llm_client.completion(
        model=MODEL_REASONING,
        messages=messages,
        temperature=0,
        max_tokens=400,
        response_format={"type": "json_object"},
    )

    try:
        return json.loads(resp.choices[0].message.content)
    except Exception:
        logger.error("Sales analysis JSON inválido")
        return {"sales_analysis": {}, "confianza": "baja", "alertas": ["parse_error"]}

TECH_SYSTEM_PROMPT = f"""
Sos Fran, mecánico experto y vendedor premium de TERCOM.

REGLAS:
- Respondé en 3-4 líneas máximo.
- Explicá en criollo qué conviene y por qué (duración, mantenimiento, causas comunes).
- Podés dar tips rápidos de diagnóstico o cuidado.
- Podés ampliar con conocimiento general de tu entrenamiento (principios mecánicos, síntomas típicos, consecuencias de un fallo) y,
  si hace falta, sumar contexto técnico de fuentes públicas que conozcas (p. ej., rangos típicos de recorrido o pares de apriete).
- Si el catálogo no trae un dato puntual (recorrido, medida, torque), usá tu conocimiento general o información pública reciente;
  si no estás seguro, aclará que es un rango aproximado. No inventes ni alteres códigos, precios o productos del catálogo.
- Cerrá ofreciendo ayuda para cotizar repuestos reales si el cliente quiere avanzar.

Enfocate en consejo técnico práctico y contextualizado.

{BUSINESS_CONTEXT}
"""


def run_planning_unificado(
    user_message: str,
    allowed_products: list,
    context_quality: dict,
    phone: str,
    short_history: list,
    sales_phase: str | None,
    pending_action: dict | None,
    last_search: dict | None,
    max_tokens: int = 1200,
) -> dict:
    """
    Llama al LLM de razonamiento con PLANNING_UNIFIED_PROMPT y devuelve
    un dict con el plan completo (incluyendo sales_analysis, requery y meta_razonamiento).
    """
    try:
        system_content = PLANNING_UNIFIED_PROMPT.strip()

        # Compactar productos permitidos para el prompt (no mandamos todo el catálogo crudo)
        productos_contexto = []
        for p in allowed_products[:MAX_PRODUCTS_FOR_LLM]:
            productos_contexto.append({
                "code": p.get("code", ""),
                "name": p.get("name", ""),
                "brand": p.get("brand", ""),
                "moto_brand": p.get("moto_brand", ""),
                "model": p.get("model", ""),
                "moto_model": p.get("moto_model", ""),
                "category": p.get("category", ""),
                "final_category": p.get("final_category", ""),
                "displacement": p.get("displacement", ""),
            })

        history_text = ""
        effective_history = short_history or _recent_history_for_prompt(phone, limit=10)
        if effective_history:
            # Tomamos solo los últimos mensajes cortos para contexto
            last_msgs = effective_history[-10:]
            parts = []
            for h in last_msgs:
                role = h.get("role", "user")
                content = h.get("content", "")
                parts.append(f"[{role.upper()}] {content}")
            history_text = "\n".join(parts)

        context_payload = {
            "user_message": user_message,
            "short_history": history_text,
            "allowed_products": productos_contexto,
            "context_quality": context_quality or {},
            "sales_phase": sales_phase,
            "pending_action": pending_action or {},
            "last_search": last_search or {},
        }

        messages = [
            {"role": "system", "content": system_content},
            {
                "role": "user",
                "content": json.dumps(context_payload, ensure_ascii=False)
            },
        ]

        with openai_sem:
            resp = llm_client.completion(
                model=MODEL_REASONING,
                messages=messages,
                temperature=0.25,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )

        raw = resp.choices[0].message.content
        try:
            plan = json.loads(raw)
        except Exception as e:
            logger.error(f"Error parseando JSON de planning_unificado: {e} - raw={raw}")
            # Fallback ultra conservador
            plan = {
                "real_intent": "product_search",
                "query_interpretada": user_message,
                "productos_elegidos": [],
                "razonamiento": "",
                "sales_analysis": {
                    "tipo_de_cliente": "",
                    "nivel_de_interes": "",
                    "senales_de_cierre": "",
                    "producto_recomendado": "",
                    "argumentos_clave": [],
                    "alternativas_seguras": [],
                    "tono_sugerido": "",
                    "nivel_de_confianza": 0.0,
                },
                "requery": {
                    "NEED_REQUERY": False,
                    "new_query": "",
                    "razon": "",
                    "tipo_de_error": "",
                    "nivel_de_confianza": 0.0,
                },
                "meta_razonamiento": {
                    "confianza": 0.0,
                    "riesgos": "",
                    "datos_faltantes": "",
                    "accion_sugerida": "",
                },
            }

        # Normalizar campos que falten
        plan.setdefault("sales_analysis", {})
        plan.setdefault("requery", {})
        plan.setdefault("meta_razonamiento", {})

        return plan

    except Exception as e:
        logger.error(f"run_planning_unificado fallo: {e}", exc_info=True)
        # Fallback mínimo
        return {
            "real_intent": "product_search",
            "query_interpretada": user_message,
            "productos_elegidos": [],
            "razonamiento": "",
            "sales_analysis": {
                "tipo_de_cliente": "",
                "nivel_de_interes": "",
                "senales_de_cierre": "",
                "producto_recomendado": "",
                "argumentos_clave": [],
                "alternativas_seguras": [],
                "tono_sugerido": "",
                "nivel_de_confianza": 0.0,
            },
            "requery": {
                "NEED_REQUERY": False,
                "new_query": "",
                "razon": "",
                "tipo_de_error": "",
                "nivel_de_confianza": 0.0,
            },
            "meta_razonamiento": {
                "confianza": 0.0,
                "riesgos": "",
                "datos_faltantes": "",
                "accion_sugerida": "",
            },
        }


def maybe_requery_and_replan(
    original_query: str,
    phone: str,
    plan: dict,
    execution_context: dict,
    short_history: list,
    sales_phase: str | None,
    pending_action: dict | None,
    last_search: dict | None,
) -> tuple[dict, list, dict]:
    """
    Si el plan sugiere NEED_REQUERY, ejecuta UNA sola nueva búsqueda con new_query,
    recalcula allowed_products y vuelve a llamar a run_planning_unificado.
    Devuelve (nuevo_plan, new_allowed_products, new_context_quality).
    Si no hay requery, devuelve el plan original y los mismos allowed_products/context.
    """
    try:
        requery_info = plan.get("requery") or {}
        need = bool(requery_info.get("NEED_REQUERY"))
        if not need:
            return plan, execution_context.get("allowed_products", []), execution_context.get("context_quality", {})

        if execution_context.get("requery_done"):
            # Ya hicimos un requery, no repetir
            return plan, execution_context.get("allowed_products", []), execution_context.get("context_quality", {})

        new_query = (requery_info.get("new_query") or "").strip()
        if not new_query:
            return plan, execution_context.get("allowed_products", []), execution_context.get("context_quality", {})

        logger.info(f"[Requery] Activado para {phone}: '{original_query}' -> '{new_query}'")

        # Nueva búsqueda híbrida solo con el nuevo query
        catalog, index, bm25, bm25_corpus = get_catalog_and_index()
        search_results = hybrid_search(
            catalog=catalog,
            index=index,
            bm25=bm25,
            bm25_corpus=bm25_corpus,
            query=new_query,
            max_results=MAX_SEARCH_RESULTS,
        )

        # Filtrar por relevancia y calidad
        candidates = search_results.get("final_candidates") if isinstance(search_results, dict) else [p for p, _ in search_results]
        filtered = filter_by_relevance(new_query, candidates, min_score=RELEVANCE_MIN_SCORE)
        allowed_products = filtered[:MAX_PRODUCTS_FOR_LLM]
        context_quality = assess_context_quality(new_query, allowed_products)

        execution_context["requery_done"] = True
        execution_context["requery_query"] = new_query
        execution_context["allowed_products"] = allowed_products
        execution_context["context_quality"] = context_quality

        # Nuevo planning con el requery aplicado
        new_plan = run_planning_unificado(
            user_message=original_query,
            allowed_products=allowed_products,
            context_quality=context_quality,
            phone=phone,
            short_history=short_history,
            sales_phase=sales_phase,
            pending_action=pending_action,
            last_search=last_search,
        )

        return new_plan, allowed_products, context_quality

    except Exception as e:
        logger.error(f"maybe_requery_and_replan fallo: {e}", exc_info=True)
        return plan, execution_context.get("allowed_products", []), execution_context.get("context_quality", {})


def build_customer_output_context(
    user_message: str,
    plan: dict,
    allowed_products: list,
    phone: str,
) -> dict:
    """
    Construye el contexto para el modelo que genera el MENSAJE FINAL al cliente.
    Usa sales_analysis y meta_razonamiento como insumo.
    """
    sales_analysis = plan.get("sales_analysis") or {}
    meta_razonamiento = plan.get("meta_razonamiento") or {}

    productos_contexto = []
    for p in allowed_products[:MAX_PRODUCTS_FOR_LLM]:
        productos_contexto.append({
            "code": p.get("code", ""),
            "name": p.get("name", ""),
            "brand": p.get("brand", ""),
            "moto_brand": p.get("moto_brand", ""),
            "model": p.get("model", ""),
            "moto_model": p.get("moto_model", ""),
            "category": p.get("category", ""),
            "final_category": p.get("final_category", ""),
            "displacement": p.get("displacement", ""),
        })

    return {
        "user_message": user_message,
        "real_intent": plan.get("real_intent"),
        "query_interpretada": plan.get("query_interpretada"),
        "productos_elegidos": plan.get("productos_elegidos") or [],
        "razonamiento_interno": plan.get("razonamiento", ""),
        "sales_analysis": sales_analysis,
        "meta_razonamiento": meta_razonamiento,
        "allowed_products": productos_contexto,
        "phone": phone,
    }


def run_customer_output_llm(plan, productos_finales, customer_state, primer_mensaje):
    payload = {
        "plan": plan,
        "productos_finales": productos_finales,
        "customer_state": customer_state,
        "primer_mensaje": primer_mensaje,
    }

    phone = plan.get("phone") or customer_state.get("phone") if isinstance(customer_state, dict) else None
    history = _recent_history_for_prompt(phone, limit=8) if phone else []
    if history:
        payload["recent_history"] = [
            {"role": h.get("role"), "content": h.get("content")}
            for h in history
        ]

    messages = [
        {"role": "system", "content": CUSTOMER_OUTPUT_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

    with openai_sem:
        resp = llm_client.completion(
            model=MODEL_OUTPUT,
            messages=messages,
            temperature=0.4,
            max_tokens=300,
        )

    return resp.choices[0].message.content

# ------------------------------------------------------------------
# GENERACIÓN DE RESPUESTAS
# ------------------------------------------------------------------
def _safe_json_parse(text):
    try:
        return json.loads(text)
    except Exception:
        try:
            cleaned = text[text.find("{"):text.rfind("}") + 1]
            return json.loads(cleaned)
        except Exception as e:
            logger.error(f"JSON parse falló: {e}, raw text: {text[:300]}")
            return {}


def _extract_json_block(text: str) -> str:
    if not text:
        return ""
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


def normalize_product_for_llm(product: dict) -> dict:
    if not product:
        return {}
    return {
        "code": product.get("code"),
        "name": product.get("name"),
        "price": float(to_decimal_money(product.get("price_ars", 0))),
        "brand": product.get("brand", ""),
        "model": product.get("model", ""),
        "category": product.get("category", ""),
    }


def build_user_profile_snapshot(phone: str, memory: dict) -> dict:
    profile = {
        "moto_principal": memory.get("most_recent_bike"),
        "ultima_busqueda": memory.get("last_search_query"),
        "categorias_recurrentes": (memory.get("purchase_pattern") or {}).get("main_category"),
    }

    history = get_history_since(phone, days=7, limit=12)
    if history:
        profile["tono_reciente"] = "directo" if any(len(h.get("content", "")) < 25 for h in history[-3:]) else "detallado"
        profile["mensajes_recientes"] = [h.get("content", "") for h in history[-5:]]

    return {k: v for k, v in profile.items() if v}


def build_enriched_context(phone, user_message, intent_data, productos_filtrados):
    """
    Construye memoria con inferencias adicionales: infiere moto habitual y patrones de compra.
    """
    history = get_history_since(phone, days=14, limit=400)
    search_history = get_search_history(phone, limit=5)
    last_search = get_last_search(phone) or {}
    cart_items = cart_get(phone)

    last_messages = []
    for h in history[-10:]:
        prefix = "Yo" if h["role"] == "user" else "Fran"
        last_messages.append(f"{prefix}: {h['content'][:220]}")

    moto_habits = []
    for record in search_history:
        q = record.get("query", "")
        if q:
            moto_habits.append(q[:80])

    cart_summary = [
        {
            "code": code,
            "qty": qty,
            "name": name,
            "price": float(to_decimal_money(price)),
        }
        for code, qty, name, price in (cart_items or [])
    ]

    last_catalog_context = last_search.get("products", [])[:5]
    most_recent_bike = ""
    if last_catalog_context:
        moto = last_catalog_context[0]
        most_recent_bike = f"{moto.get('brand', '')} {moto.get('model', '')}".strip()

    memory = {
        "history_summary": " \n".join(last_messages[-8:]),
        "habitual_queries": moto_habits,
        "last_search_query": last_search.get("query", ""),
        "cart_state": cart_summary,
        "pending_action": get_pending_action(phone),
        "most_recent_bike": most_recent_bike,
        "intent_detected": intent_data.get("intent", "unknown"),
        "allowed_products_snapshot": [
            {
                "code": p.get("code"),
                "name": p.get("name"),
                "brand": p.get("brand", ""),
                "model": p.get("model", ""),
                "price": float(to_decimal_money(p.get("price_ars", 0))),
            }
            for p in (productos_filtrados or [])[:5]
        ],
    }

    # Inferir moto habitual desde historial si no hay una reciente
    if not memory.get("most_recent_bike"):
        for pattern in memory.get("habitual_queries", []):
            parsed = parse_query_v2(pattern, phone)
            motos_detectadas = parsed.get("motos_detectadas") or []
            if motos_detectadas:
                moto = motos_detectadas[0]
                brand = moto.get("brand", "").strip()
                model = moto.get("model", "").strip()
                memory["most_recent_bike"] = f"{brand} {model}".strip()
                if memory["most_recent_bike"]:
                    break

    # Agregar patrón de compra si hay carrito
    if memory.get("cart_state"):
        categories = [item.get("category", "") for item in memory["cart_state"]]
        brands = [item.get("brand", "") for item in memory["cart_state"] if item.get("brand")]
        main_category = None
        if categories:
            counts = Counter([c for c in categories if c])
            if counts:
                main_category = counts.most_common(1)[0][0]
        memory["purchase_pattern"] = {
            "main_category": main_category,
            "avg_order_size": len(memory["cart_state"]),
            "frequent_brands": sorted(set(brands)),
        }

    # ← AGREGAR: Snapshot de productos ANTES de este mensaje
    last_search = get_last_search(phone)
    if last_search and last_search.get("products"):
        # Solo si es reciente (< 10 min)
        metadata = last_search.get("metadata", {}) or {}
        timestamp = metadata.get("timestamp")
        age = 999
        if timestamp:
            try:
                parsed_ts = datetime.fromisoformat(timestamp)
                age = (datetime.now() - parsed_ts).total_seconds() / 60
            except Exception:
                age = 999
        if age < 10:
            memory["allowed_products_snapshot"] = [
                {
                    "code": p.get("code"),
                    "name": p.get("name"),
                    "price": float(to_decimal_money(p.get("price_ars", 0))),
                    "category": p.get("category", ""),
                    "brand": p.get("brand", ""),
                    "model": p.get("model", ""),
                }
                for p in last_search["products"][:5]  # máximo 5 para no saturar el contexto
            ]
        else:
            memory["allowed_products_snapshot"] = []
    else:
        memory["allowed_products_snapshot"] = []

    return memory

def validate_reasoning_json(raw_text, allowed_products):
    parsed = _safe_json_parse(raw_text or "")
    if not isinstance(parsed, dict):
        logger.error(f"JSON parse falló: contenido inválido, raw text: {str(raw_text)[:300]}")
        return None

    allowed_codes = {str(p.get("code")) for p in (allowed_products or []) if p.get("code")}

    parsed_plan = {
        "real_intent": (parsed.get("real_intent") or "unknown").strip(),
        "query_interpretada": (parsed.get("query_interpretada") or "").strip(),
        "razonamiento": (parsed.get("razonamiento") or parsed.get("reason", "")).strip(),
    }

    sales_analysis = parsed.get("sales_analysis") or {}
    alternativas_seguras = [
        str(code)
        for code in (sales_analysis.get("alternativas_seguras") or [])
        if str(code) in allowed_codes
    ]
    producto_recomendado = sales_analysis.get("producto_recomendado")
    if producto_recomendado and str(producto_recomendado) not in allowed_codes:
        producto_recomendado = ""

    senales = sales_analysis.get("senales_de_cierre", [])
    if isinstance(senales, str):
        senales = [senales] if senales else []

    parsed_plan["sales_analysis"] = {
        "tipo_de_cliente": sales_analysis.get("tipo_de_cliente", ""),
        "nivel_de_interes": sales_analysis.get("nivel_de_interes", "medio"),
        "senales_de_cierre": senales,
        "producto_recomendado": producto_recomendado or "",
        "argumentos_clave": sales_analysis.get("argumentos_clave", []),
        "alternativas_seguras": alternativas_seguras,
        "tono_sugerido": sales_analysis.get("tono_sugerido", "concise"),
        "nivel_de_confianza": float(sales_analysis.get("nivel_de_confianza", 0.0) or 0.0),
    }

    productos_elegidos = []
    for decision in parsed.get("productos_elegidos", []) or []:
        code = str(decision.get("code", "")).strip()
        if not code or code not in allowed_codes:
            continue
        try:
            qty = max(1, int(decision.get("qty", 1)))
        except Exception:
            qty = 1
        productos_elegidos.append({
            "code": code,
            "qty": qty,
            "por_que": decision.get("por_que") or decision.get("reason") or decision.get("why", ""),
            "rol": decision.get("rol", "principal"),
        })
    parsed_plan["productos_elegidos"] = productos_elegidos

    requery = parsed.get("requery") or {}
    parsed_plan["requery"] = {
        "NEED_REQUERY": bool(requery.get("NEED_REQUERY", False)),
        "new_query": (requery.get("new_query") or "").strip(),
        "razon": (requery.get("razon") or "").strip(),
        "tipo_de_error": (requery.get("tipo_de_error") or "").strip(),
        "nivel_de_confianza": float(requery.get("nivel_de_confianza", 0.0) or 0.0),
    }
    if parsed_plan["requery"]["NEED_REQUERY"] and not parsed_plan["requery"]["new_query"]:
        logger.warning("Plan solicitó requery pero no propuso new_query")
        return None

    meta = parsed.get("meta_razonamiento") or {}
    parsed_plan["meta_razonamiento"] = {
        "confianza": float(meta.get("confianza", 0.0) or 0.0),
        "riesgos": meta.get("riesgos", ""),
        "datos_faltantes": meta.get("datos_faltantes", ""),
        "accion_sugerida": meta.get("accion_sugerida", "responder_con_cautela"),
    }

    tono_sugerido = parsed_plan["sales_analysis"].get("tono_sugerido") or "concise"
    parsed_plan["response_tone"] = tono_sugerido
    return parsed_plan


def ejecutar_plan_interno(parsed_plan, phone, productos_permitidos):
    """
    Convierte el plan JSON en acciones concretas usando sólo productos permitidos.
    Ejecuta side-effects como guardar contexto de moto y devuelve un snapshot
    seguro para el segundo paso de LLM.
    """
    try:
        if not parsed_plan:
            return None

        def confirmar_accion_humana(texto):
            frases = [
                "Listo, ya lo guardé.",
                "Perfecto, te lo dejo preparado.",
                "Genial, lo actualicé.",
                "Ok, lo tengo anotado.",
            ]
            return random.choice(frases) + " " + texto

        mensajes_internos = []

        productos_permitidos_map = {
            str(p.get("code")): p for p in (productos_permitidos or []) if p.get("code")
        }

        productos_seleccionados = []
        for decision in parsed_plan.get("productos_elegidos", []) or []:
            code = decision.get("code")
            if not code:
                continue

            producto = productos_permitidos_map.get(str(code))
            if not producto:
                logger.warning(f"Producto {code} fuera de allowed_products, se descarta")
                continue

            try:
                qty_sugerida = max(1, int(decision.get("qty", 1)))
            except Exception:
                qty_sugerida = 1

            productos_seleccionados.append({
                **producto,
                "qty_sugerida": qty_sugerida,
                "razon": decision.get("por_que", ""),
                "rol": decision.get("rol", "principal"),
            })

        for action in parsed_plan.get("actions_to_execute", []) or []:
            action_type = action.get("type")
            acciones_realizadas = False

            if action_type == "save_moto_context":
                save_moto_context(phone, action.get("brand", ""), action.get("model", ""))
                acciones_realizadas = True

            elif action_type == "add_to_cart":
                products_to_add = action.get("products", [])
                qty_each = action.get("qty_each", 1)

                for code in products_to_add:
                    producto = productos_permitidos_map.get(str(code))
                    if producto:
                        cart_add(
                            phone,
                            code,
                            qty_each,
                            producto.get("name", ""),
                            to_decimal_money(producto.get("price_ars", 0)),
                            to_decimal_money(producto.get("price_usd", 0))
                        )
                        acciones_realizadas = True

            if acciones_realizadas:
                mensajes_internos.append(confirmar_accion_humana("Si necesitás algo más avisame."))

        if parsed_plan.get("products_strategy", {}).get("sugerir_opcion_unica"):
            mensajes_internos.append("Recomendado como mejor opción por relación calidad/precio.")

        if not productos_seleccionados and not parsed_plan.get("query_interpretada"):
            mensajes_internos.append(random.choice([
                "¿Para qué modelo lo necesitás?",
                "Decime año y cilindrada y te paso exacto.",
            ]))

        return {
            "productos_finales": productos_seleccionados,
            "metadata": {
                "reference_resolution": parsed_plan.get("reference_resolution"),
            },
            "customer_state": parsed_plan.get("customer_state", {}),
            "response_tone": parsed_plan.get("response_tone"),
            "mensajes_internos": mensajes_internos,
        }
    except Exception as e:
        logger.error(f"Error ejecutando plan interno: {e}")
        return None


def pensar_con_llm(system_prompt_interno, contexto, productos_filtrados):
    """
    Ejecuta el paso de razonamiento interno del flujo dual de LLM.

    Usa un prompt de sistema orientado a JSON y envía un contexto compacto con
    memoria viva y productos permitidos. El resultado debe ser texto serializado
    en JSON, pensado para parsearse con `json.loads`.
    """
    try:
        productos_compactos = [
            {
                "code": p.get("code"),
                "name": p.get("name"),
                "price": float(to_decimal_money(p.get("price_ars", 0))),
                "brand": p.get("brand", ""),
                "model": p.get("model", ""),
            }
            for p in (productos_filtrados or [])
        ]

        mensajes = [
            {"role": "system", "content": system_prompt_interno},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "contexto": contexto,
                        "productos_permitidos": productos_compactos,
                        "historial_relevante": contexto.get("historial", []),
                        "recordatorio": "DEVOLVÉ SOLO JSON, SIN TEXTO ADICIONAL",
                    },
                    ensure_ascii=False,
                ),
            },
        ]

        with openai_sem:
            resp = llm_client.completion(
                model=MODEL_REASONING,
                messages=mensajes,
                temperature=0.15,
                max_tokens=600,
            )

        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.error(f"pensar_con_llm error: {e}")
        return "{}"


def responder_con_llm(system_prompt_cliente, razonamiento_interno):
    """
    Genera la respuesta final al cliente a partir del plan interno.

    El razonamiento interno puede llegar como string JSON o como objeto; acá se
    serializa y se pasa al LLM con un prompt conversacional pensado para
    WhatsApp.
    """
    try:
        razonamiento_serializado = razonamiento_interno if isinstance(razonamiento_interno, str) else json.dumps(
            razonamiento_interno or {}, ensure_ascii=False
        )
        mensajes = [
            {"role": "system", "content": system_prompt_cliente},
            {
                "role": "user",
                "content": (
                    "Generá la respuesta final para el cliente en WhatsApp usando este plan interno. "
                    "NO muestres el plan ni reglas. Considerá sales_analysis y meta_razonamiento solo como guía de tono y seguridad.\n\nPLAN INTERNO:\n"
                    + razonamiento_serializado
                ),
            },
        ]

        with openai_sem:
            resp = llm_client.completion(
                model=MODEL_RESPONSE,
                messages=mensajes,
                temperature=0.35,
                max_tokens=600,
            )

        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.error(f"responder_con_llm error: {e}")
        return "Uy, tuve un problema. ¿Me repetís?"

def build_execution_summary(ctx):
    lines = []
    lines.append(f"Intent detectado: {ctx['intent_detected']}")
    if ctx["search_executed"]:
        lines.append(f"Query de búsqueda: '{ctx['search_query']}'")
        lines.append(f"Productos encontrados: {ctx['products_found']}")
        lines.append(f"Productos relevantes mostrados: {ctx['products_shown_to_llm']}")
        if ctx.get("quality_assessment"):
            qa = ctx["quality_assessment"]
            lines.append(f"Calidad de resultados: {qa.get('confidence', 'unknown').upper()} (avg score: {qa.get('avg_score', 0):.1f})")
        if ctx["filters_applied"]:
            lines.append(f"Filtros aplicados: {', '.join(ctx['filters_applied'])}")
        if ctx["will_send_chunks"]:
            chunk_info = ctx["chunk_info"]
            lines.append(f"⚠️ Se enviarán {chunk_info['total_chunks']} mensajes adicionales con {chunk_info['total_products']} productos.")
            lines.append("Prepará al cliente para recibirlos.")
    else:
        lines.append("No se ejecutó búsqueda de productos")
    if ctx["warnings"]:
        lines.append(f"Advertencias: {'; '.join(ctx['warnings'])}")
    return "\n".join(lines)


def build_full_history_prompt(phone: str, user_message: str, catalog_products: list, system_prompt: str = None) -> list:
    if system_prompt is None:
        system_prompt = CITATION_ENFORCED_PROMPT

    msgs = [{"role": "system", "content": system_prompt}]

    history = get_history_since(phone, days=7, limit=2000)
    token_count = len(system_prompt.split())

    temp_msgs = []
    for h in reversed(history):
        role = "assistant" if h["role"] == "assistant" else "user"
        content = h["content"]
        temp_msgs.append({"role": role, "content": content})
        token_count += len(content.split())
        if token_count > 120000:
            break

    for m in reversed(temp_msgs):
        msgs.append(m)

    if catalog_products:
        catalog_text = "\n".join([
            f"- {p['name']} (Cod: {p.get('code', 'N/A')}) - {format_price(Decimal(str(p['price_ars'])))}"
            for p in catalog_products[:15]
        ])
        msgs.append({
            "role": "system",
            "content": f"[TOP {len(catalog_products)} PRODUCTOS DISPONIBLES]\n{catalog_text}"
        })

    msgs.append({"role": "user", "content": user_message})
    return msgs


def _recent_history_for_prompt(phone: str, limit: int = 12) -> list:
    """Recupera historial reciente asegurando contexto consistente para el LLM."""

    history = get_history_since(phone, days=7, limit=limit) if phone else []
    if history and history[-1].get("role") == "user":
        # Evitar duplicar el último mensaje del usuario cuando ya viene en el payload
        history = history[:-1]
    return history[-limit:]


def build_social_reply(phone: str, user_message: str, semantic_signals: dict | None = None) -> str:
    """Return a warm greeting crafted by the LLM; fall back to a smart template if needed."""

    semantic_signals = semantic_signals or {}
    normalized = (user_message or "").strip()

    # Contexto breve para el LLM: historial cercano y señales sociales detectadas
    recent_history = get_history_since(phone, days=1, limit=6)
    social_context = {
        "message": normalized,
        "semantic_signals": semantic_signals,
        "recent_user_messages": [h["content"] for h in recent_history if h.get("role") == "user"][-3:],
        "recent_assistant_messages": [h["content"] for h in recent_history if h.get("role") == "assistant"][-2:],
    }

    system_prompt = (
        "Sos Fran de Tercom. Respondé saludos o charla social en 1-3 líneas, tono humano y cercano. "
        "Si hay follow-ups, retoma la conversación sin repetir listas; mencioná que podés ayudar con repuestos "
        "si el cliente quiere. No inventes códigos ni precios, no armes listados."
    )

    try:
        with openai_sem:
            llm_resp = llm_client.completion(
                model=MODEL_RESPONSE,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(social_context, ensure_ascii=False)},
                ],
                temperature=0.5,
                max_tokens=120,
            )

        candidate = (llm_resp.choices[0].message.content or "").strip()
        if candidate:
            return candidate
    except Exception as e:
        logger.warning(f"Fallo LLM en social reply: {e}")

    # Fallback ligero y personalizado si el LLM no responde
    follow_up = semantic_signals.get("follow_up_markers")
    base_templates = [
        "¡Hola! Soy Fran de Tercom 🙌. Contame qué repuesto buscás y el modelo/año de tu moto y te paso opciones.",
        "¡Hola! Acá Fran de Tercom. Decime qué repuesto necesitás y qué moto tenés; te comparto precios rápido.",
        "¡Hola! Soy Fran. Contame el repuesto que buscás y el modelo de tu moto, así te ayudo al toque.",
    ]

    if follow_up:
        return (
            "¡Hola de nuevo! Soy Fran. Avisame qué repuesto y modelo de moto y te sigo ayudando en base a lo anterior."
        )

    selector_seed = f"{phone}:{normalized}" or "default"
    idx = int(hashlib.sha256(selector_seed.encode()).hexdigest(), 16) % len(base_templates)
    return base_templates[idx]


def generate_smart_ai_reply_v2(phone, user_message, catalog_products, execution_context, system_prompt=None):
    try:
        history = _recent_history_for_prompt(phone, limit=12)
        short_history = history[-8:]
        primer_mensaje = len(short_history) == 0
        memory = build_enriched_context(phone, user_message, execution_context.get("intent_details", {}), catalog_products)
        contexto_prev = {
            "mensaje_usuario": user_message,
            "search_query": execution_context.get("search_query"),
            "warnings": execution_context.get("warnings", []),
            "historial": [{"role": h["role"], "content": h["content"]} for h in short_history],
            "metadata_catalogo": {"productos_total": len(catalog_products or [])},
            "ultimo_contexto": execution_context,
            "memoria_viva": memory,
            "pending_actions": memory.get("pending_action"),
            "cart_state": memory.get("cart_state", []),
            "most_recent_bike": memory.get("most_recent_bike", ""),
        }

        productos_permitidos = catalog_products or []

        # 1) Construir conversación reducida para análisis comercial
        historial = short_history[-6:] if short_history else []
        conversacion_completa = [h.get("content", "") for h in historial]

        # 2) Ejecutar análisis comercial previo
        sales_result = run_sales_analysis_llm(
            conversacion_completa=conversacion_completa,
            productos_disponibles=productos_permitidos,
            contexto_cliente=memory,
            perfil_cliente=memory.get("perfil_cliente", {}),
            phone=phone,
        )

        # Validación final del análisis comercial
        parsed = sales_result or {}
        sales = parsed.get("sales_analysis", {}) or {}

        parsed["sales_analysis"] = {
            "tipo_de_cliente": sales.get("tipo_de_cliente", ""),
            "nivel_de_interes": sales.get("nivel_de_interes", "medio"),
            "senales_de_cierre": sales.get("senales_de_cierre", []),
            "producto_recomendado": sales.get("producto_recomendado", ""),
            "argumentos_clave": sales.get("argumentos_clave", []),
            "alternativas_seguras": sales.get("alternativas_seguras", []),
            "tono_sugerido": sales.get("tono_sugerido", "concise"),
            "nivel_de_confianza": float(sales.get("nivel_de_confianza", 0.0) or 0.0),
        }

        sales_analysis = parsed.get("sales_analysis", {})
        contexto_prev["sales_analysis"] = sales_analysis

        plan_interno = pensar_con_llm(
            system_prompt or PLANNING_UNIFIED_PROMPT,
            contexto_prev,
            productos_permitidos,
        )

        parsed_plan = validate_reasoning_json(plan_interno, productos_permitidos)

        max_requery_attempts = 1
        requery_count = 0
        while parsed_plan and parsed_plan.get("requery", {}).get("NEED_REQUERY") and requery_count < max_requery_attempts:
            requery_count += 1
            nueva_query = parsed_plan.get("requery", {}).get("new_query")
            try:
                semantic_results = hybrid_search(nueva_query, phone=phone, top_k=MAX_SEARCH_RESULTS)
                if isinstance(semantic_results, dict):
                    productos_permitidos = list(semantic_results.get("final_candidates") or [])[:MAX_PRODUCTS_FOR_LLM]
                else:
                    productos_permitidos = [p for p, _ in semantic_results][:MAX_PRODUCTS_FOR_LLM]
                execution_context["search_query"] = nueva_query
                memory = build_enriched_context(phone, user_message, execution_context.get("intent_details", {}), productos_permitidos)
                contexto_prev.update({
                    "search_query": nueva_query,
                    "memoria_viva": memory,
                    "metadata_catalogo": {"productos_total": len(productos_permitidos)},
                    "cart_state": memory.get("cart_state", []),
                })
                plan_interno = pensar_con_llm(
                    system_prompt or PLANNING_UNIFIED_PROMPT,
                    contexto_prev,
                    productos_permitidos,
                )
                parsed_plan = validate_reasoning_json(plan_interno, productos_permitidos)
            except Exception as e:
                logger.error(f"Re-búsqueda fallida: {e}")
                parsed_plan = None

        if not parsed_plan:
            logger.warning("Fallback: razonamiento inválido, usando catálogo real")
            fallback_reply = None
            if productos_permitidos:
                listado = format_search_results(productos_permitidos[:5])
                fallback_reply = (
                    "Te dejo opciones reales del catálogo mientras confirmo bien tu pedido:\n\n"
                    f"{listado}\n\n"
                    "¿Alguna te sirve o querés que refine por marca/modelo/categoría?"
                )
            else:
                fallback_reply = "Necesito un dato más para ayudarte: marca, modelo o categoría de la moto."
            return {"reply": fallback_reply, "plan": None, "execution": None}

        meta_accion = parsed_plan.get("meta_razonamiento", {}).get("accion_sugerida")
        if meta_accion == "pedir_aclaracion" and not parsed_plan.get("productos_elegidos"):
            clarification = "Necesito un dato más para afinar la búsqueda (marca/modelo/año o categoría específica)."
            return {"reply": clarification, "plan": parsed_plan, "execution": None}

        plan_ejecutado = ejecutar_plan_interno(parsed_plan, phone, productos_permitidos)
        if plan_ejecutado and plan_ejecutado.get("mensajes_internos"):
            parsed_plan["mensajes_internos"] = plan_ejecutado.get("mensajes_internos")

        productos_finales = (plan_ejecutado or {}).get("productos_finales") or []
        customer_state = (plan_ejecutado or {}).get("customer_state") or {}

        respuesta = (run_customer_output_llm(
            plan=parsed_plan,
            productos_finales=productos_finales,
            customer_state=customer_state,
            primer_mensaje=primer_mensaje,
        ) or "").strip()
        return {
            "reply": respuesta or "Uy, tuve un problema. ¿Me repetís?",
            "plan": parsed_plan,
            "execution": plan_ejecutado,
        }
    except Exception as e:
        logger.error(f"generate_smart_ai_reply_v2 error: {e}")
        return {
            "reply": "Estoy ajustando el sistema, ¿me repetís el pedido con marca y modelo?",
            "plan": None,
            "execution": None,
        }

def build_tech_expert_answer(phone, user_message):
    try:
        history = get_history_since(phone, days=1, limit=10)
        msgs = [{"role": "system", "content": TECH_SYSTEM_PROMPT}]

        for h in history[-10:]:
            role = "assistant" if h["role"] == "assistant" else "user"
            msgs.append({"role": role, "content": h["content"]})

        msgs.append({
            "role": "user",
            "content": f"Pregunta del cliente: {user_message[:600]}"
        })

        with openai_sem:
            resp = llm_client.completion(
                model=MODEL_NAME,
                messages=msgs,
                temperature=0.4,
                max_tokens=350,
            )

        txt = (resp.choices[0].message.content or "").strip()

        if not txt or len(txt) < 10:
            return "Te cuento rápido y si querés te paso opciones concretas del catálogo."

        return txt
    except Exception as e:
        logger.error(f"build_tech_expert_answer error: {e}")
        return "Estoy revisando la mejor recomendación. ¿Querés que te avise y de paso te cotizo repuestos?"


def build_checkout_response(phone):
    items = cart_get(phone)
    total, _ = cart_totals(phone)
    if items:
        resumen = "\n".join([f"- {q}x {name[:40]}" for _, q, name, _ in items[:3]])
        header = f"Tengo {len(items)} productos listos:\n{resumen}"
        total_line = f"Total estimado: {format_price(total)}."
    else:
        header = "Todavía no cargamos productos. Decime qué necesitás y lo sumo."
        total_line = ""

    pasos = (
        "Paso final: confirmame forma de pago (transferencia, efectivo o cheque clientes)"
        " y el envío (moto CABA 24-48hs, despacho interior 3-5 días o retiro)."
    )
    cierre = "¿Avanzamos con el cierre?"
    return "\n\n".join([text for text in (header, total_line, pasos, cierre) if text])


def build_payment_response(phone):
    total, _ = cart_totals(phone)
    lines = []
    if total > 0:
        lines.append(f"Total estimado del carrito: {format_price(total)}.")
    lines.append("Medios disponibles: transferencia (te paso alias), efectivo al retirar y cheque/QR para clientes habituales.")
    lines.append("Apenas mandes el comprobante libero el pedido al mensajero o despacho.")
    return "\n".join(lines)


def build_shipping_response(phone):
    items = cart_get(phone)
    if items:
        header = f"Tengo {len(items)} productos listos para envío."
    else:
        header = "Coordinamos el envío cuando me confirmes qué necesitás."
    lines = [
        header,
        "Opciones: moto propia en CABA (24-48hs, gratis arriba de $100.000), despacho a interior por expreso en 3-5 días o retiro/tu mensajero.",
        "¿Cuál te sirve así lo agenda?"
    ]
    return "\n".join(lines)

# ------------------------------------------------------------------
# FORMATO RESULTADOS
# ------------------------------------------------------------------
CATEGORY_EMOJIS = {
    "aceite": "🛢️",
    "filtro": "🔧",
    "bateria": "🔋",
    "neumatico": "🛞",
    "cadena": "⛓️",
    "bujia": "⚡",
    "pastilla": "🔴",
    "amortiguador": "🔩",
    "kit": "📦",
}


def format_search_results(products):
    lines = []
    for i, p in enumerate(products, 1):
        name_lower = p.get("name", "").lower()
        emoji = next((CATEGORY_EMOJIS[k] for k in CATEGORY_EMOJIS if k in name_lower), "📦")
        price_raw = p.get("price_ars")
        has_price = price_raw not in (None, "", 0, "0", "0.0", "0.00")
        price = format_price(price_raw) if has_price else "Precio a confirmar"
        name = p.get("name", "").strip()
        code = p.get("code", "")
        brand = p.get("brand", "")
        model = p.get("model", "")
        extra = []
        if brand:
            extra.append(brand)
        if model:
            extra.append(model)
        extra_txt = f" - {' / '.join(extra)}" if extra else ""
        lines.append(f"{emoji} {i}. {name[:50]} ({code}){extra_txt}\n   {price}")
    return "\n\n".join(lines)


def format_multi_search_response(results: dict) -> str | None:
    if results.get("multisearch"):
        blocks = []
        for cat, items in results["results"].items():
            block = f"🔹 *{cat.upper()}*\n"
            for p in items[:5]:
                block += f"- {p.get('name', '')} ({p.get('code', '')}) - ${p.get('price_ars', '')}\n"
            blocks.append(block)
        return "📦 Acá tenés por categoría:\n\n" + "\n\n".join(blocks)

    if results.get("multi_moto"):
        blocks = []
        for moto, items in results["results"].items():
            block = f"🏍️ *{moto.upper()}*\n"
            for p in items[:5]:
                block += f"- {p.get('name', '')} ({p.get('code', '')}) - ${p.get('price_ars', '')}\n"
            blocks.append(block)
        return "📦 Acá tenés por moto:\n\n" + "\n\n".join(blocks)

    if results.get("multi_moto_multi_cat"):
        blocks = []
        for combo, items in results["results"].items():
            block = f"🔧 *{combo.upper()}*\n"
            for p in items[:5]:
                block += f"- {p.get('name', '')} ({p.get('code', '')}) - ${p.get('price_ars', '')}\n"
            blocks.append(block)
        return "📦 Resultados por moto y categoría:\n\n" + "\n\n".join(blocks)

    return None


def _persist_search_snapshot(phone: str, products: list, query: str):
    """Guarda la última búsqueda y el historial cuando devolvemos listas directas."""
    if not phone:
        return

    snapshot = []
    for p in products[:MAX_ITEMS]:
        code = p.get("code")
        name = p.get("name", "")
        if not code and not name:
            continue

        snapshot.append({
            "code": code,
            "name": name,
            "price_ars": p.get("price_ars"),
            "price_usd": p.get("price_usd"),
            "qty": int(p.get("qty", 1) or 1),
        })

    if not snapshot:
        return

    save_last_search(phone, snapshot, query)
    save_to_search_history(phone, snapshot, query)

# =========================================================
# ORQUESTADOR FRAN – VERSIÓN 3.15 (templates estructurados)
# =========================================================
def orquestar_fran_v315(mensaje_usuario: str, phone: str) -> str:
    """
    Orquestador con templates estructurados para comprensión, selección y respuesta.
    """
    start_time = time.time()
    user_message = sanitize_input(mensaje_usuario or "", max_length=1500)

    if not rate_limit_check(phone):
        reply = "Demasiados mensajes, esperá un minuto."
        save_message(phone, reply, "assistant")
        return reply

    save_message(phone, user_message, "user")

    logger.info(f"[STEP 1] Understanding query: {user_message}")

    understanding = complete_template(
        "query_understanding",
        {
            "phone": phone,
            "user_message": user_message,
            "conversation_history": get_history_since(phone, days=1, limit=5),
            "last_search_query": (get_last_search(phone) or {}).get("query", "")
        },
    )

    if understanding.get("needs_clarification"):
        reply = understanding.get("clarification_question") or "¿Me pasás más detalles de la moto y el repuesto?"
        save_message(phone, reply, "assistant")
        return reply

    normalized_query = understanding.get("normalized_query") or user_message
    intent = _canonical_intent(understanding.get("intent"))

    logger.info(
        f"[STEP 1] Normalized: '{normalized_query}' | Intent: {intent} | Corrections: {understanding.get('corrections')}"
    )

    # ============================================
    # STEP 2: SEARCH
    # ============================================
    logger.info(f"[STEP 2] Searching products...")

    allowed_products = []
    quality = {"sufficient": True, "confidence": 1.0, "reason": "social_intent"}

    if intent in ["social", "greeting", "small_talk", "conversation"]:
        allowed_products = []
        logger.info("[STEP 2] Bypass search for social intent")
    else:
        allowed_payload = run_allowed_products_search(normalized_query, phone=phone)
        allowed_products = list(allowed_payload.get("final_candidates") or [])
        logger.info(f"[STEP 2] Allowed products: {len(allowed_products)}")

        if allowed_payload.get("error") == "too_many_combinations":
            reply = allowed_payload.get("message", "Pasame una sola moto o categoría.")
            save_message(phone, reply, "assistant")
            return reply

        reply = format_multi_search_response(allowed_payload)
        if reply:
            _persist_search_snapshot(
                phone,
                allowed_payload.get("final_candidates") or [],
                normalized_query,
            )
            save_message(phone, reply, "assistant")
            return reply

        quality = assess_context_quality(normalized_query, allowed_products)

        if not quality["sufficient"]:
            if quality["action"] == "ask_clarification":
                reply = quality.get("message") or "Necesito un dato más (marca/modelo/año)."
            else:
                top_products = quality.get("top_products", [])[:3]
                suggestions = "\n".join(
                    [
                        f"- {p.get('name', '')} ({p.get('code', '')}) - {format_price(p.get('price_ars', 0))}"
                        for p in top_products
                    ]
                )
                reply = (
                    "No encontré coincidencia perfecta. Tengo:\n\n"
                    f"{suggestions}\n\n"
                    "¿Te sirve alguna o dame más detalles?"
                )

            save_message(phone, reply, "assistant")
            log_interaction(phone, user_message, f"low_quality_{quality.get('reason', 'unknown')}", 0)
            log_performance(phone, "low_quality", time.time() - start_time, len(allowed_products))
            return reply

        save_last_search(
            phone,
            [
                {
                    "code": p["code"],
                    "name": p.get("name", ""),
                    "price_ars": p.get("price_ars"),
                    "price_usd": p.get("price_usd"),
                    "qty": 1,
                }
                for p in allowed_products[:MAX_ITEMS]
            ],
            normalized_query,
        )

        logger.info(
            f"[STEP 2] Found {len(allowed_products)} relevant products | Quality: {quality.get('confidence')}"
        )

    # ============================================
    # STEP 3: PRODUCT SELECTION (LLM)
    # ============================================

    # Si el intent es social, saltear la selección de productos
    if intent in ["social", "greeting", "small_talk", "conversation"]:
        logger.info("[STEP 3] Skipping product selection for social intent")
        selected_products = []
        selection = {
            "selected_products": [],
            "analysis": {
                "customer_type": "nuevo",
                "interest_level": "bajo",
                "key_arguments": []
            },
            "action": "show_products"
        }
    else:
        logger.info(f"[STEP 3] Selecting best products...")

        selection = complete_template(
            "product_selection",
            {
                "phone": phone,
                "normalized_query": normalized_query,
                "original_query": user_message,
                "entities": understanding.get("entities", {}),
                "intent": intent,
                "allowed_products": [
                    {
                        "code": p.get("code", ""),
                        "name": p.get("name", ""),
                        "price_ars": float(p.get("price_ars", 0)),
                        "brand": p.get("brand", ""),
                        "model": p.get("model", ""),
                        "category": p.get("category", ""),
                    }
                    for p in allowed_products[:MAX_PRODUCTS_FOR_LLM]
                ],
                "conversation_context": {
                    "cart_items": len(cart_get(phone)),
                    "sales_phase": get_sales_phase(phone),
                    "is_first_message": len(get_history_since(phone, days=1, limit=5)) <= 1,
                },
            },
        )

        if selection.get("action") == "ask_clarification":
            reply = selection.get("clarification_needed", "Necesito un dato más (marca/modelo/año).")
            save_message(phone, reply, "assistant")
            return reply

        selected_products = selection.get("selected_products", [])

        logger.info(
            f"[STEP 3] Selected {len(selected_products)} products | Customer: {selection.get('analysis', {}).get('customer_type')}"
        )

    # ============================================
    # STEP 4: RESPONSE GENERATION (LLM)
    # ============================================
    logger.info(f"[STEP 4] Generating response...")

    response = complete_template(
        "response_generation",
        {
            "phone": phone,
            "intent": intent,
            "selected_products": selected_products,
            "customer_analysis": selection.get("analysis", {}),
            "query_context": {
                "original": user_message,
                "normalized": normalized_query,
                "corrections": understanding.get("corrections", []),
            },
            "conversation_state": {
                "sales_phase": get_sales_phase(phone),
                "cart_total": format_price(cart_totals(phone)[0]),
            },
        },
    )

    reply = response.get("message", "")
    products_cited = response.get("products_cited", [])

    # ============================================
    # STEP 5: POST-VALIDATION
    # ============================================
    logger.info(f"[STEP 5] Validating response...")

    allowed_codes = {p.get("code") for p in allowed_products[:MAX_PRODUCTS_FOR_LLM] if p.get("code")}
    hallucinated = set(products_cited) - allowed_codes

    if hallucinated:
        logger.error(f"⚠️ LLM cited invalid codes: {hallucinated}")
        reply = format_search_results(allowed_products[:5])
        reply = f"Te muestro opciones:\n\n{reply}\n\n¿Cuál te sirve?"

    if len(allowed_products) > MAX_PRODUCTS_FOR_LLM:
        remaining_products = allowed_products[MAX_PRODUCTS_FOR_LLM:]
        if remaining_products:
            chunks = [
                remaining_products[i : i + PRODUCTS_PER_CHUNK]
                for i in range(0, len(remaining_products), PRODUCTS_PER_CHUNK)
            ]

            for idx, chunk in enumerate(chunks, 1):
                chunk_text = f"━━━ Más opciones ({idx}/{len(chunks)}) ━━━\n"
                chunk_text += format_search_results(chunk)
                time.sleep(0.5)
                send_long_message(phone, chunk_text)

    save_message(phone, reply, "assistant")
    log_interaction(phone, user_message, intent, len(selected_products))
    log_performance(phone, intent, time.time() - start_time, len(allowed_products))
    update_sales_phase_from_intent(phone, intent)

    logger.info(f"[DONE] Response sent | Duration: {time.time()-start_time:.2f}s")

    return reply

# =========================================================
# ORQUESTADOR FRAN – VERSIÓN 3.16 (ARQUITECTURA HÍBRIDA)
# =========================================================

LLM1_UNDERSTANDING_SCHEMA = {
    "type": "object",
    "required": [
        "phase",
        "raw_query",
        "intent",
        "brand",
        "model",
        "displacement_cc",
        "usage_context",
        "product_type",
        "metadata",
        "confidence",
    ],
    "properties": {
        "phase": {"const": "understanding"},
        "raw_query": {"type": "string"},
        "intent": {"type": "string"},
        "brand": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "displacement_cc": {"type": ["integer", "null"]},
        "usage_context": {"type": ["string", "null"]},
        "product_type": {"type": ["string", "null"]},
        "metadata": {
            "type": "object",
            "required": ["year_range", "additional_constraints", "ambiguity_level"],
            "properties": {
                "year_range": {
                    "anyOf": [
                        {"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2},
                        {"type": "null"},
                    ]
                },
                "additional_constraints": {"type": ["string", "null"]},
                "ambiguity_level": {"enum": ["low", "medium", "high"]},
            },
        },
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
}

SEARCH_PHASE_SCHEMA = {
    "type": "object",
    "required": [
        "phase",
        "query_params",
        "results",
        "total_results",
        "target_family",
        "has_structured_compatibility",
        "needs_llm_compatibility",
    ],
    "properties": {
        "phase": {"const": "search"},
        "query_params": {
            "type": "object",
            "required": ["text_query", "filters"],
            "properties": {
                "text_query": {"type": "string"},
                "filters": {
                    "type": "object",
                    "required": ["brand", "model", "displacement_cc"],
                    "properties": {
                        "brand": {"type": ["string", "null"]},
                        "model": {"type": ["string", "null"]},
                        "displacement_cc": {"type": ["integer", "null"]},
                    },
                },
            },
        },
        "results": {"type": "array"},
        "total_results": {"type": "integer"},
        "target_family": {"type": ["string", "null"]},
        "has_structured_compatibility": {"type": "boolean"},
        "needs_llm_compatibility": {"type": "boolean"},
    },
}

LLM2_SCHEMA = {
    "type": "object",
    "required": ["phase", "candidates_evaluated", "llm2_confidence_overall", "needs_requery", "products_excluded"],
    "properties": {
        "phase": {"const": "llm2_reasoning"},
        "candidates_evaluated": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "product_id",
                    "compatibility_decision",
                    "confidence_score",
                    "technical_reasoning",
                    "justification_type",
                    "risk_level",
                ],
                "properties": {
                    "product_id": {"type": ["string", "integer"]},
                    "compatibility_decision": {"enum": ["compatible", "incompatible", "marginal"]},
                    "confidence_score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                    "technical_reasoning": {"type": "string"},
                    "justification_type": {
                        "enum": ["catalog_match", "specification_inference", "semantic_similarity"],
                    },
                    "risk_level": {"enum": ["low", "medium", "high"]},
                    "name": {"type": ["string", "null"]},
                },
            },
        },
        "products_excluded": {"type": "array"},
        "llm2_confidence_overall": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "needs_requery": {"type": "boolean"},
    },
}


def _validate_phase(payload: dict, schema: dict, phase_name: str) -> bool:
    try:
        validate(payload, schema)
        return True
    except ValidationError as e:
        logger.error(f"[v3.16][{phase_name}] JSON inválido: {e.message}")
        return False


def _normalize_confidence(value: float, minimum: float = 0.0) -> float:
    try:
        val = float(value)
    except Exception:
        return minimum
    return max(min(val, 1.0), minimum)


def _derive_ambiguity(confidence: float) -> str:
    if confidence < 0.7:
        return "high"
    if confidence < 0.85:
        return "medium"
    return "low"


def detect_follow_up_intent(query: str, session: dict | None) -> dict | None:
    """LLM-driven follow-up detection based on the last search context."""

    if not session or not session.get("last_search"):
        return None

    last_context = session.get("last_search") or {}

    prompt = f"""Eres un asistente analizando si el nuevo mensaje del usuario es un 
follow-up (refinamiento, precio, cantidad, etc) de una búsqueda anterior.

BÚSQUEDA ANTERIOR:
- Query: \"{last_context.get('query', '')}\"
- Marca: {last_context.get('metadata', {}).get('brand')}
- Modelo: {last_context.get('metadata', {}).get('model')}
- Familia: {last_context.get('metadata', {}).get('family')}
- Productos encontrados: {len(last_context.get('results') or [])}

NUEVO MENSAJE:
\"{query}\"

Responde SOLO en JSON (sin markdown):
{{
    "is_follow_up": true/false,
    "follow_up_type": "price" | "quantity" | "show_more" | "comparison" | "reference" | "cart_action" | null,
    "reasoning": "breve explicación máx 50 caracteres",
    "inherit_context": true/false,
    "context_fields": ["brand", "model", "family"]
}}
"""

    try:
        with openai_sem:
            resp = llm_client.completion(
                model=MODEL_REASONING,
                messages=[
                    {"role": "system", "content": prompt},
                ],
                temperature=0.1,
                response_format={"type": "json_object"},
            )

        llm_result = json.loads(resp.choices[0].message.content)
        if llm_result.get("is_follow_up"):
            return {
                "detected": True,
                "type": llm_result.get("follow_up_type"),
                "reasoning": llm_result.get("reasoning"),
                "use_cached_results": True,
                "cached_product_ids": [
                    r.get("product_id") for r in (last_context.get("results") or []) if r.get("product_id")
                ][:5],
                "inherit_context": llm_result.get("inherit_context"),
                "context_fields": llm_result.get("context_fields") or [],
                "inherited_confidence": last_context.get("confidence") or 0.0,
            }
    except Exception as e:
        logger.warning(f"Follow-up detection LLM failed: {e}")
        return None

    return None


def _phase1_llm1_understanding(user_message: str, phone: str | None = None, session: dict | None = None) -> dict:
    semantic_signals = detect_semantic_entities(user_message)
    implicit_cart_action = detect_implicit_cart_action(user_message, phone) if phone else None

    lower_query = (user_message or "").lower()

    # FIX: Clasificar intent principal basado en señales semánticas
    # Permitir multi-intent: un mensaje puede tener componente social + técnico
    intent = "product_search"
    if semantic_signals.get("is_simple_greeting"):
        # Solo clasificar como social puro si es un saludo simple SIN señales técnicas
        intent = "social"
    elif semantic_signals.get("has_social") and not semantic_signals.get("has_technical"):
        # Social sin ninguna señal técnica
        intent = "social"
    elif any(token in lower_query for token in ["compar", "vs", "versus"]):
        intent = "comparacion"
    elif intent == "product_search" and "especific" in lower_query:
        intent = "especificacion"
    # Si hay señales técnicas, intent es product search (incluso si también hay marcadores sociales)

    # FIX MULTI-INTENT: Extraer entidades si hay señales técnicas, INDEPENDIENTEMENTE del intent
    # Esto permite mensajes como "hola, quiero baterías" donde hay social + product_search
    brand = None
    model = None
    displacement = None
    product_type = None

    intents = merge_intents_with_semantics(semantic_signals, intent)
    if implicit_cart_action:
        intent = "cart_action"
        if "cart_action" not in intents:
            intents = ["cart_action"] + intents
    if semantic_signals.get("has_technical") and "product_search" in intents:
        intent = "product_search"
    elif intents:
        intent = intents[0]

    # Solo NO extraer entidades si es un saludo PURO (is_simple_greeting Y no has_technical)
    should_extract_entities = not (semantic_signals.get("is_simple_greeting") and not semantic_signals.get("has_technical"))

    if should_extract_entities:
        brand = (semantic_signals.get("brands") or [None])[0]
        model = (semantic_signals.get("models") or [None])[0]

        for code in semantic_signals.get("codes") or []:
            if code.isdigit():
                try:
                    displacement = int(code)
                    break
                except Exception:
                    continue

        detected_families = detect_families_in_query(user_message)
        if detected_families:
            product_type = detected_families[0]

    confidence = 0.85 if brand or model or semantic_signals.get("technical_tokens") else 0.65
    confidence = _normalize_confidence(confidence, minimum=0.3)

    intent_compat = "busca_producto" if intent == "product_search" else intent
    intents_compat = ["busca_producto" if i == "product_search" else i for i in intents]

    payload = {
        "phase": "understanding",
        "raw_query": user_message,
        "intent": intent_compat,
        "intents": intents_compat,
        "intent_canonical": intent,
        "intents_canonical": intents,
        "brand": brand,
        "model": model,
        "displacement_cc": displacement,
        "usage_context": None,
        "product_type": product_type,
        "metadata": {
            "year_range": None,
            "additional_constraints": None,
            "ambiguity_level": _derive_ambiguity(confidence),
            "is_simple_greeting": semantic_signals.get("is_simple_greeting", False),
            "has_technical": semantic_signals.get("has_technical", False),
            "has_social": semantic_signals.get("has_social", False),
        },
        "confidence": confidence,
        "semantic_signals": semantic_signals,
        "implicit_cart_action": implicit_cart_action,
    }

    session_data = session or SessionMemory.get(phone)

    # Completar la moto usando contexto reciente si el usuario viene de otra consulta
    if phone and intent == "product_search" and semantic_signals.get("has_follow_up"):
        last_search = session_data.get("last_search") if session_data else get_last_search(phone)
        if last_search:
            last_meta = last_search.get("metadata") or {}
            last_products = last_search.get("products") or []
            mentioned_brands = {t for t in semantic_signals.get("tokens", []) if t in _BRANDS_NORMALIZED}
            mentioned_models = {t for t in semantic_signals.get("tokens", []) if t in _MODELS_NORMALIZED}
            if not brand:
                brand = (last_meta.get("brand") or last_meta.get("moto_brand") or "").strip() or None
                if not brand and last_products:
                    brand = (
                        last_products[0].get("brand")
                        or last_products[0].get("moto_brand")
                        or ""
                    ).strip() or None
            elif semantic_signals.get("has_follow_up") and (
                not mentioned_brands or brand.lower() not in mentioned_brands
            ):
                brand = (
                    (last_meta.get("brand") or last_meta.get("moto_brand") or "").strip()
                    or (
                        (last_products[0].get("brand") if last_products else None)
                        or (last_products[0].get("moto_brand") if last_products else None)
                        or ""
                    ).strip()
                )
                if brand == "":
                    brand = None
            if not model:
                model = (last_meta.get("model") or last_meta.get("moto_model") or "").strip() or None
                if not model and last_products:
                    model = (
                        last_products[0].get("model")
                        or last_products[0].get("moto_model")
                        or ""
                    ).strip() or None
            elif semantic_signals.get("has_follow_up") and (
                not mentioned_models or model.lower() not in mentioned_models
            ):
                model = (
                    (last_meta.get("model") or last_meta.get("moto_model") or "").strip()
                    or (
                        (last_products[0].get("model") if last_products else None)
                        or (last_products[0].get("moto_model") if last_products else None)
                        or ""
                    ).strip()
                )
                if model == "":
                    model = None

            if brand:
                payload["brand"] = brand
            if model:
                payload["model"] = model
            if brand or model:
                payload.setdefault("metadata", {})["contextual_moto_source"] = "last_search"

    if payload["confidence"] < 0.7:
        payload["metadata"]["ambiguity_level"] = "high"

    if not _validate_phase(payload, LLM1_UNDERSTANDING_SCHEMA, "FASE1"):
        payload["confidence"] = 0.3
        payload["metadata"]["ambiguity_level"] = "high"

    follow_up_block = {"detected": False}
    if phone:
        fu = detect_follow_up_intent(user_message, session_data or SessionMemory.get(phone))
        if fu:
            follow_up_block = fu
            if fu.get("inherit_context") and fu.get("context_fields"):
                last_ctx = (session_data or SessionMemory.get(phone) or {}).get("last_search", {})
                meta = last_ctx.get("metadata") or {}
                if "brand" in fu.get("context_fields", []) and not payload.get("brand"):
                    payload["brand"] = meta.get("brand") or meta.get("moto_brand")
                if "model" in fu.get("context_fields", []) and not payload.get("model"):
                    payload["model"] = meta.get("model") or meta.get("moto_model")
                if "family" in fu.get("context_fields", []) and not payload.get("product_type"):
                    payload["product_type"] = meta.get("family")
            if fu.get("inherited_confidence"):
                payload["confidence"] = max(payload.get("confidence", 0), fu.get("inherited_confidence", 0))

    payload["follow_up"] = follow_up_block

    return payload


def _canonical_intent(intent: str | None) -> str:
    if intent == "busca_producto":
        return "product_search"
    return intent or "product_search"


def _normalize_score_from_rank(rank: int, max_items: int) -> float:
    if max_items <= 1:
        return 1.0
    return max(0.0, 1.0 - (rank - 1) / max_items)


def _phase2_hybrid_search(understanding: dict, phone: str) -> dict:
    text_query_parts = [understanding.get("brand"), understanding.get("model"), understanding.get("product_type"), understanding.get("raw_query")]
    text_query = " ".join([p for p in text_query_parts if p]) or understanding.get("raw_query", "")

    detected_families = detect_families_in_query(understanding.get("raw_query", ""))
    target_family = detected_families[0] if detected_families else None

    search_results = hybrid_search(text_query, phone=phone, top_k=SEARCH_TOP_K)
    fused = search_results.get("fused") if isinstance(search_results, dict) else []
    if not fused:
        candidates = search_results.get("final_candidates") if isinstance(search_results, dict) else []
        fused = [(c, 0.0) for c in (candidates or [])]

    structured_results = []
    for idx, (product, score) in enumerate(fused[:SEARCH_TOP_K], 1):
        fam_name = product.get("family_name")
        has_structured = bool((product.get("moto_brand") or product.get("brand")) and (product.get("moto_model") or product.get("model")))
        needs_llm_family = needs_llm_compatibility(fam_name)

        structured_results.append(
            {
                "product_id": product.get("code") or str(idx),
                "name": product.get("name") or product.get("description", ""),
                "price_ars": product.get("price_ars"),
                "price_usd": product.get("price_usd"),
                "bm25_score": _normalize_score_from_rank(idx, SEARCH_TOP_K),
                "faiss_similarity": _normalize_score_from_rank(idx, SEARCH_TOP_K) if score else 0.5,
                "hybrid_rank": idx,
                "catalog_data": {
                    "marca_moto": product.get("moto_brand") or product.get("brand") or None,
                    "modelo_moto": product.get("moto_model") or product.get("model") or None,
                    "cilindrada": product.get("displacement") or product.get("cilindrada") or None,
                    "familia": fam_name,
                    "compatibilidad_declarada": product.get("compatibilidad_declarada") or product.get("compatibility"),
                },
                "family": fam_name,
                "has_structured_compatibility": has_structured,
                "needs_llm_compatibility": needs_llm_family,
            }
        )

        if not target_family and fam_name:
            target_family = fam_name

    payload = {
        "phase": "search",
        "query_params": {
            "text_query": text_query,
            "filters": {
                "brand": understanding.get("brand"),
                "model": understanding.get("model"),
                "displacement_cc": understanding.get("displacement_cc"),
            },
        },
        "results": structured_results,
        "total_results": len(structured_results),
        "target_family": target_family,
        "has_structured_compatibility": bool(target_family and not needs_llm_compatibility(target_family)),
        "needs_llm_compatibility": needs_llm_compatibility(target_family) if target_family else any(r.get("needs_llm_compatibility") for r in structured_results),
    }

    if not _validate_phase(payload, SEARCH_PHASE_SCHEMA, "FASE2"):
        payload["results"] = []
        payload["total_results"] = 0

    SessionMemory.update_last_search(
        phone,
        query=text_query,
        results=payload.get("results", []),
        metadata={
            "brand": understanding.get("brand"),
            "model": understanding.get("model"),
            "family": target_family,
        },
        confidence=understanding.get("confidence", 0),
    )

    return payload


def _phase3_compatibility_filter(understanding: dict, search_payload: dict) -> dict:
    candidates_after_filter = []
    hard_rejected = 0
    filter_policy = "layered"
    target_brand = understanding.get("brand")
    target_model = understanding.get("model")
    target_family = search_payload.get("target_family")
    target_displacement = understanding.get("displacement_cc")

    for cand in search_payload.get("results", [])[:SEARCH_TOP_K]:
        catalog_data = cand.get("catalog_data") or {}
        brand = catalog_data.get("marca_moto")
        model = catalog_data.get("modelo_moto")
        family = catalog_data.get("familia") or cand.get("family")
        has_structured = bool(brand and model)
        declared = (catalog_data.get("compatibilidad_declarada") or "").lower() or None
        needs_llm = needs_llm_compatibility(family) or not has_structured

        if needs_llm:
            filter_policy = "llm_assisted"

        status = "pending_reasoning"
        reason = "Faltan datos estructurados para decisión"
        confidence = 0.6
        proceed = True

        if has_structured and target_brand and target_model:
            brand_match = target_brand.lower() in (brand or "").lower()
            model_match = target_model.lower() in (model or "").lower()
            if declared == "incompatible" or (target_brand and target_model and not (brand_match and model_match)):
                status = "hard_incompatible"
                reason = "Catálogo contradice compatibilidad declarada"
                confidence = 0.95
                proceed = False
                hard_rejected += 1
            elif brand_match and model_match:
                status = "hard_compatible"
                reason = "Compatibilidad dura por coincidencia marca/modelo"
                confidence = 0.9
                proceed = needs_llm
        elif declared == "incompatible":
            status = "hard_incompatible"
            reason = "Compatibilidad explícita marcada como incompatible"
            confidence = 0.95
            proceed = False
            hard_rejected += 1

        if status == "pending_reasoning" and needs_llm:
            reason = "Familia sin compatibilidad estructurada, enviar a LLM2"
            confidence = 0.6

        if not ALLOW_MISSING_MOTO_DATA and status == "pending_reasoning" and not has_structured:
            status = "hard_incompatible"
            reason = "Datos incompletos y ALLOW_MISSING_MOTO_DATA=false"
            confidence = 0.5
            proceed = False
            hard_rejected += 1

        if status == "pending_reasoning" and target_displacement and catalog_data.get("cilindrada"):
            try:
                if int(target_displacement) == int(catalog_data.get("cilindrada")):
                    reason = "Coincidencia por cilindrada, verificar compatibilidad"
                    confidence = 0.72
            except Exception:
                pass

        candidates_after_filter.append(
            {
                "product_id": cand["product_id"],
                "status": status,
                "reason": reason,
                "confidence": confidence,
                "proceedes_to_llm2": proceed and status != "hard_incompatible",
            }
        )

    return {
        "phase": "compatibility_filter",
        "candidates_after_filter": candidates_after_filter,
        "hard_rejected_count": hard_rejected,
        "filter_policy": filter_policy,
    }


def _phase4_llm2_reasoning(understanding: dict, search_payload: dict, filter_payload: dict) -> dict:
    evaluated = []
    excluded = []

    results_lookup = {r["product_id"]: r for r in search_payload.get("results", [])}
    for cand in filter_payload.get("candidates_after_filter", []):
        product = results_lookup.get(cand["product_id"], {})
        catalog_data = product.get("catalog_data") or {}

        brand_match = catalog_data.get("marca_moto") and understanding.get("brand") and understanding.get("brand").lower() in catalog_data.get("marca_moto", "").lower()
        model_match = catalog_data.get("modelo_moto") and understanding.get("model") and understanding.get("model").lower() in catalog_data.get("modelo_moto", "").lower()
        displacement_match = False
        if understanding.get("displacement_cc") and catalog_data.get("cilindrada"):
            try:
                displacement_match = int(catalog_data.get("cilindrada")) == int(understanding.get("displacement_cc"))
            except Exception:
                displacement_match = False

        justification = "semantic_similarity"
        decision = "marginal"
        confidence_local = _normalize_confidence(cand.get("confidence", 0.55), minimum=0.0)
        technical_reasoning = "Sin datos declarados, se infiere por similitud semántica y familia."
        price_ars = product.get("price_ars")
        if price_ars is not None:
            try:
                price_ars = float(to_decimal_money(price_ars))
            except Exception:
                price_ars = None

        if cand.get("status") == "hard_compatible" or (brand_match and model_match):
            decision = "compatible"
            confidence_local = 0.9 if cand.get("status") == "hard_compatible" else 0.82
            justification = "catalog_match"
            technical_reasoning = "Catálogo declara marca/modelo compatibles."
        elif brand_match or model_match or displacement_match:
            decision = "compatible"
            confidence_local = 0.78
            justification = "specification_inference"
            technical_reasoning = "Coincidencia parcial en marca/modelo/cilindrada, falta confirmación completa."
        elif cand.get("status") == "hard_incompatible":
            decision = "incompatible"
            confidence_local = 0.9
            justification = "catalog_match"
            technical_reasoning = "Catálogo indica incompatibilidad o contradicción con la moto declarada."

        risk_level = "high" if confidence_local < 0.6 else "medium"
        if decision == "compatible" and confidence_local >= 0.85:
            risk_level = "low"

        record = {
            "product_id": cand["product_id"],
            "name": product.get("name"),
            "compatibility_decision": decision,
            "confidence_score": _normalize_confidence(confidence_local, minimum=0.0),
            "technical_reasoning": technical_reasoning,
            "justification_type": justification,
            "risk_level": risk_level,
            "price_ars": price_ars,
        }

        if STRICT_MODE and decision == "incompatible":
            excluded.append({"product_id": cand["product_id"], "exclusion_reason": "Compatibilidad marcada como incompatible"})
            continue

        if cand.get("proceedes_to_llm2") or cand.get("status") == "hard_compatible":
            evaluated.append(record)

    if evaluated:
        overall_confidence = sum([d.get("confidence_score", 0) for d in evaluated]) / len(evaluated)
    elif excluded:
        overall_confidence = 0.3
    else:
        overall_confidence = 0.4

    payload = {
        "phase": "llm2_reasoning",
        "candidates_evaluated": evaluated,
        "products_excluded": excluded,
        "llm2_confidence_overall": _normalize_confidence(overall_confidence, minimum=0.0),
        "needs_requery": overall_confidence < CONFIDENCE_THRESHOLD,
    }

    if not _validate_phase(payload, LLM2_SCHEMA, "FASE4"):
        payload["llm2_confidence_overall"] = 0.3
        payload["needs_requery"] = True

    return payload


def search_payload_family_fallback(understanding: dict) -> str | None:
    detected = detect_families_in_query(understanding.get("raw_query", ""))
    return detected[0] if detected else None


def _phase5_requery(understanding: dict, attempt: int) -> tuple[str, str]:
    strategy = "original_query"
    brand = understanding.get("brand") or ""
    model = understanding.get("model") or ""
    displacement = str(understanding.get("displacement_cc") or "").strip()
    family = understanding.get("product_type") or search_payload_family_fallback(understanding)

    def _expand_model_variants(base_model: str) -> list[str]:
        if not base_model:
            return []
        norm = normalize_search_query(base_model)
        variants = {norm}
        if any(ch.isdigit() for ch in norm):
            variants.add(norm.replace(" ", ""))
            variants.add(" ".join(re.findall(r"[a-zA-Z]+|\d+", norm)))
        return [v for v in variants if v]

    if attempt == 1:
        strategy = "expand_moto_variants"
        variants = _expand_model_variants(model)
        core = variants[0] if variants else model
        new_query = " ".join([t for t in [brand, core, displacement, family] if t]).strip()
    elif attempt == 2:
        strategy = "minimal_core_query"
        main_token = family or (model.split()[0] if model else "") or (normalize_search_query(understanding.get("raw_query", "")).split()[:1] or [""])[0]
        new_query = " ".join([t for t in [brand, model, main_token] if t]).strip()
    else:
        strategy = "semantic_expansion"
        semantic_terms = [brand, model, displacement, family, understanding.get("usage_context") or "", understanding.get("raw_query")]
        new_query = " ".join([t for t in semantic_terms if t]).strip()

    new_query = new_query or understanding.get("raw_query", "")
    return strategy, new_query


def _phase6_fallback(reason: str) -> dict:
    return {
        "phase": "fallback",
        "status": reason,
        "fallback_action": "return_top_N_with_disclaimer",
        "results": [],
        "fallback_message": (
            "Necesito más datos para asegurar compatibilidad. Si querés ver más productos o tenés dudas, avisame."
        ),
    }


def _phase7_llm3_response(understanding: dict, reasoning_payload: dict, fallback_payload: dict | None = None) -> dict:
    evaluations = reasoning_payload.get("candidates_evaluated") or []
    message_type = "confident_match" if reasoning_payload.get("llm2_confidence_overall", 0) >= CONFIDENCE_THRESHOLD else "partial_match"
    recommendations = []

    badge_map = {
        "compatible": "✅ Coincide exactamente",
        "marginal": "⚠️ Probablemente compatible",
        "incompatible": "❓ Verificar con vendedor",
    }

    for dec in evaluations:
        if dec.get("compatibility_decision") == "incompatible" and STRICT_MODE:
            continue
        badge = badge_map.get(dec.get("compatibility_decision"), "❓ Verificar con vendedor")
        price_value = dec.get("price_ars")
        price_decimal = to_decimal_money(price_value) if price_value is not None else None
        price_formatted = format_price(price_decimal) if price_decimal is not None else None
        recommendations.append(
            {
                "product_id": dec.get("product_id"),
                "product_name": dec.get("name", ""),
                "confidence_badge": badge,
                "price_ars": float(price_decimal) if price_decimal is not None else None,
                "price_formatted": price_formatted,
            }
        )

    whatsapp_response = ""
    if recommendations:
        lines = ["Te dejo opciones compatibles:"]
        for rec in recommendations[:5]:
            price_text = rec.get("price_formatted") or "Precio a confirmar"
            lines.append(f"- {rec['product_name']} ({rec['confidence_badge']}) | {price_text}")
        whatsapp_response = "\n".join(lines)
    elif fallback_payload:
        fallback_msg = fallback_payload.get("fallback_message") or "Necesito más datos para asegurar compatibilidad."
        whatsapp_response = fallback_msg.strip()
    else:
        whatsapp_response = "Necesito confirmar la moto para asegurarte compatibilidad."

    return {
        "phase": "response_generation",
        "message_type": message_type if recommendations else "clarification_needed",
        "whatsapp_response": whatsapp_response[:1000],
        "product_recommendations": recommendations,
        "fallback_message": fallback_payload.get("fallback_message") if fallback_payload else None,
    }

def orquestar_fran_v316(mensaje_usuario: str, phone: str) -> str:
    """Orquestador JSON-first con fases controladas y re-query automático."""

    start_time = time.time()
    user_message = sanitize_input(mensaje_usuario or "", max_length=1500)

    if not rate_limit_check(phone):
        reply = "Demasiados mensajes, esperá un minuto."
        save_message(phone, reply, "assistant")
        return reply

    save_message(phone, user_message, "user")

    # --------------------------------------------
    # FASE 1: LLM1 Understanding (JSON)
    # --------------------------------------------
    session_ctx = SessionMemory.get(phone)
    understanding = _phase1_llm1_understanding(user_message, phone=phone, session=session_ctx)
    logger.info(f"[v3.16][FASE1] {json.dumps(understanding, ensure_ascii=False)}")

    # FIX: Bypass temprano SOLO para saludos puros (sin componente técnico)
    # Esto permite multi-intent: "hola, quiero baterías" ejecutará búsqueda
    metadata = understanding.get("metadata", {})
    is_pure_greeting = (
        metadata.get("is_simple_greeting", False)
        and not metadata.get("has_technical", False)
    )

    if is_pure_greeting:
        reply = "¡Hola! ¿En qué te puedo ayudar?"
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "social", 0)
        logger.info(f"[v3.16] Saludo puro detectado, bypass de búsqueda | Duration: {time.time()-start_time:.2f}s")
        return reply

    reasoning_payload = None
    fallback_payload = None
    requery_attempt = 0

    canonical_intent = _canonical_intent(understanding.get("intent_canonical") or understanding.get("intent"))
    understanding["intent_canonical"] = canonical_intent

    if canonical_intent == "social":
        reply = build_social_reply(phone, user_message, understanding.get("semantic_signals"))
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "social", 0)
        log_performance(phone, "social", time.time() - start_time, 0)
        update_sales_phase_from_intent(phone, "social")
        logger.info("[v3.16] Ruta social detectada en fase 1, se responde sin búsqueda")
        return reply

    if canonical_intent == "cart_action":
        implicit_cart_action = understanding.get("implicit_cart_action") or detect_implicit_cart_action(user_message, phone)
        reply = None

        if implicit_cart_action and implicit_cart_action.get("action") == "add_each_quantity":
            pending = {
                "action_data": {
                    "qty": max(1, int(implicit_cart_action.get("quantity") or 1)),
                    "products": implicit_cart_action.get("products") or [],
                    "cart_hash": compute_cart_hash_from_items(cart_get(phone)),
                },
                "created_at": datetime.now().isoformat(),
            }
            reply = apply_add_each_quantity_pending(phone, pending)
        else:
            reply = handle_cart_action(phone, user_message)

        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "cart_action", 0)
        log_performance(phone, "cart_action", time.time() - start_time, 0)
        update_sales_phase_from_intent(phone, "cart_action")
        logger.info("[v3.16] Ruta de carrito detectada, se ejecuta acción sin búsqueda")
        return reply

    while requery_attempt <= MAX_REQUERY_ATTEMPTS:
        session_ctx = SessionMemory.get(phone)
        elapsed_ms = (time.time() - start_time) * 1000
        if elapsed_ms > RESPONSE_TIMEOUT_MS:
            logger.warning("[v3.16] Timeout global, activando fallback")
            fallback_payload = _phase6_fallback("timeout")
            break

        # --------------------------------------------
        # FASE 2: Búsqueda híbrida (BM25 + FAISS) o cacheada
        # --------------------------------------------
        follow_up_info = understanding.get("follow_up") or {}
        cached_search = None
        if (
            follow_up_info.get("use_cached_results")
            and session_ctx
            and session_ctx.get("last_search")
            and requery_attempt == 0
        ):
            cached_search = _build_cached_search_payload(understanding, session_ctx.get("last_search"))

        search_payload = cached_search or _phase2_hybrid_search(understanding, phone)
        logger.info(f"[v3.16][FASE2] {json.dumps(search_payload, ensure_ascii=False)}")

        # --------------------------------------------
        # FASE 3: Filtro de compatibilidad inteligente
        # --------------------------------------------
        compatibility_payload = _phase3_compatibility_filter(understanding, search_payload)
        logger.info(f"[v3.16][FASE3] {json.dumps(compatibility_payload, ensure_ascii=False)}")

        # --------------------------------------------
        # FASE 4: LLM2 Reasoning (heurístico si LLM off)
        # --------------------------------------------
        if LLM_REASONING_ENABLED:
            reasoning_payload = _phase4_llm2_reasoning(understanding, search_payload, compatibility_payload)
        else:
            reasoning_payload = {
                "phase": "llm2_reasoning",
                "candidates_evaluated": [],
                "products_excluded": [],
                "llm2_confidence_overall": 0.5,
                "needs_requery": False,
            }
        logger.info(f"[v3.16][FASE4] {json.dumps(reasoning_payload, ensure_ascii=False)}")

        low_llm1_conf = understanding.get("confidence", 0) < CONFIDENCE_THRESHOLD
        trigger_requery = reasoning_payload.get("needs_requery") or low_llm1_conf

        if not trigger_requery:
            break

        requery_attempt += 1
        if requery_attempt > MAX_REQUERY_ATTEMPTS:
            break

        strategy, new_query = _phase5_requery(understanding, requery_attempt)
        logger.info(f"[v3.16][FASE5] intento={requery_attempt} estrategia={strategy} nueva_query='{new_query}'")
        understanding["raw_query"] = new_query
        understanding["metadata"]["ambiguity_level"] = "medium"

    # --------------------------------------------
    # FASE 6: Fallback si no hay confianza
    # --------------------------------------------
    if (not reasoning_payload or reasoning_payload.get("llm2_confidence_overall", 0) < CONFIDENCE_THRESHOLD) and not fallback_payload:
        fallback_payload = _phase6_fallback("low_confidence_results")
        fallback_payload["fallback_message"] = (
            "Tengo algunas opciones pero necesito confirmar la moto. Contame cuál preferís o si querés que te muestre más productos."
        )

    # --------------------------------------------
    # FASE 7: LLM3 Response Generation
    # --------------------------------------------
    response_payload = _phase7_llm3_response(understanding, reasoning_payload or {}, fallback_payload)
    logger.info(f"[v3.16][FASE7] {json.dumps(response_payload, ensure_ascii=False)}")

    reply = response_payload.get("whatsapp_response") or "Necesito un poco más de información para ayudarte mejor."
    save_message(phone, reply, "assistant")
    intent_for_logs = _canonical_intent(understanding.get("intent_canonical") or understanding.get("intent"))
    log_interaction(phone, user_message, intent_for_logs, len(response_payload.get("product_recommendations", [])))
    log_performance(phone, intent_for_logs, time.time() - start_time, len(reasoning_payload.get("candidates_evaluated", [])) if reasoning_payload else 0)
    update_sales_phase_from_intent(phone, intent_for_logs)

    logger.info(f"[v3.16 - DONE] Hybrid JSON pipeline complete | Duration: {time.time()-start_time:.2f}s")

    return reply

# =========================================================
# ORQUESTADOR PRINCIPAL – VERSIÓN 3.17
# =========================================================
def orquestar_fran_v317(mensaje_usuario: str, phone: str) -> str:
    start_time = time.time()
    user_message = sanitize_input(mensaje_usuario or "", max_length=1500)

    if not rate_limit_check(phone):
        reply = "Demasiados mensajes, esperá un minuto."
        save_message(phone, reply, "assistant")
        return reply

    save_message(phone, user_message, "user")

    catalog, centroid, schema = get_v317_resources()
    if not catalog or schema is None:
        reply = "No pude acceder al catálogo en este momento. Probá de nuevo en unos instantes."
        save_message(phone, reply, "assistant")
        return reply

    try:
        output = orquestar_v317(user_message, catalog, centroid, schema)
        final_response = output.get("final_response") or {}
        reply = final_response.get("message") or "No pude procesar tu pedido, ¿podés reformularlo?"

        trace = output.get("trace") or []
        classifier_phase = next(
            (step for step in trace if isinstance(step, dict) and step.get("phase") == "classifier"),
            {},
        )
        intent = classifier_phase.get("intent") or "otros"
        search_phase = next(
            (step for step in trace if isinstance(step, dict) and step.get("phase") == "search"),
            {},
        )
        products_count = len(search_phase.get("results") or [])

        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, intent, products_count)
        log_performance(phone, intent, time.time() - start_time, products_count)
        update_sales_phase_from_intent(phone, intent)

        return reply
    except Exception as exc:  # pragma: no cover - fallback vital en producción
        logger.exception("[v3.17] Error crítico, activando fallback a v3.16: %s", exc)
        try:
            return orquestar_fran_v316(user_message, phone)
        except Exception as fallback_exc:  # pragma: no cover - double safety
            logger.exception("[v3.16] Fallback también falló: %s", fallback_exc)
            reply = "Uy, tuve un problema técnico. Probá de nuevo en un ratito."
            save_message(phone, reply, "assistant")
            return reply

# =========================================================
# ORQUESTADOR PRINCIPAL – VERSIÓN 3.14
# =========================================================
def orquestar_fran(mensaje_usuario, phone):
    """
    Orquestador unificado de Fran.

    Gestiona rate limiting, búsqueda, memoria y el doble
    paso de LLM (razonamiento interno + respuesta conversacional).
    """
    start_time = time.time()
    user_message = sanitize_input(mensaje_usuario or "", max_length=1500)
    save_message(phone, user_message, "user")

    if not rate_limit_check(phone):
        reply = "Demasiados mensajes, esperá un minuto."
        save_message(phone, reply, "assistant")
        return reply

    last_search_data = get_last_search(phone) or {}
    last_search_query = (last_search_data.get("query") or "").strip()

    execution_context = {
        "intent_detected": "unified",
        "intent_details": {},
        "search_query": user_message,
        "search_executed": False,
        "products_found": 0,
        "products_shown_to_llm": 0,
        "filters_applied": [],
        "quality_assessment": None,
        "will_send_chunks": False,
        "chunk_info": None,
        "warnings": [],
    }

    products = []
    query_for_search = user_message
    corrections = []

    if query_for_search and len(query_for_search.split()) < 4 and last_search_query:
        query_for_search = f"{last_search_query} {query_for_search}".strip()
        execution_context["warnings"].append("query_refined_with_last_search")

    query_for_search, corrections = autocorrect_keywords(query_for_search)
    if corrections:
        execution_context["warnings"].append(f"Autocorrect: {', '.join(corrections)}")

    execution_context["search_query"] = query_for_search

    semantic_results = hybrid_search(query_for_search, phone=phone, top_k=MAX_SEARCH_RESULTS)

    if isinstance(semantic_results, dict):
        if semantic_results.get("error") == "too_many_combinations":
            reply = semantic_results.get("message") or "Hay demasiadas combinaciones, pasame una sola moto o categoría."
            save_message(phone, reply, "assistant")
            log_interaction(phone, user_message, "too_many_combinations", 0)
            log_performance(phone, "too_many_combinations", time.time()-start_time, 0)
            return reply
        execution_context["search_executed"] = True
        total_found = sum(len(v) for v in (semantic_results.get("results") or {}).values())
        execution_context["products_found"] = total_found or len(semantic_results.get("final_candidates") or [])
        reply = format_multi_search_response(semantic_results)
        if reply:
            _persist_search_snapshot(
                phone,
                semantic_results.get("final_candidates") or [],
                query_for_search,
            )
            save_message(phone, reply, "assistant")
            log_interaction(phone, user_message, "multi_search", total_found)
            log_performance(phone, "multi_search", time.time()-start_time, total_found)
            update_sales_phase_from_intent(phone, "product_search")
            return reply
        semantic_candidates = list(semantic_results.get("final_candidates") or [])
    else:
        semantic_candidates = [p for p, _ in semantic_results]

    products = semantic_candidates

    execution_context["search_executed"] = True
    execution_context["products_found"] = len(products)

    if products:
        original_len = len(products)
        filtered_products = filter_by_relevance(query_for_search, products, min_score=RELEVANCE_MIN_SCORE)
        if filtered_products:
            products = filtered_products
            execution_context["filters_applied"].append(f"relevance: {len(filtered_products)}/{original_len} passed")

    products = _order_products_for_llm(products)

    quality_assessment = assess_context_quality(query_for_search, products)
    execution_context["quality_assessment"] = quality_assessment
    log_quality_metrics(
        phone,
        query_for_search,
        quality_assessment.get("avg_score", 0),
        quality_assessment.get("max_score", 0),
        quality_assessment.get("relevant_count", 0),
    )

    if not quality_assessment["sufficient"]:
        if quality_assessment["action"] == "ask_clarification":
            reply = quality_assessment["message"]
        elif quality_assessment["action"] == "suggest_alternatives":
            top_products = quality_assessment.get("top_products", [])
            suggestions = "\n".join(
                [
                    f"- {p.get('name', '')} ({p.get('code', '')}) - {format_price(p.get('price_ars', 0))}"
                    for p in top_products
                ]
            )
            reply = (
                "No encontré coincidencia perfecta, pero tengo estas opciones que se acercan:\n\n"
                f"{suggestions}\n\n"
                "O dame un poco más de detalle (marca/modelo/año) y afinamos la búsqueda."
            )
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, f"low_quality_{quality_assessment['reason']}", 0)
        log_performance(phone, "low_quality", time.time()-start_time, 0)
        return reply

    if products:
        save_last_search(phone, [
            {
                "code": p["code"],
                "name": p["name"],
                "price_ars": p["price_ars"],
                "price_usd": p["price_usd"],
                "qty": 1
            }
            for p in products[:150]
        ], query_for_search)

    top_products, product_chunks = _slice_products_for_llm(products)

    execution_context["products_shown_to_llm"] = min(len(products), MAX_PRODUCTS_FOR_LLM)

    if len(products) > MAX_PRODUCTS_FOR_LLM:
        execution_context["will_send_chunks"] = True
        num_chunks = len(product_chunks)
        execution_context["chunk_info"] = {
            "total_chunks": num_chunks,
            "products_per_chunk": PRODUCTS_PER_CHUNK,
            "total_products": len(products)
        }

    result = generate_smart_ai_reply_v2(
        phone,
        user_message,
        top_products,
        execution_context,
        system_prompt=CITATION_ENFORCED_PROMPT
    )

    reply = result.get("reply") or "Uy, tuve un problema. ¿Me repetís?"
    plan = result.get("plan") or {}
    real_intent = plan.get("real_intent", "unknown")
    execution_context["intent_detected"] = real_intent

    if products:
        allowed_products = top_products
        reply = validate_and_fix_response(reply, allowed_products, phone, execution_context)

    if execution_context["will_send_chunks"] and products:
        for idx, chunk in enumerate(product_chunks, 1):
            chunk_text = f"━━━ Bloque {idx}/{len(product_chunks)} ({len(chunk)} productos) ━━━\n"
            chunk_text += format_search_results(chunk)
            if idx > 1:
                time.sleep(0.5)
            send_long_message(phone, chunk_text)

    save_message(phone, reply, "assistant")
    log_interaction(phone, user_message, real_intent, len(products))
    log_performance(phone, real_intent, time.time()-start_time, len(products))
    update_sales_phase_from_intent(phone, real_intent)
    return reply


def run_agent(phone, user_message):
    """Compatibilidad hacia atrás con el nombre anterior."""
    return orquestar_fran(user_message, phone)

# ------------------------------------------------------------------
# MULTI-MENSAJE
# ------------------------------------------------------------------
def send_long_message(phone, text, chunk_size=1600):
    if not twilio_rest_client or not phone or not text:
        return False

    try:
        parts = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
        logger.info(f"Enviando {len(parts)} chunks a {phone}")

        for idx, part in enumerate(parts):
            try:
                message = twilio_rest_client.messages.create(
                    from_=TWILIO_WHATSAPP_FROM,
                    body=f"({idx + 1}/{len(parts)})\n{part}" if len(parts) > 1 else part,
                    to=phone
                )
                logger.info(f"Chunk {idx + 1}/{len(parts)} enviado: {message.sid}")

                if idx < len(parts) - 1:
                    time.sleep(1.1)

            except Exception as e:
                logger.error(f"Error enviando chunk {idx + 1}: {e}")
                raise

        logger.info(f"Mensaje largo enviado exitosamente: {len(parts)} partes")
        return True

    except Exception as e:
        logger.error(f"Error critico en send_long_message: {e}", exc_info=True)
        return False

# ------------------------------------------------------------------
# WEBHOOK WHATSAPP
# ------------------------------------------------------------------
@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    try:
        logger.info("=" * 50)
        logger.info("WEBHOOK RECIBIDO")
        logger.info(f"From: {request.form.get('From', 'N/A')}")
        logger.info(f"Body: {request.form.get('Body', 'N/A')}")
        logger.info(f"MessageSid: {request.form.get('MessageSid', 'N/A')}")
        logger.info("=" * 50)

        from_number = request.form.get("From", "")
        message_body = sanitize_input(request.form.get("Body", "").strip(), max_length=1500)

        if not from_number or not message_body:
            logger.warning("Mensaje sin From o Body")
            return Response("<Response></Response>", mimetype="text/xml")

        logger.info(f"Mensaje sanitizado: {message_body}")

        if is_duplicate_message(from_number, message_body):
            logger.info(f"Mensaje duplicado ignorado de {from_number}")
            return Response("<Response></Response>", mimetype="text/xml")

        logger.info(f"Procesando mensaje de {from_number}: {message_body}")

        if not rate_limit_check(from_number):
            resp = MessagingResponse()
            resp.message("Demasiados mensajes, esperá un minuto.")
            return Response(str(resp), mimetype="text/xml")

        is_bulk, count = is_bulk_list_request(message_body)
        if is_bulk and count > INSTANT_THRESHOLD:
            job_id = create_bulk_job(from_number, message_body, count)
            if not job_id:
                resp = MessagingResponse()
                resp.message("Tuve un problema procesando la lista, probá de nuevo")
                return Response(str(resp), mimetype="text/xml")
            resp = MessagingResponse()
            resp.message(f"Perfecto, es una lista larga ({count} items). La proceso y te aviso con el total.")
            return Response(str(resp), mimetype="text/xml")

        # A/B/C routing entre versiones 3.14, 3.15 y 3.16
        version = get_orchestrator_version(from_number)
        logger.info(f"Using orchestrator version: {version} for {from_number}")

        if version == "3.17":
            reply = orquestar_fran_v317(message_body, from_number)
        elif version == "3.16":
            reply = orquestar_fran_v316(message_body, from_number)
        elif version == "3.15":
            reply = orquestar_fran_v315(message_body, from_number)
        else:  # 3.14
            reply = orquestar_fran(message_body, from_number)

        logger.info(f"Respuesta generada: {len(reply)} caracteres")
        logger.info(f"Preview: {reply[:100]}...")

        if len(reply) <= WHATSAPP_MSG_LIMIT:
            logger.info("Mensaje corto, usando TwiML")
            resp = MessagingResponse()
            resp.message(reply)
            return Response(str(resp), mimetype="text/xml")
        else:
            logger.info("Mensaje largo, enviando por Twilio REST")
            send_long_message(from_number, reply)
            return Response("<Response></Response>", mimetype="text/xml")

    except Exception as e:
        logger.exception(f"Error crítico en webhook: {e}")
        try:
            resp = MessagingResponse()
            resp.message("Uy, tuve un problema técnico. Probá de nuevo en un ratito.")
            return Response(str(resp), mimetype="text/xml")
        except:
            return Response("<Response></Response>", mimetype="text/xml")

# ------------------------------------------------------------------
# HEALTH / API
# ------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    catalog, index, bm25_index, _ = get_catalog_and_index()
    return jsonify({
        "status": "ok",
        "version": "3.17",
        "catalog_size": len(catalog) if catalog else 0,
        "architecture": "hybrid_router_v317",
        "orchestrators": {
            "v3.17": "dynamic router + hybrid search v3.17",
            "v3.16": "hybrid (templates + reasoning + re-query)",
            "v3.15": "structured templates",
            "v3.14": "dual LLM reasoning"
        },
        "features": [
            "context_quality_check",
            "relevance_scoring",
            "forced_code_citation",
            "post_validation",
            "faiss_global_index",
            "bm25_index",
            "families_index",
            "progressive_family_fallback",
            "intent_detector_v2",
            "implicit_cart_v2",
            "pending_actions_execution",
            "checkout_intents",
            "sales_phase_tracking"
        ]
    }), 200

# ------------------------------------------------------------------
# ✅ Cargar índice al arrancar
# ------------------------------------------------------------------
catalog, index, bm25_index, bm25_corpus = get_catalog_and_index()
if catalog and index:
    logger.info("✅ Índices inicializados: FAISS=%s BM25=%s", len(catalog), "ok" if bm25_index else "error")
else:
    logger.error("❌ No se pudieron inicializar los índices al arranque")

# ------------------------------------------------------------------
# ✅ Inicializar DB
# ------------------------------------------------------------------
init_db()

# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("=" * 60)
    logger.info("🚀 Iniciando Fran 3.14 - Motor Híbrido Familias + FAISS + Intents/Contexto 2.0 + doble LLM")
    logger.info("=" * 60)
    logger.info(f"Puerto: {port}")
    logger.info(f"Catálogo: {len(catalog) if catalog else 0} productos")
    logger.info(f"Tipo de cambio inicial: {get_exchange_rate()}")
    logger.info(f"Relevance min score: {RELEVANCE_MIN_SCORE}")
    logger.info(f"Quality thresholds: HIGH={QUALITY_HIGH_THRESHOLD}, MED={QUALITY_MEDIUM_THRESHOLD}")
    logger.info("=" * 60)
    logger.info("Características nuevas en 3.14:")
    logger.info("  ✅ Doble llamada LLM (plan interno + respuesta final)")
    logger.info("  ✅ Orquestador unificado orquestar_fran")
    logger.info("  ✅ Validación de búsqueda con reintento sugerido por LLM")
    logger.info("  ✅ INTENTOS 2.0 (prompt ampliado, más sinónimos argentinos)")
    logger.info("  ✅ CONTEXTO 2.0 (pending_actions con cart_hash y ejecución real)")
    logger.info("  ✅ Mensajes más humanos (no dice 'no encontré' si hay alternativas)")
    logger.info("  ✅ Pedidos implícitos mejorados ('2 de todos', 'todos x5', etc.)")
    logger.info("  ✅ Uso efectivo de pending_actions + snapshot de carrito")
    logger.info("  ✅ Context Quality Check pre-LLM")
    logger.info("  ✅ Relevance Scoring general (sin keywords hardcoded)")
    logger.info("  ✅ Forced Code Citation en prompts")
    logger.info("  ✅ Post-validation de códigos y nombres")
    logger.info("  ✅ FAISS + BM25 con búsqueda híbrida (hybrid_search)")
    logger.info("  ✅ Índice de familias (FAMILIES_INDEX)")
    logger.info("  ✅ Fallback progresivo por familia + categoría")
    logger.info("  ✅ k dinámico según haya/no haya familia en la query")
    logger.info("  ✅ Checkout intents + sales phase tracker")
    logger.info("=" * 60)

    app.run(host="0.0.0.0", port=port, debug=False)
