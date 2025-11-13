# =========================================================
# Fran 3.10.2 – Bot Mayorista Inteligente (FULL FIX +)
# =========================================================
# - Todos los fixes aplicados
# - Listo para producción
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

# ------------------------------------------------------------
# LOGGER
# ------------------------------------------------------------
logger = logging.getLogger("fran310")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)

logger.info("✅ Imports completados")

# ------------------------------------------------------------
# CONFIG
# ------------------------------------------------------------
OPENAI_API_KEY = (os.environ.get("OPENAI_API_KEY") or "").strip()
if not OPENAI_API_KEY:
    raise RuntimeError("Falta OPENAI_API_KEY")

MODEL_NAME = (os.environ.get("MODEL_NAME") or "gpt-4o").strip()
CATALOG_URL = (
    os.environ.get("CATALOG_URL") or
    "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/main/catalogo_tercom_faiss.csv"
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

MAX_SEARCH_RESULTS = int(os.environ.get("MAX_SEARCH_RESULTS", "40"))
MAX_PRODUCTS_FOR_LLM = int(os.environ.get("MAX_PRODUCTS_FOR_LLM", "15"))
WHATSAPP_MSG_LIMIT = int(os.environ.get("WHATSAPP_MSG_LIMIT", "3500"))
PRODUCTS_PER_CHUNK = int(os.environ.get("PRODUCTS_PER_CHUNK", "30"))

INSTANT_THRESHOLD = 15
ASYNC_QUICK = 40
ASYNC_MEDIUM = 80
MAX_ITEMS = 150
BULK_TIMEOUT = 240
MAX_BULK_ITEMS = 150

REQUEST_HEADERS = {"User-Agent": "FranBot/3.10.2"}

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

# ---- funciones faltantes ----
def validate_search_quality(query, products):
    if not products:
        return {"quality": "none", "score": 0, "suggestion": "Sin resultados"}
    q_words = set(normalize_search_query(query).split())
    scores = []
    for p in products[:10]:
        p_words = set(normalize_search_query(p.get("name","")).split())
        overlap = len(q_words & p_words) / len(q_words) if q_words else 0
        scores.append(overlap)
    avg = sum(scores) / len(scores) if scores else 0
    if avg < 0.3:
        return {"quality": "low", "score": int(avg*100), "suggestion": "¿Marca/modelo/año?"}
    if avg < 0.6:
        return {"quality": "medium", "score": int(avg*100)}
    return {"quality": "high", "score": int(avg*100)}

CATEGORY_KEYWORDS = {
    "bateria": ["bateria", "batería", "battery"],
    "aceite": ["aceite", "lubricante", "oil", "yamalube", "castrol"],
    "filtro": ["filtro", "filter"],
    "cadena": ["cadena", "chain", "transmision"],
    "bujia": ["bujia", "bujía", "spark"],
}

def detect_category_filter(query):
    if not query:
        return None
    q = query.lower()
    for cat, words in CATEGORY_KEYWORDS.items():
        if any(w in q for w in words):
            return cat
    return None

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
# DATABASE – SQLITE RESILIENTE
# ------------------------------------------------------------
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
            CREATE TABLE IF NOT EXISTS session_summary (
                phone TEXT PRIMARY KEY, products_mentioned TEXT, brands_mentioned Text,
                last_intent TEXT, message_count INTEGER DEFAULT 0, updated_at TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS customer_data (
                phone TEXT PRIMARY KEY, name TEXT, address TEXT, notes TEXT,
                created_at TEXT, updated_at TEXT
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
            res = requests.get(EXCHANGE_API_URL, timeout=REQUESTS_TIMEOUT, headers=REQUEST_HEADERS)
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

def get_history_since(phone, days=3, limit=20):
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
        "top_category": max(set(p.get("category", "") for p in products), key=lambda c: sum(1 for p in products if p.get("category") == c), default=""),
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

def update_session_summary(phone, products, brands, intent):
    if not phone:
        return
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT message_count FROM session_summary WHERE phone=?", (phone,))
            row = cur.fetchone()
            count = (row[0] if row else 0) + 1
            conn.execute(
                """INSERT INTO session_summary
                (phone, products_mentioned, brands_mentioned, last_intent, message_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                products_mentioned=excluded.products_mentioned,
                brands_mentioned=excluded.brands_mentioned,
                last_intent=excluded.last_intent,
                message_count=excluded.message_count,
                updated_at=excluded.updated_at""",
                (phone, json.dumps(products), json.dumps(brands), intent, count, datetime.now().isoformat())
            )
    except Exception as e:
        logger.error(f"Error actualizando session_summary: {e}")

def get_session_summary(phone):
    if not phone:
        return None
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT products_mentioned, brands_mentioned, last_intent, message_count "
                "FROM session_summary WHERE phone=?",
                (phone,)
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "products": json.loads(row[0]) if row[0] else [],
                "brands": json.loads(row[1]) if row[1] else [],
                "intent": row[2],
                "count": row[3]
            }
    except Exception as e:
        logger.error(f"Error leyendo session_summary: {e}")
        return None

def save_customer_data(phone, name=None, address=None, notes=None):
    if not phone:
        return
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name, address, notes FROM customer_data WHERE phone=?", (phone,))
            row = cur.fetchone()
            now = datetime.now().isoformat()

            if row:
                new_name = name if name else row[0]
                new_address = address if address else row[1]
                new_notes = notes if notes else row[2]
                conn.execute(
                    "UPDATE customer_data SET name=?, address=?, notes=?, updated_at=? WHERE phone=?",
                    (new_name, new_address, new_notes, now, phone)
                )
            else:
                conn.execute(
                    "INSERT INTO customer_data (phone, name, address, notes, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (phone, name or "", address or "", notes or "", now, now)
                )
    except Exception as e:
        logger.error(f"Error guardando customer_data: {e}")

def get_customer_data(phone):
    if not phone:
        return None
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name, address, notes FROM customer_data WHERE phone=?", (phone,))
            row = cur.fetchone()
            if not row:
                return None
            return {"name": row[0], "address": row[1], "notes": row[2]}
    except Exception as e:
        logger.error(f"Error leyendo customer_data: {e}")
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
# CARRITO – TTL 7 DÍAS
# ------------------------------------------------------------------
def cart_add(phone, code, qty, name, price_ars, price_usd):
    if not phone or not code:
        return False
    try:
        qty = max(1, min(int(qty or 1), 1000))
        price_ars = price_ars.quantize(Decimal("0.01"))
        price_usd = price_usd.quantize(Decimal("0.01"))

        catalog, _ = get_catalog_and_index()
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

def cart_get(phone, max_age_hours=168):  # 7 días
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
        r = requests.get(CATALOG_URL, timeout=REQUESTS_TIMEOUT, headers=REQUEST_HEADERS)
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
            if variant_norm in col:
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

        idx_code = _extract_column(header, ["codigo", "code", "id"])
        idx_name = _extract_column(header, ["producto", "descripcion", "description", "nombre", "name"])
        idx_usd = _extract_column(header, ["usd", "dolar", "precio en dolares", "price_usd"])
        idx_ars = _extract_column(header, ["ars", "pesos", "precio en pesos", "price_ars"])
        idx_brand = _extract_column(header, ["marca", "brand"])
        idx_model = _extract_column(header, ["modelo", "model"])
        idx_category = _extract_column(header, ["categoria", "category", "rubro"])
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
            logger.info(f"FAISS cargado desde disco: {len(catalog)} productos")
            return index, catalog
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
                logger.warning(f"Error cargando cache de embeddings: {e}")

        texts_to_embed = []
        text_indices = []

        for idx, text in enumerate(texts):
            if text not in cache:
                texts_to_embed.append(text)
                text_indices.append(idx)

        if texts_to_embed:
            logger.info(f"Generando embeddings para {len(texts_to_embed)} textos nuevos...")
            vectors = []
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
                        vectors.extend(chunk_vectors)

                        for text, vec in zip(chunk, chunk_vectors):
                            cache[text] = vec

                        break
                    except RateLimitError as e:
                        if retry < max_retries - 1:
                            wait_time = (2 ** retry) * random.uniform(1, 2)
                            logger.warning(f"RateLimitError en embeddings, reintentando en {wait_time:.2f}s...")
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

        index, catalog = load_faiss_index()

        if index and catalog:
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
# BÚSQUEDA – FILTRO LIVIANO + LLM EXPANSOR
# ------------------------------------------------------------------
hybrid_cache = TTLCache(maxsize=1024, ttl=300)

def hybrid_search(query, limit=60):
    if not query:
        return []
    q_norm = normalize_search_query(query)
    products = hybrid_cache.get(q_norm)
    if products is None:
        products = hybrid_search_impl(q_norm, limit)
        hybrid_cache[q_norm] = products

    q_low = query.lower()
    for cat, words in CATEGORY_KEYWORDS.items():
        if any(w in q_low for w in words):
            products = [p for p in products
                        if any(w in p.get("name","").lower() or
                               w in p.get("category","").lower()
                               for w in words)]
            break
    logger.info(f"[SEARCH] query='{query}' -> {len(products)} prod después de filtros")
    return products[:15]   # nunca más de 15

def fuzzy_search(query, limit=100):
    catalog, _ = get_catalog_and_index()
    if not catalog or not query:
        return []
    try:
        names = [p["search_text"] for p in catalog]
        matches = process.extract(query, names, scorer=fuzz.WRatio, limit=limit)  # sin workers
        return [(catalog[idx], score) for _, score, idx in matches if score >= 75]
    except Exception as e:
        logger.error(f"Error en fuzzy_search: {e}")
        return []

def semantic_search(query, top_k=60, max_retries=3):
    catalog, index = get_catalog_and_index()
    if not catalog or index is None or not query:
        return []
    try:
        for retry in range(max_retries):
            try:
                with openai_sem:
                    resp = client.embeddings.create(
                        input=[query],
                        model="text-embedding-3-small",
                        timeout=REQUESTS_TIMEOUT
                    )
                emb = np.array([resp.data[0].embedding]).astype("float32")
                break
            except RateLimitError as e:
                if retry < max_retries - 1:
                    wait_time = (2 ** retry) * random.uniform(1, 2)
                    logger.warning(f"RateLimitError en busqueda semantica, reintentando en {wait_time:.2f}s...")
                    time.sleep(wait_time)
                else:
                    logger.error(f"RateLimitError persistente: {e}")
                    return []

        D, I = index.search(emb, top_k)
        results = []
        for dist, idx in zip(D[0], I[0]):
            if 0 <= idx < len(catalog):
                score = 1.0 / (1.0 + float(dist))
                results.append((catalog[idx], score))
        return results
    except Exception as e:
        logger.error(f"Error en busqueda semantica: {e}")
        return []

def hybrid_search_impl(query, limit=60):
    if not query:
        return []
    # ---- filtro por categoría ANTES de mezclar ----
    q_low = query.lower()
    for cat, words in CATEGORY_KEYWORDS.items():
        if any(w in q_low for w in words):
            fuzzy_raw = fuzzy_search(query, limit=80)
            semantic_raw = semantic_search(query, top_k=60)
            # filtramos
            fuzzy_raw = [(p, s) for p, s in fuzzy_raw if any(w in p.get("name","").lower() or w in p.get("category","").lower() for w in words)]
            semantic_raw = [(p, s) for p, s in semantic_raw if any(w in p.get("name","").lower() or w in p.get("category","").lower() for w in words)]
            break
    else:
        # sin categoría detectada → sin filtro
        fuzzy_raw = fuzzy_search(query, limit=80)
        semantic_raw = semantic_search(query, top_k=60)

    # mezcla igual que antes
    combined = {}
    for prod, score in fuzzy_raw:
        key = prod["code"]
        combined[key] = {"prod": prod, "fuzzy": score / 100.0, "sem": 0.0}

    for prod, score in semantic_raw:
        key = prod["code"]
        if key not in combined:
            combined[key] = {"prod": prod, "fuzzy": 0.0, "sem": score}
        else:
            combined[key]["sem"] = max(combined[key]["sem"], score)

    final = []
    for v in combined.values():
        combined_score = 0.7 * v["sem"] + 0.3 * v["fuzzy"]
        final.append((v["prod"], combined_score))

    final.sort(key=lambda x: x[1], reverse=True)
    return [p for p, _ in final[:limit]]

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
        matches = hybrid_search(product_name, limit=3)
        if matches:
            best = matches[0]
            price_ars = to_decimal_money(best["price_ars"])
            subtotal = (price_ars * requested_qty).quantize(Decimal("0.01"))
            total_quoted += subtotal
            results.append({
                "requested": product_name,
                "found": best["name"],
                "code": best["code"],
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

            matches = hybrid_search(product_name, limit=3)
            if matches:
                best = matches[0]
                price_ars = to_decimal_money(best["price_ars"])
                subtotal = (price_ars * requested_qty).quantize(Decimal("0.01"))
                total_quoted += subtotal
                results.append({
                    "requested": product_name,
                    "found": best["name"],
                    "code": best["code"],
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

# Lanzar 2 workers
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
# INTENT DETECTOR CON LLM
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
# IMPLÍCITO – “3 de cada una”
# ------------------------------------------------------------------
def detect_implicit_cart_action(message, phone):
    patterns = {
        r"(\d+)\s+de\s+cada": "add_each_quantity",
        r"(todo|todos|todas)": "add_all",
        r"(esos|esas|los|las)\s+(quiero|necesito|dame)": "add_referenced"
    }
    msg = message.lower()
    for pat, action in patterns.items():
        m = re.search(pat, msg)
        if m:
            last = get_last_search(phone)
            if last and last.get("products"):
                return {
                    "action": action,
                    "quantity": int(m.group(1)) if action == "add_each_quantity" else 1,
                    "products": last["products"]
                }
    return None

# ------------------------------------------------------------------
# LLM REPLY – EXECUTION CONTEXT
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

SMART_SYSTEM_PROMPT_V2 = f"""
Sos Fran, vendedor mayorista de TERCOM (motopartes, Argentina).

Recibirás:
- [RESULTADO DE BUSQUEDA] → intent, total encontrado, calidad, si vienen chunks
- [TOP N PRODUCTOS] → solo los que ves

Reglas:
- Si total > 15, **decí cuántos encontraste** y que vienen en partes
- Si calidad == "low", **pedí marca/modelo** en vez de listar
- Nunca inventes productos que no estén en [TOP N PRODUCTOS]
- Cerrá con pregunta de venta

=== VALIDACIÓN OBLIGATORIA ===
Antes de listar, descartá cualquier producto que NO sea {{product_type}}.
Si ninguno calza, decí: “No tengo {{product_type}} para {{modelo}}, pero tengo estas alternativas:”
y mostrá los 3 más cercanos.

{BUSINESS_CONTEXT}
"""

def build_execution_summary(ctx):
    lines = []
    lines.append(f"Intent detectado: {ctx['intent_detected']}")
    if ctx["search_executed"]:
        lines.append(f"Query de búsqueda: '{ctx['search_query']}'")
        lines.append(f"Productos encontrados: {ctx['products_found']}")
        lines.append(f"Productos mostrados a ti (LLM): {ctx['products_shown_to_llm']}")
        if ctx["quality_score"] is not None:
            quality_label = "ALTA" if ctx["quality_score"] >= 60 else "MEDIA" if ctx["quality_score"] >= 30 else "BAJA"
            lines.append(f"Calidad de resultados: {quality_label} ({ctx['quality_score']}%)")
        if ctx["filters_applied"]: lines.append(f"Filtros aplicados: {', '.join(ctx['filters_applied'])}")
        if ctx["will_send_chunks"]:
            chunk_info = ctx["chunk_info"]
            lines.append(f"⚠️ IMPORTANTE: Se enviarán {chunk_info['total_chunks']} mensajes adicionales con el listado completo de {chunk_info['total_products']} productos.")
            lines.append("Tu respuesta debe PREPARAR al cliente para recibir estos mensajes. Ejemplo: 'Dale, encontré X productos. Te los mando en partes para que los veas bien.'")
    else: lines.append("No se ejecutó búsqueda de productos")
    if ctx["warnings"]: lines.append(f"Advertencias: {'; '.join(ctx['warnings'])}")
    return "\n".join(lines)

def generate_smart_ai_reply_v2(phone, user_message, catalog_products, execution_context):
    try:
        history = get_history_since(phone, days=3, limit=15)
        user_context = ""

        # ---- prompt personalizado ----
        product_type = ("batería" if any(k in user_message.lower() for k in ("bateria","batería","battery"))
                        else "filtro" if any(k in user_message.lower() for k in ("filtro","filter"))
                        else "cadena" if any(k in user_message.lower() for k in ("cadena","chain"))
                        else "aceite" if any(k in user_message.lower() for k in ("aceite","oil"))
                        else "bujía" if any(k in user_message.lower() for k in ("bujia","bujía","spark"))
                        else "repuesto")
        personalized_prompt = SMART_SYSTEM_PROMPT_V2.replace("{{product_type}}", product_type)

        msgs = [{"role": "system", "content": personalized_prompt}]
        for h in history[-15:]:
            role = "assistant" if h["role"] == "assistant" else "user"
            msgs.append({"role": role, "content": h["content"]})
        if user_context:
            msgs.append({"role": "system", "name": "context", "content": f"[CONTEXTO DE SESION]\n{user_context}"})
        exec_summary = build_execution_summary(execution_context)
        msgs.append({"role": "system", "name": "execution", "content": f"[RESULTADO DE BUSQUEDA]\n{exec_summary}"})

        # ---- filtro ANTES de armar el texto que ve el LLM ----
        if catalog_products:
            filtered = []
            for p in catalog_products:
                if product_type in ("batería","bateria","battery") and any(k in p.get("name","").lower() for k in ("batería","bateria","battery")):
                    filtered.append(p)
                elif product_type in ("filtro","filter") and any(k in p.get("name","").lower() for k in ("filtro","filter")):
                    filtered.append(p)
                elif product_type in ("cadena","chain") and any(k in p.get("name","").lower() for k in ("cadena","chain")):
                    filtered.append(p)
                elif product_type in ("aceite","oil") and any(k in p.get("name","").lower() for k in ("aceite","oil")):
                    filtered.append(p)
                elif product_type in ("bujía","bujia","spark") and any(k in p.get("name","").lower() for k in ("bujía","bujia","spark")):
                    filtered.append(p)
                else:
                    filtered.append(p)   # para "repuesto" u otros, dejamos pasar

            if not filtered:
                # ---- no hay coincidencias ----
                return f"No encontré {product_type}s para esa moto. ¿Me decís marca y modelo exacto?"

            catalog_text = "\n".join([f"- {p['name']} (Cod: {p.get('code', 'N/A')}) - {format_price(Decimal(str(p['price_ars'])))}" for p in filtered])
            msgs.append({"role": "system", "name": "products", "content": f"[TOP {len(filtered)} PRODUCTOS]\n{catalog_text}"})

        msgs.append({"role": "user", "content": user_message})

        with openai_sem:
            resp = client.chat.completions.create(model=MODEL_NAME, messages=msgs, temperature=0.3, max_tokens=500, timeout=REQUESTS_TIMEOUT)
        txt = (resp.choices[0].message.content or "").strip()
        if not txt or len(txt) < 10: return "Uy, tuve un problema. ¿Me repetís?"
        return txt
    except Exception as e:
        logger.error(f"generate_smart_ai_reply_v2 error: {e}")
        return "Uy, tuve un problema técnico. Probá de nuevo en un ratito."

# ------------------------------------------------------------------
# RESPUESTA TÉCNICA ESPECIALIZADA
# ------------------------------------------------------------------
TECH_SYSTEM_PROMPT = """
Sos Fran, vendedor experto en motopartes. Tu objetivo es VENDER RÁPIDO, no educar.

=== BREVEDAD EXTREMA ===
LIMITE ESTRICTO: Máximo 4 líneas (aprox 250 caracteres)

- Respuesta directa a la pregunta
- Sin introducción ni contexto
- Sin explicaciones teóricas
- Cierre con pregunta de venta

Si sentís ganas de decir estas FRASES PROHIBIDAS, FRENA:
- "Para que entiendas..."
- "Históricamente..."
- "Es importante saber que..."
- "Primero déjame explicarte..."
- "Hay varias cosas a considerar..."
- "Técnicamente hablando..."

PLANTILLA OBLIGATORIA:
Línea 1: Diferencia clave directa
Líneas 2-3: Cuál de TUS productos (con precio)
Línea 4: ¿Lo/Los agregamos?

=== REGLAS ANTI-ALUCINACIÓN ===
1. SOLO menciona productos del "Contexto CSV" que te paso
1. NUNCA inventes códigos, marcas o precios
1. Si no está en el contexto, NO LO NOMBRES

=== CONOCIMIENTO TÉCNICO: ULTRA BREVE ===
Podés explicar conceptos, pero en 1 LÍNEA:
- "El sintético dura más pero es más caro"
- "Para frío el 10W, para calor el 20W"

NUNCA termines con:
- "Espero haberte ayudado"
- "Cualquier cosa avisame"
- "Saludos"

Sos vendedor EFICIENTE, no Wikipedia.
"""

def build_technical_answer(phone, user_message, top_products):
    try:
        context_lines = []
        for p in (top_products or [])[:3]:
            context_lines.append(
                f"- {p.get('name','')} (cod {p.get('code','')}) "
                f"marca {p.get('brand','')} modelo {p.get('model','')}"
            )
        ctx = "Contexto CSV:\n" + "\n".join(context_lines) if context_lines else "Contexto CSV: (sin coincidencias exactas)"

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
                max_tokens=400 if detect_intent_llm(user_message).get("intent") == "pregunta_tecnica" else 300,
                timeout=REQUESTS_TIMEOUT
            )
        
        txt = (resp.choices[0].message.content or "").strip()
        
        if not txt or len(txt) < 10:
            return "Te confirmo medidas/compatibilidades y te aviso. ¿Querés que lo deje listo?"
        
        txt = sanitize_llm_response(txt, top_products)
        
        return txt
    except Exception as e:
        logger.error(f"build_technical_answer error: {e}")
        return "Estoy revisando las especificaciones técnicas. ¿Querés que te avise y mientras vemos alternativas?"

# ------------------------------------------------------------------
# VALIDACIÓN ANTI-ALUCINACIÓN Y ANTI-DIVAGACIÓN
# ------------------------------------------------------------------
def sanitize_llm_response(response_text, allowed_products):
    if not response_text:
        return "No pude generar una respuesta. ¿Me repetís?"

    allowed_codes = {p.get("code", "") for p in allowed_products if p.get("code")}
    mentioned_codes = set(re.findall(r'\d{4}/\d{5}-\d{3}', response_text))
    hallucinated = mentioned_codes - allowed_codes

    if hallucinated:
        logger.warning(f"LLM mencionó códigos no permitidos: {hallucinated}")
        if allowed_products:
            product_list = "\n".join([
                f"- {p.get('name', '')} ({p.get('code', '')}) - {format_price(p.get('price_ars', 0))}"
                for p in allowed_products[:5]
            ])
            return (
                f"Dale, mirá lo que tengo disponible:\n\n{product_list}\n\n"
                "¿Cuál te sirve o necesitás que te explique las diferencias?"
            )
        else:
            return "No encontré ese repuesto específico en el catálogo. ¿Me pasás más detalles?"

    lines = [l.strip() for l in response_text.split('\n') if l.strip()]
    if len(lines) > 6:
        logger.warning(f"Respuesta demasiado larga: {len(lines)} líneas")
        if len(lines) >= 4:
            return '\n'.join(lines[-4:]) + "\n\n¿Lo agregamos?"
        else:
            return response_text

    return response_text

# ------------------------------------------------------------------
# IA EMPÁTICA
# ------------------------------------------------------------------
SMART_SYSTEM_PROMPT = f"""
Sos Fran, vendedor mayorista de TERCOM (motopartes, Argentina).

=== MENTALIDAD: VENDEDOR DIRECTO ===
- Argentino natural: che, dale, mira, vos
- Empático PERO eficiente (no terapeuta)
- OBJETIVO: Cerrar venta en menos de 3 mensajes

=== BREVEDAD BRUTAL ===
LIMITE: 5 líneas MAX (salvo listados de productos)
TARGET: 40-60 palabras por respuesta

FRASES PROHIBIDAS que indican que te estás yendo por las ramas:
- "Para que entiendas..."
- "Históricamente..."
- "Es importante saber que..."
- "Primero déjame explicarte..."
- "Hay varias cosas a considerar..."
- "Técnicamente hablando..."

Si sentís ganas de escribir alguna, FRENA y reescribí.

PLANTILLA OBLIGATORIA:
Línea 1: Entender qué busca (o dar productos directo)
Líneas 2-3: Productos con precios O info técnica MÍNIMA
Línea 4-5: Pregunta de cierre (¿lo agregamos? ¿cuál te sirve?)

=== GROUNDING ESTRICTO ===
NUNCA menciones productos que no te pasé
- Si te doy lista de productos, SOLO menciona ESOS
- NO inventes códigos, marcas o precios
- NO digas "también tengo X" si X no está en tu lista

=== CONOCIMIENTO TÉCNICO: ULTRA BREVE ===
Podés explicar conceptos, pero en 1 LÍNEA:
- "El sintético dura más pero es más caro"
- "Para frío el 10W, para calor el 20W"

=== CIERRE DE VENTA OBLIGATORIO ===
SIEMPRE terminá con una de estas:
- "¿Lo agregamos al carrito?"
- "¿Cuál te sirve?"
- "¿Confirmamos?"
- "¿Qué cantidad necesitás?"
- "¿Paso presupuesto?"

NUNCA termines con:
- "Espero haberte ayudado"
- "Cualquier cosa avisame"
- "Saludos"

{BUSINESS_CONTEXT}

Sos vendedor que SABE de motos, ENTIENDE a la gente, pero CIERRA VENTAS.
No sos un chatbot genérico. Sos un tipo que vende repuestos y quiere ayudar AL CLIENTE A COMPRAR.
"""

def build_enhanced_context(phone, user_message):
    if not phone:
        return ""

    try:
        session = get_session_summary(phone)
        search_hist = get_search_history(phone, limit=5)
        customer = get_customer_data(phone)
        pending = get_pending_action(phone)

        context_parts = []

        if session and session.get("count", 0) > 0:
            context_parts.append(f"[SESION: {session['count']} mensajes")
            if session.get("brands"):
                context_parts.append(f", marcas: {', '.join(session['brands'][:3])}")
            if session.get("products"):
                context_parts.append(f", productos: {', '.join(session['products'][:3])}")
            context_parts.append("]")

        if search_hist:
            context_parts.append(f"\n[BUSQUEDAS PREVIAS: {len(search_hist)} cotizaciones")
            for i, s in enumerate(search_hist[:3], 1):
                prods = s.get("products", [])
                if prods:
                    context_parts.append(f"\n  {i}. {s.get('query', '')}: {len(prods)} items")
            context_parts.append("]")

        if customer and customer.get("name"):
            context_parts.append(f"\n[CLIENTE: {customer['name']}")
            if customer.get("address"):
                context_parts.append(f", {customer['address']}")
            context_parts.append("]")

        if pending:
            action_type = pending.get("action_type", "")
            context_text = pending.get("context", "")
            context_parts.append(f"\n[ACCION PENDIENTE: {action_type}")
            if context_text:
                context_parts.append(f" - {context_text}")
            context_parts.append("]")

        brands_mentioned = []
        products_mentioned = []
        lower_msg = user_message.lower()

        brand_keywords = ["yamaha", "honda", "suzuki", "zanella", "rouser", "guerrero", "corven", "gilera", "motomel", "bajaj", "ktm"]
        product_keywords = ["aceite", "filtro", "bujia", "pastilla", "cadena", "kit", "amortiguador", "bateria", "neumatico"]

        for brand in brand_keywords:
            if brand in lower_msg:
                brands_mentioned.append(brand)

        for product in product_keywords:
            if product in lower_msg:
                products_mentioned.append(product)

        if brands_mentioned or products_mentioned:
            intent = "search" if any(x in lower_msg for x in ["busca", "tenes", "precio"]) else "chat"
            update_session_summary(phone, products_mentioned, brands_mentioned, intent)

        return "".join(context_parts) if context_parts else ""
    except Exception as e:
        logger.error(f"Error en build_enhanced_context: {e}")
        return ""

def generate_smart_ai_reply(phone, user_message, catalog_products):
    try:
        history = get_history_since(phone, days=3, limit=15)
        context = build_enhanced_context(phone, user_message)

        msgs = [{"role": "system", "content": SMART_SYSTEM_PROMPT}]

        for h in history[-15:]:
            role = "assistant" if h["role"] == "assistant" else "user"
            msgs.append({"role": role, "content": h["content"]})

        if context:
            msgs.append({"role": "user", "content": f"{context}\n\nMensaje: {user_message}"})
        else:
            msgs.append({"role": "user", "content": f"Mensaje: {user_message}"})

        if catalog_products:
            catalog_text = "\n".join([
                f"- {p['name']} (Cod: {p.get('code', 'N/A')}) - {format_price(Decimal(str(p['price_ars'])))}"
                for p in catalog_products[:8]
            ])
            msgs.append({
                "role": "assistant",
                "content": f"(Productos encontrados en catalogo)\n{catalog_text}"
            })

        with openai_sem:
            resp = client.chat.completions.create(
                model=MODEL_NAME,
                messages=msgs,
                temperature=0.3,
                max_tokens=400 if detect_intent_llm(user_message).get("intent") == "pregunta_tecnica" else 300,
                timeout=REQUESTS_TIMEOUT
            )

        txt = (resp.choices[0].message.content or "").strip()

        if not txt or len(txt) < 10:
            return "Uy, tuve un problema. ¿Me repetis?"

        txt = sanitize_llm_response(txt, catalog_products)

        return txt

    except Exception as e:
        logger.error(f"IA fallo: {e}", exc_info=True)
        return "Uy, tuve un problema tecnico. Proba de nuevo en un ratito."

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
# AGENTE PRINCIPAL – VERSIÓN 3.10.2
# =========================================================
def run_agent(phone, user_message):
    start_time = time.time()
    save_message(phone, user_message, "user")

    if not rate_limit_check(phone):
        save_message(phone, "Demasiados mensajes, esperá un minuto.", "assistant")
        return "Demasiados mensajes, esperá un minuto."

    # 1. Detectar intent
    intent_data = detect_intent_llm(user_message)
    intent = intent_data.get("intent", "desconocido")
    query_for_search = intent_data.get("query") or user_message

    # 2. CREAR EXECUTION CONTEXT
    execution_context = {
        "intent_detected": intent,
        "search_query": query_for_search,
        "search_executed": False,
        "products_found": 0,
        "products_shown_to_llm": 0,
        "filters_applied": [],
        "quality_score": None,
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

    # 4. Detectar “3 de cada una” implícito
    implicit = detect_implicit_cart_action(user_message, phone)
    if implicit:
        products = implicit["products"][:MAX_PRODUCTS_FOR_LLM]
        qty = implicit["quantity"]
        total = sum(to_decimal_money(p["price_ars"]) * qty for p in products)
        reply = (
            f"Dale! Te preparo {qty} unidades de cada uno:\n"
            f"Total aprox: {format_price(total)}\n\n"
            "¿Confirmás? (decime 'si' o 'dale')"
        )
        save_pending_action(phone, "add_each_quantity", {"qty": qty, "products": products}, context=f"{qty} de cada uno")
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "implicit_cart", len(products))
        log_performance(phone, "implicit_cart", time.time()-start_time, len(products))
        return reply

    # 5. Búsqueda + quality + filtros
    products = []
    if intent in {"busqueda_catalogo", "pregunta_tecnica", "desconocido"}:
        products = hybrid_search(query_for_search, limit=MAX_SEARCH_RESULTS)
        execution_context["search_executed"] = True
        execution_context["products_found"] = len(products)
        quality = validate_search_quality(query_for_search, products)
        execution_context["quality_score"] = quality["score"]
        if detect_category_filter(query_for_search):
            execution_context["filters_applied"].append(f"category: {detect_category_filter(query_for_search)}")
        if len(products) > MAX_PRODUCTS_FOR_LLM and quality["quality"] == "low":
            reply = (
                f"Encontré {len(products)} productos pero algunos no calzan bien.\n\n"
                f"💡 {quality['suggestion']}\n\n"
                "¿Mostramos igual o refinamos?"
            )
            save_message(phone, reply, "assistant")
            log_performance(phone, intent, time.time()-start_time, len(products))
            return reply
        if products:
            save_last_search(phone, [
                {"code": p["code"], "name": p["name"], "price_ars": p["price_ars"], "price_usd": p["price_usd"], "qty": 1}
                for p in products[:150]
            ], query_for_search)
        execution_context["products_shown_to_llm"] = min(len(products), MAX_PRODUCTS_FOR_LLM)
        execution_context["will_send_chunks"] = len(products) > MAX_PRODUCTS_FOR_LLM
        if execution_context["will_send_chunks"]:
            num_chunks = (len(products) + PRODUCTS_PER_CHUNK - 1) // PRODUCTS_PER_CHUNK
            execution_context["chunk_info"] = {"total_chunks": num_chunks, "products_per_chunk": PRODUCTS_PER_CHUNK, "total_products": len(products)}

    # 6. Generar respuesta con execution context
    if intent == "pregunta_tecnica":
        reply = build_technical_answer(phone, user_message, products[:8])
    else:
        reply = generate_smart_ai_reply_v2(phone, user_message, products[:MAX_PRODUCTS_FOR_LLM], execution_context)

    # 7. Enviar chunks DESPUÉS de la respuesta inicial (solo si quality no es low)
    if execution_context["will_send_chunks"]:
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
try:
    from twilio.rest import Client as TwilioClient
    from twilio.request_validator import RequestValidator
except Exception:
    TwilioClient = None
    RequestValidator = None

twilio_rest_available = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and TwilioClient)
twilio_rest_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if twilio_rest_available else None
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN) if (RequestValidator and TWILIO_AUTH_TOKEN) else None

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
        logger.info(f"Body length: {len(request.form.get('Body', ''))}")
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

        reply = run_agent(from_number, message_body)

        logger.info(f"Respuesta generada: {len(reply)} caracteres")
        logger.info(f"Preview: {reply[:100]}...")
        logger.info(f"Longitud de respuesta: {len(reply)} caracteres")

        if len(reply) <= WHATSAPP_MSG_LIMIT:
            logger.info("Mensaje corto, usando TwiML")
            resp = MessagingResponse()
            resp.message(reply)
            logger.info(f"Respuesta TwiML generada: {reply[:100]}...")
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
    return jsonify({"status": "ok", "version": "3.10.2"}), 200


# ------------------------------------------------------------------
# ✅ Cargar índice y cache al arrancar (evita regenerar embeddings)
catalog, index = load_faiss_index()
if not (catalog and index):
    catalog, index = get_catalog_and_index()
else:
    logger.info("✅ Índice FAISS encontrado en disco: %s productos", len(catalog))


# ------------------------------------------------------------------
# ✅ Fuerza creación de tablas al arrancar el contenedor
init_db()


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Iniciando Fran 3.10.2 en puerto {port}")
    logger.info(f"Catalogo: {len(catalog) if catalog else 0} productos")
    logger.info(f"TC inicial: {get_exchange_rate()}")
    app.run(host="0.0.0.0", port=port, debug=False)
