# Fran 3.11.4 - Arquitectura Mejorada Inspirada en Claude

Acá va el código completo con la nueva arquitectura:

```python
# =========================================================
# Fran 3.11.4 – Bot Mayorista Inteligente
# =========================================================
# NUEVA ARQUITECTURA inspirada en Claude:
# - Context Quality Check pre-LLM
# - Relevance scoring general (no keywords hardcoded)
# - Forced code citation
# - Post-validation de códigos mencionados
# - Fallbacks seguros
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
from dotenv import load_dotenv
from cachetools import TTLCache

load_dotenv()
app = Flask(__name__)

REQUESTS_HEADERS = {
    "User-Agent": "Safari/605.1.15",
    "Accept": "text/plain"
}

CSV_URL = "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/Fran-3.11/catalogo_limpio_final_v4.csv"

# ------------------------------------------------------------
# LOGGER
# ------------------------------------------------------------
logger = logging.getLogger("fran314")
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
CATALOG_URL = (
    os.environ.get("CATALOG_URL") or
    "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/Fran-3.11/catalogo_limpio_final_v4.csv"
).strip()

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

# Nuevos parámetros de calidad
RELEVANCE_MIN_SCORE = float(os.environ.get("RELEVANCE_MIN_SCORE", "60.0"))
QUALITY_HIGH_THRESHOLD = float(os.environ.get("QUALITY_HIGH_THRESHOLD", "70.0"))
QUALITY_MEDIUM_THRESHOLD = float(os.environ.get("QUALITY_MEDIUM_THRESHOLD", "50.0"))

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

_catalog_and_index_cache = {"catalog": None, "index": None, "built_at": None}
_catalog_lock = Lock()

_embeddings_cache_lock = Lock()

# Cache de fuzzy matching para post-validation
_fuzzy_match_cache = TTLCache(maxsize=5000, ttl=3600)

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
    
    Componentes:
    - 40% overlap de palabras clave
    - 40% fuzzy match del nombre completo
    - 20% match de categoría
    """
    q_norm = normalize_search_query(query)
    q_words = set(q_norm.split())
    
    # Texto del producto (multi-campo)
    p_text = normalize_search_query(
        f"{product.get('name', '')} {product.get('category', '')} "
        f"{product.get('keywords', '')} {product.get('brand', '')} {product.get('model', '')}"
    )
    p_words = set(p_text.split())
    
    # 1. Overlap de palabras (40%)
    overlap = len(q_words & p_words) / len(q_words) if q_words else 0
    overlap_score = overlap * 40
    
    # 2. Fuzzy match del nombre completo (40%)
    product_name = normalize_search_query(product.get('name', ''))
    try:
        fuzzy_ratio = fuzz.token_set_ratio(q_norm, product_name)
        fuzzy_score = fuzzy_ratio * 0.4
    except Exception:
        fuzzy_score = 0
    
    # 3. Category match (20%)
    category_score = 0
    product_cat = normalize_search_query(product.get('category', ''))
    for q_word in q_words:
        if len(q_word) >= 4:  # Solo palabras significativas
            if q_word in product_cat or product_cat in q_word:
                category_score = 20
                break
    
    total = overlap_score + fuzzy_score + category_score
    return min(total, 100)


def filter_by_relevance(query: str, products: list, min_score: float = RELEVANCE_MIN_SCORE) -> list:
    """
    Filtra productos por relevancia mínima.
    Si NINGUNO supera min_score, retorna lista vacía.
    """
    if not products or not query:
        return []
    
    scored = []
    for p in products:
        score = calculate_relevance_score(query, p)
        if score >= min_score:
            scored.append((p, score))
    
    # Ordenar por score descendente
    scored.sort(key=lambda x: x[1], reverse=True)
    
    return [p for p, score in scored]


# ------------------------------------------------------------
# NUEVO: CONTEXT QUALITY ASSESSMENT
# ------------------------------------------------------------
def assess_context_quality(query: str, products: list) -> dict:
    """
    Evalúa si el contexto recuperado es suficiente para responder.
    Similar a cómo Claude valida sus tool results.
    
    Returns:
        dict con:
        - sufficient: bool
        - reason: str
        - action: str (proceed | ask_clarification | suggest_alternatives)
        - confidence: str (high | medium | low)
        - message: str (opcional, para respuestas tempranas)
    """
    if not products:
        return {
            "sufficient": False,
            "reason": "no_results",
            "action": "ask_clarification",
            "confidence": "none",
            "message": "No encontré ese repuesto en el catálogo. ¿Me pasás más detalles? (marca/modelo/año)"
        }
    
    # Calcular relevancia de top productos
    scores = [calculate_relevance_score(query, p) for p in products[:10]]
    avg_score = sum(scores) / len(scores) if scores else 0
    max_score = max(scores) if scores else 0
    
    # Contar productos relevantes
    relevant_count = sum(1 for s in scores if s >= RELEVANCE_MIN_SCORE)
    
    logger.info(f"Quality assessment - Avg: {avg_score:.1f}, Max: {max_score:.1f}, Relevant: {relevant_count}/{len(products[:10])}")
    
    # Si el MEJOR producto es irrelevante
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
    
    # Si hay pocos productos relevantes pero algunos buenos
    if relevant_count < 3 and max_score >= RELEVANCE_MIN_SCORE:
        return {
            "sufficient": True,
            "reason": "limited_but_valid",
            "action": "show_with_caveat",
            "confidence": "medium",
            "relevant_count": relevant_count,
            "avg_score": avg_score
        }
    
    # Contexto de alta calidad
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
    """
    Extrae todos los códigos TERCOM mencionados en la respuesta del LLM.
    Busca patrones como:
    - (1234/56789-012)
    - (código 1234/56789-012)
    - 1234/56789-012 (suelto)
    """
    codes = set()
    
    # Patrón 1: Códigos entre paréntesis con o sin "código"
    pattern1 = r'\((?:código\s+|cod\s+)?(\d{4}/\d{5}-\d{3})\)'
    codes.update(re.findall(pattern1, response_text, re.IGNORECASE))
    
    # Patrón 2: Códigos sueltos
    pattern2 = r'\b(\d{4}/\d{5}-\d{3})\b'
    codes.update(re.findall(pattern2, response_text))
    
    return codes


def validate_response_codes(response_text: str, allowed_products: list) -> dict:
    """
    Valida que todos los códigos mencionados existan en productos permitidos.
    Esto previene que el LLM invente códigos.
    """
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
        # LLM no citó ningún código (puede ser válido para respuestas generales)
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
    """
    Valida que los nombres de productos mencionados existan en el catálogo.
    Usa fuzzy matching para tolerar pequeñas variaciones.
    """
    if not allowed_products:
        return {"valid": True}
    
    # Extraer nombres mencionados
    mentioned_names = []
    
    # Nombres entre comillas
    quoted = re.findall(r'"([^"]{5,})"', response_text)
    mentioned_names.extend(quoted)
    
    # Nombres después de "tengo", "hay", etc.
    patterns = [
        r'(?:tengo|tenemos|hay|encontré)\s+(?:el\s+|la\s+|los\s+|las\s+)?([A-ZÁÉÍÓÚÑ][a-záéíóúñA-ZÁÉÍÓÚÑ\s]{5,50})(?:\s+(?:para|de|en|cod|código|\()|\.|\,|$)',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, response_text, re.IGNORECASE)
        mentioned_names.extend(matches)
    
    if not mentioned_names:
        return {"valid": True}
    
    # Normalizar nombres permitidos
    allowed_names_normalized = [
        normalize_search_query(p.get("name", "")) 
        for p in allowed_products if p.get("name")
    ]
    
    hallucinated_names = []
    for name in mentioned_names:
        name_norm = normalize_search_query(name)
        
        # Buscar match con fuzzy
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
# AUTOCORRECTOR (de 3.11.3)
# ------------------------------------------------------------
CATEGORY_MAP = {
    "amortiguador": ["amort", "shock", "suspension"],
    "bateria": ["bateria", "batería", "battery"],
    "aceite": ["aceite", "oil", "lubricante"],
    "filtro": ["filtro", "filter"],
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
        "espejo", "cubierta", "neumatico", "ruleman", "rodamiento",
        "corona", "piñon", "kit transmision", "kit freno"
    ])
    vocab = {normalize_search_query(t) for t in base_tokens if t}
    return sorted(vocab)

AUTOCORRECT_VOCAB = _build_autocorrect_vocab()

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
    """
    Autocorrector simple que no toca códigos ni números.
    """
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
        
        if len(base) <= 3:
            new_tokens.append(raw)
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
# QUERY PARSING (de 3.11.3)
# ------------------------------------------------------------
def parse_query_v2(query: str) -> dict:
    q = normalize_search_query(query)
    tokens = q.split()
    out = {"brands": [], "models": [], "category": None, "raw": q}
    
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
    
    return out

def filter_catalog(catalog, parsed):
    if not catalog:
        return []
    brands = set(parsed["brands"])
    models = set(parsed["models"])
    cat = parsed["category"]
    
    def _match(p):
        if brands:
            p_brand = normalize_search_query(p.get("brand", ""))
            if not any(b in p_brand for b in brands):
                return False
        if models:
            p_model = normalize_search_query(p.get("model", ""))
            if not any(m in p_model for m in models):
                return False
        if cat:
            p_cat = normalize_search_query(p.get("category", ""))
            if not any(v in p_cat for v in CATEGORY_MAP.get(cat, [cat])):
                return False
        return True
    
    return [p for p in catalog if _match(p)]


# ------------------------------------------------------------
# PENDING ACTIONS
# ------------------------------------------------------------
def save_pending_action(phone, action_type, action_data, context="", ttl_minutes=30):
    if not phone:
        return
    try:
        items = cart_get(phone)
        cart_snapshot = json.dumps(sorted([(i[0], i[1]) for i in items]))
        action_data["cart_hash"] = hashlib.md5(cart_snapshot.encode()).hexdigest()
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
        
        catalog, _idx = get_catalog_and_index()
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
# CATÁLOGO
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
        idx_usd = _extract_column(header, ["price_importado", "precio_importado", "usd", "dolar", "precio en dolares", "price_usd"])
        idx_ars = _extract_column(header, ["price_nacional", "precio_nacional", "ars", "pesos", "precio en pesos", "price_ars"])
        idx_brand = _extract_column(header, ["marca_final", "marca", "brand"])
        idx_model = _extract_column(header, ["modelo_final", "modelo", "model"])
        idx_category = _extract_column(header, ["categoria_nueva", "categoria", "category", "rubro"])
        idx_keywords = _extract_column(header, ["keywords", "palabras clave", "sinonimos"])
        idx_oem = _extract_column(header, ["oem", "codigo oem", "original"])
        idx_alt = _extract_column(header, ["alt_names", "nombres alternativos", "alias"])
        idx_vehicle = _extract_column(header, ["vehicle_type", "tipo de moto", "aplica a"])
        
        exchange = get_exchange_rate()
        catalog = []
        
        for line in data_rows:
            if not line:
                continue
            try:
                code = line[idx_code].strip() if (idx_code is not None and idx_code < len(line)) else ""
                name = line[idx_name].strip() if (idx_name is not None and idx_name < len(line)) else ""
                
                price_usd = to_decimal_money(line[idx_usd]) if (idx_usd is not None and idx_usd < len(line)) else Decimal("0")
                price_ars = to_decimal_money(line[idx_ars]) if (idx_ars is not None and idx_ars < len(line)) else Decimal("0")
                
                if price_ars == 0 and price_usd > 0:
                    price_ars = (price_usd * exchange).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                
                brand = line[idx_brand].strip() if (idx_brand is not None and idx_brand < len(line)) else ""
                model = line[idx_model].strip() if (idx_model is not None and idx_model < len(line)) else ""
                category = line[idx_category].strip() if (idx_category is not None and idx_category < len(line)) else ""
                keywords = line[idx_keywords].strip() if (idx_keywords is not None and idx_keywords < len(line)) else ""
                oem = line[idx_oem].strip() if (idx_oem is not None and idx_oem < len(line)) else ""
                alt_names = line[idx_alt].strip() if (idx_alt is not None and idx_alt < len(line)) else ""
                vehicle_type = line[idx_vehicle].strip() if (idx_vehicle is not None and idx_vehicle < len(line)) else ""
                
                search_text_parts = [
                    name,
                    f"marca {brand}" if brand else "",
                    f"modelo {model}" if model else "",
                    f"categoria {category}" if category else "",
                    f"aplica a {vehicle_type}" if vehicle_type else "",
                    f"equivalente oem {oem}" if oem else "",
                    f"tambien llamado {alt_names}" if alt_names else "",
                    f"palabras clave {keywords}" if keywords else "",
                ]
                search_text = " ".join([p for p in search_text_parts if p]).strip()
                
                if not name and not search_text:
                    continue
                
                catalog.append({
                    "code": code,
                    "name": name,
                    "price_usd": float(price_usd),
                    "price_ars": float(price_ars),
                    "brand": brand,
                    "model": model,
                    "category": category,
                    "keywords": keywords,
                    "oem": oem,
                    "alt_names": alt_names,
                    "vehicle_type": vehicle_type,
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
        text_indices = []
        
        for idx, text in enumerate(texts):
            if text not in cache:
                texts_to_embed.append(text)
                text_indices.append(idx)
        
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
                                timeout=REQUESTS_TIMEOUT
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
        
        index = faiss.IndexFlatL2(vecs.shape[1])
        index.add(vecs)
        
        logger.info(f"Indice FAISS creado con {vecs.shape[0]} vectores")
        return index, vecs.shape[0]
    except Exception as e:
        logger.error(f"Error construyendo FAISS: {e}", exc_info=True)
        return None, 0

def get_catalog_and_index():
    with _catalog_lock:
        if _catalog_and_index_cache["catalog"] is not None:
            return _catalog_and_index_cache["catalog"], _catalog_and_index_cache["index"]
        
        catalog, index = load_faiss_index()
        
        if catalog and index:
            _catalog_and_index_cache["catalog"] = catalog
            _catalog_and_index_cache["index"] = index
            _catalog_and_index_cache["built_at"] = datetime.utcnow().isoformat()
            return catalog, index
        
        catalog = load_catalog_enriched()
        index, _ = _build_faiss_index_from_catalog(catalog)
        
        if index and catalog:
            save_faiss_index(index, catalog)
        
        _catalog_and_index_cache["catalog"] = catalog
        _catalog_and_index_cache["index"] = index
        _catalog_and_index_cache["built_at"] = datetime.utcnow().isoformat()
        return catalog, index

# ------------------------------------------------------------------
# BÚSQUEDA SEMÁNTICA
# ------------------------------------------------------------------
def semantic_search_v2(query: str, top_k: int = 60) -> list:
    catalog, _ = get_catalog_and_index()
    if not catalog or not query:
        return []
    
    # Pre-filtro por categoría si aplica
    detected_category = None
    for cat, variants in CATEGORY_MAP.items():
        if any(v in normalize_search_query(query) for v in variants):
            detected_category = cat
            break
    
    if detected_category:
        filtered_catalog = [
            p for p in catalog 
            if detected_category in normalize_search_query(p.get("category", ""))
        ]
        if filtered_catalog:
            logger.info(f"Pre-filtro por categoría '{detected_category}': {len(filtered_catalog)} productos")
            catalog = filtered_catalog
    
    # Filtro lexical
    parsed = parse_query_v2(query)
    filtered = filter_catalog(catalog, parsed)
    
    if not filtered:
        filtered = catalog
    
    # Embeddings solo del sub-conjunto
    texts = [p["search_text"] for p in filtered]
    vectors = generate_embeddings_with_cache(texts)
    vecs = np.array(vectors).astype("float32")
    
    # Índice temporal
    dim = vecs.shape[1]
    temp_index = faiss.IndexFlatIP(dim)
    faiss.normalize_L2(vecs)
    temp_index.add(vecs)
    
    # Embedding del query
    emb = generate_embeddings_with_cache([query])[0]
    emb = np.array([emb]).astype("float32")
    faiss.normalize_L2(emb)
    
    D, I = temp_index.search(emb, min(top_k, len(filtered)))
    results = []
    for dist, idx in zip(D[0], I[0]):
        if 0 <= idx < len(filtered):
            score = float(dist)
            results.append((filtered[idx], score))
    
    return sorted(results, key=lambda x: x[1], reverse=True)

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
        line = line.strip()
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
    has_quote_intent = any(kw in lower for kw in ["cotiz", "precio", "cuanto", "tenes", "stock", "pedido", "lista"])
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
        
        matches = semantic_search_v2(corrected_name, top_k=3)
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
            
            matches = semantic_search_v2(corrected_name, top_k=3)
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
# INTENT DETECTOR
# ------------------------------------------------------------------
INTENT_SYSTEM_PROMPT = """
Sos un clasificador de intenciones para un vendedor mayorista (WhatsApp).
No respondas al usuario. No agregues explicaciones.
Tu única salida será un JSON válido (una línea), con este esquema:
{"intent":"<uno de: saludo|busqueda_catalogo|pregunta_tecnica|pedido_codigo|agregar_carrito|ver_carrito|vaciar_carrito|confirmar|cancelar|desconocido>", "query":"<texto util para buscar o ''>"}

Criterios estrictos:
- "hola", "buen día", "que tal" → saludo
- Contiene código tipo 1234/56789-012 → pedido_codigo
- "tenes", "precio", "busco", nombre de pieza/marca → busqueda_catalogo
- "cuánto mide", "longitud", "amperaje", "equivalencia", "sirve para", "diferencia" → pregunta_tecnica
- "agregalo", "sumalo", "metelo" → agregar_carrito
- "ver carrito", "mostrar carrito" → ver_carrito
- "vaciar carrito", "limpiar carrito" → vaciar_carrito
- "si", "ok", "dale" justo después de oferta → confirmar
- "no", "cancelar" → cancelar
- Otro caso → desconocido

Devolvé SIEMPRE JSON de una línea.
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
                ],
                timeout=REQUESTS_TIMEOUT
            )
        raw = (resp.choices[0].message.content or "").strip()
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1:
            raw = raw[start:end+1]
        
        data = json.loads(raw)
        if not isinstance(data, dict):
            return {"intent": "desconocido", "query": ""}
        
        intent = str(data.get("intent", "desconocido")).strip()
        query = str(data.get("query", "")).strip()
        
        valid_intents = {
            "saludo", "busqueda_catalogo", "pregunta_tecnica", "pedido_codigo",
            "agregar_carrito", "ver_carrito", "vaciar_carrito", "confirmar", "cancelar", "desconocido"
        }
        if intent not in valid_intents:
            intent = "desconocido"
        
        return {"intent": intent, "query": query or msg.strip()[:600]}
    except Exception as e:
        logger.error(f"detect_intent_llm error: {e}")
        return {"intent": "desconocido", "query": msg.strip()[:600]}

# ------------------------------------------------------------------
# PEDIDOS IMPLÍCITOS
# ------------------------------------------------------------------
def detect_implicit_cart_action(message, phone):
    msg = (message or "").lower()
    last = get_last_search(phone)
    if not last or not last.get("products"):
        return None
    
    products = last["products"]
    
    # A) "3 de cada", "3 de cada una/uno", "3 c/u"
    m = re.search(r"(\d+)\s*(de\s*cada(\s+una|\s+uno)?|c\/u)", msg)
    if m:
        qty = int(m.group(1))
        return {
            "action": "add_each_quantity",
            "quantity": qty,
            "products": products
        }
    
    # B) "2 de todos", "3 de todas"
    m = re.search(r"(\d+)\s+de\s+(todos?|todas?)", msg)
    if m:
        qty = int(m.group(1))
        return {
            "action": "add_each_quantity",
            "quantity": qty,
            "products": products
        }
    
    # C) "todos x5" / "todos por 5" / "todas 5"
    m = re.search(r"(todos?|todas?)\s*(x|por)?\s*(\d+)", msg)
    if m:
        qty = int(m.group(3))
        return {
            "action": "add_each_quantity",
            "quantity": qty,
            "products": products
        }
    
    # D) "agregame todos", "sumame todos", "mandame todos"
    if re.search(r"(agregame|sumame|mandame|poneme|dejame)\s+(todos?|todas?)", msg):
        return {
            "action": "add_each_quantity",
            "quantity": 1,
            "products": products
        }
    
    # E) "esos" / "esas" / "los que me pasaste" / "los de arriba"
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
# NUEVO: PROMPTS MEJORADOS CON FORCED CITATION
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
Sos Fran, vendedor experto en motopartes. Tu objetivo es VENDER RÁPIDO.

=== BREVEDAD EXTREMA ===
LIMITE ESTRICTO: Máximo 4 líneas

PLANTILLA OBLIGATORIA:
Línea 1: Diferencia clave directa
Líneas 2-3: Cuál de TUS productos (con código y precio)
Línea 4: ¿Lo/Los agregamos?

=== CITACIÓN OBLIGATORIA ===
Si mencionás un producto, incluí su código:
✅ "El ACEITE YAMALUBE 10W40 SINTETICO (1234/56789-012) - $15.000 es sintético, dura más"
❌ "El Yamalube 10W40 es sintético" (falta código)

=== CONOCIMIENTO TÉCNICO: ULTRA BREVE ===
Podés explicar conceptos en 1 LÍNEA:
- "El sintético dura más pero es más caro"
- "Para frío el 10W, para calor el 20W"

{BUSINESS_CONTEXT}

Sos vendedor EFICIENTE, no Wikipedia.
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
        
        # GATEKEEPER: validación de producto solicitado vs disponible
        user_lower = user_message.lower()
        product_type = (
            "cubierta" if any(k in user_lower for k in ("cubierta","neumatico","llanta","tire"))
            else "batería" if any(k in user_lower for k in ("bateria","batería","battery"))
            else "filtro" if any(k in user_lower for k in ("filtro","filter"))
            else "cadena" if any(k in user_lower for k in ("cadena","chain"))
            else "aceite" if any(k in user_lower for k in ("aceite","oil"))
            else "bujía" if any(k in user_lower for k in ("bujia","bujía","spark"))
            else "amortiguador" if any(k in user_lower for k in ("amort","shock","suspension"))
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
                f"2) En ese caso, decí: 'No tengo {product_type}s exactos, pero puedo ayudarte con otros repuestos.'\n"
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
                timeout=REQUESTS_TIMEOUT
            )
        txt = (resp.choices[0].message.content or "").strip()
        if not txt or len(txt) < 10:
            return "Uy, tuve un problema. ¿Me repetís?"
        return txt
    except Exception as e:
        logger.error(f"generate_smart_ai_reply_v2 error: {e}")
        return "Uy, tuve un problema técnico. Probá de nuevo en un ratito."

def build_technical_answer(phone, user_message, top_products):
    try:
        context_lines = []
        for p in (top_products or [])[:3]:
            context_lines.append(
                f"- {p.get('name','')} (cod {p.get('code','')}) "
                f"marca {p.get('brand','')} modelo {p.get('model','')}"
            )
        ctx = "Productos disponibles:\n" + "\n".join(context_lines) if context_lines else "Productos disponibles: (sin coincidencias exactas)"
        
        history = get_history_since(phone, days=1, limit=10)
        msgs = [{"role": "system", "content": TECH_SYSTEM_PROMPT}]
        
        for h in history[-10:]:
            role = "assistant" if h["role"] == "assistant" else "user"
            msgs.append({"role": role, "content": h["content"]})
        
        msgs.append({
            "role": "user",
            "content": f"{ctx}\n\nPregunta del cliente: {user_message[:600]}"
        })
        
        with openai_sem:
            resp = client.chat.completions.create(
                model=MODEL_NAME,
                messages=msgs,
                temperature=0.3,
                max_tokens=400,
                timeout=REQUESTS_TIMEOUT
            )
        
        txt = (resp.choices[0].message.content or "").strip()
        
        if not txt or len(txt) < 10:
            return "Te confirmo medidas/compatibilidades y te aviso. ¿Querés que lo deje listo?"
        
        # Validar respuesta técnica
        code_validation = validate_response_codes(txt, top_products)
        name_validation = validate_mentioned_names(txt, top_products)
        
        if not code_validation["valid"] or not name_validation["valid"]:
            logger.warning("Respuesta técnica con alucinaciones, usando fallback")
            return "Estoy revisando las especificaciones técnicas. ¿Querés que te avise cuando tenga la info exacta?"
        
        return txt
    except Exception as e:
        logger.error(f"build_technical_answer error: {e}")
        return "Estoy revisando las especificaciones técnicas. ¿Querés que te avise y mientras vemos alternativas?"

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
        emoji = next((CATEGORY_EMOJIS[k] for k in CATEGORY_EMOJIS if k in p.get("name", "").lower()), "📦")
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
# AGENTE PRINCIPAL – VERSIÓN 3.11.4
# =========================================================
def run_agent(phone, user_message):
    start_time = time.time()
    save_message(phone, user_message, "user")
    
    if not rate_limit_check(phone):
        reply = "Demasiados mensajes, esperá un minuto."
        save_message(phone, reply, "assistant")
        return reply
    
    # 1. Detectar intent
    intent_data = detect_intent_llm(user_message)
    intent = intent_data.get("intent", "desconocido")
    raw_query_for_search = intent_data.get("query") or user_message
    
    # 2. Execution context
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
    
    # 3. Acciones rápidas sin búsqueda
    if intent == "saludo":
        reply = "¡Hola! Soy Fran de TERCOM, ¿en qué te puedo ayudar?"
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "saludo", 0)
        log_performance(phone, "saludo", time.time()-start_time, 0)
        return reply
    
    if intent == "ver_carrito":
        items = cart_get(phone)
        if not items:
            reply = "Tu carrito está vacío."
        else:
            total, discount = cart_totals(phone)
            lines = ["TU CARRITO:\n"]
            for code, q, name, price in items:
                subtotal = (price * q).quantize(Decimal("0.01"))
                lines.append(f"- {q}x {name[:40]} = {format_price(subtotal)}")
            lines.append(f"\nTOTAL: {format_price(total)}")
            reply = "\n".join(lines)
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "ver_carrito", 0)
        log_performance(phone, "ver_carrito", time.time()-start_time, 0)
        return reply
    
    if intent == "vaciar_carrito":
        cart_clear(phone)
        reply = "Listo, vacié tu carrito."
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "vaciar_carrito", 0)
        log_performance(phone, "vaciar_carrito", time.time()-start_time, 0)
        return reply
    
    # 4. Detectar pedidos implícitos
    implicit = detect_implicit_cart_action(user_message, phone)​​​​​​​​​​​
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
    
    # 5. Búsqueda con autocorrect
    products = []
    query_for_search = raw_query_for_search
    corrections = []
    
    if intent in {"busqueda_catalogo", "pregunta_tecnica", "desconocido"}:
        # Autocorrect
        query_for_search, corrections = autocorrect_keywords(raw_query_for_search)
        if corrections:
            execution_context["warnings"].append(f"Autocorrect: {', '.join(corrections)}")
            logger.info(f"Autocorrect aplicado: {', '.join(corrections)}")
        
        execution_context["search_query"] = query_for_search
        
        # Búsqueda semántica
        semantic_results = semantic_search_v2(query_for_search, top_k=MAX_SEARCH_RESULTS)
        products = [p for p, _ in semantic_results]
        
        execution_context["search_executed"] = True
        execution_context["products_found"] = len(products)
        
        # ========== NUEVO: FILTRO DE RELEVANCIA ==========
        if products and intent in {"busqueda_catalogo", "pregunta_tecnica"}:
            filtered_products = filter_by_relevance(query_for_search, products, min_score=RELEVANCE_MIN_SCORE)
            
            logger.info(f"Relevance filter: {len(filtered_products)}/{len(products)} productos relevantes")
            
            # Si hay filtrados relevantes, usar solo esos
            if filtered_products:
                products = filtered_products
                execution_context["filters_applied"].append(f"relevance: {len(filtered_products)}/{len(products)} passed")
            
            # Si NO hay productos relevantes después del filtro
            else:
                logger.warning(f"Sin productos relevantes para query: {query_for_search}")
                
                # Tomar los 3 con mayor score para sugerir
                top_3 = sorted(
                    [(p, calculate_relevance_score(query_for_search, p)) for p in products[:10]],
                    key=lambda x: x[1],
                    reverse=True
                )[:3]
                
                suggestions = "\n".join([
                    f"- {p.get('name', '')} ({p.get('code', '')}) - {format_price(p.get('price_ars', 0))}"
                    for p, score in top_3
                ])
                
                reply = (
                    f"No encontré ese repuesto específico.\n\n"
                    f"¿Te sirve alguna de estas alternativas?\n\n{suggestions}\n\n"
                    f"O pasame más detalles (marca/modelo/año) para buscarte algo exacto."
                )
                save_message(phone, reply, "assistant")
                log_interaction(phone, user_message, "no_relevant_results", 0)
                log_performance(phone, intent, time.time()-start_time, 0)
                return reply
        # ========== FIN FILTRO DE RELEVANCIA ==========
        
        # ========== NUEVO: CONTEXT QUALITY CHECK ==========
        quality_assessment = assess_context_quality(query_for_search, products)
        execution_context["quality_assessment"] = quality_assessment
        
        logger.info(f"Quality assessment: {quality_assessment}")
        
        # Respuesta temprana si el contexto no es suficiente
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
                    f"No encontré ese repuesto exacto.\n\n"
                    f"¿Te sirve alguna de estas alternativas?\n\n{suggestions}\n\n"
                    f"O dame más detalles para afinar la búsqueda."
                )
            
            save_message(phone, reply, "assistant")
            log_interaction(phone, user_message, f"low_quality_{quality_assessment['reason']}", 0)
            log_performance(phone, intent, time.time()-start_time, 0)
            return reply
        # ========== FIN CONTEXT QUALITY CHECK ==========
        
        # Guardar búsqueda
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
        
        # Determinar si enviar chunks (solo para búsqueda_catalogo, NO para pregunta_tecnica)
        if intent == "busqueda_catalogo" and len(products) > MAX_PRODUCTS_FOR_LLM:
            execution_context["will_send_chunks"] = True
            num_chunks = (len(products) + PRODUCTS_PER_CHUNK - 1) // PRODUCTS_PER_CHUNK
            execution_context["chunk_info"] = {
                "total_chunks": num_chunks, 
                "products_per_chunk": PRODUCTS_PER_CHUNK, 
                "total_products": len(products)
            }
    
    # 6. Generar respuesta
    if intent == "pregunta_tecnica":
        reply = build_technical_answer(phone, user_message, products[:8])
        execution_context["will_send_chunks"] = False  # No chunks para técnica
    else:
        reply = generate_smart_ai_reply_v2(
            phone, 
            user_message, 
            products[:MAX_PRODUCTS_FOR_LLM], 
            execution_context,
            system_prompt=CITATION_ENFORCED_PROMPT
        )
    
    # ========== NUEVO: POST-VALIDATION ==========
    if products:
        code_validation = validate_response_codes(reply, products[:MAX_PRODUCTS_FOR_LLM])
        name_validation = validate_mentioned_names(reply, products[:MAX_PRODUCTS_FOR_LLM])
        
        if not code_validation["valid"]:
            logger.error(f"⚠️ LLM alucinó códigos: {code_validation.get('hallucinated_codes', [])}")
            
            # Fallback seguro
            if products[:5]:
                product_list = "\n".join([
                    f"- {p.get('name', '')} ({p.get('code', '')}) - {format_price(p.get('price_ars', 0))}"
                    for p in products[:5]
                ])
                reply = (
                    f"Dale, te muestro lo que tengo:\n\n{product_list}\n\n"
                    "¿Cuál te sirve?"
                )
            else:
                reply = "Encontré productos pero necesito más info. ¿Me pasás marca/modelo específico?"
        
        elif not name_validation["valid"]:
            logger.warning(f"⚠️ LLM mencionó productos dudosos: {name_validation.get('hallucinated_names', [])}")
            
            # Fallback a lista simple
            if products[:5]:
                product_list = "\n".join([
                    f"- {p.get('name', '')} ({p.get('code', '')}) - {format_price(p.get('price_ars', 0))}"
                    for p in products[:5]
                ])
                reply = (
                    f"Dale, mirá lo que tengo:\n\n{product_list}\n\n"
                    "¿Cuál te sirve?"
                )
        
        elif code_validation.get("warning") == "no_citations":
            logger.info("LLM no citó códigos (puede ser respuesta general válida)")
    # ========== FIN POST-VALIDATION ==========
    
    # 7. Enviar chunks DESPUÉS de la respuesta (solo para búsqueda_catalogo)
    if execution_context["will_send_chunks"] and intent == "busqueda_catalogo":
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
        
        # Detectar lista masiva
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
            resp.message("Uy, tuve un problema técnico. Proba de nuevo en un ratito.")
            return Response(str(resp), mimetype="text/xml")
        except:
            return Response("<Response></Response>", mimetype="text/xml")

# ------------------------------------------------------------------
# HEALTH / API
# ------------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    catalog, index = get_catalog_and_index()
    return jsonify({
        "status": "ok", 
        "version": "3.11.4",
        "catalog_size": len(catalog) if catalog else 0,
        "architecture": "claude_inspired",
        "features": [
            "context_quality_check",
            "relevance_scoring",
            "forced_code_citation",
            "post_validation"
        ]
    }), 200

# ------------------------------------------------------------------
# ✅ Cargar índice al arrancar
# ------------------------------------------------------------------
catalog, index = load_faiss_index()
if not (catalog and index):
    catalog, index = get_catalog_and_index()
else:
    logger.info("✅ Índice FAISS encontrado en disco: %s productos", len(catalog))

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
    logger.info("🚀 Iniciando Fran 3.11.4 - Arquitectura Inspirada en Claude")
    logger.info("=" * 60)
    logger.info(f"Puerto: {port}")
    logger.info(f"Catálogo: {len(catalog) if catalog else 0} productos")
    logger.info(f"Tipo de cambio inicial: {get_exchange_rate()}")
    logger.info(f"Relevance min score: {RELEVANCE_MIN_SCORE}")
    logger.info(f"Quality thresholds: HIGH={QUALITY_HIGH_THRESHOLD}, MED={QUALITY_MEDIUM_THRESHOLD}")
    logger.info("=" * 60)
    logger.info("Características nuevas:")
    logger.info("  ✅ Context Quality Check pre-LLM")
    logger.info("  ✅ Relevance Scoring general (sin keywords hardcoded)")
    logger.info("  ✅ Forced Code Citation en prompts")
    logger.info("  ✅ Post-validation de códigos y nombres")
    logger.info("  ✅ Fallbacks seguros")
    logger.info("=" * 60)
    
    app.run(host="0.0.0.0", port=port, debug=False)
