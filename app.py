# =========================================================
# Fran 3.9.3 – Bot Mayorista Inteligente (CRITICAL FIXES)
# =========================================================
# Fixes incluidos desde 3.9.2:
# – Worker: try/except global + respawn automático
# – openai_sem = Semaphore(5)
# – cart_add devuelve bool
# – DB: cerrar conexión antes de reintentar
# – Lock en embeddings cache
# – Índice en expires_at
# – max_tokens dinámico
# – Limpieza de trabajos antiguos
# – Rate limiting en API REST
# – Validación de carrito antes de confirmar
#
# Fixes CRÍTICOS en 3.9.3:
# – init_db() ejecutado FUERA de if __name__ (fix DB tables)
# – Worker logging: catch Empty específicamente (no spam logs)
# – Verificación de hybrid_search disponible
# =========================================================

import os, json, csv, io, sqlite3, logging, re, unicodedata, time, threading, pickle, random, hashlib
from datetime import datetime, timedelta
from collections import defaultdict
from functools import lru_cache
from contextlib import contextmanager
from threading import Lock, Semaphore
from queue import Queue, Empty
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

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
logger = logging.getLogger("fran39")
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
REQUESTS_TIMEOUT = int(os.environ.get("REQUESTS_TIMEOUT", "30"))
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "tercom.db")
FAISS_INDEX_PATH = os.environ.get("FAISS_INDEX_PATH", "catalog.faiss")
FAISS_MAPPING_PATH = os.environ.get("FAISS_MAPPING_PATH", "catalog_mapping.pkl")

_safe_catalog_hash = CATALOG_URL.replace("/", "_").replace(":", "_").replace(".", "_")[-40:]
EMBEDDINGS_CACHE_PATH = f"embeddings_cache_{_safe_catalog_hash}.pkl"

MAX_SEARCH_RESULTS = int(os.environ.get("MAX_SEARCH_RESULTS", "60"))
MAX_PRODUCTS_FOR_LLM = int(os.environ.get("MAX_PRODUCTS_FOR_LLM", "20"))
WHATSAPP_MSG_LIMIT = int(os.environ.get("WHATSAPP_MSG_LIMIT", "3500"))
PRODUCTS_PER_CHUNK = int(os.environ.get("PRODUCTS_PER_CHUNK", "40"))

INSTANT_THRESHOLD = 20
ASYNC_QUICK = 50
ASYNC_MEDIUM = 100
MAX_ITEMS = 200
BULK_TIMEOUT = 300
MAX_BULK_ITEMS = 200

REQUEST_HEADERS = {"User-Agent": "FranBot/3.9.3"}

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

openai_sem = Semaphore(5)

exchange_cache = {"rate": None, "timestamp": None}
EXCHANGE_CACHE_TTL = 3600

user_requests = defaultdict(list)
RATE_LIMIT = 30
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

def sanitize_input(text, max_length=2000):
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

# ------------------------------------------------------------
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
                phone TEXT PRIMARY KEY, products_json TEXT, query TEXT, timestamp TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS session_summary (
                phone TEXT PRIMARY KEY, products_mentioned TEXT, brands_mentioned TEXT,
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

def cleanup_old_jobs():
    try:
        with get_db_connection() as conn:
            cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
            conn.execute("DELETE FROM bulk_jobs WHERE created_at < ? AND status='processing'", (cutoff,))
            logger.info("Limpieza de trabajos antiguos completada")
    except Exception as e:
        logger.error(f"Error en cleanup_old_jobs: {e}")

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

# ------------------------------------------------------------
# ANALYTICS
# ------------------------------------------------------------
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

# ------------------------------------------------------------
# TIPO DE CAMBIO
# ------------------------------------------------------------
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

# ------------------------------------------------------------
# RATE LIMIT
# ------------------------------------------------------------
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

# ------------------------------------------------------------
# PERSISTENCIA
# ------------------------------------------------------------
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

def get_history_since(phone, days=3, limit=30):
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
                """INSERT INTO last_search (phone, products_json, query, timestamp)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                products_json=excluded.products_json,
                query=excluded.query,
                timestamp=excluded.timestamp""",
                (phone, json.dumps(serializable, ensure_ascii=False), query or "", datetime.now().isoformat())
            )
    except Exception as e:
        logger.error(f"Error guardando last_search: {e}")

def get_last_search(phone):
    if not phone:
        return None
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT products_json, query FROM last_search WHERE phone=?", (phone,))
            row = cur.fetchone()
            if not row:
                return None
            products = json.loads(row[0])
            return {"products": products, "query": row[1]}
    except Exception as e:
        logger.error(f"Error leyendo last_search: {e}")
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

# ------------------------------------------------------------
# CATÁLOGO
# ------------------------------------------------------------
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
            batch = 512
            max_retries = 5

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

# ------------------------------------------------------------
# BÚSQUEDA
# ------------------------------------------------------------
SEARCH_ALIASES = {
    "yama": "yamaha",
    "zan": "zanella",
    "hond": "honda",
    "suzu": "suzuki",
    "baj": "bajaj",
    "rouser": "bajaj rouser",
    "gil": "gilera",
    "corv": "corven",
    "motom": "motomel",
    "guerr": "guerrero",
    "twister": "honda twister",
    "cbx": "honda cbx",
    "wave": "honda wave",
}

def normalize_search_query(query):
    if not query:
        return ""
    q = strip_accents(query.lower())
    for alias, repl in SEARCH_ALIASES.items():
        q = q.replace(alias, repl)
    q = re.sub(r"[^a-z0-9\s]", " ", q)
    return " ".join(q.split())

def fuzzy_search(query, limit=200):
    catalog, _ = get_catalog_and_index()
    if not catalog or not query:
        return []
    try:
        names = [p["search_text"] for p in catalog]
        matches = process.extract(query, names, scorer=fuzz.WRatio, limit=limit, workers=-1)
        results = []
        for _, score, idx in matches:
            if score >= 60 and idx < len(catalog):
                results.append((catalog[idx], score))
        return results
    except Exception as e:
        logger.error(f"Error en fuzzy_search: {e}")
        return []

def semantic_search(query, top_k=400, max_retries=3):
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

hybrid_cache = TTLCache(maxsize=2048, ttl=600)

def hybrid_search_impl(query, limit=120):
    if not query:
        return []
    try:
        fuzzy_results = fuzzy_search(query, limit=200)
        semantic_results = semantic_search(query, top_k=400)

        combined = {}
        for prod, score in fuzzy_results:
            key = prod["code"]
            combined[key] = {"prod": prod, "fuzzy": score / 100.0, "sem": 0.0}

        for prod, score in semantic_results:
            key = prod["code"]
            if key not in combined:
                combined[key] = {"prod": prod, "fuzzy": 0.0, "sem": score}
            else:
                combined[key]["sem"] = max(combined[key]["sem"], score)

        final = []
        for v in combined.values():
            combined_score = 0.6 * v["sem"] + 0.4 * v["fuzzy"]
            final.append((v["prod"], combined_score))

        final.sort(key=lambda x: x[1], reverse=True)
        return [p for p, _ in final[:limit]]
    except Exception as e:
        logger.error(f"Error en hybrid_search: {e}")
        return []

def hybrid_search(query, limit=120):
    if not query:
        return []
    query_normalized = normalize_search_query(query)
    if query_normalized in hybrid_cache:
        return hybrid_cache[query_normalized]
    res = hybrid_search_impl(query_normalized, limit)
    hybrid_cache[query_normalized] = res
    return res

logger.info("✅ hybrid_search definida correctamente")

# ------------------------------------------------------------
# CARRITO – VALIDA EXISTENCIA
# ------------------------------------------------------------
def cart_add(phone, code, qty, name, price_ars, price_usd):
    if not phone or not code:
        return False
    try:
        qty = max(1, min(int(qty or 1), 1000))
        price_ars = price_ars.quantize(Decimal("0.01"))
        price_usd = price_usd.quantize(Decimal("0.01"))

        catalog, _ = get_catalog_and_index()
        prod = next((p for p in catalog if p["code"] == code), None)
        if not prod:
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

def cart_get(phone, max_age_hours=24):
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

# ------------------------------------------------------------
# LISTAS MASIVAS
# ------------------------------------------------------------
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
    """Worker con logging mejorado - no spam en logs"""
    while True:
        try:
            job = bulk_queue.get(timeout=1)
            process_bulk_async(job)
            bulk_queue.task_done()
        except Empty:
            # Esto es normal cuando no hay trabajos, no logear
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

# ------------------------------------------------------------
# MULTI-MENSAJE
# ------------------------------------------------------------
def send_long_message(phone, text, chunk_size=1200):
    if not twilio_rest_client:
        logger.error("Twilio client no disponible")
        return False

    if not phone:
        logger.error("Phone number vacio")
        return False

    if not text:
        logger.warning("Texto vacio, no hay nada que enviar")
        return True

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

# ------------------------------------------------------------
# INTENT DETECTOR CON LLM
# ------------------------------------------------------------
INTENT_SYSTEM_PROMPT = """
Sos un clasificador de intenciones para un vendedor mayorista (WhatsApp).
No respondas al usuario. No agregues explicaciones.
Tu única salida será un JSON válido (una línea), con este esquema:
{"intent":"<uno de: saludo|busqueda_catalogo|pregunta_tecnica|pedido_codigo|agregar_carrito|ver_carrito|vaciar_carrito|confirmar|cancelar|desconocido>", "query":"<texto util para buscar o ''>"}

Criterios rápidos:
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
                    {"role": "user", "content": msg.strip()[:800]}
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
        
        return {"intent": intent, "query": query}
    except Exception as e:
        logger.error(f"detect_intent_llm error: {e}")
        return {"intent": "desconocido", "query": ""}

# ------------------------------------------------------------
# RESPUESTA TÉCNICA ESPECIALIZADA
# ------------------------------------------------------------
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
            "content": f"{ctx}\n\nPregunta del cliente: {user_message[:800]}"
        })
        
        with openai_sem:
            resp = client.chat.completions.create(
                model=MODEL_NAME,
                messages=msgs,
                temperature=0.3,
                max_tokens=500 if detect_intent_llm(user_message).get("intent") == "pregunta_tecnica" else 350,
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

# ------------------------------------------------------------
# VALIDACIÓN ANTI-ALUCINACIÓN Y ANTI-DIVAGACIÓN
# ------------------------------------------------------------
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
    if len(lines) > 8:
        logger.warning(f"Respuesta demasiado larga: {len(lines)} líneas")
        if len(lines) >= 4:
            return '\n'.join(lines[-4:]) + "\n\n¿Lo agregamos?"
        else:
            return response_text

    return response_text

# ------------------------------------------------------------
# IA EMPÁTICA
# ------------------------------------------------------------
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
        history = get_history_since(phone, days=3, limit=20)
        context = build_enhanced_context(phone, user_message)

        msgs = [{"role": "system", "content": SMART_SYSTEM_PROMPT}]

        for h in history[-20:]:
            role = "assistant" if h["role"] == "assistant" else "user"
            msgs.append({"role": role, "content": h["content"]})

        if context:
            msgs.append({"role": "user", "content": f"{context}\n\nMensaje: {user_message}"})
        else:
            msgs.append({"role": "user", "content": f"Mensaje: {user_message}"})

        if catalog_products:
            catalog_text = "\n".join([
                f"- {p['name']} (Cod: {p.get('code', 'N/A')}) - {format_price(Decimal(str(p['price_ars'])))}"
                for p in catalog_products[:10]
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
                max_tokens=500 if detect_intent_llm(user_message).get("intent") == "pregunta_tecnica" else 350,
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

# ------------------------------------------------------------
# FORMATO RESULTADOS
# ------------------------------------------------------------
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

# ------------------------------------------------------------
# AGENTE PRINCIPAL CON MEJORAS COGNITIVAS
# ------------------------------------------------------------
def run_agent(phone, user_message):
    if not phone or not user_message:
        return "Error: mensaje vacio"

    start_time = time.time()
    save_message(phone, user_message, "user")

    intent_data = detect_intent_llm(user_message)
    intent = intent_data.get("intent", "desconocido")
    query_for_search = intent_data.get("query") or user_message

    logger.info(f"Intent detectado: {intent}, query: {query_for_search[:50]}")

    pending = get_pending_action(phone)

    if intent == "confirmar":
        if not pending:
            reply = "No tengo ninguna acción pendiente para confirmar. ¿Qué necesitás?"
        else:
            action_type = pending["action_type"]
            action_data = pending["action_data"]
            
            if action_type == "add_to_cart":
                current_items = cart_get(phone)
                current_cart_hash = hashlib.md5(json.dumps(sorted([(i[0], i[1]) for i in current_items])).encode()).hexdigest()
                if pending["action_data"].get("cart_hash") != current_cart_hash:
                    return "El carrito cambió. ¿Confirmás con los productos actuales?"

                catalog, _ = get_catalog_and_index()
                products = action_data.get("products", [])
                added_count = 0
                total_added = Decimal("0")
                
                for p in products:
                    code = p.get("code", "")
                    qty = int(p.get("qty", 1))
                    ok, norm = validate_tercom_code(code)
                    if ok:
                        prod = next((x for x in catalog if x["code"] == norm), None)
                        if prod:
                            price_ars = to_decimal_money(prod["price_ars"])
                            price_usd = to_decimal_money(prod["price_usd"])
                            if cart_add(phone, norm, qty, prod["name"], price_ars, price_usd):
                                added_count += 1
                                total_added += price_ars * qty
                
                clear_pending_action(phone)
                reply = f"Perfecto! Agregué {added_count} ítems al carrito por {format_price(total_added)}.\n\n¿Pasame tus datos para el presupuesto: nombre, dirección y teléfono?"
            else:
                clear_pending_action(phone)
                reply = "Listo, confirmado!"
        
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "confirmar", 0)
        log_performance(phone, "confirmar", time.time()-start_time, 0)
        return reply

    if intent == "cancelar":
        if not pending:
            reply = "No hay nada pendiente para cancelar. ¿En qué te puedo ayudar?"
        else:
            clear_pending_action(phone)
            reply = "Dale, cancelado. ¿Qué más necesitás?"
        
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "cancelar", 0)
        log_performance(phone, "cancelar", time.time()-start_time, 0)
        return reply

    if intent == "saludo":
        reply = "¡Hola! Soy Fran de TERCOM 👋 ¿Qué repuesto necesitás? Decime marca/modelo y te ayudo."
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

    if intent == "agregar_carrito":
        last = get_last_search(phone)
        if not last or not last.get("products"):
            reply = "No tengo productos recientes para agregar. Buscá algo primero y te preparo el carrito."
        else:
            products = last["products"][:200]
            total_estimate = sum(to_decimal_money(p.get("price_ars", 0)) * int(p.get("qty", 1)) for p in products)
            save_pending_action(
                phone,
                action_type="add_to_cart",
                action_data={"products": products},
                context=f"{len(products)} productos por {format_price(total_estimate)}"
            )
            reply = (
                f"Dale! Te agrego {len(products)} productos por {format_price(total_estimate)} aprox.\n\n"
                "¿Confirmás? (decime 'si' o 'dale' para confirmar, 'no' para cancelar)"
            )
        
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "agregar_carrito", len(products) if last else 0)
        log_performance(phone, "agregar_carrito", time.time()-start_time, 0)
        return reply

    if intent == "pedido_codigo":
        m = re.search(r"\d{4}/\d{5}-\d{3}", user_message)
        if m:
            code = m.group(0)
            catalog, _ = get_catalog_and_index()
            found = [p for p in catalog if p["code"] == code]
            if found:
                p = found[0]
                reply = f"{p['name']} (Cod: {code}) – {format_price(p['price_ars'])}. ¿Cuántas unidades querés?"
            else:
                reply = f"El código {code} no figura en mi lista. ¿Tenés otro o buscamos por nombre?"
        else:
            reply = "Pasame el código completo así: 1234/56789-012"
        save_message(phone, reply, "assistant")
        log_interaction(phone, user_message, "pedido_codigo", 1)
        log_performance(phone, "pedido_codigo", time.time()-start_time, 1)
        return reply

    is_bulk, item_count = is_bulk_list_request(user_message)

    if is_bulk:
        log_interaction(phone, user_message, "bulk_quote", item_count)

        if item_count < INSTANT_THRESHOLD:
            result = process_bulk_sync(phone, user_message)
            if result.get("success") and result.get("results"):
                products_for_save = [
                    {
                        "code": r["code"],
                        "name": r["found"],
                        "price_ars": r["price_unit"],
                        "price_usd": float(Decimal(str(r["price_unit"])) / get_exchange_rate()),
                        "qty": int(r["quantity"])
                    }
                    for r in result["results"]
                ]
                save_last_search(phone, products_for_save, "Lista")
                save_to_search_history(phone, products_for_save, "Lista")

            found = result.get("found_count", 0)
            not_found = result.get("not_found_count", 0)
            total = result.get("total_quoted", 0)

            lines = ["Listo! Acá está tu cotización:\n"]
            lines.append(f"{found} productos encontrados")
            if not_found > 0:
                lines.append(f"{not_found} sin stock")
            lines.append(f"\nTOTAL: {format_price(Decimal(str(total)))}")
            lines.append("\n¿Los agregamos? Decime: dale")

            final = "\n".join(lines)
        else:
            if item_count < ASYNC_QUICK:
                wait_msg = f"Dale! Son {item_count} productos, te preparo la cotización y vuelvo con vos en un minuto"
            elif item_count < ASYNC_MEDIUM:
                wait_msg = f"Uh, lista grande! Son {item_count} productos\nDame 2-3 minutos que te armo todo y te aviso"
            else:
                wait_msg = f"Tremenda lista che! {item_count} productos\nMe va a llevar unos 4-5 minutos\nSeguí navegando tranqui, te aviso"

            job_id = create_bulk_job(phone, user_message, item_count)

            if job_id:
                final = wait_msg
            else:
                final = "Uy, tuve un problema. ¿Me mandás la lista de nuevo?"

        elapsed = time.time() - start_time
        log_performance(phone, "bulk_quote", elapsed, item_count)

        save_message(phone, final, "assistant")
        return final

    products = []
    if intent in {"busqueda_catalogo", "pregunta_tecnica", "desconocido"}:
        # Verificar que hybrid_search está disponible
        try:
            products = hybrid_search(query_for_search, limit=MAX_SEARCH_RESULTS)
        except NameError:
            logger.error("hybrid_search no disponible, usando fallback")
            catalog, _ = get_catalog_and_index()
            products = [p for p in catalog if query_for_search.lower() in p.get("name", "").lower()][:MAX_SEARCH_RESULTS]
        
        total = len(products)
        log_interaction(phone, user_message, intent, total)

        if total:
            save_last_search(phone, [
                {
                    "code": p["code"],
                    "name": p["name"],
                    "price_ars": p["price_ars"],
                    "price_usd": p["price_usd"],
                    "qty": 1
                }
                for p in products[:200]
            ], query_for_search)
            save_to_search_history(phone, [
                {
                    "code": p["code"],
                    "name": p["name"],
                    "price_ars": p["price_ars"],
                    "price_usd": p["price_usd"],
                    "qty": 1
                }
                for p in products[:200]
            ], query_for_search)

        if total == 0:
            reply = (
                "No encontré ese repuesto en el catálogo.\n\n"
                "¿Pasame marca, modelo y año de la moto y te busco lo más parecido?"
            )
            save_message(phone, reply, "assistant")
            log_performance(phone, intent, time.time()-start_time, 0)
            return reply

        if total > MAX_PRODUCTS_FOR_LLM:
            header = (
                f"Encontré *{total} productos* para: {user_message}\n\n"
                "Te los mando en partes así WhatsApp no los corta"
            )
            chunks = [products[i:i + PRODUCTS_PER_CHUNK] for i in range(0, total, PRODUCTS_PER_CHUNK)]
            full_text = [header]
            for idx, ch in enumerate(chunks, 1):
                full_text.append(f"\n━━━ Bloque {idx}/{len(chunks)} ({len(ch)} items) ━━━")
                full_text.append(format_search_results(ch))
            final = "\n".join(full_text)

            elapsed = time.time() - start_time
            log_performance(phone, intent, elapsed, total)

            save_message(phone, f"[listado largo {total}]", "assistant")
            return final

        if intent == "pregunta_tecnica":
            reply = build_technical_answer(phone, user_message, products[:10])
            save_message(phone, reply, "assistant")
            log_performance(phone, intent, time.time()-start_time, len(products))
            return reply

        reply = generate_smart_ai_reply(phone, user_message, products[:10])
        save_message(phone, reply, "assistant")
        log_performance(phone, intent, time.time()-start_time, len(products))
        return reply

    reply = "Dale, contame qué repuesto necesitás (marca, modelo, año) y te paso opciones."
    save_message(phone, reply, "assistant")
    log_interaction(phone, user_message, "chat", 0)
    log_performance(phone, "chat", time.time()-start_time, 0)
    return reply

logger.info("✅ run_agent definido correctamente")

# ------------------------------------------------------------
# API REST
# ------------------------------------------------------------
@app.route("/api/cart/<phone>", methods=["GET"])
def api_get_cart(phone):
    try:
        if not rate_limit_check(phone):
            return jsonify({"ok": False, "error": "Rate limit excedido"}), 429
        items = cart_get(phone)
        total, discount = cart_totals(phone)
        return jsonify({
            "ok": True,
            "items": [{"code": c, "qty": q, "name": n, "price": float(p)} for c, q, n, p in items],
            "total": float(total),
            "discount": float(discount)
        })
    except Exception as e:
        logger.error(f"Error en api_get_cart: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/cart/<phone>/add", methods=["POST"])
def api_add_to_cart(phone):
    try:
        if not rate_limit_check(phone):
            return jsonify({"ok": False, "error": "Rate limit excedido"}), 429
        
        data = request.get_json()
        code = data.get("code")
        qty = int(data.get("qty", 1))
        
        if not code:
            return jsonify({"ok": False, "error": "Codigo requerido"}), 400
        
        catalog, _ = get_catalog_and_index()
        prod = next((p for p in catalog if p["code"] == code), None)
        
        if not prod:
            return jsonify({"ok": False, "error": "Producto no encontrado"}), 404
        
        price_ars = to_decimal_money(prod["price_ars"])
        price_usd = to_decimal_money(prod["price_usd"])
        
        success = cart_add(phone, code, qty, prod["name"], price_ars, price_usd)
        
        if success:
            return jsonify({"ok": True, "message": "Agregado al carrito"})
        else:
            return jsonify({"ok": False, "error": "No se pudo agregar"}), 500
    except Exception as e:
        logger.error(f"Error en api_add_to_cart: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/search", methods=["GET"])
def api_search():
    try:
        query = request.args.get("q", "").strip()
        limit = min(int(request.args.get("limit", 20)), 100)
        
        if not query:
            return jsonify({"ok": False, "error": "Query requerida"}), 400
        
        products = hybrid_search(query, limit=limit)
        
        return jsonify({
            "ok": True,
            "count": len(products),
            "products": products[:limit]
        })
    except Exception as e:
        logger.error(f"Error en api_search: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "version": "3.9.3"}), 200

# ------------------------------------------------------------
# WEBHOOK WHATSAPP
# ------------------------------------------------------------
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
        message_body = request.form.get("Body", "").strip()

        if not from_number or not message_body:
            logger.warning("Mensaje sin From o Body")
            return Response("<Response></Response>", mimetype="text/xml")

        message_body = sanitize_input(message_body, max_length=2000)
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
        logger.exception(f"Error critico en webhook: {e}")
        try:
            resp = MessagingResponse()
            resp.message("Uy, tuve un problema tecnico. Proba de nuevo en un ratito.")
            return Response(str(resp), mimetype="text/xml")
        except:
            return Response("<Response></Response>", mimetype="text/xml")

# ------------------------------------------------------------
# INICIALIZACIÓN - CRÍTICO: EJECUTAR ANTES DE GUNICORN FORK
# ------------------------------------------------------------
init_db()
cleanup_old_jobs()

logger.info("Precargando catalogo...")
_ = get_catalog_and_index()
logger.info("Sistema listo.")

# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Iniciando Fran 3.9.3 en puerto {port}")
    logger.info(f"Modelo LLM: {MODEL_NAME}")
    catalog, _ = get_catalog_and_index()
    logger.info(f"Catalogo: {len(catalog) if catalog else 0} productos")
    logger.info(f"TC inicial: {get_exchange_rate()}")
    app.run(host="0.0.0.0", port=port, debug=False)
