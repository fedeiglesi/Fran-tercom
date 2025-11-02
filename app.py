# coding: utf-8
# =========================================================
# Fran 3.8 - Full Merge (Parte 1/4)
# Infraestructura + DB + Persistencia + Rate Limit
# Basado en Fran 3.7, preparado para catálogo enriquecido
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
from contextmanager import contextmanager  # lo redefinimos abajo igual
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

# =========================================================
# CARGA .ENV Y APP
# =========================================================
load_dotenv()
app = Flask(__name__)

# =========================================================
# LOGGER (como 3.7)
# =========================================================
logger = logging.getLogger("fran38")
logger.setLevel(logging.INFO)
logger.propagate = False  # evita duplicados en Gunicorn
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)

# =========================================================
# VARIABLES DE ENTORNO
# =========================================================
OPENAI_API_KEY = (os.environ.get("OPENAI_API_KEY") or "").strip()
if not OPENAI_API_KEY:
    raise RuntimeError("❌ Falta OPENAI_API_KEY")

MODEL_NAME = (os.environ.get("MODEL_NAME") or "gpt-4o").strip()

# en 3.8 vamos a usar el CSV enriquecido, pero mantenemos el nombre
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

# =========================================================
# NUEVAS CONSTANTES FRAN 3.8 (para Parte 3)
# =========================================================
# hasta 500 resultados crudos de FAISS
MAX_SEARCH_RESULTS = int(os.environ.get("MAX_SEARCH_RESULTS", "500"))
# cuántos productos como máximo permitimos pasarle a la LLM
MAX_PRODUCTS_FOR_LLM = int(os.environ.get("MAX_PRODUCTS_FOR_LLM", "50"))
# límite de caracteres por mensaje de WhatsApp
WHATSAPP_MSG_LIMIT = int(os.environ.get("WHATSAPP_MSG_LIMIT", "3500"))
# cuántos productos mostramos en modo lista
PRODUCTS_PER_CHUNK = int(os.environ.get("PRODUCTS_PER_CHUNK", "80"))

# umbrales de 3.7 que seguimos usando
INSTANT_THRESHOLD = 20
ASYNC_QUICK = 50
ASYNC_MEDIUM = 100
MAX_ITEMS = 200

# headers comunes
REQUEST_HEADERS = {"User-Agent": "FranBot/3.8"}

# =========================================================
# TWILIO (igual que en 3.7)
# =========================================================
try:
    from twilio.rest import Client as TwilioClient
    from twilio.request_validator import RequestValidator
except Exception:
    TwilioClient = None
    RequestValidator = None

twilio_rest_available = bool(
    TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and TwilioClient
)
twilio_rest_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if twilio_rest_available else None
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN) if (RequestValidator and TWILIO_AUTH_TOKEN) else None

# =========================================================
# CLIENTES Y LOCKS
# =========================================================
client = OpenAI(api_key=OPENAI_API_KEY)
cart_lock = Lock()
exchange_lock = Lock()
bulk_queue = Queue()

# caché de tipo de cambio
exchange_cache = {"rate": None, "timestamp": None}
EXCHANGE_CACHE_TTL = 3600  # 1 hora

# rate limit por usuario
user_requests = defaultdict(list)
RATE_LIMIT = 30
RATE_WINDOW = 60  # seg

# caché de catálogo + faiss (en Parte 2 guardamos acá)
_catalog_and_index_cache = {"catalog": None, "index": None, "built_at": None}
_catalog_lock = Lock()

# =========================================================
# DB
# =========================================================
def ensure_db_dir():
    try:
        db_dir = os.path.dirname(DB_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
    except Exception as e:
        logger.warning(f"No se pudo crear dir DB: {e}")


@contextmanager
def get_db_connection():
    ensure_db_dir()
    conn = None
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

        # modo WAL
        try:
            c.execute("PRAGMA journal_mode=WAL;")
        except Exception as e:
            logger.warning(f"No se pudo activar WAL: {e}")

        # 1) conversaciones
        c.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                phone TEXT,
                message TEXT,
                role TEXT,
                timestamp TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_conv_phone ON conversations(phone, timestamp DESC)")

        # 2) carrito
        c.execute("""
            CREATE TABLE IF NOT EXISTS carts (
                phone TEXT,
                code TEXT,
                quantity INTEGER,
                name TEXT,
                price_ars TEXT,
                price_usd TEXT,
                created_at TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_cart_phone ON carts(phone)")

        # 3) estado de usuario
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_state (
                phone TEXT PRIMARY KEY,
                last_code TEXT,
                last_name TEXT,
                last_price_ars TEXT,
                updated_at TEXT
            )
        """)

        # 4) historial de búsquedas
        c.execute("""
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT,
                products_json TEXT,
                query TEXT,
                timestamp TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_search_phone ON search_history(phone, timestamp DESC)")

        # 5) última búsqueda
        c.execute("""
            CREATE TABLE IF NOT EXISTS last_search (
                phone TEXT PRIMARY KEY,
                products_json TEXT,
                query TEXT,
                timestamp TEXT
            )
        """)

        # 6) resumen de sesión
        c.execute("""
            CREATE TABLE IF NOT EXISTS session_summary (
                phone TEXT PRIMARY KEY,
                products_mentioned TEXT,
                brands_mentioned TEXT,
                last_intent TEXT,
                message_count INTEGER DEFAULT 0,
                updated_at TEXT
            )
        """)

        # 7) datos del cliente
        c.execute("""
            CREATE TABLE IF NOT EXISTS customer_data (
                phone TEXT PRIMARY KEY,
                name TEXT,
                address TEXT,
                notes TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)

        # 8) órdenes
        c.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                phone TEXT,
                customer_name TEXT,
                customer_address TEXT,
                items_json TEXT,
                total_ars TEXT,
                status TEXT,
                created_at TEXT
            )
        """)

        # 9) jobs masivos
        c.execute("""
            CREATE TABLE IF NOT EXISTS bulk_jobs (
                job_id TEXT PRIMARY KEY,
                phone TEXT,
                raw_list TEXT,
                total_items INTEGER,
                processed_items INTEGER,
                found_items INTEGER,
                results_json TEXT,
                status TEXT,
                created_at TEXT,
                completed_at TEXT
            )
        """)

        # 10) analytics
        c.execute("""
            CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT,
                message TEXT,
                intent_detected TEXT,
                products_count INTEGER,
                timestamp TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_interactions_phone ON interactions(phone, timestamp DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_interactions_intent ON interactions(intent_detected)")

init_db()

# =========================================================
# UTILS BÁSICAS
# =========================================================
def strip_accents(s: str) -> str:
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
        s = (
            str(x)
            .replace("USD", "")
            .replace("ARS", "")
            .replace("$", "")
            .replace(" ", "")
            .strip()
        )
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


def validate_tercom_code(code: str):
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
# TIPO DE CAMBIO CON CACHÉ (1 HORA)
# =========================================================
def get_exchange_rate() -> Decimal:
    with exchange_lock:
        now = datetime.now().timestamp()

        # uso cache
        if exchange_cache["rate"] and exchange_cache["timestamp"]:
            age = now - exchange_cache["timestamp"]
            if age < EXCHANGE_CACHE_TTL:
                return exchange_cache["rate"]

        # si no hay cache, voy a la API
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
# RATE LIMIT POR USUARIO
# =========================================================
def rate_limit_check(phone: str) -> bool:
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
# PERSISTENCIA (conversaciones, búsquedas, carrito base)
# =========================================================
def save_message(phone: str, msg: str, role: str):
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


def get_history_since(phone: str, days: int = 3, limit: int = 30):
    if not phone:
        return []
    try:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT message, role, timestamp FROM conversations "
                "WHERE phone = ? AND timestamp >= ? "
                "ORDER BY timestamp ASC LIMIT ?",
                (phone, since, limit)
            )
            rows = cur.fetchall()
            return [{"role": r[1], "content": r[0], "timestamp": r[2]} for r in rows]
    except Exception as e:
        logger.error(f"Error leyendo historial: {e}")
        return []


def save_to_search_history(phone: str, products: list, query: str):
    if not phone or not products:
        return
    try:
        serializable = [
            {
                "code": p.get("code", ""),
                "name": p.get("name", ""),
                "price_ars": float(p.get("price_ars", 0)),
                "price_usd": float(p.get("price_usd", 0)),
                "qty": int(p.get("qty", 1)),
            }
            for p in products
        ]
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO search_history (phone, products_json, query, timestamp) VALUES (?, ?, ?, ?)",
                (phone, json.dumps(serializable, ensure_ascii=False), query or "", datetime.now().isoformat())
            )
            # mantener solo últimas 5
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


def get_search_history(phone: str, limit: int = 5):
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
            return [
                {"products": json.loads(r[0]), "query": r[1], "timestamp": r[2]}
                for r in rows
            ]
    except Exception as e:
        logger.error(f"Error leyendo search_history: {e}")
        return []


def save_last_search(phone: str, products: list, query: str):
    if not phone or not products:
        return
    try:
        serializable = [
            {
                "code": p.get("code", ""),
                "name": p.get("name", ""),
                "price_ars": p.get("price_ars", 0),
                "price_usd": p.get("price_usd", 0),
                "qty": int(p.get("qty", 1)),
            }
            for p in products
        ]
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO last_search (phone, products_json, query, timestamp)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                    products_json=excluded.products_json,
                    query=excluded.query,
                    timestamp=excluded.timestamp
                """,
                (phone, json.dumps(serializable, ensure_ascii=False), query or "", datetime.now().isoformat())
            )
    except Exception as e:
        logger.error(f"Error guardando last_search: {e}")


def get_last_search(phone: str):
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


def update_session_summary(phone: str, products: list, brands: list, intent: str):
    if not phone:
        return
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT message_count FROM session_summary WHERE phone=?", (phone,))
            row = cur.fetchone()
            count = (row[0] if row else 0) + 1
            conn.execute(
                """
                INSERT INTO session_summary
                (phone, products_mentioned, brands_mentioned, last_intent, message_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                    products_mentioned=excluded.products_mentioned,
                    brands_mentioned=excluded.brands_mentioned,
                    last_intent=excluded.last_intent,
                    message_count=excluded.message_count,
                    updated_at=excluded.updated_at
                """,
                (
                    phone,
                    json.dumps(products),
                    json.dumps(brands),
                    intent,
                    count,
                    datetime.now().isoformat()
                )
            )
    except Exception as e:
        logger.error(f"Error actualizando session_summary: {e}")


def get_session_summary(phone: str):
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
                "count": row[3],
            }
    except Exception as e:
        logger.error(f"Error leyendo session_summary: {e}")
        return None

# =========================================================
# LISTAS MASIVAS (estructuras base) - procesadas en Parte 4
# =========================================================
def parse_bulk_list(text: str):
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

def is_bulk_list_request(text: str) -> tuple:
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

# =========================================================
# BULK WORKER (hilo) - la lógica final la metemos en Parte 4
# =========================================================
def bulk_worker():
    while True:
        try:
            job = bulk_queue.get(timeout=1)
            # en Parte 4 implementamos process_bulk_async(job)
            # acá solo evitamos que se cuelgue
            logger.info(f"[bulk_worker] Job recibido: {job}")
            bulk_queue.task_done()
        except Exception:
            continue

threading.Thread(target=bulk_worker, daemon=True).start()

# =========================================================
# PARTE 2/4 – CATÁLOGO ENRIQUECIDO + FAISS PERSISTENTE
# =========================================================
import csv
import io
import pickle
import threading

# Config FAISS
SEMANTIC_TOP_K = 400
FUZZY_TOP_K = 200
HYBRID_RETURN_LIMIT = 120
FAISS_BATCH = 512

# cache en memoria
_catalog_and_index_cache = {"catalog": None, "index": None, "built_at": None}
_catalog_lock = threading.Lock()


def _load_raw_csv():
    """Descarga el CSV enriquecido directamente desde GitHub"""
    try:
        r = requests.get(CATALOG_URL, timeout=REQUESTS_TIMEOUT, headers=REQUEST_HEADERS)
        r.raise_for_status()
        r.encoding = "utf-8"
        return r.text
    except Exception as e:
        logger.error(f"Error descargando CSV enriquecido: {e}")
        return ""


def _extract_column(header_row, key_variants):
    """Busca columna por nombre flexible"""
    header_norm = [strip_accents(h) for h in header_row]
    for variant in key_variants:
        variant_norm = strip_accents(variant)
        for idx, col in enumerate(header_norm):
            if variant_norm in col:
                return idx
    return None


def load_catalog_enriched():
    """Carga y estructura el catálogo enriquecido"""
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

            # Texto enriquecido para vectorización
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
            logger.warning(f"Error procesando línea CSV: {e}")
            continue

    logger.info(f"[Fran 3.8] Catálogo enriquecido cargado: {len(catalog)} productos")
    return catalog


def save_faiss_index(index, catalog):
    """Guarda índice y mapeo en disco"""
    try:
        faiss.write_index(index, FAISS_INDEX_PATH)
        with open(FAISS_MAPPING_PATH, "wb") as f:
            pickle.dump(catalog, f)
        logger.info(f"[Fran 3.8] FAISS guardado en disco ({len(catalog)} productos)")
    except Exception as e:
        logger.error(f"Error guardando FAISS: {e}")


def load_faiss_index():
    """Carga FAISS si ya existe"""
    try:
        if os.path.exists(FAISS_INDEX_PATH) and os.path.exists(FAISS_MAPPING_PATH):
            index = faiss.read_index(FAISS_INDEX_PATH)
            with open(FAISS_MAPPING_PATH, "rb") as f:
                catalog = pickle.load(f)
            logger.info(f"[Fran 3.8] FAISS cargado desde disco: {len(catalog)} productos")
            return index, catalog
    except Exception as e:
        logger.warning(f"No se pudo cargar FAISS desde disco: {e}")
    return None, None


def _build_faiss_index_from_catalog(catalog):
    """Crea el índice FAISS desde el catálogo enriquecido"""
    try:
        if not catalog:
            return None, 0

        texts = [c["search_text"] for c in catalog]
        if not texts:
            return None, 0

        vectors = []
        for i in range(0, len(texts), FAISS_BATCH):
            chunk = texts[i:i + FAISS_BATCH]
            resp = client.embeddings.create(
                input=chunk,
                model="text-embedding-3-small",
                timeout=REQUESTS_TIMEOUT
            )
            vectors.extend([d.embedding for d in resp.data])

        if not vectors:
            return None, 0

        vecs = np.array(vectors).astype("float32")
        index = faiss.IndexFlatL2(vecs.shape[1])
        index.add(vecs)

        logger.info(f"[Fran 3.8] Índice FAISS creado con {vecs.shape[0]} vectores")
        return index, vecs.shape[0]
    except Exception as e:
        logger.error(f"Error construyendo FAISS: {e}", exc_info=True)
        return None, 0


def get_catalog_and_index():
    """Devuelve catálogo e índice FAISS desde memoria, disco o reconstruido"""
    with _catalog_lock:
        if _catalog_and_index_cache["catalog"] is not None:
            return _catalog_and_index_cache["catalog"], _catalog_and_index_cache["index"]

        index, catalog = load_faiss_index()
        if index is not None and catalog is not None:
            _catalog_and_index_cache["catalog"] = catalog
            _catalog_and_index_cache["index"] = index
            _catalog_and_index_cache["built_at"] = datetime.utcnow().isoformat()
            return catalog, index

        catalog = load_catalog_enriched()
        index, _ = _build_faiss_index_from_catalog(catalog)
        if index is not None and catalog:
            save_faiss_index(index, catalog)

        _catalog_and_index_cache["catalog"] = catalog
        _catalog_and_index_cache["index"] = index
        _catalog_and_index_cache["built_at"] = datetime.utcnow().isoformat()
        return catalog, index


logger.info("[Fran 3.8] Precargando catálogo enriquecido...")
_ = get_catalog_and_index()
logger.info("[Fran 3.8] Catálogo enriquecido e índice FAISS listos.")

# =========================================================
# PARTE 3/4 – BÚSQUEDA HÍBRIDA + RESPUESTA MULTI-MENSAJE
# =========================================================

from rapidfuzz import process, fuzz

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
    "storm": "honda storm",
    "cgl": "honda cgl",
    "cg": "honda cg",
}


def normalize_search_query(query: str) -> str:
    """Limpia y normaliza texto de búsqueda"""
    if not query:
        return ""
    q = strip_accents(query.lower())
    for alias, repl in SEARCH_ALIASES.items():
        q = q.replace(alias, repl)
    q = re.sub(r"[^a-z0-9áéíóúñ\s]", " ", q)
    return " ".join(q.split())


def fuzzy_search(query: str, limit: int = 50):
    """Búsqueda por similitud rápida"""
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


def semantic_search(query: str, top_k: int = 50):
    """Búsqueda semántica vía embeddings FAISS"""
    catalog, index = get_catalog_and_index()
    if not catalog or index is None or not query:
        return []
    try:
        resp = client.embeddings.create(
            input=[query],
            model="text-embedding-3-small",
            timeout=REQUESTS_TIMEOUT
        )
        emb = np.array([resp.data[0].embedding]).astype("float32")
        D, I = index.search(emb, top_k)
        results = []
        for dist, idx in zip(D[0], I[0]):
            if 0 <= idx < len(catalog):
                score = 1.0 / (1.0 + float(dist))
                results.append((catalog[idx], score))
        return results
    except Exception as e:
        logger.error(f"Error en semantic_search: {e}")
        return []


def hybrid_search(query: str, limit: int = 50):
    """Combinación ponderada de fuzzy + FAISS"""
    if not query:
        return []
    try:
        query = normalize_search_query(query)
        fuzzy_results = fuzzy_search(query, limit=FUZZY_TOP_K)
        semantic_results = semantic_search(query, top_k=SEMANTIC_TOP_K)

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
# ENVÍO MULTI-MENSAJE TWILIO
# =========================================================
def send_long_message(phone: str, text: str, chunk_size: int = 1300):
    """Divide texto largo en varios mensajes WhatsApp"""
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
# WEBHOOK TWILIO EXTENDIDO
# =========================================================
@app.before_request
def validate_twilio_signature():
    if request.path.rstrip("/") == "/webhook" and twilio_validator:
        signature = request.headers.get("X-Twilio-Signature", "")
        url = request.url.replace("http://", "https://")
        params = request.form.to_dict()
        if not twilio_validator.validate(url, params, signature):
            logger.warning(f"Firma Twilio inválida desde {request.remote_addr}")
            return Response("Forbidden", status=403)


@app.route("/webhook", methods=["POST"])
def whatsapp_webhook():
    from_number = request.form.get("From", "")
    message_body = request.form.get("Body", "").strip()

    if not from_number or not message_body:
        logger.warning("Webhook sin From o Body")
        resp = MessagingResponse()
        resp.message("Error: mensaje vacío")
        return str(resp)

    if not rate_limit_check(from_number):
        logger.warning(f"Rate limit excedido para {from_number}")
        resp = MessagingResponse()
        resp.message("Esperá un toque que me saturaste. Probá en un minuto.")
        return str(resp)

    logger.info(f"Mensaje recibido de {from_number}: {message_body[:100]}")

    try:
        reply = run_agent(from_number, message_body)
    except Exception as e:
        logger.error(f"Error ejecutando agente: {e}", exc_info=True)
        reply = "Uy, tuve un problema técnico. Probá de nuevo en un ratito."

    # Si la respuesta es muy larga, se divide automáticamente
    if len(reply) > 1300:
        send_long_message(from_number, reply)
        twiml = MessagingResponse()
        twiml.message("Te mandé la respuesta en varios mensajes 📦")
        return str(twiml)
    else:
        twiml = MessagingResponse()
        twiml.message(reply)
        logger.info(f"Respuesta enviada a {from_number}: {reply[:100]}")
        return str(twiml)

# =========================================================
# PARTE 4/4 – IA EMPÁTICA, AGENTE PRINCIPAL Y APIS
# =========================================================

# ----------------------------
# 1) Tipo de cambio con caché
# ----------------------------
def get_exchange_rate() -> Decimal:
    """Devuelve el TC; si falla usa cache y sino usa DEFAULT_EXCHANGE."""
    with exchange_lock:
        now = datetime.now().timestamp()

        # cache válida
        if exchange_cache["rate"] and exchange_cache["timestamp"]:
            age = now - exchange_cache["timestamp"]
            if age < EXCHANGE_CACHE_TTL:
                return exchange_cache["rate"]

        # intento online
        try:
            res = requests.get(EXCHANGE_API_URL, timeout=REQUESTS_TIMEOUT, headers=REQUEST_HEADERS)
            res.raise_for_status()
            data = res.json()
            # dolarapi.com/v1/dolares/oficial → {"moneda":"USD","casa":"oficial","nombre":"Oficial","compra":...,"venta":...}
            venta = data.get("venta") or data.get("sell") or data.get("oficial") or None
            rate = to_decimal_money(venta) if venta is not None else DEFAULT_EXCHANGE
            exchange_cache["rate"] = rate
            exchange_cache["timestamp"] = now
            return rate
        except Exception as e:
            logger.warning(f"[Fran 3.8] Fallo tasa cambio online: {e}")
            # usar cache vieja
            if exchange_cache["rate"]:
                return exchange_cache["rate"]
            # usar default
            return DEFAULT_EXCHANGE


# --------------------------------
# 2) Helpers de persistencia (DB)
# --------------------------------
def save_message(phone: str, msg: str, role: str):
    if not phone or not msg:
        return
    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO conversations (phone, message, role, timestamp) VALUES (?, ?, ?, ?)",
                (phone, msg, role, datetime.utcnow().isoformat())
            )
    except Exception as e:
        logger.error(f"[Fran 3.8] Error guardando conversación: {e}")


def get_history_since(phone: str, days: int = 3, limit: int = 30):
    if not phone:
        return []
    try:
        since = (datetime.utcnow() - timedelta(days=days)).isoformat()
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT message, role, timestamp FROM conversations "
                "WHERE phone=? AND timestamp>=? "
                "ORDER BY timestamp ASC LIMIT ?",
                (phone, since, limit)
            )
            rows = cur.fetchall()
            return [{"role": r[1], "content": r[0], "timestamp": r[2]} for r in rows]
    except Exception as e:
        logger.error(f"[Fran 3.8] Error leyendo historial: {e}")
        return []


def save_to_search_history(phone: str, products: list, query: str):
    if not phone or not products:
        return
    try:
        serializable = [
            {
                "code": p.get("code", ""),
                "name": p.get("name", ""),
                "price_ars": float(p.get("price_ars", 0)),
                "price_usd": float(p.get("price_usd", 0)),
                "qty": int(p.get("qty", 1)),
            }
            for p in products
        ]
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO search_history (phone, products_json, query, timestamp) VALUES (?, ?, ?, ?)",
                (phone, json.dumps(serializable, ensure_ascii=False), query or "", datetime.utcnow().isoformat())
            )
            # limpiar viejo → dejar últimas 5
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
        logger.error(f"[Fran 3.8] Error guardando search_history: {e}")


def get_search_history(phone: str, limit: int = 5):
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
            return [
                {
                    "products": json.loads(r[0]) if r[0] else [],
                    "query": r[1],
                    "timestamp": r[2]
                }
                for r in rows
            ]
    except Exception as e:
        logger.error(f"[Fran 3.8] Error leyendo search_history: {e}")
        return []


def save_last_search(phone: str, products: list, query: str):
    if not phone or not products:
        return
    try:
        serializable = [
            {
                "code": p.get("code", ""),
                "name": p.get("name", ""),
                "price_ars": float(p.get("price_ars", 0)),
                "price_usd": float(p.get("price_usd", 0)),
                "qty": int(p.get("qty", 1)),
            }
            for p in products
        ]
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO last_search (phone, products_json, query, timestamp)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                    products_json=excluded.products_json,
                    query=excluded.query,
                    timestamp=excluded.timestamp
                """,
                (phone, json.dumps(serializable, ensure_ascii=False), query or "", datetime.utcnow().isoformat())
            )
    except Exception as e:
        logger.error(f"[Fran 3.8] Error guardando last_search: {e}")


def get_last_search(phone: str):
    if not phone:
        return None
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT products_json, query, timestamp FROM last_search WHERE phone=?",
                (phone,)
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "products": json.loads(row[0]) if row[0] else [],
                "query": row[1],
                "timestamp": row[2]
            }
    except Exception as e:
        logger.error(f"[Fran 3.8] Error leyendo last_search: {e}")
        return None


def update_session_summary(phone: str, products: list, brands: list, intent: str):
    if not phone:
        return
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT message_count FROM session_summary WHERE phone=?", (phone,))
            row = cur.fetchone()
            count = (row[0] if row else 0) + 1
            conn.execute(
                """
                INSERT INTO session_summary
                (phone, products_mentioned, brands_mentioned, last_intent, message_count, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                    products_mentioned=excluded.products_mentioned,
                    brands_mentioned=excluded.brands_mentioned,
                    last_intent=excluded.last_intent,
                    message_count=excluded.message_count,
                    updated_at=excluded.updated_at
                """,
                (
                    phone,
                    json.dumps(products, ensure_ascii=False),
                    json.dumps(brands, ensure_ascii=False),
                    intent,
                    count,
                    datetime.utcnow().isoformat()
                )
            )
    except Exception as e:
        logger.error(f"[Fran 3.8] Error actualizando session_summary: {e}")


def get_session_summary(phone: str):
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
                "count": row[3],
            }
    except Exception as e:
        logger.error(f"[Fran 3.8] Error leyendo session_summary: {e}")
        return None


def save_customer_data(phone: str, name: str = None, address: str = None, notes: str = None):
    if not phone:
        return
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT name, address, notes FROM customer_data WHERE phone=?", (phone,))
            row = cur.fetchone()
            now = datetime.utcnow().isoformat()
            if row:
                new_name = name if name else row[0]
                new_addr = address if address else row[1]
                new_notes = notes if notes else row[2]
                conn.execute(
                    "UPDATE customer_data SET name=?, address=?, notes=?, updated_at=? WHERE phone=?",
                    (new_name, new_addr, new_notes, now, phone)
                )
            else:
                conn.execute(
                    "INSERT INTO customer_data (phone, name, address, notes, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (phone, name or "", address or "", notes or "", now, now)
                )
    except Exception as e:
        logger.error(f"[Fran 3.8] Error guardando customer_data: {e}")


def get_customer_data(phone: str):
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
        logger.error(f"[Fran 3.8] Error leyendo customer_data: {e}")
        return None


def create_order(phone: str, customer_name: str, customer_address: str, items: list, total_ars: str):
    if not phone:
        return None
    try:
        order_id = f"ORD-{int(time.time())}"
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO orders
                (order_id, phone, customer_name, customer_address, items_json, total_ars, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    phone,
                    customer_name,
                    customer_address,
                    json.dumps(items, ensure_ascii=False),
                    total_ars,
                    "confirmed",
                    datetime.utcnow().isoformat()
                )
            )
        return order_id
    except Exception as e:
        logger.error(f"[Fran 3.8] Error creando orden: {e}")
        return None


# ------------------------------------
# 3) IA empática (mismo espíritu 3.7)
# ------------------------------------
BUSINESS_CONTEXT = """
TERCOM - Mayorista de motopartes (Argentina)

ENVÍOS:
- CABA: 24-48hs
- Interior: 3-5 días

PAGOS:
- Transferencia
- Efectivo retirando
- Condiciones especiales para clientes

REGLAS:
- No inventar precios
- No inventar códigos
- Si no está, ofrecer alternativa
""".strip()

SMART_SYSTEM_PROMPT = f"""
Sos Fran, vendedor mayorista de TERCOM.

- Hablás en argentino simple: "che", "mirá", "dale", "te paso"
- Entendés errores de tipeo
- Siempre mostrás primero lo que encontraste en catálogo
- Si el cliente trae listados largos, le hacés resumen
- Si el pedido es demasiado grande para WhatsApp, se lo avisás

REGLAS DURAS:
- Precios siempre del catálogo
- Si no hay precio, decís que hay que verificar
- Si no hay coincidencia exacta, ofrecés lo más parecido
- Si el usuario no aclara modelo, le pedís marca/modelo/año

{BUSINESS_CONTEXT}
""".strip()


def build_enhanced_context(phone: str, user_message: str) -> str:
    history = get_search_history(phone, limit=5)
    session = get_session_summary(phone)
    customer = get_customer_data(phone)

    ctx_parts = []

    if session:
        ctx_parts.append(f"[sesión mensajes={session.get('count', 0)}]")
        if session.get("brands"):
            ctx_parts.append(f"[marcas: {', '.join(session['brands'][:4])}]")
        if session.get("products"):
            ctx_parts.append(f"[productos: {', '.join(session['products'][:6])}]")

    if history:
        ctx_parts.append(f"[últimas búsquedas={len(history)}]")
        for h in history[:3]:
            ctx_parts.append(f"[{h['query']}: {len(h['products'])} ítems]")

    if customer and customer.get("name"):
        ctx_parts.append(f"[cliente={customer['name']}]")

    if "honda" in user_message.lower():
        ctx_parts.append("[marca honda detectada]")

    return "\n".join(ctx_parts)


def generate_smart_ai_reply(phone: str, user_message: str, catalog_products: list) -> str:
    """Cuando la lista es chica, dejamos que el modelo hable lindo."""
    try:
        msgs = [{"role": "system", "content": SMART_SYSTEM_PROMPT}]

        # historial corto
        hist = get_history_since(phone, days=2, limit=15)
        for h in hist:
            msgs.append({"role": "assistant" if h["role"] == "assistant" else "user", "content": h["content"]})

        ctx = build_enhanced_context(phone, user_message)
        if ctx:
            msgs.append({"role": "user", "content": f"{ctx}\n\nUsuario: {user_message}"})
        else:
            msgs.append({"role": "user", "content": user_message})

        # agregamos lo que encontró FAISS para que no invente
        if catalog_products:
            cat_txt = "\n".join(
                f"- {p.get('name','')} ({p.get('code','')}) ${int(p.get('price_ars',0))}"
                for p in catalog_products[:10]
            )
            msgs.append({"role": "assistant", "content": f"Productos del catálogo que sí tenés:\n{cat_txt}"})

        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=msgs,
            temperature=0.35,
            max_tokens=550,
            timeout=REQUESTS_TIMEOUT
        )
        out = (resp.choices[0].message.content or "").strip()
        if not out:
            return "No te pude responder bien, pasame el modelo exacto de la moto."
        return out
    except Exception as e:
        logger.error(f"[Fran 3.8] IA falló: {e}", exc_info=True)
        return "Se me trabó la parte inteligente 😅. Repetime la consulta más corta."


# ----------------------------
# 4) Formateo de resultados
# ----------------------------
def format_search_results(products: list) -> str:
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
        extra_txt = f" – {' / '.join(extra)}" if extra else ""
        lines.append(f"{i}. *{name}* ({code}){extra_txt} – {price}")
    return "\n".join(lines)


# ----------------------------
# 5) Comandos simples
# ----------------------------
SIMPLE_COMMANDS = {
    "dale", "ok", "si", "sí", "agregá", "agregalos", "metelos", "sumalos",
    "mostrar carrito", "ver carrito", "carrito",
    "vaciar carrito", "limpiar carrito", "borrar carrito"
}


def is_simple_command(message: str) -> bool:
    if not message:
        return False
    m = message.lower().strip()
    return m in SIMPLE_COMMANDS


# ----------------------------
# 6) Agente principal
# ----------------------------
def run_agent(phone: str, user_message: str) -> str:
    """
    Lógica principal:
    - si es lista masiva → usa las funciones de la Parte 2
    - si es comando simple → carrito
    - si es búsqueda → FAISS + fallback IA
    - si hay demasiados productos → modo listado con múltiples mensajes
    """
    save_message(phone, user_message, "user")

    # 1) ¿es lista masiva?
    if " \n" in user_message or "\n" in user_message:
        # en la Parte 2 definimos: is_bulk_list_request, process_bulk_sync, create_bulk_job
        is_bulk, item_count = is_bulk_list_request(user_message)
        if is_bulk:
            # si la lista es chica → respuesta inmediata
            if item_count <= INSTANT_THRESHOLD:
                result = process_bulk_sync(phone, user_message)
                if result.get("success"):
                    found = result.get("found_count", 0)
                    not_found = result.get("not_found_count", 0)
                    total = Decimal(str(result.get("total_quoted", 0)))
                    msg = [
                        "Listo, te coticé la lista 👇",
                        f"Encontré: {found}",
                    ]
                    if not_found:
                        msg.append(f"Sin coincidencia: {not_found}")
                    msg.append(f"TOTAL: {format_price(total)}")
                    msg.append("¿Te armo el carrito? Decime: dale")
                    final = "\n".join(msg)
                    save_message(phone, final, "assistant")
                    return final
                else:
                    final = "La lista vino medio rara. Mandamela de nuevo así: `cantidad + producto` en cada línea."
                    save_message(phone, final, "assistant")
                    return final
            else:
                # lista grande → job async
                job_id = create_bulk_job(phone, user_message, item_count)
                if job_id:
                    final = (
                        f"Son {item_count} ítems, lo estoy procesando en segundo plano 💪 "
                        "te aviso por acá cuando esté."
                    )
                else:
                    final = "No pude poner la lista en la cola. Mandamela de nuevo, más prolija."
                save_message(phone, final, "assistant")
                return final

    # 2) ¿es comando simple?
    if is_simple_command(user_message):
        lower = user_message.lower().strip()
        if lower in {"ver carrito", "mostrar carrito", "carrito"}:
            items = cart_get(phone)
            if not items:
                final = "Tu carrito está vacío."
            else:
                total, discount = cart_totals(phone)
                lines = ["🛒 *Tu carrito:*"]
                for code, q, name, price in items:
                    subtotal = (price * q).quantize(Decimal("0.01"))
                    lines.append(f"- {q}x {name} ({code}) = {format_price(subtotal)}")
                lines.append(f"\nTOTAL: {format_price(total)}")
                if discount > 0:
                    lines.append(f"Descuento: {format_price(discount)}")
                final = "\n".join(lines)
            save_message(phone, final, "assistant")
            return final

        if lower in {"vaciar carrito", "limpiar carrito", "borrar carrito"}:
            cart_clear(phone)
            final = "Listo, carrito vacío 👍"
            save_message(phone, final, "assistant")
            return final

        # "dale" → agregar última búsqueda
        last = get_last_search(phone)
        if not last or not last.get("products"):
            final = "No tengo una búsqueda reciente para agregar. Pedime algo primero 😉"
            save_message(phone, final, "assistant")
            return final

        catalog, _ = get_catalog_and_index()
        added = 0
        total_added = Decimal("0")

        for p in last["products"]:
            code = p.get("code", "")
            qty = int(p.get("qty", 1))
            # buscar producto real en catálogo enriquecido
            real = next((c for c in catalog if c.get("code") == code), None)
            if not real:
                continue
            price_ars = to_decimal_money(real.get("price_ars", 0))
            price_usd = to_decimal_money(real.get("price_usd", 0))
            cart_add(phone, code, qty, real.get("name", ""), price_ars, price_usd)
            added += 1
            total_added += (price_ars * qty)

        if added:
            final = f"Agregué {added} productos al carrito por {format_price(total_added)} ✅"
        else:
            final = "No pude agregar esos productos al carrito."
        save_message(phone, final, "assistant")
        return final

    # 3) BÚSQUEDA NORMAL
    # usamos el híbrido nuevo (FAISS + fuzzy) que armamos en la Parte 3
    results = hybrid_search(user_message, limit=MAX_SEARCH_RESULTS)
    total = len(results)
    logger.info(f"[Fran 3.8] Consulta '{user_message}' → {total} resultados")

    # guardamos en historial último resultado
    if results:
        save_last_search(
            phone,
            [
                {
                    "code": p.get("code", ""),
                    "name": p.get("name", ""),
                    "price_ars": p.get("price_ars", 0),
                    "price_usd": p.get("price_usd", 0),
                    "qty": 1
                }
                for p in results[:200]
            ],
            user_message
        )
        save_to_search_history(
            phone,
            [
                {
                    "code": p.get("code", ""),
                    "name": p.get("name", ""),
                    "price_ars": p.get("price_ars", 0),
                    "price_usd": p.get("price_usd", 0),
                    "qty": 1
                }
                for p in results[:200]
            ],
            user_message
        )

    # si no hay nada
    if total == 0:
        final = (
            "No encontré ese repuesto en el catálogo 😕.\n"
            "Pasame marca, modelo y año de la moto y te busco lo más parecido."
        )
        save_message(phone, final, "assistant")
        return final

    # si hay MUCHOS → modo listado por partes
    if total > MAX_PRODUCTS_FOR_LLM:
        # armamos encabezado
        header = (
            f"🔎 Encontré *{total} productos* para: {user_message}\n"
            "Te los mando en partes así WhatsApp no los corta 👇"
        )
        chunks = [results[i:i + PRODUCTS_PER_CHUNK] for i in range(0, total, PRODUCTS_PER_CHUNK)]
        full_text = [header]
        for idx, ch in enumerate(chunks, 1):
            full_text.append(f"\n📦 Bloque {idx}/{len(chunks)} ({len(ch)} ítems)")
            full_text.append(format_search_results(ch))
        final = "\n".join(full_text)
        save_message(phone, f"[listado largo {total}]", "assistant")
        return final

    # si son pocos → IA lo acomoda, como en 3.7
    final = generate_smart_ai_reply(phone, user_message, results)
    save_message(phone, final, "assistant")
    return final


# =========================================================
# 7) API REST (mismas rutas que 3.7)
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
                {"code": code, "qty": q, "name": name, "price_ars": float(price)}
                for code, q, name, price in items
            ],
            "total_ars": float(total),
            "discount_ars": float(discount),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/quote", methods=["POST"])
def api_quote():
    try:
        data = request.get_json(force=True)
        query = data.get("query", "")
        limit = int(data.get("limit", 50))
        if not query:
            return jsonify({"ok": False, "error": "Falta query"}), 400
        prods = hybrid_search(query, limit=min(limit, MAX_SEARCH_RESULTS))
        return jsonify({
            "ok": True,
            "query": query,
            "results": [
                {
                    "code": p.get("code", ""),
                    "name": p.get("name", ""),
                    "price_ars": float(p.get("price_ars", 0)),
                    "price_usd": float(p.get("price_usd", 0)),
                    "brand": p.get("brand", ""),
                    "model": p.get("model", ""),
                    "category": p.get("category", "")
                }
                for p in prods
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
                "SELECT order_id, customer_name, total_ars, status, created_at "
                "FROM orders WHERE phone=? ORDER BY created_at DESC LIMIT 20",
                (phone,)
            )
            rows = cur.fetchall()
        return jsonify({
            "ok": True,
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
    """Pequeño dashboard para vos"""
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            # top últimas búsquedas
            cur.execute("""
                SELECT query, COUNT(*) as c
                FROM search_history
                WHERE timestamp >= datetime('now','-7 days')
                GROUP BY query
                ORDER BY c DESC
                LIMIT 15
            """)
            top_searches = [{"query": r[0], "count": r[1]} for r in cur.fetchall()]
            # tamaño catálogo actual
            catalog, index = get_catalog_and_index()
        return jsonify({
            "ok": True,
            "catalog_size": len(catalog) if catalog else 0,
            "faiss_ready": index is not None,
            "top_searches": top_searches
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# =========================================================
# 8) Healthcheck y root
# =========================================================
@app.route("/health", methods=["GET"])
def health():
    catalog, index = get_catalog_and_index()
    return jsonify({
        "ok": True,
        "service": "fran38",
        "catalog_size": len(catalog) if catalog else 0,
        "faiss_ready": index is not None,
        "exchange_rate": float(get_exchange_rate()),
        "timestamp": datetime.utcnow().isoformat()
    })


@app.route("/", methods=["GET"])
def root():
    return Response("Fran 3.8 - Bot Mayorista Inteligente (catálogo enriquecido, multi-mensaje)", status=200)


# =========================================================
# 9) MAIN
# =========================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🚀 Iniciando Fran 3.8 en puerto {port}")
    catalog, index = get_catalog_and_index()
    logger.info(f"[Fran 3.8] catálogo cargado: {len(catalog) if catalog else 0} productos")
    app.run(host="0.0.0.0", port=port, debug=False)
    
