# =========================================================
# Fran 3.15 – Bot Mayorista Inteligente
# =========================================================
# Basado en Fran 3.12/3.13 (estructura completa que pasó tests),
# con mejoras:
# - Doble llamada al LLM: razonamiento interno + respuesta final
# - Orquestador único (orquestar_fran) para todo el flujo de conversación
# - Plan interno estructurado y validación de búsqueda con reintento guiado
# - Se mantiene toda la infraestructura previa (FAISS+BM25, familias,
#   pending actions, fases, post-validaciones, chunks, etc.)
# =========================================================

import os, json, csv, io, sqlite3, logging, re, unicodedata, time, threading, pickle, random, hashlib
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
from rank_bm25 import BM25Okapi
from dotenv import load_dotenv
from cachetools import LRUCache
from jsonschema import Draft7Validator

from fran.clients import HttpClient, LLMClient
from fran.observability import CircuitBreaker, METRICS_REGISTRY, track_step

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

FRAN_DEBUG = os.environ.get("FRAN_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}
LAST_SEARCH_DEBUG: dict = {}
LAST_FILTER_CATALOG_DEBUG: dict = {}
LAST_RELEVANCE_DEBUG: dict = {}


def debug_log(message: str):
    if FRAN_DEBUG:
        logger.info(message)

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------
OPENAI_API_KEY = (os.environ.get("OPENAI_API_KEY") or "").strip()
if not OPENAI_API_KEY:
    logger.error("Falta OPENAI_API_KEY – el LLM está deshabilitado")

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
WHATSAPP_MSG_LIMIT = int(os.environ.get("WHATSAPP_MSG_LIMIT", "3500"))
PRODUCTS_PER_CHUNK = int(os.environ.get("PRODUCTS_PER_CHUNK", "30"))
TWILIO_CHUNK_LIMIT = 1600

# Nuevos parámetros de calidad (ajustados)
RELEVANCE_MIN_SCORE = float(os.environ.get("RELEVANCE_MIN_SCORE", "40.0"))
QUALITY_HIGH_THRESHOLD = float(os.environ.get("QUALITY_HIGH_THRESHOLD", "55.0"))
QUALITY_MEDIUM_THRESHOLD = float(os.environ.get("QUALITY_MEDIUM_THRESHOLD", "45.0"))

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
    Sos Fran 3.16, asistente mayorista argentino 100% LLM-first.

    Detectá TODAS las intenciones presentes en el mensaje (pueden venir varias
    en un solo texto) sin reglas hardcodeadas y devolvé spans exactos del texto
    original.

    Reglas críticas:
    - Siempre devolvé intents como array de objetos.
    - type SOLO puede ser: product_search, compare, cart_action, checkout, clarification, general_chat.
    - span es obligatorio y debe copiar literalmente el fragmento original.
    - confidence es una probabilidad 0.0–1.0 basada en comprensión semántica.
    - data incluye solo datos estructurados útiles (query, product, brand, model,
      category, action, quantity, notes) sin inventar valores.
    - No uses greeting, small_talk ni conversation como type porque no existen en el schema.
    - Respondé únicamente JSON válido según el schema.
    """,
    "output_schema": {
        "type": "object",
        "required": ["intents"],
        "properties": {
            "intents": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "required": ["type", "span", "confidence", "data"],
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": [
                                "product_search",
                                "compare",
                                "cart_action",
                                "checkout",
                                "clarification",
                                "general_chat",
                            ],
                        },
                        "span": {"type": "string"},
                        "confidence": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "data": {
                            "type": "object",
                            "properties": {
                                "query": {"type": "string"},
                                "product": {"type": "string"},
                                "brand": {"type": "string"},
                                "model": {"type": "string"},
                                "category": {"type": "string"},
                                "action": {"type": "string"},
                                "quantity": {"type": "number"},
                                "notes": {"type": "string"},
                            },
                            "additionalProperties": True,
                        },
                    },
                },
            }
        },
    },
}

PRODUCT_SELECTION_SCHEMA = {
    "task": "select_products",
    "description": """
    Elegí los productos finales directamente desde final_candidates.

    CONTEXTO Y REGLAS MAYORISTAS (FRAN 3.15):
    - Usa el mensaje original del cliente y la intención detectada para decidir.
    - Trabajá SOLO con los final_candidates provistos (NO inventes ni busques otros). Cada uno puede traer metadata de bloque.
    - Si hay más de 50 productos, vendrán marcados con block_number (bloques de 15–20). Podés dosificar la entrega priorizando los primeros bloques.
    - Si el pedido es cadena + piñón + corona, agrupá las piezas compatibles en un kit.
    - Si el pedido es múltiple (varias piezas o motos), devolvé cada grupo por separado.
    - Si hay demasiados resultados, dosificá: elegí un subconjunto representativo y marcá en action si hay más para mostrar.
    - Si falta información clave, pedí aclaración concreta (pero NO inventes productos nuevos).
    - Siempre respondé en tono mayorista y nunca inventes códigos.
    """,
    "output_schema": {
        "type": "object",
        "required": ["selected_products", "analysis", "action"],
        "properties": {
            "selected_products": {
                "type": "array",
                "maxItems": 150,
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

    SI EL INTENT ES "general_chat":
    - No generes listados ni pidas marca/modelo/año.
    - Respondé en tono humano, cálido, vendedor mayorista real.
    - La respuesta debe ser breve (1–3 líneas).
    - Podés mantener continuidad (“¡Me alegra que te haya servido!”, “¿Todo tranqui por ahí?”).
    - No menciones sistemas, búsquedas, catálogos ni procesos internos.
    - products_cited debe ser siempre [].
    - La respuesta debe ser 100% independiente del catálogo.

    REGLAS PARA RESPUESTA EN BÚSQUEDAS DE PRODUCTO:
    1. Trabajá SOLO con selected_products provistos por el paso de selección (ya vienen desde final_candidates). No inventes ni filtres en Python.
    2. Si selected_products está vacío o faltan datos clave, pedí una aclaración concreta (marca/modelo/año o qué pieza quiere).
    3. Si hay kits de transmisión (cadena + piñón + corona), agrupá en bloques separados por pieza y ofrecé armar kit.
    4. Si hay muchos resultados, dosificá: usá bloques numerados si llegan como metadata (block_number) y avisá cuántos bloques totales hay.
    5. Estructura de productos: código TERCOM + descripción limpia. No inventes precios ni códigos.
    6. products_cited debe listar solo los códigos mencionados.
    7. Mensaje final: en el último bloque agregá "Decime si querés que compare opciones o te arme el carrito."
    8. Estilo mayorista, directo y sin rodeos.
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
class _UnavailableLLMClient:
    def completion(self, *_, **__):  # noqa: D401
        """Stub que informa la falta de API key."""
        raise RuntimeError("OPENAI_API_KEY no configurada")


client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
llm_client = (
    LLMClient(
        client,
        logger=logger,
        breaker=CircuitBreaker(failure_threshold=2, recovery_time=90),
    )
    if client
    else _UnavailableLLMClient()
)


def is_llm_available() -> bool:
    return bool(OPENAI_API_KEY)
cart_lock = Lock()
exchange_lock = Lock()
bulk_queue = Queue()

openai_sem = Semaphore(3)

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

_embeddings_cache_lock = Lock()

# Cache de fuzzy matching para post-validation
_fuzzy_match_cache = LRUCache(maxsize=20000)

# Índice de familias (global)
FAMILIES_INDEX = []

TEMPLATE_FALLBACKS = {
    "query_understanding": {
        "intents": [
            {
                "type": "social",
                "span": "",
                "confidence": 0.5,
                "data": {},
            }
        ]
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
    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(data), key=lambda e: list(e.path))
    if errors:
        messages = "; ".join(
            [f"{'.'.join([str(p) for p in err.path])}: {err.message}" for err in errors]
        )
        raise ValueError(f"Schema validation failed: {messages}")
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

    last_error = None
    for attempt in range(3):
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
            if template_name == "query_understanding" and not (result.get("intents") or []):
                raise ValueError("Schema validation failed: intents array vacío")
            log_template_execution(template_name, context, result, time.time() - start_time)
            return result

        except Exception as e:
            last_error = e
            logger.warning(f"Template completion attempt {attempt+1} failed: {e}")
            time.sleep(0.2)

    logger.error(f"Template completion failed after retries: {last_error}", exc_info=True)
    fallback = TEMPLATE_FALLBACKS.get(template_name, template.get("fallback", {}))
    try:
        log_template_execution(template_name, context, fallback, time.time() - start_time)
    except Exception:
        pass
    return fallback


def llm_text_response(system_prompt: str, user_prompt: str, *, temperature: float = 0.2, model: str = MODEL_OUTPUT) -> str:
    try:
        with openai_sem:
            resp = llm_client.completion(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
            )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"LLM text response failed: {e}")
        return ""


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
        normalized = strip_accents(text or "")
        return re.findall(r"\w+", normalized)
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
    return strip_accents(query)


def _contains_word(text: str, phrase: str) -> bool:
    if not text or not phrase:
        return False
    pattern = rf"\b{re.escape(strip_accents(phrase))}\b"
    return re.search(pattern, strip_accents(text)) is not None


def normalize_moto_model(model: str) -> str | None:
    if not model:
        return None
    key = strip_accents(model).replace("  ", " ").strip()
    normalized = MOTO_MODEL_NORMALIZATION.get(key)
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
    if not products or not query:
        return []
    scored = []
    rejected = 0
    for p in products:
        score = calculate_relevance_score(query, p)
        if score >= min_score:
            scored.append((p, score))
        else:
            rejected += 1
    scored.sort(key=lambda x: x[1], reverse=True)
    filtered = [p for p, score in scored]

    if FRAN_DEBUG:
        LAST_RELEVANCE_DEBUG.clear()
        LAST_RELEVANCE_DEBUG.update(
            {
                "query": query,
                "threshold": min_score,
                "accepted": len(filtered),
                "rejected": rejected,
                "scored": scored[:20],
            }
        )
        debug_log(
            f"[DEBUG][Relevancia] umbral={min_score} aceptados={len(filtered)} rechazados={rejected} "
            + ", ".join(
                f"{idx+1}. {p.get('code', p.get('name',''))} score={s:.2f}" for idx, (p, s) in enumerate(scored[:20])
            )
        )

    return filtered

# ------------------------------------------------------------
# NUEVO: CONTEXT QUALITY ASSESSMENT
# ------------------------------------------------------------
def assess_context_quality(query: str, products: list, intent_type: str | None = None) -> dict:
    """
    Evalúa si el contexto recuperado es suficiente para responder.
    """
    if intent_type == "social":
        return {
            "sufficient": True,
            "reason": "social_intent",
            "action": "proceed",
            "confidence": "high",
        }

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


def validate_response_codes(response_text: str, final_results: list) -> dict:
    mentioned = extract_mentioned_codes(response_text)
    allowed = {p.get('code', '') for p in final_results if p.get('code')}

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


def validate_mentioned_names(response_text: str, final_results: list) -> dict:
    if not final_results:
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
        for p in final_results if p.get("name")
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


def validate_and_fix_response(reply: str, final_results: list, phone: str, execution_context: dict) -> str:
    """
    Valida la respuesta y la regenera si tiene alucinaciones.
    """
    if execution_context.get("intent_detected") not in {"product_search", "busqueda_catalogo"}:
        return reply

    code_validation = validate_response_codes(reply, final_results)
    name_validation = validate_mentioned_names(reply, final_results)

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
    "sahel", "sempre", "sol", "sonic", "sprinter", "starken", "storm", "styler", "super cub",
    "tiburon", "tiger", "titan", "tornado", "triax", "tricolor", "twister", "vc", "vento", "viggo", "vr",
    "wave", "x3m", "xr", "xtz", "zb", "ztt"
]

KNOWN_BRANDS = BRAND_LIST
KNOWN_MODELS = MODEL_LIST


MOTO_MODEL_NORMALIZATION = {
    "wave": "WAVE 110",
    "wave 110": "WAVE 110",
    "wave110": "WAVE 110",
    "wave110s": "WAVE 110",
    "wave s": "WAVE 110",
    "wave 100": "WAVE 100",
    "cg": "CG 150",
    "cg 150": "CG 150",
    "cg150": "CG 150",
    "cg125": "CG 125",
    "cg 125": "CG 125",
    "cg 160": "CG 160",
    "cg160": "CG 160",
    "ybr": "YBR 125",
    "ybr 125": "YBR 125",
    "ybr125": "YBR 125",
    "ybr 250": "YBR 250",
    "ybr250": "YBR 250",
    "gn": "GN 125",
    "gn 125": "GN 125",
    "gn125": "GN 125",
    "xr": "XR 250",
    "xr 250": "XR 250",
    "xr250": "XR 250",
    "tornado": "TORNADO",
    "titan": "TITAN",
    "titan 150": "TITAN",
    "biz": "BIZ",
    "biz 110": "BIZ 110",
    "biz110": "BIZ 110",
    "crypton": "CRYPTON",
}


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
def detect_families_in_query(query: str):
    """
    Usa FAMILIES_INDEX para detectar familias mencionadas en el texto.
    - Palabras de la familia con len >= 3
    - matching_words * 10 como boost principal
    - +15 si la familia completa aparece como substring
    """
    if not query or not FAMILIES_INDEX:
        return []

    q_norm = normalize_search_query(query)
    if not q_norm:
        return []

    results = []
    for fam in FAMILIES_INDEX:
        fam_name_norm = fam.get("family_name_norm", "")
        if not fam_name_norm:
            continue

        fam_words = [w for w in fam_name_norm.split() if w]

        if not any(w in q_norm for w in fam_words):
            continue

        matching_words = sum(1 for w in fam_words if w in q_norm)
        score = matching_words * 10

        if fam_name_norm in q_norm:
            score += 15

        score += min(fam.get("count", 0), 50) / 10.0

        if score > 0:
            results.append((fam_name_norm, score))

    if not results:
        return []

    results.sort(key=lambda x: x[1], reverse=True)
    selected = [name for name, score in results if score >= 10]
    return selected[:5]

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

    brand_hits = []
    for b in BRAND_LIST:
        if _contains_word(q, b):
            out["brands"].append(b)
            brand_hits.append(b)

    model_hits = []
    for variant, canonical in MOTO_MODEL_NORMALIZATION.items():
        if _contains_word(q, variant):
            model_hits.append({"raw": variant, "normalized": canonical})
            out["models"].append(canonical)

    if model_hits:
        for m in model_hits:
            logger.info(
                f"[MOTO] Detectada raw='{m['raw']}' -> normalizada='{m['normalized']}'"
            )

    if brand_hits and model_hits:
        for brand in brand_hits:
            for model in model_hits:
                moto_data = {
                    "brand": brand,
                    "model": model["normalized"],
                    "raw_model": model["raw"],
                }
                out["motos_detectadas"].append(moto_data)

    if "esa moto" in q or "esa misma" in q:
        ctx = get_moto_context(phone)
        if ctx:
            out["motos_detectadas"].append(ctx)

    out["families"] = detect_families_in_query(q)

    if out["motos_detectadas"]:
        out["moto_brands"] = list({m["brand"] for m in out["motos_detectadas"] if m.get("brand")})
        out["moto_models"] = list({m["model"] for m in out["motos_detectadas"] if m.get("model")})

    if model_hits and not out["moto_models"]:
        out["moto_models"] = list({m["normalized"] for m in model_hits})

    if out["motos_detectadas"]:
        logger.info(
            "[MOTO] Para filtrar: "
            + "; ".join(
                f"brand={m.get('brand','').upper()} model={m.get('model','')}"
                for m in out["motos_detectadas"]
            )
        )

    for m in out["motos_detectadas"]:
        save_moto_context(phone, m.get("brand", ""), m.get("model", ""))

    return out


def filter_catalog(catalog, parsed):
    if not catalog:
        return []
    brands = set(parsed.get("brands") or [])
    models = set(parsed.get("models") or [])
    cats = parsed.get("categories") or []
    families = set(parsed.get("families") or [])
    moto_brands = set(parsed.get("moto_brands") or [])
    moto_models = set(parsed.get("moto_models") or [])
    motos_detectadas = parsed.get("motos_detectadas") or []
    displacement = parsed.get("displacement")
    final_category = parsed.get("final_category")
    rejection_reasons = Counter()
    filtered = []

    if FRAN_DEBUG:
        debug_log(
            "[DEBUG][Filtro] Criterios: "
            f"brands={sorted(brands)} models={sorted(models)} "
            f"moto_brands={sorted(moto_brands)} moto_models={sorted(moto_models)} "
            f"motos_detectadas={motos_detectadas} families={sorted(families)} "
            f"categories={cats} displacement={displacement} final_category={final_category}"
        )

    if motos_detectadas or moto_models or moto_brands:
        before_codes = [p.get("code", p.get("name", "")) for p in catalog[:30]]
        logger.info(
            f"[MOTO][Filtro] Compatibles antes del filtro: {len(catalog)} | Ejemplos: {before_codes}"
        )

    def _match(p):
        if brands:
            p_brand = normalize_search_query(p.get("brand", ""))
            if not any(b in p_brand for b in brands):
                rejection_reasons["brand_mismatch"] += 1
                return False

        if moto_brands:
            p_moto_brand = normalize_search_query(p.get("moto_brand", "") or p.get("brand", ""))
            if not any(b in p_moto_brand for b in moto_brands):
                rejection_reasons["moto_brand_mismatch"] += 1
                return False

        if models:
            p_model = normalize_search_query(p.get("model", ""))
            if not any(m in p_model for m in models):
                rejection_reasons["model_mismatch"] += 1
                return False

        if moto_models:
            p_moto_model = normalize_search_query(p.get("moto_model", "") or p.get("model", ""))
            if not any(m in p_moto_model for m in moto_models):
                rejection_reasons["moto_model_mismatch"] += 1
                return False

        if motos_detectadas:
            p_moto_brand = normalize_search_query(p.get("moto_brand", "") or p.get("brand", ""))
            p_moto_model = normalize_search_query(p.get("moto_model", "") or p.get("model", ""))

            if p_moto_brand and p_moto_model:
                if not any(
                    normalize_search_query(m.get("brand", "")) in p_moto_brand and
                    normalize_search_query(m.get("model", "")) in p_moto_model
                    for m in motos_detectadas
                ):
                    rejection_reasons["moto_detection_mismatch"] += 1
                    return False

        if families:
            p_family = normalize_search_query(p.get("family_name", ""))
            if not p_family:
                rejection_reasons["family_missing"] += 1
                return False
            if not any(f in p_family for f in families):
                rejection_reasons["family_mismatch"] += 1
                return False

        if cats:
            p_cat = normalize_search_query(p.get("category", ""))

            if "bateria" in cats:
                name_norm = normalize_search_query(p.get("name", ""))
                if any(x in name_norm for x in ["ytx", "yb", "yt", "gel", "agm", "litio", "12v"]):
                    pass
                else:
                    if not any(v in p_cat for v in CATEGORY_MAP.get("bateria", ["bateria"])):
                        rejection_reasons["category_mismatch"] += 1
                        return False

            other_cats = [c for c in cats if c != "bateria"]
            if other_cats:
                if not any(
                    any(v in p_cat for v in CATEGORY_MAP.get(c, [c]))
                    for c in other_cats
                ):
                    rejection_reasons["category_mismatch"] += 1
                    return False

        if final_category:
            p_final_cat = normalize_search_query(p.get("final_category", "") or p.get("category", ""))
            if not p_final_cat:
                rejection_reasons["final_category_missing"] += 1
                return False
            if final_category not in p_final_cat:
                rejection_reasons["final_category_mismatch"] += 1
                return False

        if displacement:
            p_disp = normalize_search_query(p.get("displacement", ""))
            if not p_disp:
                rejection_reasons["displacement_missing"] += 1
                return False
            if displacement not in p_disp:
                rejection_reasons["displacement_mismatch"] += 1
                return False

        return True

    for p in catalog:
        if _match(p):
            filtered.append(p)

    if motos_detectadas or moto_models or moto_brands:
        after_codes = [p.get("code", p.get("name", "")) for p in filtered[:30]]
        logger.info(
            f"[MOTO][Filtro] Después del filtro: {len(filtered)} | Ejemplos: {after_codes}"
        )

    if FRAN_DEBUG:
        total = len(catalog)
        moto_rejected = sum(
            rejection_reasons.get(k, 0)
            for k in [
                "brand_mismatch",
                "moto_brand_mismatch",
                "model_mismatch",
                "moto_model_mismatch",
                "moto_detection_missing",
                "moto_detection_mismatch",
                "displacement_missing",
                "displacement_mismatch",
            ]
        )
        family_rejected = rejection_reasons.get("family_missing", 0) + rejection_reasons.get("family_mismatch", 0)
        category_rejected = (
            rejection_reasons.get("category_mismatch", 0)
            + rejection_reasons.get("final_category_missing", 0)
            + rejection_reasons.get("final_category_mismatch", 0)
        )

        LAST_FILTER_CATALOG_DEBUG.clear()
        LAST_FILTER_CATALOG_DEBUG.update(
            {
                "total": total,
                "filtered": len(filtered),
                "rejections": dict(rejection_reasons),
                "after_moto_filter": max(total - moto_rejected, 0),
                "after_family_filter": max(total - moto_rejected - family_rejected, 0),
                "after_category_filter": max(total - moto_rejected - family_rejected - category_rejected, 0),
            }
        )

        debug_log(
            "[DEBUG][Filtro] Rechazos: "
            + ", ".join(f"{k}={v}" for k, v in sorted(rejection_reasons.items()))
            + f" | Total aceptados={len(filtered)}/{total}"
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
        "compare": "search",
        "cart_action": "cart",
        "view_cart": "cart",
        "order_flow": "checkout",
        "checkout": "checkout",
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
            CREATE TABLE IF NOT EXISTS memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT,
                data_json TEXT,
                timestamp TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_memory_phone_timestamp ON memory(phone, timestamp DESC)")

        c.execute("""
            CREATE TABLE IF NOT EXISTS memory_index (
                phone TEXT PRIMARY KEY,
                last_interaction TEXT,
                order_closed_at TEXT
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


def minutes_since(timestamp_str: str) -> float | None:
    try:
        dt = datetime.fromisoformat(timestamp_str)
        return (datetime.now() - dt).total_seconds() / 60
    except Exception as e:
        logger.error(f"minutes_since error: {e}")
        return None


def update_last_interaction(phone):
    if not phone:
        return
    try:
        now = datetime.now().isoformat()
        with get_db_connection() as conn:
            conn.execute(
                """INSERT INTO memory_index (phone, last_interaction)
                VALUES (?, ?)
                ON CONFLICT(phone) DO UPDATE SET last_interaction=excluded.last_interaction""",
                (phone, now),
            )
    except Exception as e:
        logger.error(f"update_last_interaction error: {e}")


def register_order_closed(phone):
    if not phone:
        return
    try:
        now = datetime.now().isoformat()
        with get_db_connection() as conn:
            conn.execute(
                """INSERT INTO memory_index (phone, order_closed_at)
                VALUES (?, ?)
                ON CONFLICT(phone) DO UPDATE SET order_closed_at=excluded.order_closed_at""",
                (phone, now),
            )
    except Exception as e:
        logger.error(f"register_order_closed error: {e}")


def get_last_interaction(phone):
    if not phone:
        return None
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT last_interaction FROM memory_index WHERE phone=?", (phone,))
            row = cur.fetchone()
            return row[0] if row and row[0] else None
    except Exception as e:
        logger.error(f"get_last_interaction error: {e}")
        return None


def get_order_closed_at(phone):
    if not phone:
        return None
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT order_closed_at FROM memory_index WHERE phone=?", (phone,))
            row = cur.fetchone()
            return row[0] if row and row[0] else None
    except Exception as e:
        logger.error(f"get_order_closed_at error: {e}")
        return None


def clear_memory(phone):
    if not phone:
        return
    try:
        with get_db_connection() as conn:
            conn.execute("DELETE FROM last_search WHERE phone=?", (phone,))
            conn.execute("DELETE FROM search_history WHERE phone=?", (phone,))
            conn.execute("DELETE FROM memory WHERE phone=?", (phone,))
            conn.execute("DELETE FROM conversations WHERE phone=?", (phone,))
            conn.execute("DELETE FROM interactions WHERE phone=?", (phone,))
            conn.execute("DELETE FROM pending_actions WHERE phone=?", (phone,))
            conn.execute("DELETE FROM conversation_phase WHERE phone=?", (phone,))
            conn.execute("DELETE FROM carts WHERE phone=?", (phone,))
            conn.execute("DELETE FROM memory_index WHERE phone=?", (phone,))
    except Exception as e:
        logger.error(f"clear_memory error: {e}")


def default_context():
    return {
        "last_intent": "",
        "last_query": "",
        "last_products": [],
        "last_compare_products": [],
        "last_cart_action": "",
        "cart_state": [],
        "last_motorcycle_detected": "",
        "timestamp": datetime.now().isoformat(),
        "order_closed_at": None,
    }


def _normalize_context(ctx: dict) -> dict:
    base = default_context()
    if not isinstance(ctx, dict):
        return base
    merged = {**base, **ctx}
    for key in ("last_products", "last_compare_products", "cart_state"):
        if merged.get(key) is None:
            merged[key] = []
    if merged.get("timestamp") is None:
        merged["timestamp"] = datetime.now().isoformat()
    return merged


def load_context(phone: str, last_7_days: bool = True) -> dict:
    if not phone:
        return default_context()
    try:
        cutoff = (datetime.now() - timedelta(days=7)).isoformat()
        with get_db_connection() as conn:
            cur = conn.cursor()
            if last_7_days:
                cur.execute(
                    """
                    SELECT data_json, timestamp FROM memory
                    WHERE phone=? AND timestamp >= ?
                    ORDER BY timestamp DESC
                    LIMIT 1
                    """,
                    (phone, cutoff),
                )
            else:
                cur.execute(
                    "SELECT data_json, timestamp FROM memory WHERE phone=? ORDER BY timestamp DESC LIMIT 1",
                    (phone,),
                )
            row = cur.fetchone()
            if not row:
                ctx = default_context()
                logger.info(f"[MEMORY] Loaded context: {ctx}")
                return ctx
            loaded = json.loads(row[0]) if row[0] else {}
            ctx = _normalize_context(loaded)
            logger.info(f"[MEMORY] Loaded context: {ctx}")
            return ctx
    except Exception as e:
        logger.error(f"load_context error: {e}")
        ctx = default_context()
        logger.info(f"[MEMORY] Loaded context: {ctx}")
        return ctx


def save_context(phone: str, context: dict):
    if not phone:
        return
    try:
        ctx = _normalize_context(context or {})
        ctx["timestamp"] = datetime.now().isoformat()
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO memory (phone, data_json, timestamp) VALUES (?, ?, ?)",
                (phone, json.dumps(ctx, ensure_ascii=False), ctx["timestamp"]),
            )
        logger.info(f"[MEMORY] Updated context: {ctx}")
    except Exception as e:
        logger.error(f"save_context error: {e}")


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


def get_recent_searches(phone, limit=3, minutes=10080):
    if not phone:
        return []
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT data_json, timestamp
                FROM memory
                WHERE phone = ? AND timestamp >= datetime('now', ? || ' minutes')
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (phone, -minutes, limit),
            )
            rows = cur.fetchall()
            results = []
            for row in rows:
                try:
                    results.append(json.loads(row[0]))
                except Exception:
                    continue
            return results
    except Exception as e:
        logger.error(f"get_recent_searches error: {e}")
        return []


def save_last_search(phone, data, query=None):
    if not phone or not data:
        return

    payload = data if isinstance(data, dict) else {"products": data, "query": query}
    products = payload.get("products") or []
    query_text = (payload.get("query") or query or "").strip()
    timestamp = payload.get("timestamp") or datetime.now().isoformat()
    payload.setdefault("timestamp", timestamp)
    payload.setdefault("intent", payload.get("intent", "product_search"))

    meta = {
        "products": products,
        "query": query_text,
        "timestamp": timestamp,
        "summary": f"{len(products)} productos",
        "top_category": max(
            set(p.get("category", "") for p in products),
            key=lambda c: sum(1 for p in products if p.get("category") == c),
            default="",
        ),
        "total_value": sum(float(p.get("price_ars", 0)) for p in products),
        "intent": payload.get("intent", ""),
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
                (
                    phone,
                    json.dumps(meta["products"], ensure_ascii=False),
                    query_text,
                    meta["timestamp"],
                    json.dumps(meta, ensure_ascii=False),
                ),
            )

            conn.execute(
                "INSERT INTO memory (phone, data_json, timestamp) VALUES (?, ?, ?)",
                (phone, json.dumps(payload, ensure_ascii=False), timestamp),
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

            # Si pasaron más de 7 días, no usar ese contexto
            if age_minutes > 10080:
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
        register_order_closed(phone)
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


def initialize_families_index(catalog):
    global FAMILIES_INDEX
    try:
        FAMILIES_INDEX = build_families_index_from_catalog(catalog) if catalog else []
        if FAMILIES_INDEX:
            top_names = [f["family_name"] for f in FAMILIES_INDEX[:20]]
            logger.info(f"Top 20 familias: {top_names}")
        else:
            logger.warning("FAMILIES_INDEX vacío: no se encontraron familias en el catálogo")
    except Exception as e:
        logger.error(f"Error construyendo FAMILIES_INDEX: {e}", exc_info=True)


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

        texts_to_embed = []
        for idx, text in enumerate(texts):
            if text not in cache:
                texts_to_embed.append(text)

        if texts_to_embed:
            logger.info(f"Generando embeddings para {len(texts_to_embed)} textos nuevos...")
            batch = 256
            max_retries = 3
            updated_cache = False

            for i in range(0, len(texts_to_embed), batch):
                chunk = texts_to_embed[i:i + batch]

                for retry in range(max_retries):
                    try:
                        with openai_sem:
                            resp = client.embeddings.create(
                                input=chunk,
                                model="text-embedding-3-small",
                            )
                        chunk_vectors = [d.embedding for d in resp.data]

                        for text, vec in zip(chunk, chunk_vectors):
                            cache[text] = vec
                            updated_cache = True

                        break
                    except RateLimitError as e:
                        if retry < max_retries - 1:
                            wait_time = min((2 ** retry) * random.uniform(2, 5), 60)
                            logger.warning(f"RateLimitError en embeddings, reintentando en {wait_time:.2f}s... (intento {retry+1}/{max_retries})")
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
        return catalog, index, bm25_index, tokenized_corpus

# ------------------------------------------------------------------
# BÚSQUEDA HÍBRIDA (BM25 + FAISS con RRF)
# ------------------------------------------------------------------
def hybrid_search(
    query: str,
    phone: str | None = None,
    top_k: int = MAX_SEARCH_RESULTS,
    metadata_filters: dict | None = None,
    intent: str = "product_search",
) -> list | dict:
    catalog, index, bm25_index, _bm25_corpus = get_catalog_and_index()
    if not catalog or not query:
        return []

    parsed = parse_query_v2(query, phone=phone)

    if FRAN_DEBUG:
        LAST_SEARCH_DEBUG.clear()
        LAST_SEARCH_DEBUG.update(
            {
                "query": query,
                "initial_count": len(catalog),
                "parsed": parsed,
            }
        )

    def _log_ranked(stage, results, include_rank=True, limit=20):
        if not FRAN_DEBUG:
            return
        lines = []
        for idx, item in enumerate(results[:limit], 1):
            if include_rank:
                product, score, rank = item
            else:
                product, score = item
                rank = idx
            lines.append(
                f"{rank}. {product.get('code', product.get('name',''))} | {product.get('name','').strip()} | score={score:.4f}"
            )
        debug_log(f"[DEBUG][{stage}] Top {min(limit, len(results))}: " + "; ".join(lines))

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

    bm25_results = []
    if bm25_index:
        try:
            tokenized_query = _tokenize_text(query)
            scores = bm25_index.get_scores(tokenized_query)
            ranked_indices = np.argsort(scores)[::-1]
            k_bm25 = min(max(top_k * 2, top_k), len(ranked_indices))
            for rank, idx in enumerate(ranked_indices[:k_bm25], 1):
                if 0 <= idx < len(catalog):
                    bm25_results.append((catalog[idx], float(scores[idx]), rank))
            LAST_SEARCH_DEBUG["bm25_count"] = len(bm25_results)
            _log_ranked("BM25", bm25_results, include_rank=True)
        except Exception as e:
            logger.error(f"Error en búsqueda BM25: {e}", exc_info=True)
    else:
        logger.warning("BM25 no disponible, usando solo FAISS")

    faiss_results = []
    if index:
        try:
            emb = generate_embeddings_with_cache([query])[0]
            q_vec = np.array([emb]).astype("float32")
            faiss.normalize_L2(q_vec)

            has_families = bool(parsed.get("families"))
            multiplier = 4 if has_families else 8
            k_for_index = min(max(top_k * multiplier, top_k), len(catalog))

            D, I = index.search(q_vec, k_for_index)
            for rank, (dist, idx) in enumerate(zip(D[0], I[0]), 1):
                if 0 <= idx < len(catalog):
                    faiss_results.append((catalog[idx], float(dist), rank))
            LAST_SEARCH_DEBUG["faiss_count"] = len(faiss_results)
            _log_ranked("FAISS", faiss_results, include_rank=True)
        except Exception as e:
            logger.error(f"Error en búsqueda FAISS: {e}", exc_info=True)
    else:
        logger.warning("Índice FAISS no disponible, usando solo BM25")

    logger.info(
        f"[SEARCH] FAISS results: {len(faiss_results)} | BM25 results: {len(bm25_results)}"
    )

    if not bm25_results and not faiss_results:
        return []

    k_rrf = 60
    fused_scores = defaultdict(float)
    product_lookup = {}

    def add_rrf_scores(results):
        for product, _score, rank in results:
            key = product.get("code") or product.get("name") or id(product)
            if key not in product_lookup:
                product_lookup[key] = product
            fused_scores[key] += 1.0 / (k_rrf + rank)

    add_rrf_scores(bm25_results)
    add_rrf_scores(faiss_results)

    sorted_keys = sorted(fused_scores, key=lambda k: fused_scores[k], reverse=True)
    max_candidates = min(max(top_k * 2, top_k), len(sorted_keys))
    fused = [(product_lookup[k], fused_scores[k]) for k in sorted_keys[:max_candidates]]

    LAST_SEARCH_DEBUG["rrf_count"] = len(fused)
    _log_ranked("RRF", fused, include_rank=False)

    merged_results = [p for p, _ in fused]
    LAST_SEARCH_DEBUG["merged_count"] = len(merged_results)
    logger.info(f"[SEARCH] merged_results={len(merged_results)}")

    products_only = merged_results

    if FRAN_DEBUG:
        debug_log(
            "[DEBUG][Pipeline] IDs antes de filtrar allowed_products: "
            + ", ".join(p.get("code", p.get("name", "")) for p in products_only[:50])
        )

    cats = parsed.get("categories") or []
    motos = parsed.get("motos_detectadas") or []

    if len(motos) > 1 and len(cats) > 1:
        if len(motos) * len(cats) > 6:
            return {
                "error": "too_many_combinations",
                "message": "Hay muchas combinaciones de moto y categoría. Decime una sola moto o categoría para buscar mejor.",
                "final_candidates": [],
                "final_results": [],
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
        return {"multi_moto_multi_cat": True, "results": combined, "final_candidates": [], "final_results": []}

    if len(cats) > 1 and len(motos) <= 1:
        multi_results = {}
        for c in cats:
            sub_parsed = parsed.copy()
            sub_parsed["categories"] = [c]
            sub_parsed["category"] = c
            sub_filtered = filter_catalog(products_only, sub_parsed)
            multi_results[c] = sub_filtered[:MAX_SEARCH_RESULTS]
        return {"multisearch": True, "results": multi_results, "final_candidates": [], "final_results": []}

    if len(motos) > 1:
        results = {}
        for m in motos:
            sub_parsed = parsed.copy()
            sub_parsed["motos_detectadas"] = [m]
            sub_filtered = filter_catalog(products_only, sub_parsed)
            key = f"{m['brand']} {m['model']}"
            results[key] = sub_filtered[:MAX_SEARCH_RESULTS]
        return {"multi_moto": True, "results": results, "final_candidates": [], "final_results": []}

    filtered_by_moto = filter_catalog(products_only, parsed)

    def _score_for(product):
        key = product.get("code") or product.get("name") or id(product)
        return fused_scores.get(key, 0.0)

    filtered_pairs = [(p, _score_for(p)) for p in filtered_by_moto]

    if not filtered_pairs:
        families = parsed.get("families") or []
        cat = parsed.get("category")
        brands = parsed.get("brands") or []
        models = parsed.get("models") or []

        if (brands or models) and (families or cat):
            super_relaxed = {
                "families": families,
                "category": cat,
                "brands": [],
                "models": [],
                "raw": parsed.get("raw", "")
            }
            filtered_super_relaxed = filter_catalog(products_only, super_relaxed)
            filtered_pairs = [(p, _score_for(p)) for p in filtered_super_relaxed]

    if filtered_pairs:
        results = sorted(filtered_pairs, key=lambda x: x[1], reverse=True)
        filtered_by_moto_sorted = [p for p, _ in results]
        logger.info(f"[SEARCH] filtered_by_moto={len(filtered_by_moto_sorted)}")
    else:
        filtered_by_moto_sorted = []
        if intent == "product_search" and merged_results:
            logger.warning(
                f"[MOTO] Fallback → using unfiltered merged results ({len(merged_results)})"
            )
            filtered_by_moto_sorted = merged_results

    if filtered_by_moto_sorted:
        final_results = filtered_by_moto_sorted[:top_k]
    else:
        final_results = merged_results[:top_k]

    final_candidates = final_results

    LAST_SEARCH_DEBUG["after_moto_filter"] = len(filtered_by_moto)
    LAST_SEARCH_DEBUG["final_results"] = len(final_results)
    logger.info(f"[SEARCH] final_results={len(final_candidates)}")
    logger.info(f"[FINAL] Returning {len(final_candidates)} products")

    if FRAN_DEBUG:
        after_moto = LAST_FILTER_CATALOG_DEBUG.get("after_moto_filter", len(products_only)) if LAST_FILTER_CATALOG_DEBUG else len(products_only)
        after_family = LAST_FILTER_CATALOG_DEBUG.get("after_family_filter", len(filtered_by_moto)) if LAST_FILTER_CATALOG_DEBUG else len(filtered_by_moto)
        LAST_SEARCH_DEBUG.update(
            {
                "after_moto_filter": after_moto,
                "after_family_filter": after_family,
                "after_filter_catalog": len(filtered_by_moto),
            }
        )
        debug_log(
            "[DEBUG][Pipeline] Conteo tras filtros -> "
            f"motos: {after_moto}, familias: {after_family}, catalogo: {len(filtered_by_moto)}"
        )

    return {
        "faiss": faiss_results,
        "bm25": bm25_results,
        "merged": merged_results,
        "filtered_by_moto": filtered_by_moto,
        "final_results": final_results,
        "final_candidates": final_candidates,
    }


def run_allowed_products_search(normalized_query: str, phone: str | None = None, intent: str = "product_search") -> dict:
    """
    Ejecuta la búsqueda híbrida y devuelve los resultados finales.
    """
    search_results = hybrid_search(
        normalized_query,
        phone=phone,
        top_k=MAX_SEARCH_RESULTS,
        intent=intent,
    )

    if isinstance(search_results, dict):
        final_candidates = search_results.get("final_candidates", [])
    else:
        final_candidates = search_results or []
        search_results = {"final_candidates": final_candidates, "final_results": final_candidates}

    logger.info(f"[PIPELINE] received {len(final_candidates)} final_candidates from hybrid_search")

    if FRAN_DEBUG:
        debug_log(
            "[DEBUG][Pipeline] Productos finales: "
            + ", ".join(p.get("code", p.get("name", "")) for p in final_candidates[:50])
        )

    return search_results

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
        final_results = matches.get("final_results", []) if isinstance(matches, dict) else matches
        if final_results:
            best = final_results[0]
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
            final_results = matches.get("final_results", []) if isinstance(matches, dict) else matches
            if final_results:
                best = final_results[0]
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


def find_product_by_code_in_catalog(code: str):
    catalog, _idx, _bm25, _bm25_corpus = get_catalog_and_index()
    if not catalog or not code:
        return None

    ok, normalized = validate_tercom_code(code)
    target = normalized if ok else str(code).strip()

    return next((p for p in catalog if str(p.get("code", "")).strip() == target), None)


def handle_cart_action(phone, message, context_products=None):
    msg_norm = strip_accents((message or "")).lower()
    if not msg_norm:
        return "Necesito que me indiques qué producto toco del carrito."

    cart_items = cart_get(phone)
    cart_snapshot = [
        {"code": code, "qty": qty, "name": name, "price": price}
        for code, qty, name, price in cart_items
    ]

    last_search = get_last_search(phone) or {}
    last_products = context_products or last_search.get("products") or []

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

    if action == "add" and not last_products and not code:
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
            candidate = find_product_by_code_in_catalog(code)
            if candidate:
                logger.info(f"[CART] Exact code match in catalog: {candidate.get('code')}")
        if not candidate and code:
            candidate = next((p for p in last_products if p.get("code") == code), None)
        if not candidate:
            candidate = match_product_from_list(message, last_products, key="name")
        if not candidate and code:
            fallback_matches = hybrid_search(code, phone=phone, top_k=3, intent="cart_action") or []
            if isinstance(fallback_matches, dict):
                fallback_candidates = fallback_matches.get("final_results", [])
            else:
                fallback_candidates = fallback_matches
            if fallback_candidates:
                candidate = fallback_candidates[0]
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


def run_sales_analysis_llm(conversacion_completa, productos_disponibles, contexto_cliente, perfil_cliente):
    """
    Ejecuta el análisis comercial previo al planning.
    Devuelve el JSON con sales_analysis.
    """
    payload = {
        "conversacion_completa": conversacion_completa,
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
- Cerrá ofreciendo ayuda para cotizar repuestos reales si el cliente quiere avanzar.

NO inventes códigos ni productos, enfocate en el consejo técnico.

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
        for p in allowed_products:
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
        if short_history:
            # Tomamos solo los últimos mensajes cortos para contexto
            last_msgs = short_history[-10:]
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
        search_results = hybrid_search(
            query=new_query,
            phone=phone,
            top_k=MAX_SEARCH_RESULTS,
        )

        # Filtrar por relevancia y calidad
        results_for_filter = (
            search_results.get("final_results", []) if isinstance(search_results, dict) else search_results
        )
        filtered = filter_by_relevance(new_query, results_for_filter, min_score=RELEVANCE_MIN_SCORE)
        allowed_products = filtered
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
    for p in allowed_products:
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


def generate_smart_ai_reply_v2(phone, user_message, catalog_products, execution_context, system_prompt=None):
    try:
        history = get_history_since(phone, days=1, limit=12)
        if history and history[-1].get("role") == "user" and history[-1].get("content") == user_message:
            history = history[:-1]
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
        skip_product_section = execution_context.get("intent") == "social"

        if skip_product_section:
            productos_permitidos = []

        # 1) Construir conversación reducida para análisis comercial
        historial = short_history[-6:] if short_history else []
        conversacion_completa = [h.get("content", "") for h in historial]

        # 2) Ejecutar análisis comercial previo
        sales_result = run_sales_analysis_llm(
            conversacion_completa=conversacion_completa,
            productos_disponibles=productos_permitidos,
            contexto_cliente=memory,
            perfil_cliente=memory.get("perfil_cliente", {}),
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
                productos_permitidos = (
                    semantic_results.get("final_results", [])
                    if isinstance(semantic_results, dict)
                    else semantic_results
                )
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
        price = format_price(p.get("price_ars", 0))
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


def chunk_message_for_twilio(text: str, limit: int = TWILIO_CHUNK_LIMIT) -> list[str]:
    if not text:
        return []

    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks: list[str] = []
    current: list[str] = []

    for paragraph in paragraphs:
        candidate = "\n\n".join(current + [paragraph]).strip()
        if candidate and len(candidate) <= limit:
            current.append(paragraph)
            continue

        if current:
            chunks.append("\n\n".join(current).strip())
            current = []

        if len(paragraph) <= limit:
            current.append(paragraph)
            continue

        sentences = re.split(r"(?<=[.!?])\s+", paragraph)
        sentence_buffer: list[str] = []
        for sentence in sentences:
            candidate_sentence = " ".join(sentence_buffer + [sentence]).strip()
            if candidate_sentence and len(candidate_sentence) <= limit:
                sentence_buffer.append(sentence)
            else:
                if sentence_buffer:
                    chunks.append(" ".join(sentence_buffer).strip())
                sentence_buffer = [sentence]
        if sentence_buffer:
            current.append(" ".join(sentence_buffer).strip())

    if current:
        chunks.append("\n\n".join(current).strip())

    return chunks or [text]


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


def format_products_by_category(grouped: dict) -> dict:
    filtered = {}
    for cat, productos in grouped.items():
        real = [p for p in productos if p.get("codigo")]
        if len(real) > 0:
            filtered[cat] = real

    return filtered

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

    context = load_context(phone, last_7_days=True)

    if not is_llm_available():
        reply = "Estoy en mantenimiento técnico. Volvé a intentar en unos minutos."
        save_message(phone, reply, "assistant")
        return reply

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

    intents = understanding.get("intents") or []
    normalized_candidate = (
        understanding.get("normalized_query")
        or (intents[0].get("span") if intents else user_message)
        or user_message
    )
    normalized_lower = strip_accents((normalized_candidate or "").lower())
    heuristics_intent = None

    if any(w in normalized_lower for w in ["compar", "vs", "versus", "diferencia", "cual conviene", "cuál conviene"]):
        heuristics_intent = "compare"
    elif any(w in normalized_lower for w in ["medida", "stock", "compatibles", "cuanto", "cuánto"]):
        heuristics_intent = "clarification"
    elif re.search(r"\bsku\b", normalized_lower) or "agregame" in normalized_lower or re.search(r"\d{4}/\d{5}-\d{3}", normalized_lower):
        heuristics_intent = "cart_action"
    elif any(w in normalized_lower for w in ["cerrar", "listo", "enviame total", "enviame el total"]):
        heuristics_intent = "checkout"

    allowed_types = {"product_search", "compare", "cart_action", "checkout", "clarification", "general_chat"}

    updated_intents = []
    for intent in intents:
        data = intent.get("data", {}) or {}
        intent_name = intent.get("type") or ""

        if intent_name == "social":
            intent_name = "general_chat"
        elif intent_name in {"tech_question", "follow_up"}:
            intent_name = "clarification"
        elif intent_name == "order_flow":
            intent_name = "checkout"

        if heuristics_intent and intent_name not in allowed_types:
            intent_name = heuristics_intent

        if intent_name not in allowed_types:
            intent_name = heuristics_intent or "general_chat"

        if heuristics_intent and intent_name in {"clarification", "general_chat"}:
            intent_name = heuristics_intent

        if "notes" not in data or data.get("notes") is None:
            data["notes"] = ""

        if "quantity" not in data or data.get("quantity") is None:
            data["quantity"] = 1

        intent["data"] = data
        intent["type"] = intent_name or "general_chat"
        updated_intents.append(intent)

    intents = updated_intents
    if not intents:
        intents = [
            {
                "type": heuristics_intent or "general_chat",
                "span": user_message,
                "confidence": 0.5,
                "data": {"notes": "", "quantity": 1},
            }
        ]

    understanding["intents"] = intents

    primary_intent = intents[0].get("type") if intents else "general_chat"
    if not primary_intent:
        primary_intent = "general_chat"
    primary_span = intents[0].get("span") if intents else user_message
    logger.info(f"[INTENT] Assigned intent={primary_intent}")

    if understanding.get("needs_clarification"):
        reply = understanding.get("clarification_question") or "¿Me pasás más detalles de la moto y el repuesto?"
        save_message(phone, reply, "assistant")
        return reply

    normalized_query = understanding.get("normalized_query") or (
        primary_span if primary_intent == "product_search" else user_message
    )

    moto_detected = ""
    try:
        parsed_query = parse_query_v2(normalized_query, phone=phone)
        if parsed_query.get("motos_detectadas"):
            moto = parsed_query.get("motos_detectadas")[0]
            moto_detected = f"{moto.get('brand', '')} {moto.get('model', '')}".strip()
    except Exception:
        moto_detected = context.get("last_motorcycle_detected", "")

    logger.info(
        f"[STEP 1] Normalized: '{normalized_query}' | Intent: {primary_intent} | Corrections: {understanding.get('corrections')}"
    )

    recent_searches = get_recent_searches(phone)
    recent_products: list[dict] = []
    for search in recent_searches:
        if isinstance(search, dict) and search.get("products"):
            recent_products.extend(search.get("products") or [])

    deduped = {}
    for p in recent_products:
        key = p.get("code") or p.get("codigo") or p.get("name")
        if key:
            deduped[key] = p
    recent_products = list(deduped.values())

    logger.info(f"[MEMORY] Recovered {len(recent_products)} recent products")

    # ============================================
    # STEP 2: SEARCH
    # ============================================
    logger.info(f"[STEP 2] Searching products...")

    candidates: list = []
    selected_products: list = []
    selection: dict = {"selected_products": [], "analysis": {}}
    reply: str | None = None
    search_results: dict | list = {}

    use_memory = True
    context_products = context.get("last_products") or []
    if context_products:
        recent_products = context_products + recent_products
        deduped_recent = {}
        for p in recent_products:
            key = p.get("code") or p.get("codigo") or p.get("name")
            if key:
                deduped_recent[key] = p
        recent_products = list(deduped_recent.values())

    if primary_intent == "general_chat":
        logger.info("[STEP 2] Bypass search for general chat intent")
        reply = handle_general_chat_intent({"span": user_message}, phone, context)
    elif primary_intent == "product_search":
        search_results = hybrid_search(
            normalized_query, phone=phone, top_k=MAX_SEARCH_RESULTS, intent=primary_intent
        )
        logger.info(
            "[STEP 2][SEARCH] FAISS=%s BM25=%s merged=%s moto_filtered=%s final=%s",
            LAST_SEARCH_DEBUG.get("faiss_count", 0),
            LAST_SEARCH_DEBUG.get("bm25_count", 0),
            LAST_SEARCH_DEBUG.get("merged_count", 0),
            LAST_SEARCH_DEBUG.get("after_moto_filter", 0),
            LAST_SEARCH_DEBUG.get("final_results", 0),
        )
        if isinstance(search_results, dict):
            if search_results.get("error") == "too_many_combinations":
                reply = search_results.get("message", "Pasame una sola moto o categoría.")
            else:
                formatted = format_multi_search_response(search_results)
                if formatted:
                    reply = formatted
                candidates = search_results.get("final_candidates", [])
        else:
            candidates = search_results or []
            search_results = {"final_candidates": candidates, "final_results": candidates}

        logger.info(f"[STEP 2] Final candidates: {len(candidates)}")

        if candidates:
            save_last_search(
                phone,
                [
                    {
                        "code": p.get("code", ""),
                        "name": p.get("name", ""),
                        "price_ars": p.get("price_ars"),
                        "price_usd": p.get("price_usd"),
                        "qty": 1,
                    }
                    for p in candidates[:MAX_ITEMS]
                ],
                normalized_query,
            )
    elif primary_intent == "cart_action":
        reply = handle_cart_action(phone, user_message, context_products=context_products)
    elif primary_intent == "checkout":
        reply = handle_checkout_intent({"span": user_message}, phone)
    else:
        if use_memory and recent_products:
            candidates = recent_products
            logger.info(f"[STEP 2] Using memory: {len(candidates)} products")
        else:
            candidates = []
            logger.info("[STEP 2] Memory empty — no candidates")

    clean_candidates = [
        p
        for p in candidates
        if (p.get("familia") or p.get("family") or p.get("family_name"))
        and str(p.get("familia") or p.get("family") or p.get("family_name")).strip()
    ]
    if clean_candidates:
        candidates = clean_candidates

    logger.info(f"[CLEAN] final candidates: {len(candidates)}")

    # ============================================
    # STEP 3: PRODUCT SELECTION (LLM)
    # ============================================
    logger.info(f"[STEP 3] Selecting best products...")

    if reply is None and primary_intent == "product_search":
        selection_candidates = candidates[:MAX_ITEMS]
        if len(selection_candidates) > 50:
            block_size = 18
            annotated: list[dict] = []
            for idx, start in enumerate(range(0, len(selection_candidates), block_size)):
                block = selection_candidates[start:start + block_size]
                for product in block:
                    annotated.append({**product, "block_number": idx + 1})
            selection_candidates = annotated

        selection_payload = {
            "phone": phone,
            "intent": primary_intent,
            "original_message": user_message,
            "detected_intent": primary_intent,
            "normalized_query": normalized_query,
            "business_context": "Ventas mayoristas de repuestos de moto. Tono mayorista, directo, sin inventar productos.",
            "final_candidates": selection_candidates,
        }

        selection = complete_template("product_selection", selection_payload)
        selected_codes = {
            p.get("code") for p in selection.get("selected_products", []) if p.get("code")
        }
        selected_products = [
            p for p in selection_candidates if p.get("code") in selected_codes
        ]
    elif reply is None and primary_intent in {"clarification", "compare"}:
        if len(recent_products) >= 1:
            selected_products = recent_products[:7]
            logger.info(f"[COMPARE] Providing {len(selected_products)} products from memory")
        else:
            selected_products = candidates[:7]
        selection = {
            "selected_products": selected_products,
            "analysis": {"customer_type": "recurrente", "interest_level": "medio", "key_arguments": []},
        }
    else:
        selection = selection or {"selected_products": selected_products, "analysis": {}}

    logger.info(
        f"[STEP 3] Selected {len(selected_products)} products | Customer: {selection.get('analysis', {}).get('customer_type')}"
    )

    # ============================================
    # STEP 4: RESPONSE GENERATION (LLM)
    # ============================================
    logger.info(f"[STEP 4] Generating response...")

    if reply is None:
        if primary_intent == "clarification" and not selected_products:
            reply = "Necesito más detalles de la moto o el repuesto para ayudarte bien."
        elif primary_intent == "compare" and not selected_products:
            reply = "Decime cuáles de los últimos productos querés comparar o pasame los códigos."
        elif primary_intent == "product_search" and not candidates:
            reply = "No encontré resultados con esa descripción. Probá indicarme la categoría o la moto y te muestro opciones."
        elif primary_intent == "general_chat":
            reply = handle_general_chat_intent({"span": user_message}, phone, context)

    if reply is None:
        memory_context = {}
        if primary_intent in {"compare", "clarification"}:
            memory_context = {
                "last_products": context.get("last_products", []),
                "last_motorcycle_detected": context.get("last_motorcycle_detected", ""),
                "cart_state": context.get("cart_state", []),
            }

        response = complete_template(
            "response_generation",
            {
                "phone": phone,
                "intent": primary_intent,
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
                "memory_context": memory_context,
            },
        )

        reply = response.get("message", "")
        products_cited = response.get("products_cited", [])
    else:
        products_cited = []

    # ============================================
    # STEP 5: POST-VALIDATION
    # ============================================
    logger.info(f"[STEP 5] Validating response...")

    if primary_intent == "product_search":
        allowed_codes = {p.get("code") for p in candidates if p.get("code")}
        hallucinated = set(products_cited) - allowed_codes

        if hallucinated:
            logger.error(f"⚠️ LLM cited invalid codes: {hallucinated}")
            reply = format_search_results(candidates[:5])
            reply = f"Te muestro opciones:\n\n{reply}\n\n¿Cuál te sirve?"

    if selected_products or candidates:
        save_last_search(
            phone,
            {
                "products": selected_products or candidates,
                "query": normalized_query,
                "intent": primary_intent,
                "timestamp": datetime.now().isoformat(),
            },
        )

    cart_snapshot = [
        {
            "code": code,
            "qty": qty,
            "name": name,
            "price": float(to_decimal_money(price)),
        }
        for code, qty, name, price in cart_get(phone)
    ]

    context["cart_state"] = cart_snapshot
    context["last_intent"] = primary_intent

    if primary_intent in {"product_search", "clarification", "compare"}:
        context["last_query"] = normalized_query

    if primary_intent == "product_search":
        context["last_products"] = selected_products or candidates
        context["last_compare_products"] = []
        context["last_cart_action"] = ""
        if moto_detected:
            context["last_motorcycle_detected"] = moto_detected
    elif primary_intent == "compare":
        if selected_products:
            context["last_compare_products"] = selected_products
        if candidates and not context.get("last_products"):
            context["last_products"] = candidates
    elif primary_intent == "clarification":
        context["last_compare_products"] = context.get("last_compare_products", [])
    elif primary_intent == "cart_action":
        context["last_cart_action"] = user_message
    elif primary_intent == "checkout":
        context["last_cart_action"] = "checkout"
        context["order_closed_at"] = datetime.now().isoformat()

    if primary_intent == "general_chat" and not context.get("last_query"):
        context["last_query"] = user_message

    save_context(phone, context)

    save_message(phone, reply, "assistant")
    log_interaction(phone, user_message, primary_intent, len(selected_products))
    log_performance(phone, primary_intent, time.time() - start_time, len(candidates))
    update_sales_phase_from_intent(phone, primary_intent)

    logger.info(f"[DONE] Response sent | Duration: {time.time()-start_time:.2f}s")

    return reply


def handle_social_intent(intent: dict, phone: str) -> str:
    span = intent.get("span", "")
    system_prompt = (
        "Sos Fran, asistente mayorista humano y cercano. Contestá breve (1-2 líneas), "
        "en tono cálido, sin catálogo ni listas."
    )
    user_prompt = f"Mensaje social del cliente: \"{span}\""
    return llm_text_response(system_prompt, user_prompt, temperature=0.5)


def handle_general_chat_intent(intent: dict, phone: str, context: dict) -> str:
    span = intent.get("span", "")
    hints = []
    if context.get("last_motorcycle_detected"):
        hints.append(f"Última moto: {context.get('last_motorcycle_detected')}")
    if context.get("last_query"):
        hints.append(f"Última consulta: {context.get('last_query')}")
    if context.get("cart_state"):
        hints.append(f"Items en carrito: {len(context.get('cart_state', []))}")
    memory_hint = " | ".join([h for h in hints if h])
    system_prompt = (
        "Sos Fran, asistente mayorista humano y cercano. Contestá breve (1-2 líneas), "
        "en tono cálido, sin catálogo ni listas."
    )
    user_prompt = f"Mensaje del cliente: '{span}'. Contexto previo: {memory_hint}"
    return llm_text_response(system_prompt, user_prompt, temperature=0.5)


def handle_checkout_intent(intent: dict, phone: str) -> str:
    span = intent.get("span", "")
    items = cart_get(phone)
    total, discount = cart_totals(phone)
    if not items:
        return "Tu carrito está vacío. Decime qué agrego y te paso el total."
    lines = [f"{qty}x {name} ({code})" for code, qty, name, _ in items]
    discount_text = f" con descuento de {format_price(discount)}" if discount else ""
    register_order_closed(phone)
    return (
        f"Listo, cierro el pedido: {format_price(total)}{discount_text}. "
        f"Detalle: {'; '.join(lines)}. {span}".strip()
    )


def handle_social_intents_only(intents: list[dict], phone: str) -> str:
    quality = {"sufficient": True, "confidence": 1.0, "reason": "social_intent"}
    _ = quality  # Se mantiene explícito para evitar recalcular calidad en sociales

    social_intents = [i for i in intents if i.get("type") in {"social", "general_chat"}]
    if not social_intents:
        fallback_span = (intents[0].get("span") if intents else "") or ""
        social_intents = [{"type": "general_chat", "span": fallback_span, "confidence": 1.0, "data": {}}]

    responses: list[tuple[str, str]] = []
    for intent in social_intents:
        responses.append(("general_chat", handle_general_chat_intent(intent, phone, load_context(phone, last_7_days=True))))

    return combine_responses(responses, social_intents)


def handle_clarification_intent(intent: dict, phone: str) -> str:
    span = intent.get("span", "")
    data = intent.get("data", {}) or {}
    system_prompt = (
        "Pedí datos faltantes de forma simple y amable. No ofrezcas productos aún."
    )
    fields = []
    for key in ("brand", "model", "product", "category"):
        if data.get(key):
            fields.append(f"{key}: {data.get(key)}")
    info_hint = f" Datos detectados: {', '.join(fields)}." if fields else ""
    user_prompt = f"Texto del cliente: '{span}'.{info_hint}"
    return llm_text_response(system_prompt, user_prompt, temperature=0.4)


def handle_cart_action_intent(intent: dict, phone: str) -> str:
    span = intent.get("span", "")
    data = intent.get("data", {}) or {}
    enriched = span
    if data.get("product") or data.get("quantity"):
        extra = []
        if data.get("product"):
            extra.append(f"producto: {data['product']}")
        if data.get("quantity"):
            extra.append(f"cantidad: {data['quantity']}")
        enriched = f"{span} ({', '.join(extra)})"
    return handle_cart_action(phone, enriched)


def handle_product_search_intent(intent: dict, phone: str) -> tuple[str, list]:
    query_span = intent.get("span", "")
    search_results = run_allowed_products_search(query_span, phone=phone, intent=intent.get("type"))

    if isinstance(search_results, dict):
        reply = format_multi_search_response(search_results)
        candidates = search_results.get("final_candidates", []) if not reply else []
        if reply:
            return reply or "Pasame una sola moto o categoría para buscar bien.", []
    else:
        candidates = search_results or []

    logger.info(f"[MULTI] Final candidates: {len(candidates)}")

    selected_products = candidates

    response = complete_template(
        "response_generation",
        {
            "phone": phone,
            "intent": "product_search",
            "selected_products": selected_products,
            "customer_analysis": {},
            "query_context": {"original": query_span, "normalized": query_span, "corrections": []},
            "conversation_state": {
                "sales_phase": get_sales_phase(phone),
                "cart_total": format_price(cart_totals(phone)[0]),
            },
        },
    )

    reply = response.get("message", "")
    products_cited = response.get("products_cited", [])
    allowed_codes = {p.get("code") for p in candidates if p.get("code")}
    hallucinated = set(products_cited) - allowed_codes

    if hallucinated:
        logger.error(f"⚠️ LLM cited invalid codes: {hallucinated}")
        reply = format_search_results(candidates[:5])
        reply = f"Te muestro opciones:\n\n{reply}\n\n¿Cuál te sirve?"

    save_last_search(phone, candidates, query_span)
    return reply, candidates


def handle_tech_question_intent(intent: dict) -> str:
    span = intent.get("span", "")
    system_prompt = (
        "Contestá dudas técnicas o de compatibilidad de forma breve y clara, tono mayorista."
    )
    return llm_text_response(system_prompt, f"Consulta técnica: {span}", temperature=0.3)


def handle_order_flow_intent(intent: dict, phone: str) -> str:
    span = intent.get("span", "")
    total, _ = cart_totals(phone)
    cart_context = f"Carrito estimado: {format_price(total)}." if total else "Carrito aún vacío."
    system_prompt = (
        "Guiá al cliente por pasos de compra, pago o envío en tono mayorista, breve y directo."
    )
    user_prompt = f"Consulta sobre pedido/envío: '{span}'. Contexto: {cart_context}"
    return llm_text_response(system_prompt, user_prompt, temperature=0.35)


def combine_responses(responses: list[tuple[str, str]], intents: list[dict]) -> str:
    buckets: dict[str, list[str]] = {
        "general_chat": [],
        "clarification": [],
        "cart_action": [],
        "product_search": [],
        "compare": [],
        "checkout": [],
    }

    seen = set()
    for intent_type, text in responses:
        if not text:
            continue
        cleaned = text.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        buckets.setdefault(intent_type, []).append(cleaned)

    ordered_parts: list[str] = []
    for key in ("general_chat", "clarification", "cart_action", "product_search", "compare", "checkout"):
        ordered_parts.extend(buckets.get(key, []))

    if ordered_parts:
        closing = "Decime si querés que deje todo listo o busco algo más."
        if buckets.get("general_chat"):
            closing = "¡Quedo atento a lo que necesites!"
        if closing not in ordered_parts:
            ordered_parts.append(closing)

    message = "\n\n".join(ordered_parts)
    chunks = chunk_message_for_twilio(message, TWILIO_CHUNK_LIMIT)
    return "\n\n".join(chunks)


def orquestar_fran_multi_intent(mensaje_usuario: str, phone: str) -> str:
    start_time = time.time()
    user_message = sanitize_input(mensaje_usuario or "", max_length=1500)

    if not rate_limit_check(phone):
        reply = "Demasiados mensajes, esperá un minuto."
        save_message(phone, reply, "assistant")
        return reply

    save_message(phone, user_message, "user")

    if not is_llm_available():
        reply = "Estoy en mantenimiento técnico. Volvé a intentar en unos minutos."
        save_message(phone, reply, "assistant")
        return reply

    understanding = complete_template(
        "query_understanding",
        {
            "phone": phone,
            "user_message": user_message,
            "conversation_history": get_history_since(phone, days=1, limit=6),
            "last_search_query": (get_last_search(phone) or {}).get("query", ""),
        },
    )

    allowed_types = {"product_search", "cart_action", "general_chat", "clarification", "compare", "checkout"}
    intents = [
        intent
        for intent in (understanding.get("intents") or [])
        if intent.get("type") in allowed_types and (intent.get("span") or "").strip()
    ]
    if not intents:
        intents = [{"type": "general_chat", "span": user_message, "confidence": 0.5, "data": {}}]

    has_product_intent = any(i.get("type") == "product_search" for i in intents)
    if not has_product_intent:
        reply = handle_social_intents_only(intents, phone)
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "multi_intent", 0)
        log_performance(phone, "multi_intent", time.time() - start_time, 0)
        return reply

    priority = {
        "general_chat": 0,
        "clarification": 1,
        "compare": 2,
        "product_search": 3,
        "cart_action": 4,
        "checkout": 5,
    }
    intents_sorted = sorted(intents, key=lambda x: priority.get(x.get("type", "z"), 9))

    responses: list[tuple[str, str]] = []
    all_products = []

    for intent in intents_sorted:
        itype = intent.get("type")
        if itype == "general_chat":
            responses.append((itype, handle_general_chat_intent(intent, phone, load_context(phone, last_7_days=True))))
        elif itype == "clarification":
            responses.append((itype, handle_clarification_intent(intent, phone)))
        elif itype == "cart_action":
            responses.append((itype, handle_cart_action_intent(intent, phone)))
        elif itype == "product_search":
            reply, products = handle_product_search_intent(intent, phone)
            responses.append((itype, reply))
            all_products.extend(products or [])
        elif itype == "compare":
            responses.append((itype, handle_clarification_intent(intent, phone)))
        elif itype == "checkout":
            responses.append((itype, handle_checkout_intent(intent, phone)))

    final_reply = combine_responses(responses, intents_sorted)

    save_message(phone, final_reply, "assistant")
    log_interaction(phone, user_message, "multi_intent", len(all_products))
    log_performance(phone, "multi_intent", time.time() - start_time, len(all_products))

    return final_reply

# =========================================================
# ORQUESTADOR PRINCIPAL – VERSIÓN 3.15
# =========================================================
def orquestar_fran(mensaje_usuario: str, phone: str) -> str:
    """
    Orquestador unificado de Fran.

    Wrapper sobre la versión 3.15 (templates estructurados) para evitar
    mantener dos implementaciones del flujo conversacional.
    """
    return orquestar_fran_v315(mensaje_usuario, phone)

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
        parts = chunk_message_for_twilio(text, chunk_size)
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

        last_seen = get_last_interaction(from_number)
        if last_seen:
            age_minutes = minutes_since(last_seen)
            if age_minutes and age_minutes > 10080:
                clear_memory(from_number)

        closed_at = get_order_closed_at(from_number)
        if closed_at:
            closed_age = minutes_since(closed_at)
            if closed_age and closed_age > 10080:
                clear_memory(from_number)

        update_last_interaction(from_number)

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

        reply = orquestar_fran(message_body, from_number)

        logger.info(f"Respuesta generada: {len(reply)} caracteres")
        logger.info(f"Preview: {reply[:100]}...")

        if len(reply) <= TWILIO_CHUNK_LIMIT:
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
        "version": "3.15",
        "catalog_size": len(catalog) if catalog else 0,
        "architecture": "claude_inspired_families_hybrid_intents_context_v2",
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
    logger.info("🚀 Iniciando Fran 3.15 - Motor Híbrido Familias + FAISS + Intents/Contexto 2.0 + doble LLM")
    logger.info("=" * 60)
    logger.info(f"Puerto: {port}")
    logger.info(f"Catálogo: {len(catalog) if catalog else 0} productos")
    logger.info(f"Tipo de cambio inicial: {get_exchange_rate()}")
    logger.info(f"Relevance min score: {RELEVANCE_MIN_SCORE}")
    logger.info(f"Quality thresholds: HIGH={QUALITY_HIGH_THRESHOLD}, MED={QUALITY_MEDIUM_THRESHOLD}")
    logger.info("=" * 60)
    logger.info("Características nuevas en 3.15:")
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
