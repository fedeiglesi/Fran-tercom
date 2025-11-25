# =========================================================
# Fran 3.13.0 – Bot Mayorista Inteligente
# =========================================================
# Basado en Fran 3.12 (estructura completa que pasó tests),
# con mejoras:
# - INTENTOS 2.0 (prompt ampliado, más sinónimos)
# - CONTEXTO 2.0 (pending_actions realmente ejecutadas)
# - Mensajes más humanos (no dice "no encontré" si hay alternativas)
# - Pedidos implícitos mejorados ("2 de todos", "todos x5", etc.)
# - Uso efectivo de pending_actions + snapshot de carrito
# - Mantiene arquitectura FAISS + familias + quality check
# =========================================================

import os, json, csv, io, sqlite3, logging, re, unicodedata, time, threading, pickle, random, hashlib
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from collections import defaultdict
from functools import lru_cache
from contextlib import contextmanager
from threading import Lock, Semaphore
from queue import Queue, Empty

import requests
from flask import Flask, request, Response, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI, RateLimitError
from rapidfuzz import process, fuzz
import faiss
import numpy as np
from rank_bm25 import BM25Okapi
from dotenv import load_dotenv
from cachetools import TTLCache

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
_fuzzy_match_cache = TTLCache(maxsize=5000, ttl=3600)

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

# ------------------------------------------------------------
# AUTOCORRECTOR
# ------------------------------------------------------------
CATEGORY_MAP = {
    "amortiguador": ["amort", "amortiguador", "shock", "suspension", "suspensión"],
    "bateria": ["bateria", "batería", "battery", "baterias", "baterías"],
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

        if best and best[1] >= 90 and best[0] != base:
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

        fam_words = [w for w in fam_name_norm.split() if len(w) >= 3]
        if not fam_words:
            continue

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
def parse_query_v2(query: str) -> dict:
    q = normalize_search_query(query)
    tokens = q.split()
    out = {
        "brands": [],
        "models": [],
        "category": None,
        "families": [],
        "raw": q,
        "moto_brands": [],
        "moto_models": [],
        "displacement": None,
        "final_category": None,
    }

    for cat, variants in CATEGORY_MAP.items():
        if any(v in q for v in variants):
            out["category"] = cat
            break

    for b in BRAND_LIST:
        if b in q:
            out["brands"].append(b)

    for m in MODEL_LIST:
        if m in q:
            out["models"].append(m)

    out["families"] = detect_families_in_query(q)
    return out


def filter_catalog(catalog, parsed):
    if not catalog:
        return []
    brands = set(parsed.get("brands") or [])
    models = set(parsed.get("models") or [])
    cat = parsed.get("category")
    families = set(parsed.get("families") or [])
    moto_brands = set(parsed.get("moto_brands") or [])
    moto_models = set(parsed.get("moto_models") or [])
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

        if families:
            p_family = normalize_search_query(p.get("family_name", ""))
            if not p_family:
                return False
            if not any(f in p_family for f in families):
                return False

        if cat:
            p_cat = normalize_search_query(p.get("category", ""))
            if not any(v in p_cat for v in CATEGORY_MAP.get(cat, [cat])):
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
        return hashlib.md5(snapshot.encode()).hexdigest()
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
            cur.execute("SELECT products_json, query, metadata FROM last_search WHERE phone=?", (phone,))
            row = cur.fetchone()
            if not row:
                return None
            return {"products": json.loads(row[0]), "query": row[1], "metadata": json.loads(row[2]) if row[2] else {}}
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
        r = requests.get(CATALOG_URL, timeout=REQUESTS_TIMEOUT, headers=REQUESTS_HEADERS)
        r.raise_for_status()
        r.encoding = "utf-8"
        return r.text
    except Exception as e:
        logger.error(f"Error descargando CSV: {e}")
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

                        break
                    except RateLimitError as e:
                        if retry < max_retries - 1:
                            wait_time = min((2 ** retry) * random.uniform(2, 5), 60)
                            logger.warning(f"RateLimitError en embeddings, reintentando en {wait_time:.2f}s... (intento {retry+1}/{max_retries})")
                            time.sleep(wait_time)
                        else:
                            logger.error(f"RateLimitError persistente: {e}")
                            raise

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
def hybrid_search(query: str, top_k: int = MAX_SEARCH_RESULTS, metadata_filters: dict | None = None) -> list:
    catalog, index, bm25_index, _bm25_corpus = get_catalog_and_index()
    if not catalog or not query:
        return []

    parsed = parse_query_v2(query)

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

        matches = hybrid_search(corrected_name, top_k=3)
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

            matches = hybrid_search(corrected_name, top_k=3)
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
        except:
            pass


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

CITATION_ENFORCED_PROMPT = f"""
Sos Fran, vendedor mayorista de TERCOM (motopartes, Argentina).

=== REGLA DE CITACIÓN OBLIGATORIA ===
NUNCA menciones un producto sin su código completo.

FORMATO OBLIGATORIO:
"Tengo [NOMBRE PRODUCTO] (código [CÓDIGO]) - [PRECIO]"

Ejemplos CORRECTOS:
✅ "Tengo ACEITE YAMALUBE 10W40 (1234/56789-012) - $15.000"
✅ "Te sirve BATERÍA YUASA YTX9 (5678/12345-678) - $45.000"

Ejemplos INCORRECTOS:
❌ "Tengo aceite Yamalube" (falta código)
❌ "Hay varias baterías Yuasa" (no específica cuál)
❌ "Tenemos filtros de aire" (no lista productos concretos)

Si no tenés el código de un producto, NO LO MENCIONES.

=== BREVEDAD EXTREMA ===
LIMITE: 5 líneas MAX (salvo listados de productos)

PLANTILLA:
Línea 1: Entender qué busca
Líneas 2-3: Productos con códigos y precios
Línea 4-5: Pregunta de cierre

=== GROUNDING ESTRICTO ===
SOLO podés mencionar productos que están en [TOP N PRODUCTOS].
Si el cliente pidió X y NO está en tu lista → decí "No tengo X exacto, pero tengo estas alternativas:"

{BUSINESS_CONTEXT}

Sos vendedor que SABE de motos, ENTIENDE a la gente, CITA productos reales.
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
        msgs = build_full_history_prompt(phone, user_message, catalog_products, system_prompt)

        user_lower = user_message.lower()
        product_type = (
            "cubierta" if any(k in user_lower for k in ("cubierta","neumatico","neumático","llanta","tire"))
            else "batería" if any(k in user_lower for k in ("bateria","batería","battery","baterias","baterías"))
            else "filtro" if any(k in user_lower for k in ("filtro","filter","filtros"))
            else "cadena" if any(k in user_lower for k in ("cadena","chain"))
            else "aceite" if any(k in user_lower for k in ("aceite","oil"))
            else "bujía" if any(k in user_lower for k in ("bujia","bujía","spark"))
            else "amortiguador" if any(k in user_lower for k in ("amort","amortiguador","shock","suspension","suspensión"))
            else "repuesto"
        )

        msgs.insert(1, {
            "role": "system",
            "content": (
                f"CLIENTE PIDIÓ: {product_type}\n\n"
                "PRODUCTOS DISPONIBLES:\n" +
                "\n".join([
                    f"- {p['name']} ({p.get('code','')}) - {format_price(Decimal(str(p['price_ars'])))}"
                    for p in catalog_products[:10]
                ]) +
                "\n\n⚠️ VALIDACIÓN CRÍTICA:\n"
                f"1) Si NINGÚN producto de arriba es un/a {product_type}, NO LOS MENCIONES.\n"
                f"2) En ese caso, decí: 'No tengo {product_type}s exactos, pero tengo estas alternativas reales del catálogo.'\n"
                "3) Si SÍ hay productos que coinciden, listá solo ESOS.\n"
                "4) SIEMPRE incluí el código entre paréntesis cuando menciones un producto."
            )
        })

        exec_summary = build_execution_summary(execution_context)
        msgs.insert(1, {"role": "system", "content": f"[RESULTADO DE BÚSQUEDA]\n{exec_summary}"})

        with openai_sem:
            resp = client.chat.completions.create(
                model=MODEL_NAME,
                messages=msgs,
                temperature=0.3,
                max_tokens=500,
            )
        txt = (resp.choices[0].message.content or "").strip()
        if not txt or len(txt) < 10:
            return "Uy, tuve un problema. ¿Me repetís?"
        return txt
    except Exception as e:
        logger.error(f"generate_smart_ai_reply_v2 error: {e}")
        return "Uy, tuve un problema técnico. Probá de nuevo en un ratito."


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

# =========================================================
# AGENTE PRINCIPAL – VERSIÓN 3.13.0
# =========================================================
def run_agent(phone, user_message):
    start_time = time.time()
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

        semantic_results = hybrid_search(query_for_search, top_k=MAX_SEARCH_RESULTS)
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
        code_validation = validate_response_codes(reply, allowed_products)
        name_validation = validate_mentioned_names(reply, allowed_products)

        if not code_validation["valid"]:
            logger.error(f"⚠️ LLM alucinó códigos: {code_validation.get('hallucinated_codes', [])}")

            if allowed_products[:5]:
                product_list = "\n".join([
                    f"- {p.get('name', '')} ({p.get('code', '')}) - {format_price(p.get('price_ars', 0))}"
                    for p in allowed_products[:5]
                ])
                reply = (
                    f"Dale, te paso opciones reales del catálogo para no pifiar:\n\n{product_list}\n\n"
                    "¿Te sirve alguno? Si buscás otra cosa decime marca/modelo y te mando alternativas."
                )
            else:
                reply = "Encontré productos pero necesito más info. ¿Me pasás marca/modelo específico?"

        elif not name_validation["valid"]:
            logger.warning(f"⚠️ LLM mencionó productos dudosos: {name_validation.get('hallucinated_names', [])}")

            if allowed_products[:5]:
                product_list = "\n".join([
                    f"- {p.get('name', '')} ({p.get('code', '')}) - {format_price(p.get('price_ars', 0))}"
                    for p in allowed_products[:5]
                ])
                reply = (
                    f"Mirá, para no mezclar, te paso directamente lo que tengo en catálogo:\n\n{product_list}\n\n"
                    "¿Te sirve alguno? Si estás buscando otra variante avisame marca/modelo y te paso otras opciones."
                )
        elif code_validation.get("warning") == "no_citations":
            logger.info("LLM no citó códigos (puede ser respuesta general válida)")

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
                    time.sleep(0.8)

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
            resp = MessagingResponse()
            resp.message(f"Perfecto, es una lista larga ({count} items). La proceso y te aviso con el total.")
            return Response(str(resp), mimetype="text/xml")

        reply = run_agent(from_number, message_body)

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
        "version": "3.13.0",
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
    logger.info("🚀 Iniciando Fran 3.13.0 - Motor Híbrido Familias + FAISS + Intents/Contexto 2.0")
    logger.info("=" * 60)
    logger.info(f"Puerto: {port}")
    logger.info(f"Catálogo: {len(catalog) if catalog else 0} productos")
    logger.info(f"Tipo de cambio inicial: {get_exchange_rate()}")
    logger.info(f"Relevance min score: {RELEVANCE_MIN_SCORE}")
    logger.info(f"Quality thresholds: HIGH={QUALITY_HIGH_THRESHOLD}, MED={QUALITY_MEDIUM_THRESHOLD}")
    logger.info("=" * 60)
    logger.info("Características nuevas en 3.13.0:")
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
