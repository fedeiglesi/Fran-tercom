# coding: utf-8

# =========================================================

# Fran 3.8 - Bot Mayorista Inteligente COMPLETO

# =========================================================

# Mejoras v3.8:

# Catálogo enriquecido (OEM, keywords, compatibilidad)
# Multi-mensaje para listas largas
# Respuestas expandidas mayoristas (hasta 500 productos)
# FAISS persistente optimizado
# Todas las funciones de 3.7 incluidas

# =========================================================

import os
import json
import csv
import io
import sqlite3
import logging
import re
import unicodedata
import time
import threading
import pickle
from datetime import datetime, timedelta
from collections import defaultdict
from functools import lru_cache
from contextlib import contextmanager  # Import corregido
from threading import Lock
from queue import Queue
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

import requests
from flask import Flask, request, Response, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI, RateLimitError
from rapidfuzz import process, fuzz
import faiss
import numpy as np
from dotenv import load_dotenv

load_dotenv()
app = Flask(__name__)  # CORREGIDO

# =========================================================

# LOGGER

# =========================================================

logger = logging.getLogger("fran38")  # CORREGIDO
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))  # CORREGIDO
    logger.addHandler(handler)

# =========================================================

# CONFIGURACIÓN

# =========================================================

OPENAI_API_KEY = (os.environ.get("OPENAI_API_KEY") or "").strip()
if not OPENAI_API_KEY:
    raise RuntimeError("Falta OPENAI_API_KEY")

MODEL_NAME = (os.environ.get("MODEL_NAME") or "gpt-4o").strip()
CATALOG_URL = (
    os.environ.get("CATALOG_URL")
    or "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/main/catalogo_tercom_faiss.csv"
).strip()
EXCHANGE_API_URL = (os.environ.get("EXCHANGE_API_URL") or "https://dolarapi.com/v1/dolares/oficial").strip()
DEFAULT_EXCHANGE = Decimal(os.environ.get("DEFAULT_EXCHANGE", "1600.0"))
REQUESTS_TIMEOUT = int(os.environ.get("REQUESTS_TIMEOUT", "30"))
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "tercom.db")
FAISS_INDEX_PATH = os.environ.get("FAISS_INDEX_PATH", "catalog.faiss")
FAISS_MAPPING_PATH = os.environ.get("FAISS_MAPPING_PATH", "catalog_mapping.pkl")

# Constantes 3.8

MAX_SEARCH_RESULTS = int(os.environ.get("MAX_SEARCH_RESULTS", "500"))
MAX_PRODUCTS_FOR_LLM = int(os.environ.get("MAX_PRODUCTS_FOR_LLM", "50"))
WHATSAPP_MSG_LIMIT = int(os.environ.get("WHATSAPP_MSG_LIMIT", "3500"))
PRODUCTS_PER_CHUNK = int(os.environ.get("PRODUCTS_PER_CHUNK", "80"))

# Umbrales async

INSTANT_THRESHOLD = 20
ASYNC_QUICK = 50
ASYNC_MEDIUM = 100
MAX_ITEMS = 200

REQUEST_HEADERS = {"User-Agent": "FranBot/3.8"}  # CORREGIDO

# =========================================================

# TWILIO

# =========================================================

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

# Caché TC

exchange_cache = {"rate": None, "timestamp": None}
EXCHANGE_CACHE_TTL = 3600

# Rate limit

user_requests = defaultdict(list)
RATE_LIMIT = 30
RATE_WINDOW = 60

# Caché catálogo

_catalog_and_index_cache = {"catalog": None, "index": None, "built_at": None}
_catalog_lock = Lock()

# =========================================================

# DATABASE

# =========================================================

@contextmanager
def get_db_connection():
    conn = None
    try:
        db_dir = os.path.dirname(DB_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
    except Exception as e:
        logger.warning(f"No se pudo crear dir DB: {e}")

    try:
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        yield conn
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
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
        c.execute("CREATE INDEX IF NOT EXISTS idx_conv_phone ON conversations(phone, timestamp DESC)")
        
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
        c.execute("CREATE INDEX IF NOT EXISTS idx_search_phone ON search_history(phone, timestamp DESC)")
        
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
        c.execute("CREATE INDEX IF NOT EXISTS idx_interactions_phone ON interactions(phone, timestamp DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_interactions_intent ON interactions(intent_detected)")

init_db()

# =========================================================

# ANALYTICS

# =========================================================

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

# =========================================================

# UTILS

# =========================================================

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

# =========================================================

# TIPO DE CAMBIO CON CACHÉ

# =========================================================

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
            if exchange_cache["rate"]:
                return exchange_cache["rate"]
            return DEFAULT_EXCHANGE

# =========================================================

# RATE LIMIT

# =========================================================

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

# =========================================================

# PERSISTENCIA

# =========================================================

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
        order_id = f"ORD-{int(time.time())}"
        with get_db_connection() as conn:
            conn.execute(
                """INSERT INTO orders
                (order_id, phone, customer_name, customer_address, items_json, total_ars, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, phone, customer_name, customer_address, json.dumps(items), total_ars, "confirmed", datetime.now().isoformat())
            )
        return order_id
    except Exception as e:
        logger.error(f"Error creando orden: {e}")
        return None

# =========================================================

# CATÁLOGO ENRIQUECIDO

# =========================================================

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
                code = line[idx_code].strip() if idx_code is not None and idx_code < len(line) else ""
                name = line[idx_name].strip() if idx_name is not None and idx_name < len(line) else ""
                
                price_usd = to_decimal_money(line[idx_usd]) if idx_usd is not None and idx_usd < len(line) else Decimal("0")
                price_ars = to_decimal_money(line[idx_ars]) if idx_ars is not None and idx_ars < len(line) else Decimal("0")
                
                if price_ars == 0 and price_usd > 0:
                    price_ars = (price_usd * exchange).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                
                brand = line[idx_brand].strip() if idx_brand is not None and idx_brand < len(line) else ""
                model = line[idx_model].strip() if idx_model is not None and idx_model < len(line) else ""
                category = line[idx_category].strip() if idx_category is not None and idx_category < len(line) else ""
                keywords = line[idx_keywords].strip() if idx_keywords is not None and idx_keywords < len(line) else ""
                oem = line[idx_oem].strip() if idx_oem is not None and idx_oem < len(line) else ""
                alt_names = line[idx_alt].strip() if idx_alt is not None and idx_alt < len(line) else ""
                vehicle_type = line[idx_vehicle].strip() if idx_vehicle is not None and idx_vehicle < len(line) else ""
                
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
            # 🔧 FIX agregado: manejar caso sin archivos
            logger.warning("FAISS no encontrado en disco, se construirá de cero.")
            return None, None
    except Exception as e:
        logger.warning(f"No se pudo cargar FAISS desde disco: {e}")
        return None, None
        
def _build_faiss_index_from_catalog(catalog):
    try:
        if not catalog:
            return None, 0

        texts = [c["search_text"] for c in catalog]
        if not texts:
            return None, 0
        
        vectors = []
        batch = 512
        max_retries = 3
        
        for i in range(0, len(texts), batch):
            chunk = texts[i:i + batch]
            
            for retry in range(max_retries):
                try:
                    resp = client.embeddings.create(
                        input=chunk,
                        model="text-embedding-3-small",
                        timeout=REQUESTS_TIMEOUT
                    )
                    vectors.extend([d.embedding for d in resp.data])
                    break
                except RateLimitError as e:
                    if retry < max_retries - 1:
                        wait_time = 2 ** retry
                        logger.warning(f"RateLimitError en embeddings, reintentando en {wait_time}s...")
                        time.sleep(wait_time)
                    else:
                        logger.error(f"RateLimitError persistente: {e}")
                        raise
        
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

logger.info("Precargando catalogo enriquecido...")
_ = get_catalog_and_index()
logger.info("Catalogo enriquecido e indice FAISS listos.")

# =========================================================

# BÚSQUEDA HÍBRIDA

# =========================================================

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
        matches = process.extract(query, names, scorer=fuzz.WRatio, limit=limit)
        results = []
        for _, score, idx in matches:
            if score >= 60 and idx < len(catalog):
                results.append((catalog[idx], score))
        return results
    except Exception as e:
        logger.error(f"Error en fuzzy_search: {e}")
        return []

def semantic_search(query, top_k=400):
    catalog, index = get_catalog_and_index()
    if not catalog or index is None or not query:
        return []
    try:
        max_retries = 3
        for retry in range(max_retries):
            try:
                resp = client.embeddings.create(
                    input=[query],
                    model="text-embedding-3-small",
                    timeout=REQUESTS_TIMEOUT
                )
                emb = np.array([resp.data[0].embedding]).astype("float32")
                break
            except RateLimitError as e:
                if retry < max_retries - 1:
                    wait_time = 2 ** retry
                    logger.warning(f"RateLimitError en busqueda semantica, reintentando en {wait_time}s...")
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

def hybrid_search(query, limit=120):
    if not query:
        return []
    try:
        query = normalize_search_query(query)
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

# =========================================================

# CARRITO (de 3.7)

# =========================================================

def cart_add(phone, code, qty, name, price_ars, price_usd):
    if not phone or not code:
        return
    try:
        qty = max(1, min(int(qty or 1), 1000))
        price_ars = price_ars.quantize(Decimal("0.01"))
        price_usd = price_usd.quantize(Decimal("0.01"))

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
    except Exception as e:
        logger.error(f"Error en cart_add: {e}")

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
    discount = Decimal("0.05") * total if total > Decimal("10000000") else Decimal("0.00")
    final = (total - discount).quantize(Decimal("0.01"))
    return final, discount.quantize(Decimal("0.01"))

# =========================================================

# LISTAS MASIVAS

# =========================================================

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
    job_id = f"bulk_{int(time.time())}*{phone.replace(':', '*')}"
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

        logger.info(f"Procesando job {job_id}")
        
        parsed_items = parse_bulk_list(raw_list)
        results, not_found = [], []
        total_quoted = Decimal("0")
        
        for i, (requested_qty, product_name) in enumerate(parsed_items):
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
        except:
            continue

threading.Thread(target=bulk_worker, daemon=True).start()

def send_bulk_completion(phone, results):
    if not twilio_rest_client or not phone:
        return

    try:
        found = results.get("found_count", 0)
        not_found_count = results.get("not_found_count", 0)
        total = results.get("total_quoted", 0)
        
        message = f"""Listo! Procese tu lista:

{found} productos encontrados
{not_found_count} sin coincidencia exacta

TOTAL: {format_price(Decimal(str(total)))}

Los agregamos al carrito? Decime: dale"""

        twilio_rest_client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            body=message,
            to=phone
        )
        
        logger.info(f"Notificación enviada a {phone}")
    except Exception as e:
        logger.error(f"Error enviando notificación: {e}")

# =========================================================

# MULTI-MENSAJE

# =========================================================

def send_long_message(phone, text, chunk_size=1300):
    if not twilio_rest_client or not phone:
        return
    try:
        parts = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
        for idx, part in enumerate(parts):
            prefix = "" if len(parts) == 1 else f"({idx + 1}/{len(parts)})\n"
            twilio_rest_client.messages.create(
                from_=TWILIO_WHATSAPP_FROM,
                body=prefix + part,
                to=phone
            )
            time.sleep(1.5)
        logger.info(f"Enviados {len(parts)} mensajes a {phone}")
    except Exception as e:
        logger.error(f"Error en send_long_message: {e}")

# =========================================================

# COMANDOS SIMPLES

# =========================================================

SIMPLE_COMMANDS = {
    "dale", "ok", "si", "agregá", "agregalos", "metelos", "sumalos",
    "ver carrito", "mostrar carrito", "mi carrito",
    "vaciar carrito", "limpiar carrito", "borrar carrito"
}

def is_simple_command(message):
    if not message:
        return False
    lower = message.lower().strip()

    if lower in SIMPLE_COMMANDS:
        return True

    if len(lower.split()) <= 2 and lower in ["dale", "si", "ok", "agregá"]:
        return True

    return False

# =========================================================

# IA EMPÁTICA

# =========================================================

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

SMART_SYSTEM_PROMPT = f"""Sos Fran, vendedor mayorista de TERCOM (motopartes, Argentina).

=== PERSONALIDAD EMPATICA ===

- Argentino natural: che, dale, mira, vos
- Tolerante con errores: si el cliente escribe mal, entendelo igual
- Empatico: si nota frustracion, tranquilizalo
- Paciente: explica las veces que haga falta
- Proactivo: sugeri alternativas si algo no esta

=== TU TRABAJO ===

1. SIEMPRE busca primero en el catalogo que te paso
1. Si encontras productos, daselos con PRECIO del catalogo
1. Si NO estan en catalogo, usa tu conocimiento general
1. Amplia info tecnica que NO este en catalogo:
- Compatibilidades (que motos)
- Especificaciones (recorrido, amperaje, viscosidad)
- Comparaciones (diferencias entre productos)
- Recomendaciones de uso

=== REGLAS DURAS ===

- Precios SIEMPRE del catalogo (NUNCA inventes)
- Si no tenes el precio, NO lo menciones
- Info tecnica SI podes ampliarla con tu conocimiento
- Pregunta detalles para asesorar mejor (modelo, año, uso)
- Si no esta en catalogo, ofrece alternativa similar

NUNCA:

- Inventes precios
- Digas "tengo stock" si no esta en catalogo
- Garantices compatibilidad sin estar seguro

=== TONO EMPATICO ===
Cuando el cliente:

- Escribe mal → Entendelo igual sin corregirlo
- Esta confundido → "Tranqui, te ayudo a encontrar lo que necesitas"
- Pregunta lo mismo → "Dale, te repito sin drama"
- No encuentra algo → "Ese especifico no tengo, pero te puedo ofrecer X"
- Esta apurado → "Dale, vamos al punto"

{BUSINESS_CONTEXT}

Sos vendedor que SABE de motos y ENTIENDE a la gente.
"""

def build_enhanced_context(phone, user_message):
    if not phone:
        return ""

    try:
        session = get_session_summary(phone)
        search_hist = get_search_history(phone, limit=5)
        customer = get_customer_data(phone)
        
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
        
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=msgs,
            temperature=0.3,
            max_tokens=600,
            timeout=REQUESTS_TIMEOUT
        )
        
        txt = (resp.choices[0].message.content or "").strip()
        
        if not txt or len(txt) < 10:
            return "Uy, tuve un problema. Me repetis?"
        
        return txt
        
    except Exception as e:
        logger.error(f"IA fallo: {e}", exc_info=True)
        return "Uy, tuve un problema tecnico. Proba de nuevo en un ratito."

# =========================================================

# FORMATO RESULTADOS

# =========================================================

def format_search_results(products):
    lines = []
    for i, p in enumerate(products, 1):
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
        lines.append(f"{i}. {name} ({code}){extra_txt} - {price}")
    return "\n".join(lines)

# =========================================================

# AGENTE PRINCIPAL

# =========================================================

def run_agent(phone, user_message):
    if not phone or not user_message:
        return "Error: mensaje vacio"

    save_message(phone, user_message, "user")

    # Detectar intent
    intent = "unknown"
    is_bulk, item_count = is_bulk_list_request(user_message)

    if is_bulk:
        intent = "bulk_quote"
    elif is_simple_command(user_message):
        intent = "command"
    elif any(kw in user_message.lower() for kw in ["precio", "cuanto", "tenes", "busco"]):
        intent = "search"
    elif any(kw in user_message.lower() for kw in ["agregar", "carrito", "pedido"]):
        intent = "cart"
    else:
        intent = "chat"

    # 1. LISTAS MASIVAS
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
            
            lines = ["Listo! Aca esta tu cotizacion:\n"]
            lines.append(f"{found} productos encontrados")
            if not_found > 0:
                lines.append(f"{not_found} sin stock")
            lines.append(f"\nTOTAL: {format_price(Decimal(str(total)))}")
            lines.append("\nLos agregamos? Decime: dale")
            
            final = "\n".join(lines)
        else:
            if item_count < ASYNC_QUICK:
                wait_msg = f"Dale! Son {item_count} productos, te preparo la cotizacion y vuelvo con vos en un minuto"
            elif item_count < ASYNC_MEDIUM:
                wait_msg = f"Uh, lista grande! Son {item_count} productos\nDame 2-3 minutos que te armo todo y te aviso."
            else:
                wait_msg = f"Tremenda lista che! {item_count} productos\nMe va a llevar unos 4-5 minutos.\nSegui navegando tranqui, te aviso."
            
            job_id = create_bulk_job(phone, user_message, item_count)
            
            if job_id:
                final = wait_msg
            else:
                final = "Uy, tuve un problema. Me mandas la lista de nuevo?"
        
        save_message(phone, final, "assistant")
        return final

    # 2. COMANDOS SIMPLES
    if is_simple_command(user_message):
        log_interaction(phone, user_message, "command", 0)
        lower = user_message.lower().strip()
        
        if any(trig in lower for trig in ["dale", "ok", "si", "agregá", "agregalos"]):
            last = get_last_search(phone)
            if not last or not last.get("products"):
                final = "No tengo productos recientes para agregar. Busca algo primero."
            else:
                catalog, _ = get_catalog_and_index()
                added_count = 0
                total_added = Decimal("0")
                
                for p in last["products"]:
                    code = p.get("code", "")
                    qty = int(p.get("qty", 1))
                    ok, norm = validate_tercom_code(code)
                    if ok:
                        prod = next((x for x in catalog if x["code"] == norm), None)
                        if prod:
                            price_ars = to_decimal_money(prod["price_ars"])
                            price_usd = to_decimal_money(prod["price_usd"])
                            cart_add(phone, norm, qty, prod["name"], price_ars, price_usd)
                            added_count += 1
                            total_added += price_ars * qty
                
                if added_count > 0:
                    final = f"Listo! Agregue {added_count} items al carrito por {format_price(total_added)}. Pasame tus datos para el presupuesto: nombre, direccion y telefono."
                else:
                    final = "No pude agregar los productos al carrito."
        
        elif "carrito" in lower and ("ver" in lower or "mostrar" in lower or lower == "carrito"):
            items = cart_get(phone)
            if not items:
                final = "Tu carrito esta vacio."
            else:
                total, discount = cart_totals(phone)
                lines = ["TU CARRITO:\n"]
                for code, q, name, price in items:
                    subtotal = (price * q).quantize(Decimal("0.01"))
                    lines.append(f"- {q}x {name} = {format_price(subtotal)}")
                lines.append(f"\nTOTAL: {format_price(total)}")
                final = "\n".join(lines)
        
        elif "vaciar" in lower or "limpiar" in lower or "borrar" in lower:
            cart_clear(phone)
            final = "Listo! Vacie tu carrito."
        
        else:
            final = "Hola! Soy Fran de Tercom. Que estas buscando?"
        
        save_message(phone, final, "assistant")
        return final

    # 3. BÚSQUEDA NORMAL
    catalog_products = hybrid_search(user_message, limit=MAX_SEARCH_RESULTS)
    total = len(catalog_products)

    log_interaction(phone, user_message, intent, total)

    if catalog_products:
        save_last_search(phone, [
            {
                "code": p["code"],
                "name": p["name"],
                "price_ars": p["price_ars"],
                "price_usd": p["price_usd"],
                "qty": 1
            }
            for p in catalog_products[:200]
        ], user_message)
        save_to_search_history(phone, [
            {
                "code": p["code"],
                "name": p["name"],
                "price_ars": p["price_ars"],
                "price_usd": p["price_usd"],
                "qty": 1
            }
            for p in catalog_products[:200]
        ], user_message)

    if total == 0:
        final = (
            "No encontre ese repuesto en el catalogo.\n"
            "Pasame marca, modelo y año de la moto y te busco lo mas parecido."
        )
        save_message(phone, final, "assistant")
        return final

    # Si hay MUCHOS → modo listado
    if total > MAX_PRODUCTS_FOR_LLM:
        header = (
            f"Encontre *{total} productos* para: {user_message}\n"
            "Te los mando en partes asi WhatsApp no los corta"
        )
        chunks = [catalog_products[i:i + PRODUCTS_PER_CHUNK] for i in range(0, total, PRODUCTS_PER_CHUNK)]
        full_text = [header]
        for idx, ch in enumerate(chunks, 1):
            full_text.append(f"\nBloque {idx}/{len(chunks)} ({len(ch)} items)")
            full_text.append(format_search_results(ch))
        final = "\n".join(full_text)
        save_message(phone, f"[listado largo {total}]", "assistant")
        return final

    # Si son pocos → IA
    final = generate_smart_ai_reply(phone, user_message, catalog_products)
    save_message(phone, final, "assistant")
    return final

# =========================================================

# API REST

# =========================================================

@app.route("/api/cart/<phone>", methods=["GET"])
def api_get_cart(phone):
    try:
        items = cart_get(phone)
        total, discount = cart_totals(phone)
        return jsonify({
            "ok": True,
            "phone": phone,
            "items": [
                {"code": code, "qty": q, "name": name, "price": float(price)}
                for code, q, name, price in items
            ],
            "total": float(total),
            "discount": float(discount)
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/quote", methods=["POST"])
def api_quote():
    try:
        data = request.get_json()
        query = data.get("query", "")
        limit = data.get("limit", 10)

        if not query:
            return jsonify({"ok": False, "error": "Falta query"}), 400
        
        products = hybrid_search(query, limit=limit)
        
        return jsonify({
            "ok": True,
            "query": query,
            "results": [
                {
                    "code": p["code"],
                    "name": p["name"],
                    "price_ars": float(p["price_ars"]),
                    "price_usd": float(p["price_usd"]),
                    "brand": p.get("brand", ""),
                    "model": p.get("model", ""),
                    "category": p.get("category", "")
                }
                for p in products
            ]
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/orders/<phone>", methods=["GET"])
def api_get_orders(phone):
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT order_id, customer_name, total_ars, status, created_at FROM orders WHERE phone=? ORDER BY created_at DESC LIMIT 10",
                (phone,)
            )
            rows = cur.fetchall()

            return jsonify({
                "ok": True,
                "phone": phone,
                "orders": [
                    {
                        "order_id": r[0],
                        "customer_name": r[1],
                        "total_ars": r[2],
                        "status": r[3],
                        "created_at": r[4]
                    }
                    for r in rows
                ]
            })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/analytics", methods=["GET"])
def api_analytics():
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            cur.execute("""
                SELECT intent_detected, COUNT(*) as count 
                FROM interactions 
                WHERE timestamp >= datetime('now', '-7 days')
                GROUP BY intent_detected
            """)
            intents = [{"intent": r[0], "count": r[1]} for r in cur.fetchall()]
            
            cur.execute("""
                SELECT message, COUNT(*) as count 
                FROM interactions 
                WHERE intent_detected = 'search' AND timestamp >= datetime('now', '-7 days')
                GROUP BY message 
                ORDER BY count DESC 
                LIMIT 10
            """)
            top_searches = [{"query": r[0], "count": r[1]} for r in cur.fetchall()]
            
            return jsonify({
                "ok": True,
                "period": "7_days",
                "intents": intents,
                "top_searches": top_searches
            })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# =========================================================

# WEBHOOK

# =========================================================

@app.before_request
def validate_twilio_signature():
    if request.path.rstrip("/") == "/webhook" and twilio_validator:
        signature = request.headers.get("X-Twilio-Signature", "")
        url = request.url.replace("http://", "https://")
        params = request.form.to_dict()
        if not twilio_validator.validate(url, params, signature):
            logger.warning(f"Firma Twilio invalida desde {request.remote_addr}")
            return Response("Forbidden", status=403)

@app.route("/webhook", methods=["POST"])
def whatsapp_webhook():
    from_number = request.form.get("From", "")
    message_body = request.form.get("Body", "").strip()

    if not from_number or not message_body:
        logger.warning("Webhook sin From o Body")
        resp = MessagingResponse()
        resp.message("Error: mensaje vacio")
        return str(resp)

    if not rate_limit_check(from_number):
        logger.warning(f"Rate limit excedido para {from_number}")
        resp = MessagingResponse()
        resp.message("Espera un toque que me saturaste. Proba en un minuto.")
        return str(resp)

    logger.info(f"Mensaje recibido de {from_number}: {message_body[:100]}")

    try:
        reply = run_agent(from_number, message_body)
    except Exception as e:
        logger.error(f"Error ejecutando agente: {e}", exc_info=True)
        reply = "Uy, tuve un problema tecnico. Proba de nuevo en un ratito."

    # Multi-mensaje si es muy largo
    if len(reply) > 1300:
        send_long_message(from_number, reply)
        twiml = MessagingResponse()
        twiml.message("Te mande la respuesta en varios mensajes")
        return str(twiml)
    else:
        twiml = MessagingResponse()
        twiml.message(reply)
        logger.info(f"Respuesta enviada a {from_number}: {reply[:100]}")
        return str(twiml)

@app.route("/health", methods=["GET"])
def health():
    catalog, index = get_catalog_and_index()
    exchange = get_exchange_rate()

    return jsonify({
        "ok": True,
        "service": "fran38",
        "version": "3.8",
        "model": MODEL_NAME,
        "catalog_size": len(catalog) if catalog else 0,
        "faiss_ready": index is not None,
        "exchange_rate": float(exchange),
        "timestamp": datetime.now().isoformat()
    }), 200

@app.route("/", methods=["GET"])
def root():
    return Response("Fran 3.8 - Bot Mayorista Inteligente (Catalogo Enriquecido + Multi-Mensaje)", status=200, mimetype="text/plain")

if __name__ == "__main__":  # CORREGIDO
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Iniciando Fran 3.8 en puerto {port}")
    logger.info(f"Modelo LLM: {MODEL_NAME}")
    catalog, _ = get_catalog_and_index()
    logger.info(f"Catalogo: {len(catalog) if catalog else 0} productos")
    logger.info(f"TC inicial: {get_exchange_rate()}")
    app.run(host="0.0.0.0", port=port, debug=False)
    
