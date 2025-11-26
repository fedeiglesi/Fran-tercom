# =========================================================
# Fran 3.14 – Bot Mayorista Inteligente
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
from functools import lru_cache
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

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------
OPENAI_API_KEY = (os.environ.get("OPENAI_API_KEY") or "").strip()
if not OPENAI_API_KEY:
    raise RuntimeError("Falta OPENAI_API_KEY")

MODEL_NAME = (os.environ.get("MODEL_NAME") or "gpt-4o-mini").strip()
# Usar modelo más barato para reasoning
MODEL_REASONING = "gpt-4o-mini"  # más barato, rápido
MODEL_RESPONSE = "gpt-4o-mini"   # mantener calidad conversacional
USE_SALES_PROMPT_FLOW = (os.environ.get("USE_SALES_PROMPT_FLOW", "true").strip().lower() == "true")

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
WHATSAPP_MSG_LIMIT = int(os.environ.get("WHATSAPP_MSG_LIMIT", "3500"))
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

_embeddings_cache_lock = Lock()

# Cache de fuzzy matching para post-validation
_fuzzy_match_cache = LRUCache(maxsize=20000)

# Índice de familias (global)
FAMILIES_INDEX = []

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
    for p in products:
        score = calculate_relevance_score(query, p)
        if score >= min_score:
            scored.append((p, score))
    scored.sort(key=lambda x: x[1], reverse=True)
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
        logger.warning("Respuesta con alucinaciones detectadas, regenerando...")

        if execution_context.get("regenerated", 0) >= 2:
            return "Tuve problemas validando la respuesta. Repetíme el pedido para que no te pase algo incorrecto."

        execution_context["regenerated"] = execution_context.get("regenerated", 0) + 1
        save_message(phone, reply, "assistant_faulty")

        if allowed_products[:5]:
            product_list = "\n".join([
                f"• {p['name']} (código {p['code']}) - {format_price(p['price_ars'])}"
                for p in allowed_products[:5]
            ])

            return (
                f"Mirá, te paso lo que tengo en catálogo para lo que buscás:\n\n"
                f"{product_list}\n\n"
                f"¿Alguno te sirve? Si necesitás otra cosa decime marca/modelo específico."
            )

        return "No encontré coincidencias exactas. Dame más detalles (marca/modelo/año) y te busco opciones precisas."

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
    brands = set(parsed.get("brands") or [])
    models = set(parsed.get("models") or [])
    cats = parsed.get("categories") or []
    families = set(parsed.get("families") or [])
    moto_brands = set(parsed.get("moto_brands") or [])
    moto_models = set(parsed.get("moto_models") or [])
    motos_detectadas = parsed.get("motos_detectadas") or []
    displacement = parsed.get("displacement")
    final_category = parsed.get("final_category")

    def _match(p):
        if brands:
            p_brand = normalize_search_query(p.get("brand", ""))
            if not any(b in p_brand for b in brands):
                return False

        if moto_brands:
            p_moto_brand = normalize_search_query(p.get("moto_brand", "") or p.get("brand", ""))
            if not any(b in p_moto_brand for b in moto_brands):
                return False

        if models:
            p_model = normalize_search_query(p.get("model", ""))
            if not any(m in p_model for m in models):
                return False

        if moto_models:
            p_moto_model = normalize_search_query(p.get("moto_model", "") or p.get("model", ""))
            if not any(m in p_moto_model for m in moto_models):
                return False

        if motos_detectadas:
            p_moto_brand = normalize_search_query(p.get("moto_brand", "") or p.get("brand", ""))
            p_moto_model = normalize_search_query(p.get("moto_model", "") or p.get("model", ""))
            if not p_moto_brand or not p_moto_model:
                return False
            if not any(
                normalize_search_query(m.get("brand", "")) in p_moto_brand and
                normalize_search_query(m.get("model", "")) in p_moto_model
                for m in motos_detectadas
            ):
                return False

        if families:
            p_family = normalize_search_query(p.get("family_name", ""))
            if not p_family:
                return False
            if not any(f in p_family for f in families):
                return False

        if cats:
            p_cat = normalize_search_query(p.get("category", ""))

            # Batería tiene reglas especiales
            if "bateria" in cats:
                name_norm = normalize_search_query(p.get("name", ""))
                if any(x in name_norm for x in ["ytx", "yb", "yt", "gel", "agm", "litio", "12v"]):
                    pass
                else:
                    if not any(v in p_cat for v in CATEGORY_MAP.get("bateria", ["bateria"])):
                        return False

            # Otras categorías
            other_cats = [c for c in cats if c != "bateria"]
            if other_cats:
                if not any(
                    any(v in p_cat for v in CATEGORY_MAP.get(c, [c]))
                    for c in other_cats
                ):
                    return False

        if final_category:
            p_final_cat = normalize_search_query(p.get("final_category", "") or p.get("category", ""))
            if not p_final_cat:
                return False
            if final_category not in p_final_cat:
                return False

        if displacement:
            p_disp = normalize_search_query(p.get("displacement", ""))
            if not p_disp:
                return False
            if displacement not in p_disp:
                return False

        return True

    return [p for p in catalog if _match(p)]

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

        try:
            res = requests.get(EXCHANGE_API_URL, timeout=REQUESTS_TIMEOUT, headers=REQUESTS_HEADERS)
            res.raise_for_status()
            venta = res.json().get("venta", None)
            rate = to_decimal_money(venta) if venta is not None else DEFAULT_EXCHANGE
            exchange_cache["rate"] = rate
            exchange_cache["timestamp"] = now
            return rate
        except Exception as e:
            logger.warning(f"Fallo tasa cambio: {e}")
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
@lru_cache(maxsize=1)
def _load_raw_csv():
    try:
        if CATALOG_URL.startswith("http"):
            r = requests.get(CATALOG_URL, timeout=REQUESTS_TIMEOUT, headers=REQUESTS_HEADERS)
            r.raise_for_status()
            r.encoding = "utf-8"
            return r.text

        local_path = Path(CATALOG_URL.replace("file://", ""))
        if local_path.exists():
            return local_path.read_text(encoding="utf-8")

        logger.warning(f"Ruta de catálogo inválida: {CATALOG_URL}")
    except Exception as e:
        logger.error(f"Error descargando CSV: {e}")

    if LOCAL_CSV_FALLBACK.exists():
        logger.info("Usando catálogo local de respaldo")
        return LOCAL_CSV_FALLBACK.read_text(encoding="utf-8")

    return ""


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
def hybrid_search(query: str, phone: str | None = None, top_k: int = MAX_SEARCH_RESULTS, metadata_filters: dict | None = None) -> list | dict:
    catalog, index, bm25_index, _bm25_corpus = get_catalog_and_index()
    if not catalog or not query:
        return []

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
        except Exception as e:
            logger.error(f"Error en búsqueda FAISS: {e}", exc_info=True)
    else:
        logger.warning("Índice FAISS no disponible, usando solo BM25")

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
            results = fused

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:top_k]

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
        if matches:
            best, score = matches[0]
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
            if matches:
                best, score = matches[0]
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
# INTENT DETECTOR 2.0
# ------------------------------------------------------------------
INTENT_SYSTEM_PROMPT = """
Sos un clasificador de intenciones para un vendedor mayorista (WhatsApp).
NO respondas al usuario. NO agregues explicaciones.
Tu única salida será un JSON válido (UNA línea) con este esquema EXACTO:
{"intent":"<uno de: small_talk|product_search|cart_action|order_flow|payment|shipping|tech_expert|view_cart|empty_cart|confirmation|negation|unknown>", "query":"<texto util para buscar o ''>"}

Criterios estrictos (habla argentina):

- "hola", "buen día", "buenas", "que tal", "cómo va", "gracias" → small_talk

- Si aparece un código tipo 1234/56789-012 → product_search (query = código)
- Pedidos de repuestos / precios / "tenés", "busco", "algo para", menciona marca-modelo → product_search

- Verbos de carrito: "agregá", "sumame", "sacame", "bajame", "subilo", "ponelo", "agregame", "cargame" → cart_action
- "ver carrito", "qué tengo", "mostrame el carrito" → view_cart
- "vaciar", "limpia todo", "borra el carrito" → empty_cart

- "listo, cómo sigo?", "qué opciones hay?", "ya estaría", "cerremos", "hacemos el pedido" → order_flow (checkout)
- Pagos: "cómo pago", "transferencia", "efectivo", "tenés QR", "cheque", "pago" → payment
- Envíos: "envío", "mandás moto", "retiro", "mensajero", "cuánto tarda" → shipping

- Preguntas técnicas/mecánicas: "por qué", "qué conviene", "cuánto dura", "cada cuánto" → tech_expert

- "sí", "dale", "ok", "perfecto", "vamos" → confirmation
- "no", "cancelá", "dejalo", "me arrepentí" → negation

- Cualquier otro caso → unknown

La clave "query" solo debe contener texto útil para buscar en catálogo (aplica a product_search). Para otras intenciones usá "".

IMPORTANTE: devolvé SIEMPRE un JSON de una sola línea.
"""


def detect_intent_llm(msg):
    try:
        with openai_sem:
            resp = client.chat.completions.create(
                model=MODEL_NAME,
                temperature=0,
                max_tokens=50,
                messages=[
                    {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                    {"role": "user", "content": msg.strip()[:600]}
                ]
            )
        raw = (resp.choices[0].message.content or "").strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start:end+1]

        data = json.loads(raw)
        if not isinstance(data, dict):
            return {"intent": "unknown", "query": ""}

        intent = str(data.get("intent", "unknown")).strip()
        query = str(data.get("query", "")).strip()

        valid_intents = {
            "small_talk", "product_search", "cart_action", "order_flow", "payment", "shipping",
            "tech_expert", "view_cart", "empty_cart", "confirmation", "negation", "unknown"
        }
        if intent not in valid_intents:
            intent = "unknown"

        return {"intent": intent, "query": query or msg.strip()[:600]}
    except Exception as e:
        logger.error(f"detect_intent_llm error: {e}")
        return {"intent": "unknown", "query": msg.strip()[:600]}

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
SALES_INTELLIGENCE_PROMPT = """
Sos Fran, vendedor mayorista experto con 20 años de experiencia en motopartes en Argentina.

Tu especialidad: entender clientes en 2-3 mensajes y cerrar ventas de forma natural.

ENTRADA que recibirás:
- conversacion_completa: últimos 20 mensajes
- productos_disponibles: qué tenés para ofrecer
- contexto_cliente: moto habitual, compras previas, carrito actual
- perfil_cliente: cómo es este cliente (si ya lo conocés)

TU TRABAJO:
Analizá la conversación como lo haría un vendedor experto y respondé:

1. ¿QUÉ QUIERE REALMENTE ESTE CLIENTE?
   - No solo qué pidió, sino qué NECESITA
   - ¿Es la pregunta correcta o está confundido?
   - ¿Tiene una necesidad oculta? (ej: pide filtro pero debería cambiar aceite también)

2. ¿DÓNDE ESTÁ EN EL PROCESO DE COMPRA?
   - ¿Está explorando, comparando, o listo para comprar?
   - ¿Qué frenos tiene? (precio, duda técnica, no sabe qué necesita)
   - ¿Qué lo haría comprar AHORA?

3. ¿CÓMO DEBERÍA VENDERLE A ESTE CLIENTE ESPECÍFICO?
   - Según su personalidad: ¿directo o consultivo?
   - Según su expertise: ¿técnico o simple?
   - Según su urgencia: ¿empujar o educar?
   - ¿Qué lenguaje/tono funcionaría mejor?

4. ¿CUÁL ES LA JUGADA ÓPTIMA?
   - ¿Qué productos mostrar? (¿1 opción o 3?)
   - ¿Cómo presentarlos? (precio, calidad, disponibilidad)
   - ¿Qué decir para cerrar? (pregunta, afirmación, oferta)
   - ¿Agregar urgencia/incentivo o no?

IMPORTANTE:
- Usá tu conocimiento de ventas (que ya tenés como LLM)
- NO sigas reglas rígidas, adaptate a ESTE cliente en ESTE momento
- Pensá como vendedor que quiere ayudar Y cerrar la venta
- Si algo no tiene sentido en la conversación, decilo

FORMATO DE SALIDA (JSON):

{
  "analisis_cliente": {
    "necesidad_real": "string - qué necesita de verdad",
    "necesidad_vs_pedido": "string - ¿pidió lo correcto o está confundido?",
    "nivel_urgencia": "string - bajo/medio/alto + por qué",
    "nivel_confianza": "string - desconfiado/neutral/confiado",
    "señales_compra": ["string", "string"],
    "frenos_detectados": ["string", "string"]
  },
  
  "momento_de_venta": {
    "fase": "string - en qué está (explorando/decidiendo/comprando)",
    "probabilidad_cierre": 0.75,
    "que_necesita_para_comprar": "string - qué falta para que cierre",
    "ventana_temporal": "string - cuánto tiempo tenés (ahora/hoy/esta_semana)"
  },
  
  "estrategia_recomendada": {
    "enfoque": "string - consultivo/directo/educativo",
    "tono": "string - cómo hablarle (amigable/profesional/urgente)",
    "productos_a_mostrar": {
      "cantidad": 1,
      "criterio": "string - por qué esa cantidad",
      "orden": "string - cómo ordenarlos (mejor primero, más barato, etc)"
    },
    "como_cerrar": {
      "tipo": "string - pregunta/afirmacion/oferta/validacion",
      "lenguaje": "string - qué decir exactamente (ej: '¿lo agregamos?')",
      "agregar_urgencia": true/false,
      "agregar_valor": "string - qué beneficio destacar"
    }
  },
  
  "intuicion_vendedor": {
    "este_cliente_es": "string - tipo de cliente en pocas palabras",
    "voy_a_cerrar_si": "string - qué tengo que hacer para vender",
    "riesgos": ["string"],
    "oportunidades": ["string"]
  },
  
  "jugada_optima": "string - en 2-3 oraciones, qué haría un vendedor experto acá"
}
"""

PRODUCT_SELECTION_PROMPT = """
Sos el gerente comercial de TERCOM. Tu trabajo: decidir QUÉ productos mostrar y CÓMO para maximizar venta.

ENTRADA:
- analisis_ventas: el análisis del paso anterior
- productos_disponibles: lista completa de opciones (código, nombre, precio, specs)
- restricciones: stock, precios, familias

TU TRABAJO:
Basándote en el análisis de ventas, seleccioná productos y decidí cómo presentarlos.

PENSÁ COMO COMERCIAL:
- Si el cliente es price-sensitive → mostrar opción económica primero
- Si busca calidad → destacar premium
- Si está confundido → dar 2-3 opciones claras con diferencias
- Si está decidido → confirmar su elección y sugerir complementos

NO USAR REGLAS, USAR CRITERIO:
- ¿Este cliente quiere 1 opción o 5?
- ¿Ordeno por precio, calidad, o popularidad?
- ¿Destaco specs técnicos o beneficios prácticos?
- ¿Agrego productos complementarios o no?

FORMATO DE SALIDA:

{
  "productos_seleccionados": [
    {
      "code": "1234/56789-001",
      "presentacion": {
        "orden": 1,
        "highlight": "precio",
        "enfasis": "La más económica - $15.200",
        "beneficio_clave": "Te dura 12.000km fácil",
        "specs_mostrar": ["medida", "marca"],
        "agregar_social_proof": false
      },
      "razon_seleccion": "Cliente busca opción económica"
    }
  ],
  
  "cross_sell": {
    "sugerir": true/false,
    "productos": ["codigo"],
    "momento": "ahora|despues_de_confirmar",
    "como_presentar": "string - ej: 'Aprovechá y llevate el aceite que va con eso'"
  },
  
  "estructura_oferta": {
    "tipo": "lista|comparacion|recomendacion_unica|paquete",
    "razon": "string - por qué esta estructura"
  }
}
"""

SALES_RESPONSE_PROMPT = """
Sos Fran, vendedor de TERCOM. Escribí el mensaje de WhatsApp perfecto para cerrar esta venta.

ENTRADA:
- analisis_ventas: análisis del cliente
- productos_seleccionados: qué productos y cómo presentarlos
- perfil_cliente: personalidad y preferencias (si existe)
- conversacion_previa: contexto

TU TRABAJO:
Escribir el mensaje de venta PERFECTO para ESTE cliente en ESTE momento.

REGLAS DE ORO:
1. Escribí como hablarías naturalmente (sos argentino, mayorista, experto)
2. Adaptate al cliente (su tono, su urgencia, su nivel técnico)
3. SIEMPRE incluí call-to-action claro
4. Asumí la venta (lenguaje assumptivo)
5. Sé breve si el cliente es conciso, detallado si aprecia explicaciones

NO HAGAS:
- Listas genéricas de productos sin contexto
- "Espero que te sirva" / "Cualquier cosa avisame" (pasivo)
- Bombardear con specs si no las pidió
- Sonar como robot o chatbot

SÍ HACE:
- Confirmar que entendiste
- Presentar productos con VALOR (no solo precio)
- Cerrar con pregunta/afirmación que asume compra
- Usar lenguaje del cliente (si dice "che" vos también)

NO PIENSES EN REGLAS, PENSÁ: ¿Qué diría el mejor vendedor de motopartes que conocés?
"""

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
Sos Fran, vendedor mayorista de motopartes en TERCOM (Argentina).

ESTILO DE COMUNICACIÓN:
- Tono: Profesional pero cercano, como un vendedor experto de confianza
- Vocabulario: Argentino natural (usá "che", "dale", "mirá") sin sonar forzado
- Brevedad: Mensajes concretos, máximo 4-5 líneas antes de listar productos
- Proactividad: Siempre cerrá con una acción concreta para el cliente

ESTRUCTURA DE RESPUESTA (seguí este orden):

1. APERTURA (1 línea):
   - Si encontraste lo que busca: "Dale, acá tengo lo que necesitás"
   - Si hay opciones: "Mirá, tengo estas opciones que te pueden servir"
   - Si falta info: "Para buscarte lo justo necesito un dato más"

2. PRODUCTOS (si aplica):
   - Formato ESTRICTO: [NOMBRE] (código [CÓDIGO]) - [PRECIO]
   - Máximo 5 productos en respuesta inicial
   - Si hay más, avisá: "Tengo X más, avisame si querés que te los pase"
   - NUNCA inventes códigos o precios

3. CONSEJO (opcional, 1 línea):
   - Si tiene sentido, agregá tip rápido: "El sintético te dura el doble"
   - Solo si aporta valor comercial

4. CIERRE CON ACCIÓN:
   - Propuesta concreta: "¿Los agregamos al carrito?"
   - O pregunta específica: "¿Es para 110cc o 125cc?"
   - O siguiente paso: "Confirmo stock y te paso el total"

EJEMPLO BUENO:
"Dale, para la Wave 110 tengo:

Filtro Aceite Mann (0956/12345-001) - $8.500
Filtro Aire K&N (0956/12346-002) - $12.300

El K&N te dura más pero ambos van bien. ¿Los cargo al carrito?"

EJEMPLO MALO (no hacer):
"¡Hola! Muchas gracias por tu consulta. He revisado nuestro catálogo y encontré varias opciones interesantes que podrían servirte. A continuación te detallo los productos disponibles con sus características..."
[muy largo, formal, sin acción]

REGLAS DE PRODUCTOS:
- Si el plan interno te pasó products_decision, usá SOLO esos
- Siempre citá código entre paréntesis: (código XXXX/XXXXX-XXX)
- Precios siempre con formato: $X.XXX (punto como separador de miles)
- Si un producto no tiene código en el plan, NO lo menciones

MANEJO DE CASOS ESPECIALES:
- Cliente confuso: Hacé 1 pregunta específica (marca O modelo O año)
- Sin stock exacto: Ofrecé alternativas equivalentes
- Productos dudosos: Aclaralo: "Puede ser que busques X, si no avisame"

{BUSINESS_CONTEXT}
"""

# Compatibilidad hacia atrás
CITATION_ENFORCED_PROMPT = CUSTOMER_OUTPUT_PROMPT

INTERNAL_REASONING_PROMPT = """
Sos el sistema de razonamiento interno de Fran. Tu trabajo es ANALIZAR y PLANIFICAR, no hablar con el cliente.

ENTRADA que recibirás:
- mensaje_usuario: lo que escribió
- productos_permitidos: lista de productos del catálogo (SOLO podés elegir de acá)
- historial: conversaciones previas
- memoria_viva: contexto del cliente (moto habitual, búsquedas previas, carrito)
- cart_state: productos actuales en el carrito
- pending_action: si hay alguna acción esperando confirmación

TU TAREA (paso a paso):

1. INTERPRETAR EL PEDIDO (con expansión semántica):
   
   a) SINÓNIMOS Y JERGA ARGENTINA:
      - "gomas" / "cubiertas" / "cauchos" → buscar NEUMÁTICOS
      - "amortiguadores" / "shocks" → buscar SUSPENSIÓN
      - "bujías" / "candelas" → buscar BUJÍAS
      - "batería" / "acumulador" → buscar BATERÍAS
      - "filtro" puede ser: filtro de aceite, filtro de aire, filtro de nafta
      - Si el usuario usa jerga, normalizá al término técnico del catálogo
   
   b) CONTEXTO IMPLÍCITO:
      - Si dice solo una categoría ("cubiertas", "espejos") pero en memoria_viva
        hay una moto reciente (ej: "fz16"), ASUMIR que es para esa moto
      - Si dice "y [producto]?" está pidiendo otro producto para la MISMA moto

   REGLA DE CONSISTENCIA:
      - Usá SIEMPRE los mismos mapeos de sinónimos (gomas → neumaticos/cubiertas, etc.).
      - Para referencias a productos anteriores, usá únicamente memoria_viva.allowed_products_snapshot en el orden recibido.
   
   c) REFERENCIAS A PRODUCTOS ANTERIORES:
      - "los tres" / "esos tres" → primeros 3 de memoria_viva.allowed_products_snapshot
      - "los que me mostraste" → todos de memoria_viva.allowed_products_snapshot
      - "el primero" / "el segundo" → producto en esa posición del snapshot
      - "el más barato" / "el más caro" → ordenar por precio
      - "todos" / "todos esos" → todos los productos del último mensaje

2. CONSTRUIR QUERY DE BÚSQUEDA ENRIQUECIDA:
   
   Si el mensaje original es ambiguo o incompleto, construí una query mejorada:
   
   Ejemplos:
   - Usuario: "cubiertas"
     memoria_viva.most_recent_bike: "yamaha fz16"
     → new_query: "neumaticos yamaha fz16"
   
   - Usuario: "gomas para la moto"
     memoria_viva.most_recent_bike: "honda wave 110"
     → new_query: "neumaticos honda wave 110"
   
   - Usuario: "y espejos?"
     memoria_viva.last_search_query: "filtros yamaha fz16"
     → new_query: "espejos yamaha fz16"

3. EVALUAR PRODUCTOS DISPONIBLES:
   - Revisá productos_permitidos uno por uno
   - Para CADA producto relevante, decidí:
     * ¿Coincide con lo que busca? (score 0-100)
     * ¿Qué cantidad tiene sentido? (default: 1)
     * ¿Por qué lo recomendarías? (1 frase)

4. TOMAR DECISIÓN:
   
   a) SI encontraste productos que encajan bien (score > 70):
      → status: "OK"
      → products_decision: [lista de productos seleccionados con qty y razón]
      → extracted_entities: {términos normalizados}
     
   b) SI el usuario hace referencia a productos anteriores:
      → status: "OK"
      → products_decision: [productos de memoria_viva.allowed_products_snapshot]
      → reason: "Usuario solicitó productos de búsqueda anterior"
     
   c) SI productos son dudosos (score 50-70):
      → status: "OK" (igual mostrá opciones pero avisá en "reason")
      → products_decision: [los mejores que tenés]
     
   d) SI necesitás refinar búsqueda (productos irrelevantes):
      → status: "NEED_REQUERY"
      → new_query: query mejorada usando sinónimos + contexto de memoria_viva
     
   e) SI falta info crítica (no sabés marca/modelo/categoría):
      → status: "NEED_CLARIFICATION"
      → message_to_user_if_clarification: pregunta específica

FORMATO DE SALIDA (JSON puro, sin markdown):
{
  "status": "OK|NEED_REQUERY|NEED_CLARIFICATION",
  "reason": "string explicando la decisión interna",
  "semantic_expansion": {
    "original_terms": ["gomas"],
    "normalized_terms": ["neumaticos", "cubiertas"],
    "context_added": "yamaha fz16"
  },
  "products_decision": [
    {
      "code": "1234/56789-012",
      "name": "nombre del producto",
      "qty": 1,
      "why": "razón comercial en 1 frase",
      "confidence_score": 85
    }
  ],
  "new_query": "query refinada (solo si NEED_REQUERY)",
  "message_to_user_if_clarification": "pregunta (solo si NEED_CLARIFICATION)",
  "extracted_entities": {
    "brand": "yamaha",
    "model": "fz16",
    "category": "neumaticos",
    "original_category": "gomas",
    "displacement_cc": "150"
  },
  "reference_resolution": {
    "type": "previous_search|implicit_quantity|none",
    "resolved_to": "3 productos de búsqueda anterior de espejos"
  },
  "pending_actions": [
    {"type": "save_moto_context", "brand": "yamaha", "model": "fz16"}
  ],
  "memory_updates": {
    "most_recent_bike": "Yamaha FZ16",
    "search_pattern": "busca repuestos regularmente"
  }
}

EJEMPLOS COMPLETOS:

Ejemplo 1 - Sinónimo:
mensaje_usuario: "tenes gomas para una fz16?"
memoria_viva: {most_recent_bike: ""}
productos_permitidos: [neumaticos yamaha fz16...]

Respuesta:
{
  "status": "NEED_REQUERY",
  "reason": "Usuario usó 'gomas' (sinónimo de neumáticos). Busco con término normalizado.",
  "semantic_expansion": {
    "original_terms": ["gomas"],
    "normalized_terms": ["neumaticos", "cubiertas"],
    "context_added": "yamaha fz16"
  },
  "new_query": "neumaticos yamaha fz16",
  "extracted_entities": {
    "brand": "yamaha",
    "model": "fz16",
    "category": "neumaticos",
    "original_category": "gomas"
  }
}

Ejemplo 2 - Contexto implícito:
mensaje_usuario: "y espejos?"
memoria_viva: {
  most_recent_bike: "yamaha fz16",
  last_search_query: "cubiertas fz16"
}
productos_permitidos: [espejos yamaha fz16...]

Respuesta:
{
  "status": "NEED_REQUERY",
  "reason": "Usuario pidió otra categoría ('espejos') para la misma moto del contexto",
  "semantic_expansion": {
    "original_terms": ["espejos"],
    "normalized_terms": ["espejos", "retrovisores"],
    "context_added": "yamaha fz16"
  },
  "new_query": "espejos yamaha fz16",
  "extracted_entities": {
    "brand": "yamaha",
    "model": "fz16",
    "category": "espejos"
  }
}

Ejemplo 3 - Referencia a productos anteriores:
mensaje_usuario: "sumas los tres al carrito"
memoria_viva: {
  allowed_products_snapshot: [
    {code: "1234/00001-001", name: "Espejo izq FZ16", price: 5000},
    {code: "1234/00002-002", name: "Espejo der FZ16", price: 5000},
    {code: "1234/00003-003", name: "Soporte espejo", price: 2500}
  ]
}

Respuesta:
{
  "status": "OK",
  "reason": "Usuario solicitó agregar los 3 productos mostrados anteriormente",
  "reference_resolution": {
    "type": "implicit_quantity",
    "resolved_to": "primeros 3 productos de búsqueda anterior"
  },
  "products_decision": [
    {
      "code": "1234/00001-001",
      "name": "Espejo izq FZ16",
      "qty": 1,
      "why": "Usuario pidió 'los tres' refiriéndose a la búsqueda anterior",
      "confidence_score": 100
    },
    {
      "code": "1234/00002-002",
      "name": "Espejo der FZ16",
      "qty": 1,
      "why": "Segundo producto de la lista anterior",
      "confidence_score": 100
    },
    {
      "code": "1234/00003-003",
      "name": "Soporte espejo",
      "qty": 1,
      "why": "Tercer producto de la lista anterior",
      "confidence_score": 100
    }
  ],
  "pending_actions": [
    {
      "type": "add_to_cart",
      "products": ["1234/00001-001", "1234/00002-002", "1234/00003-003"],
      "qty_each": 1
    }
  ]
}
"""

META_COGNITION_PROMPT = """
Sos un experto en análisis de conversaciones comerciales B2B. Tu trabajo es entender la INTENCIÓN REAL del cliente.

ENTRADA:
- mensaje_actual: lo que acaba de escribir
- historial_completo: últimos 10 mensajes (usuario + bot)
- contexto: moto habitual, búsquedas previas, carrito

TU TRABAJO:
Analizá la conversación como un vendedor experto y respondé estas preguntas:

1. ESTADO EMOCIONAL / SATISFACCIÓN:
   - ¿Está satisfecho con lo que le mostramos antes?
   - ¿Está confundido o frustrado?
   - ¿Está explorando opciones o ya decidió?

2. CONTINUIDAD:
   - ¿Es un tema NUEVO o CONTINUACIÓN del anterior?
   - Si es continuación: ¿está profundizando o cambiando de ángulo?
   - Si es nuevo: ¿es para la misma moto o cambió de contexto?

3. INTENCIÓN REAL:
   - ¿Qué quiere LOGRAR con este mensaje?
   - ¿Hay alguna frustración oculta? (ej: "me refería a..." = "no me entendiste")
   - ¿Está listo para comprar o todavía investigando?

4. NIVEL DE EXPERTISE:
   - ¿Usa términos técnicos correctos o jerga/sinónimos?
   - ¿Es mecánico/taller o usuario final?
   - ¿Qué nivel de detalle necesita?

FORMATO DE SALIDA (JSON):
{
  "satisfaction_level": "satisfied|neutral|confused|frustrated",
  "conversation_flow": "new_topic|continuation_same|continuation_pivot|clarification",
  "intent_type": "exploration|purchase_ready|technical_question|complaint",
  "customer_profile": {
    "expertise": "mechanic|enthusiast|casual_user",
    "confidence": "high|medium|low",
    "bike_context": "same_as_before|new_bike|unknown"
  },
  "hidden_signals": {
    "frustration_detected": true/false,
    "reason": "string explicando qué pasó",
    "suggested_recovery": "string con cómo recuperar la conversación"
  },
  "recommended_approach": "show_more_options|clarify_need|confirm_understanding|proceed_with_last_context"
}

EJEMPLOS:

Ejemplo 1 - Frustración oculta:
historial: [
  {bot: "Te paso esta cubierta para FZ16: [1 producto]"},
  {user: "Me refería a gomas para la moto, cubiertas"}
]

Respuesta:
{
  "satisfaction_level": "confused",
  "conversation_flow": "clarification",
  "intent_type": "clarification",
  "customer_profile": {
    "expertise": "casual_user",
    "confidence": "low",
    "bike_context": "same_as_before"
  },
  "hidden_signals": {
    "frustration_detected": true,
    "reason": "Usuario usó sinónimo ('gomas') pero el bot solo mostró 1 producto. Dice 'me refería a' indicando que siente que no lo entendimos. En realidad SÍ le mostramos lo correcto, pero probablemente esperaba MÁS opciones.",
    "suggested_recovery": "Reconocer que le mostramos lo correcto pero ofrecer MÁS variedad: 'Claro, esas son las cubiertas/gomas disponibles. Te paso más opciones con diferentes medidas y marcas'"
  },
  "recommended_approach": "show_more_options"
}

Ejemplo 2 - Continuación natural:
historial: [
  {bot: "Acá tenés 3 opciones de filtros para FZ16"},
  {user: "y espejos?"}
]

Respuesta:
{
  "satisfaction_level": "satisfied",
  "conversation_flow": "continuation_same",
  "intent_type": "exploration",
  "customer_profile": {
    "expertise": "mechanic",
    "confidence": "high",
    "bike_context": "same_as_before"
  },
  "hidden_signals": {
    "frustration_detected": false,
    "reason": "Está armando pedido completo para FZ16, va por partes",
    "suggested_recovery": null
  },
  "recommended_approach": "proceed_with_last_context"
}

Ejemplo 3 - Usuario listo para comprar:
historial: [
  {bot: "Te paso 3 espejos para FZ16: [lista]"},
  {user: "Tenía, me sumas los tres al carrito?"}
]

Respuesta:
{
  "satisfaction_level": "satisfied",
  "conversation_flow": "continuation_same",
  "intent_type": "purchase_ready",
  "customer_profile": {
    "expertise": "mechanic",
    "confidence": "high",
    "bike_context": "same_as_before"
  },
  "hidden_signals": {
    "frustration_detected": false,
    "reason": "Está conforme con opciones, quiere avanzar a checkout",
    "suggested_recovery": null
  },
  "recommended_approach": "confirm_cart_addition"
}
"""

PLANNING_PROMPT_V2 = f"""
{INTERNAL_REASONING_PROMPT}

NUEVO: Recibirás también un análisis de meta-cognición:

meta_analysis: {{
  "satisfaction_level": "...",
  "recommended_approach": "...",
  "hidden_signals": {{...}}
}}

Usá esta info para:
1. Si hay frustración detectada, seguí el "suggested_recovery"
2. Si el approach es "show_more_options", buscá más productos (top 10 en vez de top 3)
3. Si el approach es "clarify_need", generá NEED_CLARIFICATION con pregunta específica
4. Si el approach es "confirm_understanding", incluí en el plan una validación explícita
5. Mantené consistencia con los ejemplos: sinónimos normalizados y referencias resueltas con allowed_products_snapshot.

Ejemplo:
Si meta_analysis dice:
{{
  "satisfaction_level": "confused",
  "hidden_signals": {{
    "suggested_recovery": "Mostrar más variedad de cubiertas"
  }}
}}

Entonces tu plan debe:
{{
  "status": "NEED_REQUERY",
  "new_query": "neumaticos yamaha fz16 todas las marcas",
  "reason": "Usuario esperaba más opciones, amplío búsqueda",
  "response_tone": "empathetic_recovery"
}}
"""

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


def call_sales_json_llm(prompt: str, payload: dict, model: str = None, temperature: float = 0.35, max_tokens: int = 700):
    model = model or MODEL_REASONING
    try:
        with openai_sem:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )

        raw = (resp.choices[0].message.content or "").strip()
        parsed = _safe_json_parse(_extract_json_block(raw))
        return parsed if isinstance(parsed, dict) else None
    except Exception as e:
        logger.error(f"call_sales_json_llm error: {e}")
        return None


def call_sales_text_llm(prompt: str, payload: dict, model: str = None, temperature: float = 0.35, max_tokens: int = 500):
    model = model or MODEL_RESPONSE
    try:
        with openai_sem:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )

        return (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.error(f"call_sales_text_llm error: {e}")
        return ""


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


def generate_meta_cognition(mensaje_actual: str, history: list, contexto: dict) -> dict:
    try:
        payload = {
            "mensaje_actual": (mensaje_actual or "")[:600],
            "historial_completo": history[-10:] if history else [],
            "contexto": contexto or {},
        }

        with openai_sem:
            resp = client.chat.completions.create(
                model=MODEL_REASONING,
                messages=[
                    {"role": "system", "content": META_COGNITION_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                temperature=0.2,
                max_tokens=300,
            )

        raw = (resp.choices[0].message.content or "").strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start:end + 1]

        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.error(f"generate_meta_cognition error: {e}")
        return {}


def validate_reasoning_json(raw_text):
    parsed = _safe_json_parse(raw_text or "")
    if not isinstance(parsed, dict):
        logger.error(f"JSON parse falló: contenido inválido, raw text: {str(raw_text)[:300]}")
        return None

    status = parsed.get("status")
    if status not in {"OK", "NEED_REQUERY", "NEED_CLARIFICATION"}:
        logger.error(f"JSON de razonamiento con status inválido: {parsed}")
        return None

    if status == "NEED_REQUERY" and not parsed.get("new_query"):
        return None

    if status == "NEED_CLARIFICATION" and not parsed.get("message_to_user_if_clarification"):
        return None

    if not isinstance(parsed.get("products_decision", []), list):
        parsed["products_decision"] = []

    return parsed


def ejecutar_plan_interno(parsed_plan, phone, productos_permitidos):
    """
    Convierte el plan JSON en acciones concretas usando sólo productos permitidos.
    Ejecuta side-effects como guardar contexto de moto y devuelve un snapshot
    seguro para el segundo paso de LLM.
    """
    try:
        if not parsed_plan or parsed_plan.get("status") != "OK":
            return None

        productos_permitidos_map = {
            str(p.get("code")): p for p in (productos_permitidos or []) if p.get("code")
        }

        # ← NUEVO: Manejar referencias a productos anteriores
        reference_resolution = parsed_plan.get("reference_resolution", {})
        if reference_resolution.get("type") in ["previous_search", "implicit_quantity"]:
            # El LLM ya resolvió la referencia en products_decision
            # Solo validamos que esos códigos existen
            logger.info(f"Resolviendo referencia: {reference_resolution.get('resolved_to')}")

        productos_seleccionados = []
        for decision in parsed_plan.get("products_decision", []):
            code = decision.get("code")
            if not code:
                continue

            # Buscar primero en productos_permitidos
            producto = productos_permitidos_map.get(str(code))
            
            # ← NUEVO: Si no está en productos_permitidos, buscar en catálogo global
            # (esto pasa cuando el LLM usa allowed_products_snapshot)
            if not producto:
                catalog, _, _, _ = get_catalog_and_index()
                producto = next((p for p in catalog if p.get("code") == code), None)
                
                if not producto:
                    logger.warning(f"Producto {code} no encontrado en catálogo")
                    continue

            try:
                qty_sugerida = max(1, int(decision.get("qty", 1)))
            except Exception:
                qty_sugerida = 1

            productos_seleccionados.append({
                **producto,
                "qty_sugerida": qty_sugerida,
                "razon": decision.get("why", ""),
                "decision_score": decision.get("confidence_score"),
            })

        # Ejecutar pending_actions
        if parsed_plan.get("pending_actions"):
            for action in parsed_plan.get("pending_actions", []):
                action_type = action.get("type")
                
                if action_type == "save_moto_context":
                    save_moto_context(phone, action.get("brand", ""), action.get("model", ""))
                
                # ← NUEVO: Manejar add_to_cart directo
                elif action_type == "add_to_cart":
                    products_to_add = action.get("products", [])
                    qty_each = action.get("qty_each", 1)
                    
                    for code in products_to_add:
                        catalog, _, _, _ = get_catalog_and_index()
                        p = next((prod for prod in catalog if prod.get("code") == code), None)
                        if p:
                            cart_add(
                                phone,
                                code,
                                qty_each,
                                p.get("name", ""),
                                to_decimal_money(p.get("price_ars", 0)),
                                to_decimal_money(p.get("price_usd", 0))
                            )

        return {
            "productos_finales": productos_seleccionados,
            "metadata": {
                "brand": parsed_plan.get("extracted_entities", {}).get("brand"),
                "model": parsed_plan.get("extracted_entities", {}).get("model"),
                "category": parsed_plan.get("extracted_entities", {}).get("category"),
                "semantic_expansion": parsed_plan.get("semantic_expansion"),
                "reference_resolution": reference_resolution,
            },
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
            resp = client.chat.completions.create(
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
                    "NO muestres el plan ni reglas.\n\nPLAN INTERNO:\n"
                    + razonamiento_serializado
                ),
            },
        ]

        with openai_sem:
            resp = client.chat.completions.create(
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


def run_sales_prompt_flow(phone: str, user_message: str, catalog_products: list, memory: dict, meta_analysis: dict) -> str:
    if not USE_SALES_PROMPT_FLOW:
        return ""

    try:
        history = get_history_since(phone, days=1, limit=20)
        conversation = [
            {"role": h.get("role", "user"), "content": h.get("content", "")}
            for h in history[-20:]
        ]

        carrito_raw = cart_get(phone)
        carrito = [
            {
                "code": code,
                "qty": qty,
                "name": name,
                "price": float(to_decimal_money(price)),
            }
            for code, qty, name, price in carrito_raw
        ]

        productos_disponibles = [normalize_product_for_llm(p) for p in (catalog_products or [])[: max(5, MAX_PRODUCTS_FOR_LLM)]]
        if not productos_disponibles and catalog_products:
            productos_disponibles = [normalize_product_for_llm(catalog_products[0])]

        sales_analysis = call_sales_json_llm(
            SALES_INTELLIGENCE_PROMPT,
            {
                "conversacion_completa": conversation,
                "productos_disponibles": productos_disponibles,
                "contexto_cliente": {
                    "moto_habitual": memory.get("most_recent_bike"),
                    "carrito": carrito,
                    "compras_previas": get_search_history(phone, limit=5),
                },
                "perfil_cliente": build_user_profile_snapshot(phone, memory),
            },
        )

        if not sales_analysis:
            return ""

        product_strategy = call_sales_json_llm(
            PRODUCT_SELECTION_PROMPT,
            {
                "analisis_ventas": sales_analysis,
                "productos_disponibles": productos_disponibles,
                "restricciones": {"max_options": MAX_PRODUCTS_FOR_LLM},
            },
        )

        if not product_strategy:
            return ""

        reply = call_sales_text_llm(
            SALES_RESPONSE_PROMPT,
            {
                "analisis_ventas": sales_analysis,
                "productos_seleccionados": product_strategy,
                "perfil_cliente": build_user_profile_snapshot(phone, memory),
                "conversacion_previa": meta_analysis or {},
            },
            temperature=0.4,
        )

        return reply
    except Exception as e:
        logger.error(f"run_sales_prompt_flow error: {e}")
        return ""


def generate_smart_ai_reply_v2(phone, user_message, catalog_products, execution_context, system_prompt=None):
    try:
        history = get_history_since(phone, days=1, limit=12)
        intent_info = execution_context.get("intent_details", {"intent": execution_context.get("intent_detected", "unknown")})
        memory = build_enriched_context(phone, user_message, intent_info, catalog_products)
        historial_compacto = [
            {"role": h["role"], "content": h["content"][:400]}
            for h in history[-10:]
        ]
        meta_context = {
            "most_recent_bike": memory.get("most_recent_bike"),
            "last_search_query": memory.get("last_search_query"),
            "cart_state": memory.get("cart_state", []),
        }
        meta_analysis = generate_meta_cognition(user_message, historial_compacto, meta_context)
        contexto = {
            "mensaje_usuario": user_message,
            "intent": intent_info.get("intent", execution_context.get("intent_detected", "unknown")),
            "search_query": execution_context.get("search_query"),
            "warnings": execution_context.get("warnings", []),
            "historial": [{"role": h["role"], "content": h["content"]} for h in history[-8:]],
            "metadata_catalogo": {"productos_total": len(catalog_products or [])},
            "ultimo_contexto": execution_context,
            "memoria_viva": memory,
            "pending_actions": memory.get("pending_action"),
            "cart_state": memory.get("cart_state", []),
            "most_recent_bike": memory.get("most_recent_bike", ""),
            "meta_analysis": meta_analysis,
        }

        fast_reply = None
        if USE_SALES_PROMPT_FLOW:
            fast_reply = run_sales_prompt_flow(phone, user_message, catalog_products, memory, meta_analysis)
            if fast_reply:
                logger.info("generate_smart_ai_reply_v2: respuesta vía sales_prompt_flow")
                return fast_reply
            logger.info("generate_smart_ai_reply_v2: sales_prompt_flow vacío, uso doble LLM como fallback")
        else:
            logger.info("generate_smart_ai_reply_v2: sales_prompt_flow desactivado, uso doble LLM")

        productos_permitidos = catalog_products or []
        plan_interno = pensar_con_llm(
            system_prompt or PLANNING_PROMPT_V2,
            contexto,
            productos_permitidos,
        )

        parsed_plan = validate_reasoning_json(plan_interno)

        max_requery_attempts = 2
        requery_count = 0
        while parsed_plan and parsed_plan.get("status") == "NEED_REQUERY" and requery_count < max_requery_attempts:
            requery_count += 1
            nueva_query = parsed_plan.get("new_query")
            try:
                semantic_results = hybrid_search(nueva_query, phone=phone, top_k=MAX_SEARCH_RESULTS)
                if isinstance(semantic_results, dict):
                    semantic_results = []
                productos_permitidos = [p for p, _ in semantic_results][:MAX_PRODUCTS_FOR_LLM]
                execution_context["search_query"] = nueva_query
                memory = build_enriched_context(phone, user_message, intent_info, productos_permitidos)
                contexto.update({
                    "search_query": nueva_query,
                    "memoria_viva": memory,
                    "metadata_catalogo": {"productos_total": len(productos_permitidos)},
                    "cart_state": memory.get("cart_state", []),
                    "meta_analysis": meta_analysis,
                })
                plan_interno = pensar_con_llm(
                    system_prompt or PLANNING_PROMPT_V2,
                    contexto,
                    productos_permitidos,
                )
                parsed_plan = validate_reasoning_json(plan_interno)
            except Exception as e:
                logger.error(f"Re-búsqueda fallida: {e}")
                parsed_plan = None

        if not parsed_plan:
            logger.warning("Fallback: razonamiento inválido, usando catálogo real")
            if productos_permitidos:
                listado = format_search_results(productos_permitidos[:5])
                return (
                    "Te dejo opciones reales del catálogo mientras confirmo bien tu pedido:\n\n"
                    f"{listado}\n\n"
                    "¿Alguna te sirve o querés que refine por marca/modelo/categoría?"
                )
            return "Necesito un dato más para ayudarte: marca, modelo o categoría de la moto."

        status = parsed_plan.get("status")
        if status == "NEED_CLARIFICATION":
            return parsed_plan.get("message_to_user_if_clarification") or "Pasame marca/modelo/año así lo busco bien."

        plan_ejecutado = ejecutar_plan_interno(parsed_plan, phone, productos_permitidos)
        razonamiento_final = json.dumps({**parsed_plan, **(plan_ejecutado or {})}, ensure_ascii=False)
        respuesta = responder_con_llm(CUSTOMER_OUTPUT_PROMPT, razonamiento_final)
        return respuesta or "Uy, tuve un problema. ¿Me repetís?"
    except Exception as e:
        logger.error(f"generate_smart_ai_reply_v2 error: {e}")
        return "Estoy ajustando el sistema, ¿me repetís el pedido con marca y modelo?"


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
            resp = client.chat.completions.create(
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

# =========================================================
# ORQUESTADOR PRINCIPAL – VERSIÓN 3.14
# =========================================================
def orquestar_fran(mensaje_usuario, phone):
    """
    Orquestador unificado de Fran.

    Gestiona rate limiting, detección de intención, búsqueda, memoria y el doble
    paso de LLM (razonamiento interno + respuesta conversacional). Siempre deja
    registrado el historial y actualiza fases de venta.
    """
    start_time = time.time()
    user_message = sanitize_input(mensaje_usuario or "", max_length=1500)
    save_message(phone, user_message, "user")

    if not rate_limit_check(phone):
        reply = "Demasiados mensajes, esperá un minuto."
        save_message(phone, reply, "assistant")
        return reply

    intent_data = detect_intent_llm(user_message)
    intent = intent_data.get("intent", "unknown")
    raw_query_for_search = intent_data.get("query") or user_message
    current_phase = get_sales_phase(phone)
    last_search_data = get_last_search(phone) or {}
    last_search_query = (last_search_data.get("query") or "").strip()

    execution_context = {
        "intent_detected": intent,
        "intent_details": intent_data,
        "search_query": raw_query_for_search,
        "search_executed": False,
        "products_found": 0,
        "products_shown_to_llm": 0,
        "filters_applied": [],
        "quality_assessment": None,
        "will_send_chunks": False,
        "chunk_info": None,
        "warnings": []
    }

    # ------------------------------------------------------
    # Confirmar / cancelar acciones pendientes (CONTEXTO 2.0)
    # ------------------------------------------------------
    pending = get_pending_action(phone)

    if intent == "confirmation" and pending:
        if pending.get("action_type") == "add_each_quantity":
            reply = apply_add_each_quantity_pending(phone, pending)
            clear_pending_action(phone)
            save_message(phone, reply, "assistant")
            log_interaction(phone, user_message, "confirmation_pending_add_each", 0)
            log_performance(phone, "confirmation_pending_add_each", time.time()-start_time, 0)
            update_sales_phase_from_intent(phone, intent)
            return reply

    if intent == "negation" and pending:
        clear_pending_action(phone)
        reply = "Listo, no avanzo con eso."
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "negation_pending", 0)
        log_performance(phone, "negation_pending", time.time()-start_time, 0)
        update_sales_phase_from_intent(phone, intent)
        return reply

    # ------------------------------------------------------
    # Intenciones directas simples
    # ------------------------------------------------------
    if intent == "confirmation":
        phase_responses = {
            "checkout": "Perfecto, definimos pago o envío y lo cierro.",
            "payment": "Genial, espero el comprobante y te confirmo.",
            "shipping": "Dale, coordinemos la logística. ¿Moto CABA o despacho interior?",
            "cart": "Listo, sigo ajustando el carrito con lo que me digas."
        }
        reply = phase_responses.get(current_phase, "Perfecto, sigo atento. Decime si querés que agregue algo más.")
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "confirmation", 0)
        log_performance(phone, "confirmation", time.time()-start_time, 0)
        update_sales_phase_from_intent(phone, intent)
        return reply

    if intent == "negation":
        reply = "Sin drama, quedo atento si querés hacer otro pedido."
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "negation", 0)
        log_performance(phone, "negation", time.time()-start_time, 0)
        update_sales_phase_from_intent(phone, intent)
        return reply

    if intent == "small_talk":
        reply = "¡Hola! Soy Fran de TERCOM, ¿en qué te puedo ayudar?"
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "small_talk", 0)
        log_performance(phone, "small_talk", time.time()-start_time, 0)
        update_sales_phase_from_intent(phone, intent)
        return reply

    if intent == "view_cart":
        items = cart_get(phone)
        if not items:
            reply = "Tu carrito está vacío."
        else:
            total, _ = cart_totals(phone)
            lines = ["TU CARRITO:\n"]
            for code, q, name, price in items:
                subtotal = (price * q).quantize(Decimal("0.01"))
                lines.append(f"- {q}x {name[:40]} = {format_price(subtotal)}")
            lines.append(f"\nTOTAL: {format_price(total)}")
            reply = "\n".join(lines)
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "view_cart", 0)
        log_performance(phone, "view_cart", time.time()-start_time, 0)
        update_sales_phase_from_intent(phone, intent)
        return reply

    if intent == "empty_cart":
        cart_clear(phone)
        reply = "Listo, vacié tu carrito."
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "empty_cart", 0)
        log_performance(phone, "empty_cart", time.time()-start_time, 0)
        update_sales_phase_from_intent(phone, intent)
        return reply

    # ------------------------------------------------------
    # Pedidos implícitos sobre la última búsqueda
    # ------------------------------------------------------
    implicit = detect_implicit_cart_action(user_message, phone)
    if implicit:
        products = implicit["products"][:MAX_PRODUCTS_FOR_LLM]
        qty = implicit["quantity"]
        total = sum(to_decimal_money(p["price_ars"]) * qty for p in products)
        reply = (
            f"Dale! Te preparo {qty} unidad(es) de cada uno de los últimos productos que te mostré.\n"
            f"Total aprox: {format_price(total)}\n\n"
            "¿Confirmás? (decime 'si' o 'dale')"
        )
        save_pending_action(phone, "add_each_quantity", {"qty": qty, "products": products}, context=f"{qty} de cada uno")
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "implicit_cart", len(products))
        log_performance(phone, "implicit_cart", time.time()-start_time, len(products))
        return reply

    if intent == "cart_action":
        reply = handle_cart_action(phone, user_message)
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "cart_action", 0)
        log_performance(phone, "cart_action", time.time()-start_time, 0)
        update_sales_phase_from_intent(phone, intent)
        return reply

    if intent == "order_flow":
        reply = build_checkout_response(phone)
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "order_flow", 0)
        log_performance(phone, "order_flow", time.time()-start_time, 0)
        update_sales_phase_from_intent(phone, intent)
        return reply

    if intent == "payment":
        reply = build_payment_response(phone)
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "payment", 0)
        log_performance(phone, "payment", time.time()-start_time, 0)
        update_sales_phase_from_intent(phone, intent)
        return reply

    if intent == "shipping":
        reply = build_shipping_response(phone)
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "shipping", 0)
        log_performance(phone, "shipping", time.time()-start_time, 0)
        update_sales_phase_from_intent(phone, intent)
        return reply

    if intent == "tech_expert":
        reply = build_tech_expert_answer(phone, user_message)
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "tech_expert", 0)
        log_performance(phone, "tech_expert", time.time()-start_time, 0)
        update_sales_phase_from_intent(phone, intent)
        return reply

    # ------------------------------------------------------
    # Búsqueda en catálogo
    # ------------------------------------------------------
    products = []
    query_for_search = raw_query_for_search
    corrections = []

    if intent in {"product_search", "unknown"}:
        if raw_query_for_search and len(raw_query_for_search.split()) < 4 and last_search_query:
            query_for_search = f"{last_search_query} {raw_query_for_search}".strip()
            execution_context["warnings"].append("query_refined_with_last_search")
            logger.info(f"Query refinada con contexto previo: '{query_for_search}'")

        query_for_search, corrections = autocorrect_keywords(query_for_search)
        if corrections:
            execution_context["warnings"].append(f"Autocorrect: {', '.join(corrections)}")
            logger.info(f"Autocorrect aplicado: {', '.join(corrections)}")

        execution_context["search_query"] = query_for_search

        semantic_results = hybrid_search(query_for_search, phone=phone, top_k=MAX_SEARCH_RESULTS)

        if isinstance(semantic_results, dict):
            if semantic_results.get("error") == "too_many_combinations":
                reply = semantic_results.get("message") or "Hay demasiadas combinaciones, pasame una sola moto o categoría."
                save_message(phone, reply, "assistant")
                log_interaction(phone, user_message, "too_many_combinations", 0)
                log_performance(phone, intent, time.time()-start_time, 0)
                return reply
            execution_context["search_executed"] = True
            total_found = sum(len(v) for v in (semantic_results.get("results") or {}).values())
            execution_context["products_found"] = total_found
            reply = format_multi_search_response(semantic_results)
            if reply:
                save_message(phone, reply, "assistant")
                log_interaction(phone, user_message, intent, total_found)
                log_performance(phone, intent, time.time()-start_time, total_found)
                update_sales_phase_from_intent(phone, intent)
                return reply
            semantic_results = []

        products = [p for p, _ in semantic_results]

        execution_context["search_executed"] = True
        execution_context["products_found"] = len(products)

        # Filtro por relevancia solo cuando hay algo razonable que mostrar
        if products and intent in {"product_search"}:
            original_len = len(products)
            filtered_products = filter_by_relevance(query_for_search, products, min_score=RELEVANCE_MIN_SCORE)

            logger.info(f"Relevance filter: {len(filtered_products)}/{original_len} productos relevantes")

            if filtered_products:
                products = filtered_products
                execution_context["filters_applied"].append(f"relevance: {len(filtered_products)}/{original_len} passed")
            else:
                logger.warning(f"Sin productos claramente relevantes para query: {query_for_search}")

                top_candidates = semantic_results[:10]
                top_3 = top_candidates[:3]

                suggestions = "\n".join([
                    f"- {p.get('name', '')} ({p.get('code', '')}) - {format_price(p.get('price_ars', 0))}"
                    for p, score in top_3
                ])

                reply = (
                    "No encontré coincidencia perfecta con lo que pediste, pero tengo opciones que se acercan:\n\n"
                    f"{suggestions}\n\n"
                    "Si querés algo más puntual, pasame más detalles (marca/modelo/año) así lo clavo mejor."
                )
                save_message(phone, reply, "assistant")
                log_interaction(phone, user_message, "no_relevant_results", 0)
                log_performance(phone, intent, time.time()-start_time, 0)
                return reply

        quality_assessment = assess_context_quality(query_for_search, products)
        execution_context["quality_assessment"] = quality_assessment

        logger.info(f"Quality assessment: {quality_assessment}")

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
                suggestions = "\n".join([
                    f"- {p.get('name', '')} ({p.get('code', '')}) - {format_price(p.get('price_ars', 0))}"
                    for p in top_products
                ])
                reply = (
                    "No encontré coincidencia perfecta, pero tengo estas opciones que se acercan:\n\n"
                    f"{suggestions}\n\n"
                    "O dame un poco más de detalle (marca/modelo/año) y afinamos la búsqueda."
                )

            save_message(phone, reply, "assistant")
            log_interaction(phone, user_message, f"low_quality_{quality_assessment['reason']}", 0)
            log_performance(phone, intent, time.time()-start_time, 0)
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

        execution_context["products_shown_to_llm"] = min(len(products), MAX_PRODUCTS_FOR_LLM)

        if intent == "product_search" and len(products) > MAX_PRODUCTS_FOR_LLM:
            execution_context["will_send_chunks"] = True
            num_chunks = (len(products) + PRODUCTS_PER_CHUNK - 1) // PRODUCTS_PER_CHUNK
            execution_context["chunk_info"] = {
                "total_chunks": num_chunks,
                "products_per_chunk": PRODUCTS_PER_CHUNK,
                "total_products": len(products)
            }

    # ------------------------------------------------------
    # Respuesta del modelo
    # ------------------------------------------------------
    reply = generate_smart_ai_reply_v2(
        phone,
        user_message,
        products[:MAX_PRODUCTS_FOR_LLM],
        execution_context,
        system_prompt=CITATION_ENFORCED_PROMPT
    )

    # ------------------------------------------------------
    # Validación post-LLM (códigos / nombres)
    # ------------------------------------------------------
    if products:
        allowed_products = products[:MAX_PRODUCTS_FOR_LLM]
        reply = validate_and_fix_response(reply, allowed_products, phone, execution_context)

    # ------------------------------------------------------
    # Enviar listados largos por chunks
    # ------------------------------------------------------
    if execution_context["will_send_chunks"] and intent == "product_search" and products:
        chunks = [products[i:i + PRODUCTS_PER_CHUNK] for i in range(0, len(products), PRODUCTS_PER_CHUNK)]
        for idx, chunk in enumerate(chunks, 1):
            chunk_text = f"\n━━━ Bloque {idx}/{len(chunks)} ({len(chunk)} productos) ━━━\n"
            chunk_text += format_search_results(chunk)
            if idx > 1:
                time.sleep(0.5)
            send_long_message(phone, chunk_text)

    save_message(phone, reply, "assistant")
    log_interaction(phone, user_message, intent, len(products))
    log_performance(phone, intent, time.time()-start_time, len(products))
    update_sales_phase_from_intent(phone, intent)
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
        "version": "3.14",
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
