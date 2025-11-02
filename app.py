# coding: utf-8
# =========================================================
# Fran 3.8 - WhatsApp Bot Mayorista Inteligente
# =========================================================
# Mejoras v3.8 sobre 3.7:
# ✅ Soporta catálogo enriquecido (brand, model, category, subcategory, alt_names, vehicle_targets, searchable_text)
# ✅ Búsquedas sin corte: FAISS puede devolver 200/300/500 resultados
# ✅ Respuestas largas se dividen en varios mensajes de WhatsApp (Twilio)
# ✅ Si hay muchos productos, no se los pasa a la LLM (responde en modo “listado”)
# ✅ Mantiene el mismo nombre de archivo/URL que venías usando
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
from contextlib import contextmanager
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
# CARGA ENV y APP
# =========================================================
load_dotenv()
app = Flask(__name__)

# =========================================================
# LOGGER
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

# ⬇️ dejemos el nombre que vos usás en GitHub
CATALOG_URL = (
    os.environ.get("CATALOG_URL")
    or "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/main/LISTA_TERCOM_LIMPIA.csv"
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
# NUEVAS CONSTANTES FRAN 3.8
# =========================================================
# cuántos resultados máximo puede devolver FAISS/híbrida
MAX_SEARCH_RESULTS = int(os.environ.get("MAX_SEARCH_RESULTS", "500"))
# cuántos productos como mucho le pasamos a la LLM
MAX_PRODUCTS_FOR_LLM = int(os.environ.get("MAX_PRODUCTS_FOR_LLM", "50"))
# límite de caracteres por mensaje de WhatsApp
WHATSAPP_MSG_LIMIT = int(os.environ.get("WHATSAPP_MSG_LIMIT", "3500"))
# cuantos productos mostramos en “modo lista” por mensaje
PRODUCTS_PER_CHUNK = int(os.environ.get("PRODUCTS_PER_CHUNK", "80"))

# umbrales que ya tenías
INSTANT_THRESHOLD = 20
ASYNC_QUICK = 50
ASYNC_MEDIUM = 100
MAX_ITEMS = 200  # este lo vamos a usar pero ya no nos va a “cortar” la búsqueda

# Headers para requests
REQUEST_HEADERS = {"User-Agent": "FranBot/3.8"}

# =========================================================
# TWILIO (igual que antes)
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

# caché tipo de cambio
exchange_cache = {"rate": None, "timestamp": None}
EXCHANGE_CACHE_TTL = 3600  # 1 hora

# rate limit por usuario
user_requests = defaultdict(list)
RATE_LIMIT = 30
RATE_WINDOW = 60  # segundos

# caché de catálogo + faiss
_catalog_and_index_cache = {"catalog": None, "index": None, "built_at": None}
_catalog_lock = Lock()

# =========================================================
# DB
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
                phone TEXT,
                message TEXT,
                role TEXT,
                timestamp TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_conv_phone ON conversations(phone, timestamp DESC)")

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

        c.execute("""
            CREATE TABLE IF NOT EXISTS user_state (
                phone TEXT PRIMARY KEY,
                last_code TEXT,
                last_name TEXT,
                last_price_ars TEXT,
                updated_at TEXT
            )
        """)

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

        c.execute("""
            CREATE TABLE IF NOT EXISTS last_search (
                phone TEXT PRIMARY KEY,
                products_json TEXT,
                query TEXT,
                timestamp TEXT
            )
        """)

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
# CATÁLOGO + FAISS PERSISTENTE (Fran 3.8)
# Lee CSV enriquecido: catalogo_tercom_faiss.csv
# =========================================================
import csv
import io
import pickle
import threading
from functools import lru_cache
from decimal import Decimal, ROUND_HALF_UP

import faiss
import numpy as np

# -----------------------------------------------------------------
# CONFIG FRAN 3.8 (podés dejarlas así)
# -----------------------------------------------------------------
CATALOG_URL = (
    os.environ.get("CATALOG_URL")
    or "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/main/catalogo_tercom_faiss.csv"
).strip()

FAISS_INDEX_PATH = os.environ.get("FAISS_INDEX_PATH", "catalog.faiss")
FAISS_MAPPING_PATH = os.environ.get("FAISS_MAPPING_PATH", "catalog_mapping.pkl")

# cuántos vectores traemos en la búsqueda bruta
SEMANTIC_TOP_K = 400          # antes 20
FUZZY_TOP_K = 200             # antes 20
HYBRID_RETURN_LIMIT = 120     # lo que dejamos pasar al agente (después lo cortamos en mensajes)
FAISS_BATCH = 512

# cache en memoria
_catalog_and_index_cache = {"catalog": None, "index": None, "built_at": None}
_catalog_lock = threading.Lock()


# ---------------------------------------------------------
# utilidades ya conocidas
# ---------------------------------------------------------
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
    except Exception:
        return Decimal("0")


# ---------------------------------------------------------
# 1) Descarga del CSV enriquecido
#    Formato esperado:
#    code,name,price_usd,price_ars,brand,model,category,keywords,oem,alt_names,vehicle_type
#    (si alguna columna no está, no rompe)
# ---------------------------------------------------------
@lru_cache(maxsize=1)
def _load_raw_csv():
    try:
        r = requests.get(CATALOG_URL, timeout=REQUESTS_TIMEOUT, headers=REQUEST_HEADERS)
        r.raise_for_status()
        r.encoding = "utf-8"
        return r.text
    except Exception as e:
        logger.error(f"Error descargando CSV enriquecido: {e}")
        return ""


def _extract_column(header_row, rows, key_variants):
    """
    Busca en el header una de las variantes y devuelve índice o None.
    """
    header_norm = [strip_accents(h) for h in header_row]
    for variant in key_variants:
        variant_norm = strip_accents(variant)
        for idx, col in enumerate(header_norm):
            if variant_norm in col:
                return idx
    return None


def load_catalog_enriched():
    """
    Carga el catálogo y arma una lista de dicts con TODOS los campos
    que nos sirven para el vector.
    """
    text = _load_raw_csv()
    if not text:
        return []

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return []

    header = rows[0]
    data_rows = rows[1:]

    # índices de columnas más comunes
    idx_code = _extract_column(header, rows, ["codigo", "code", "id"])
    idx_name = _extract_column(header, rows, ["producto", "descripcion", "description", "nombre", "name"])
    idx_usd = _extract_column(header, rows, ["usd", "dolar", "precio en dolares", "price_usd"])
    idx_ars = _extract_column(header, rows, ["ars", "pesos", "precio en pesos", "price_ars"])
    idx_brand = _extract_column(header, rows, ["marca", "brand"])
    idx_model = _extract_column(header, rows, ["modelo", "model"])
    idx_category = _extract_column(header, rows, ["categoria", "category", "rubro"])
    idx_keywords = _extract_column(header, rows, ["keywords", "palabras clave", "sinonimos"])
    idx_oem = _extract_column(header, rows, ["oem", "codigo oem", "original"])
    idx_alt = _extract_column(header, rows, ["alt_names", "nombres alternativos", "alias"])
    idx_vehicle = _extract_column(header, rows, ["vehicle_type", "tipo de moto", "aplica a"])

    exchange = get_exchange_rate()
    catalog = []

    for line in data_rows:
        if not line:
            continue
        try:
            code = (line[idx_code].strip() if idx_code is not None and idx_code < len(line) else "")
            name = (line[idx_name].strip() if idx_name is not None and idx_name < len(line) else "")

            price_usd = to_decimal_money(line[idx_usd]) if idx_usd is not None and idx_usd < len(line) else Decimal("0")
            price_ars = to_decimal_money(line[idx_ars]) if idx_ars is not None and idx_ars < len(line) else Decimal("0")

            if price_ars == 0 and price_usd > 0:
                price_ars = (price_usd * exchange).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

            brand = (line[idx_brand].strip() if idx_brand is not None and idx_brand < len(line) else "")
            model = (line[idx_model].strip() if idx_model is not None and idx_model < len(line) else "")
            category = (line[idx_category].strip() if idx_category is not None and idx_category < len(line) else "")
            keywords = (line[idx_keywords].strip() if idx_keywords is not None and idx_keywords < len(line) else "")
            oem = (line[idx_oem].strip() if idx_oem is not None and idx_oem < len(line) else "")
            alt_names = (line[idx_alt].strip() if idx_alt is not None and idx_alt < len(line) else "")
            vehicle_type = (line[idx_vehicle].strip() if idx_vehicle is not None and idx_vehicle < len(line) else "")

            # esto es lo que VA A VECTOR: el "super texto"
            # acá metemos toda la magia oferta/demanda
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
                # sin nombre no tiene sentido
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
            logger.warning(f"Error procesando línea CSV enriquecido: {e}")
            continue

    logger.info(f"[Fran 3.8] Catálogo enriquecido cargado: {len(catalog)} productos")
    return catalog


# ---------------------------------------------------------
# 2) Guardar / cargar FAISS
# ---------------------------------------------------------
def save_faiss_index(index, catalog):
    try:
        faiss.write_index(index, FAISS_INDEX_PATH)
        with open(FAISS_MAPPING_PATH, "wb") as f:
            pickle.dump(catalog, f)
        logger.info(f"[Fran 3.8] FAISS guardado en disco ({len(catalog)} productos)")
    except Exception as e:
        logger.error(f"Error guardando FAISS: {e}")


def load_faiss_index():
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


# ---------------------------------------------------------
# 3) Construcción de FAISS desde el catálogo enriquecido
# ---------------------------------------------------------
def _build_faiss_index_from_catalog(catalog: list):
    """
    Construye FAISS usando catalog[i]["search_text"]
    """
    try:
        if not catalog:
            return None, 0

        texts = [c["search_text"] for c in catalog]
        if not texts:
            return None, 0

        vectors = []
        max_retries = 3

        for i in range(0, len(texts), FAISS_BATCH):
            chunk = texts[i:i + FAISS_BATCH]

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
                        logger.warning(f"RateLimit embeddings (build) reintento en {wait_time}s...")
                        time.sleep(wait_time)
                    else:
                        logger.error(f"RateLimit persistente en build embeddings: {e}")
                        raise
                except Exception as e:
                    logger.error(f"Error en embeddings (build): {e}")
                    raise

        if not vectors:
            return None, 0

        vecs = np.array(vectors).astype("float32")
        if vecs.ndim != 2 or vecs.shape[0] == 0 or vecs.shape[1] == 0:
            return None, 0

        index = faiss.IndexFlatL2(vecs.shape[1])
        index.add(vecs)

        logger.info(f"[Fran 3.8] Índice FAISS creado con {vecs.shape[0]} vectores")
        return index, vecs.shape[0]
    except Exception as e:
        logger.error(f"Error construyendo FAISS desde catálogo enriquecido: {e}", exc_info=True)
        return None, 0


# ---------------------------------------------------------
# 4) Cargar catálogo + índice (con cache)
# ---------------------------------------------------------
def get_catalog_and_index():
    """
    - intenta cargar desde memoria
    - después desde disco
    - si no, lo construye de cero y lo guarda
    """
    with _catalog_lock:
        if _catalog_and_index_cache["catalog"] is not None:
            return _catalog_and_index_cache["catalog"], _catalog_and_index_cache["index"]

        # 1) intento disco
        index, catalog = load_faiss_index()
        if index is not None and catalog is not None:
            _catalog_and_index_cache["catalog"] = catalog
            _catalog_and_index_cache["index"] = index
            _catalog_and_index_cache["built_at"] = datetime.utcnow().isoformat()
            return catalog, index

        # 2) construir de cero desde el CSV enriquecido
        catalog = load_catalog_enriched()
        index, _ = _build_faiss_index_from_catalog(catalog)

        if index is not None and catalog:
            save_faiss_index(index, catalog)

        _catalog_and_index_cache["catalog"] = catalog
        _catalog_and_index_cache["index"] = index
        _catalog_and_index_cache["built_at"] = datetime.utcnow().isoformat()
        return catalog, index


logger.info("[Fran 3.8] Precargando catálogo e índice FAISS enriquecido...")
_ = get_catalog_and_index()
logger.info("[Fran 3.8] Catálogo enriquecido e índice listos.")

# =========================================================
# BÚSQUEDA HÍBRIDA Y RESPUESTA MULTIMENSAJE (Fran 3.8)
# =========================================================
import textwrap

def embed_texts(texts):
    """Genera embeddings con reintentos automáticos."""
    if not texts:
        return []
    vectors = []
    for i in range(0, len(texts), FAISS_BATCH):
        chunk = texts[i:i + FAISS_BATCH]
        for retry in range(3):
            try:
                resp = client.embeddings.create(
                    input=chunk,
                    model="text-embedding-3-small",
                    timeout=REQUESTS_TIMEOUT
                )
                vectors.extend([d.embedding for d in resp.data])
                break
            except RateLimitError:
                wait = 2 ** retry
                logger.warning(f"Rate limit, reintentando en {wait}s")
                time.sleep(wait)
            except Exception as e:
                logger.error(f"Error generando embeddings: {e}")
                break
    return np.array(vectors).astype("float32")


# ---------------------------------------------------------
# 1) Búsqueda semántica (FAISS)
# ---------------------------------------------------------
def semantic_search(query, catalog, index, top_k=SEMANTIC_TOP_K):
    try:
        if not query or not index:
            return []
        query_vec = embed_texts([query])
        if query_vec is None or query_vec.size == 0:
            return []
        scores, idx = index.search(query_vec, top_k)
        results = []
        for i, score in enumerate(scores[0]):
            if i >= len(catalog):
                continue
            prod = catalog[idx[0][i]]
            results.append((prod, float(score)))
        return results
    except Exception as e:
        logger.error(f"Error semantic_search: {e}")
        return []


# ---------------------------------------------------------
# 2) Búsqueda difusa (fuzzy)
# ---------------------------------------------------------
def fuzzy_search(query, catalog, top_k=FUZZY_TOP_K):
    try:
        choices = [c["search_text"] for c in catalog]
        matches = process.extract(query, choices, scorer=fuzz.WRatio, limit=top_k)
        results = []
        for match in matches:
            text, score, idx = match
            prod = catalog[idx]
            results.append((prod, float(score)))
        return results
    except Exception as e:
        logger.error(f"Error fuzzy_search: {e}")
        return []


# ---------------------------------------------------------
# 3) Combinación híbrida
# ---------------------------------------------------------
def hybrid_search(query, top_k=HYBRID_RETURN_LIMIT):
    catalog, index = get_catalog_and_index()
    if not catalog or not index:
        return []

    sem = semantic_search(query, catalog, index, top_k=SEMANTIC_TOP_K)
    fuz = fuzzy_search(query, catalog, top_k=FUZZY_TOP_K)

    combined = {}
    for prod, s in sem + fuz:
        code = prod["code"]
        if code not in combined:
            combined[code] = {"prod": prod, "score": 0}
        combined[code]["score"] += s

    ranked = sorted(combined.values(), key=lambda x: x["score"], reverse=True)
    results = [x["prod"] for x in ranked[:MAX_SEARCH_RESULTS]]

    logger.info(f"[Fran 3.8] Búsqueda híbrida '{query}' → {len(results)} resultados")
    return results


# ---------------------------------------------------------
# 4) Armado de respuesta textual
# ---------------------------------------------------------
def format_search_results(products):
    """Convierte lista de productos en texto para WhatsApp."""
    lines = []
    for i, p in enumerate(products, 1):
        price_ars = format_price(p.get("price_ars", 0))
        name = p.get("name", "").strip()
        code = p.get("code", "")
        lines.append(f"{i}. *{name}* ({code}) - {price_ars}")
    return "\n".join(lines)


# ---------------------------------------------------------
# 5) Envío de mensajes divididos (multi-WhatsApp)
# ---------------------------------------------------------
def send_long_message(phone, text):
    """Divide texto largo en varios mensajes."""
    if not twilio_rest_client:
        logger.warning("Twilio no disponible")
        return

    chunks = textwrap.wrap(text, WHATSAPP_MSG_LIMIT, replace_whitespace=False)
    for part in chunks:
        msg = MessagingResponse()
        msg.message(part)
        twilio_rest_client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            body=part,
            to=phone
        )
        time.sleep(0.8)  # pequeña pausa entre mensajes


# ---------------------------------------------------------
# 6) Lógica de búsqueda principal
# ---------------------------------------------------------
def handle_search_query(phone, query):
    if not rate_limit_check(phone):
        return "Estás enviando muchos mensajes. Esperá unos segundos."

    results = hybrid_search(query)
    total = len(results)

    if total == 0:
        return f"No encontré resultados para '{query}'."

    if total > MAX_PRODUCTS_FOR_LLM:
        # modo listado directo
        text = f"🔍 Se encontraron {total} productos para '{query}'. Te los envío en varias partes:\n\n"
        chunks = [results[i:i+PRODUCTS_PER_CHUNK] for i in range(0, total, PRODUCTS_PER_CHUNK)]
        for i, chunk in enumerate(chunks, 1):
            header = f"\n📦 Bloque {i}/{len(chunks)} ({len(chunk)} productos)\n"
            body = format_search_results(chunk)
            send_long_message(phone, header + body)
        return f"Te envié {len(chunks)} mensajes con los {total} resultados."

    else:
        # pocos resultados → se puede procesar o enviar a LLM si querés
        text = f"Encontré {total} resultados para '{query}':\n\n"
        text += format_search_results(results)
        return text

# =========================================================
# FLASK APP - WEBHOOK TWILIO / WHATSAPP
# =========================================================

@app.route("/whatsapp", methods=["POST"])
def whatsapp_webhook():
    """Recibe mensajes entrantes de WhatsApp."""
    try:
        from_number = request.form.get("From", "")
        to_number = request.form.get("To", "")
        body = request.form.get("Body", "").strip()

        if not body:
            logger.warning(f"Mensaje vacío de {from_number}")
            return str(MessagingResponse().message("No entendí tu mensaje."))

        # Log básico
        logger.info(f"[Fran 3.8] Mensaje de {from_number}: {body}")

        # --- GUARDAR CONVERSACIÓN ---
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO conversations (phone, message, role, timestamp) VALUES (?, ?, ?, ?)",
                (from_number, body, "user", datetime.utcnow().isoformat()),
            )

        # --- PROCESAR MENSAJE ---
        reply_text = handle_search_query(from_number, body)

        # --- SI LA RESPUESTA ES CORTA: ENVIAR NORMAL ---
        if len(reply_text) < WHATSAPP_MSG_LIMIT:
            resp = MessagingResponse()
            resp.message(reply_text)
            with get_db_connection() as conn:
                conn.execute(
                    "INSERT INTO conversations (phone, message, role, timestamp) VALUES (?, ?, ?, ?)",
                    (from_number, reply_text, "assistant", datetime.utcnow().isoformat()),
                )
            return str(resp)

        # --- SI LA RESPUESTA ES LARGA: ENVIAR EN BLOQUES ---
        else:
            send_long_message(from_number, reply_text)
            return str(MessagingResponse().message("📦 Enviando los resultados por partes..."))

    except Exception as e:
        logger.error(f"Error en whatsapp_webhook: {e}", exc_info=True)
        return str(MessagingResponse().message("Ocurrió un error procesando tu mensaje."))


# =========================================================
# HEALTH CHECK + RAÍZ
# =========================================================
@app.route("/")
def root():
    return jsonify({"status": "ok", "version": "Fran 3.8", "timestamp": datetime.utcnow().isoformat()})


# =========================================================
# MAIN
# =========================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"🚀 Iniciando Fran 3.8 en puerto {port}")
    app.run(host="0.0.0.0", port=port, debug=False)

