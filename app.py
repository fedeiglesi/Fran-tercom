# coding: utf-8

# =========================================================

# Fran 3.6 - WhatsApp Bot Mayorista Inteligente

# =========================================================

# Features:

# ✅ Multi-búsqueda (5 cotizaciones simultáneas, flujo circular)

# ✅ IA por default (lógica invertida - contexto técnico)

# ✅ Carrito completo (agregar/modificar/sacar cantidades)

# ✅ Async listas grandes (20-200 items con notificación)

# ✅ Memoria persistente (sesión + historial 3 días)

# ✅ Captura datos cliente

# ✅ Presupuesto formateado WhatsApp

# ✅ Órdenes confirmadas

# ✅ Lenguaje natural argentino

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
from datetime import datetime, timedelta
from collections import defaultdict
from functools import lru_cache
from contextlib import contextmanager
from threading import Lock
from queue import Queue
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

import requests
from flask import Flask, request, Response
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from rapidfuzz import process, fuzz
import faiss
import numpy as np
from dotenv import load_dotenv

load_dotenv()
app = Flask(**name**)
logging.basicConfig(level=logging.INFO, format=”%(asctime)s - %(levelname)s - %(message)s”)
logger = logging.getLogger(“fran36”)

# =========================================================

# CONFIGURACIÓN

# =========================================================

OPENAI_API_KEY = (os.environ.get(“OPENAI_API_KEY”) or “”).strip()
if not OPENAI_API_KEY:
raise RuntimeError(“Falta OPENAI_API_KEY”)

MODEL_NAME = (os.environ.get(“MODEL_NAME”) or “gpt-4o”).strip()
CATALOG_URL = (os.environ.get(“CATALOG_URL”, “https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/main/LISTA_TERCOM_LIMPIA.csv”) or “”).strip()
EXCHANGE_API_URL = (os.environ.get(“EXCHANGE_API_URL”, “https://dolarapi.com/v1/dolares/oficial”) or “”).strip()
DEFAULT_EXCHANGE = Decimal(os.environ.get(“DEFAULT_EXCHANGE”, “1600.0”))
REQUESTS_TIMEOUT = int(os.environ.get(“REQUESTS_TIMEOUT”, “30”))
TWILIO_WHATSAPP_FROM = os.environ.get(“TWILIO_WHATSAPP_FROM”, “”)
TWILIO_ACCOUNT_SID = os.environ.get(“TWILIO_ACCOUNT_SID”, “”)
TWILIO_AUTH_TOKEN = os.environ.get(“TWILIO_AUTH_TOKEN”, “”)
DB_PATH = os.environ.get(“DB_PATH”, “tercom.db”)

# Umbrales async

INSTANT_THRESHOLD = 20
ASYNC_QUICK = 50
ASYNC_MEDIUM = 100
MAX_ITEMS = 200

# Twilio

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

# Cola para procesamiento async

bulk_queue = Queue()

# =========================================================

# DATABASE

# =========================================================

@contextmanager
def get_db_connection():
try:
db_dir = os.path.dirname(DB_PATH)
if db_dir and not os.path.exists(db_dir):
os.makedirs(db_dir, exist_ok=True)
except Exception as e:
logger.warning(f”No se pudo crear dir DB: {e}”)
conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
conn.row_factory = sqlite3.Row
try:
yield conn
conn.commit()
except Exception as e:
conn.rollback()
logger.error(f”DB error: {e}”)
raise
finally:
conn.close()

def init_db():
with get_db_connection() as conn:
c = conn.cursor()
try:
c.execute(“PRAGMA journal_mode=WAL;”)
except Exception as e:
logger.warning(f”No se pudo activar WAL: {e}”)

```
    # Conversaciones
    c.execute("""CREATE TABLE IF NOT EXISTS conversations (
        phone TEXT, message TEXT, role TEXT, timestamp TEXT
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_conv_phone ON conversations(phone, timestamp DESC)")
    
    # Carrito
    c.execute("""CREATE TABLE IF NOT EXISTS carts (
        phone TEXT, code TEXT, quantity INTEGER, name TEXT,
        price_ars TEXT, price_usd TEXT, created_at TEXT
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cart_phone ON carts(phone)")
    
    # Estado usuario
    c.execute("""CREATE TABLE IF NOT EXISTS user_state (
        phone TEXT PRIMARY KEY, last_code TEXT, last_name TEXT,
        last_price_ars TEXT, updated_at TEXT
    )""")
    
    # Multi-búsqueda (5 últimas)
    c.execute("""CREATE TABLE IF NOT EXISTS search_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        phone TEXT, products_json TEXT, query TEXT, timestamp TEXT
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_search_phone ON search_history(phone, timestamp DESC)")
    
    # Última búsqueda (para "dale")
    c.execute("""CREATE TABLE IF NOT EXISTS last_search (
        phone TEXT PRIMARY KEY, products_json TEXT, query TEXT, timestamp TEXT
    )""")
    
    # Sesión con resumen
    c.execute("""CREATE TABLE IF NOT EXISTS session_summary (
        phone TEXT PRIMARY KEY, products_mentioned TEXT, brands_mentioned TEXT,
        last_intent TEXT, message_count INTEGER DEFAULT 0, updated_at TEXT
    )""")
    
    # Datos cliente
    c.execute("""CREATE TABLE IF NOT EXISTS customer_data (
        phone TEXT PRIMARY KEY, name TEXT, address TEXT, notes TEXT,
        created_at TEXT, updated_at TEXT
    )""")
    
    # Órdenes
    c.execute("""CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY, phone TEXT, customer_name TEXT,
        customer_address TEXT, items_json TEXT, total_ars TEXT,
        status TEXT, created_at TEXT
    )""")
    
    # Jobs async
    c.execute("""CREATE TABLE IF NOT EXISTS bulk_jobs (
        job_id TEXT PRIMARY KEY, phone TEXT, raw_list TEXT,
        total_items INTEGER, processed_items INTEGER, found_items INTEGER,
        results_json TEXT, status TEXT, created_at TEXT, completed_at TEXT
    )""")
```

init_db()

# =========================================================

# PERSISTENCIA

# =========================================================

def save_message(phone: str, msg: str, role: str):
try:
with get_db_connection() as conn:
conn.execute(“INSERT INTO conversations VALUES (?, ?, ?, ?)”,
(phone, msg, role, datetime.now().isoformat()))
except Exception as e:
logger.error(f”Error guardando mensaje: {e}”)

def get_history_since(phone: str, days: int = 3, limit: int = 30):
try:
since = (datetime.now() - timedelta(days=days)).isoformat()
with get_db_connection() as conn:
cur = conn.cursor()
cur.execute(
“SELECT message, role, timestamp FROM conversations “
“WHERE phone = ? AND timestamp >= ? ORDER BY timestamp ASC LIMIT ?”,
(phone, since, limit))
rows = cur.fetchall()
return [{“role”: r[1], “content”: r[0], “timestamp”: r[2]} for r in rows]
except Exception as e:
logger.error(f”Error leyendo historial: {e}”)
return []

def save_to_search_history(phone: str, products: list, query: str):
“”“Guarda en historial de búsquedas (últimas 5)”””
try:
serializable = [
{“code”: p.get(“code”, “”), “name”: p.get(“name”, “”),
“price_ars”: float(p.get(“price_ars”, 0)), “price_usd”: float(p.get(“price_usd”, 0)),
“qty”: int(p.get(“qty”, 1))}
for p in products
]
with get_db_connection() as conn:
# Guardar nueva búsqueda
conn.execute(
“INSERT INTO search_history (phone, products_json, query, timestamp) VALUES (?, ?, ?, ?)”,
(phone, json.dumps(serializable, ensure_ascii=False), query, datetime.now().isoformat()))

```
        # Mantener solo últimas 5
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM search_history WHERE phone=? ORDER BY timestamp DESC LIMIT -1 OFFSET 5",
            (phone,))
        old_ids = [r[0] for r in cur.fetchall()]
        if old_ids:
            placeholders = ",".join("?" * len(old_ids))
            conn.execute(f"DELETE FROM search_history WHERE id IN ({placeholders})", old_ids)
except Exception as e:
    logger.error(f"Error guardando search_history: {e}")
```

def get_search_history(phone: str, limit: int = 5):
“”“Recupera últimas búsquedas”””
try:
with get_db_connection() as conn:
cur = conn.cursor()
cur.execute(
“SELECT products_json, query, timestamp FROM search_history “
“WHERE phone=? ORDER BY timestamp DESC LIMIT ?”,
(phone, limit))
rows = cur.fetchall()
return [{“products”: json.loads(r[0]), “query”: r[1], “timestamp”: r[2]} for r in rows]
except Exception as e:
logger.error(f”Error leyendo search_history: {e}”)
return []

def save_last_search(phone: str, products: list, query: str):
“”“Guarda última búsqueda (para comando ‘dale’)”””
try:
serializable = [
{“code”: p.get(“code”, “”), “name”: p.get(“name”, “”),
“price_ars”: float(p.get(“price_ars”, 0)), “price_usd”: float(p.get(“price_usd”, 0)),
“qty”: int(p.get(“qty”, 1))}
for p in products
]
with get_db_connection() as conn:
conn.execute(
“”“INSERT INTO last_search (phone, products_json, query, timestamp)
VALUES (?, ?, ?, ?)
ON CONFLICT(phone) DO UPDATE SET
products_json=excluded.products_json, query=excluded.query, timestamp=excluded.timestamp”””,
(phone, json.dumps(serializable, ensure_ascii=False), query, datetime.now().isoformat()))
except Exception as e:
logger.error(f”Error guardando last_search: {e}”)

def get_last_search(phone: str):
try:
with get_db_connection() as conn:
cur = conn.cursor()
cur.execute(“SELECT products_json, query FROM last_search WHERE phone=?”, (phone,))
row = cur.fetchone()
if not row:
return None
products = json.loads(row[0])
return {“products”: products, “query”: row[1]}
except Exception as e:
logger.error(f”Error leyendo last_search: {e}”)
return None

def update_session_summary(phone: str, products: list, brands: list, intent: str):
try:
with get_db_connection() as conn:
cur = conn.cursor()
cur.execute(“SELECT message_count FROM session_summary WHERE phone=?”, (phone,))
row = cur.fetchone()
count = (row[0] if row else 0) + 1
conn.execute(
“”“INSERT INTO session_summary (phone, products_mentioned, brands_mentioned, last_intent, message_count, updated_at)
VALUES (?, ?, ?, ?, ?, ?)
ON CONFLICT(phone) DO UPDATE SET
products_mentioned=excluded.products_mentioned, brands_mentioned=excluded.brands_mentioned,
last_intent=excluded.last_intent, message_count=excluded.message_count, updated_at=excluded.updated_at”””,
(phone, json.dumps(products), json.dumps(brands), intent, count, datetime.now().isoformat()))
except Exception as e:
logger.error(f”Error actualizando session_summary: {e}”)

def get_session_summary(phone: str):
try:
with get_db_connection() as conn:
cur = conn.cursor()
cur.execute(“SELECT products_mentioned, brands_mentioned, last_intent, message_count FROM session_summary WHERE phone=?”, (phone,))
row = cur.fetchone()
if not row:
return None
return {
“products”: json.loads(row[0]) if row[0] else [],
“brands”: json.loads(row[1]) if row[1] else [],
“intent”: row[2],
“count”: row[3]
}
except Exception as e:
logger.error(f”Error leyendo session_summary: {e}”)
return None

def save_customer_data(phone: str, name: str = None, address: str = None, notes: str = None):
try:
with get_db_connection() as conn:
cur = conn.cursor()
cur.execute(“SELECT name, address, notes FROM customer_data WHERE phone=?”, (phone,))
row = cur.fetchone()
now = datetime.now().isoformat()

```
        if row:
            # Actualizar solo campos no nulos
            new_name = name if name else row[0]
            new_address = address if address else row[1]
            new_notes = notes if notes else row[2]
            conn.execute(
                "UPDATE customer_data SET name=?, address=?, notes=?, updated_at=? WHERE phone=?",
                (new_name, new_address, new_notes, now, phone))
        else:
            conn.execute(
                "INSERT INTO customer_data (phone, name, address, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (phone, name or "", address or "", notes or "", now, now))
except Exception as e:
    logger.error(f"Error guardando customer_data: {e}")
```

def get_customer_data(phone: str):
try:
with get_db_connection() as conn:
cur = conn.cursor()
cur.execute(“SELECT name, address, notes FROM customer_data WHERE phone=?”, (phone,))
row = cur.fetchone()
if not row:
return None
return {“name”: row[0], “address”: row[1], “notes”: row[2]}
except Exception as e:
logger.error(f”Error leyendo customer_data: {e}”)
return None

def create_order(phone: str, customer_name: str, customer_address: str, items: list, total_ars: str):
try:
order_id = f”ORD-{int(time.time())}”
with get_db_connection() as conn:
conn.execute(
“”“INSERT INTO orders (order_id, phone, customer_name, customer_address, items_json, total_ars, status, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)”””,
(order_id, phone, customer_name, customer_address, json.dumps(items), total_ars, “confirmed”, datetime.now().isoformat()))
return order_id
except Exception as e:
logger.error(f”Error creando orden: {e}”)
return None

# Rate limit

user_requests = defaultdict(list)
RATE_LIMIT = 30
RATE_WINDOW = 60

def rate_limit_check(phone: str) -> bool:
now = datetime.now().timestamp()
user_requests[phone] = [t for t in user_requests[phone] if now - t < RATE_WINDOW]
if len(user_requests[phone]) >= RATE_LIMIT:
return False
user_requests[phone].append(now)
return True

# =========================================================

# UTILS

# =========================================================

def strip_accents(s: str) -> str:
if not s:
return “”
return “”.join(ch for ch in unicodedata.normalize(“NFKD”, s) if not unicodedata.combining(ch)).lower()

def to_decimal_money(x) -> Decimal:
try:
s = str(x).replace(“USD”, “”).replace(“ARS”, “”).replace(”$”, “”).replace(” “, “”)
if “,” in s and “.” in s:
s = s.replace(”.”, “”).replace(”,”, “.”)
elif “,” in s:
s = s.replace(”,”, “.”)
d = Decimal(s)
except Exception:
d = Decimal(“0”)
return d.quantize(Decimal(“0.01”), rounding=ROUND_HALF_UP)

def format_price(price: Decimal) -> str:
“”“Formato argentino: $2.800”””
return f”${price:,.0f}”.replace(”,”, “.”)

def validate_tercom_code(code: str):
pattern = r”^\d{4}/\d{5}-\d{3}$”
s = str(code).strip()
if re.match(pattern, s):
return True, s
code_clean = re.sub(r”[^0-9]”, “”, s)
if len(code_clean) == 12:
normalized = f”{code_clean[:4]}/{code_clean[4:9]}-{code_clean[9:12]}”
return True, normalized
return False, s

# =========================================================

# CATÁLOGO Y BÚSQUEDA

# =========================================================

def get_exchange_rate() -> Decimal:
try:
res = requests.get(EXCHANGE_API_URL, timeout=REQUESTS_TIMEOUT)
res.raise_for_status()
venta = res.json().get(“venta”, None)
return to_decimal_money(venta) if venta is not None else DEFAULT_EXCHANGE
except Exception as e:
logger.warning(f”Fallo tasa cambio: {e}”)
return DEFAULT_EXCHANGE

_catalog_and_index_cache = {“catalog”: None, “index”: None, “built_at”: None}
_catalog_lock = Lock()

@lru_cache(maxsize=1)
def _load_raw_csv():
r = requests.get(CATALOG_URL, timeout=REQUESTS_TIMEOUT)
r.raise_for_status()
r.encoding = “utf-8”
return r.text

def load_catalog():
try:
text = _load_raw_csv()
reader = csv.reader(io.StringIO(text))
rows = list(reader)
if not rows:
return []
header = [strip_accents(h) for h in rows[0]]

```
    def find_idx(keys):
        for i, h in enumerate(header):
            if any(k in h for k in keys):
                return i
        return None
    
    idx_code = find_idx(["codigo", "code"])
    idx_name = find_idx(["producto", "descripcion", "description", "nombre", "name"])
    idx_usd = find_idx(["usd", "dolar", "precio en dolares"])
    idx_ars = find_idx(["ars", "pesos", "precio en pesos"])
    
    exchange = get_exchange_rate()
    catalog = []
    
    for line in rows[1:]:
        if not line:
            continue
        code = (line[idx_code].strip() if idx_code is not None and idx_code < len(line) else "")
        name = (line[idx_name].strip() if idx_name is not None and idx_name < len(line) else "")
        usd = to_decimal_money(line[idx_usd]) if idx_usd is not None and idx_usd < len(line) else Decimal("0")
        ars = to_decimal_money(line[idx_ars]) if idx_ars is not None and idx_ars < len(line) else Decimal("0")
        
        if ars == 0 and usd > 0:
            ars = (usd * exchange).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        
        if name and (usd > 0 or ars > 0):
            catalog.append({
                "code": code,
                "name": name,
                "price_usd": float(usd),
                "price_ars": float(ars)
            })
    
    logger.info(f"Catalogo cargado: {len(catalog)} productos")
    return catalog
except Exception as e:
    logger.error(f"Error cargando catalogo: {e}", exc_info=True)
    return []
```

def _build_faiss_index_from_catalog(catalog):
try:
if not catalog:
return None, 0
texts = [str(p.get(“name”, “”)).strip() for p in catalog if str(p.get(“name”, “”)).strip()]
if not texts:
return None, 0

```
    vectors = []
    batch = 512
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        resp = client.embeddings.create(input=chunk, model="text-embedding-3-small", timeout=REQUESTS_TIMEOUT)
        vectors.extend([d.embedding for d in resp.data])
    
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
```

def get_catalog_and_index():
with _catalog_lock:
if _catalog_and_index_cache[“catalog”] is not None:
return _catalog_and_index_cache[“catalog”], _catalog_and_index_cache[“index”]
catalog = load_catalog()
index, _ = _build_faiss_index_from_catalog(catalog)
_catalog_and_index_cache[“catalog”] = catalog
_catalog_and_index_cache[“index”] = index
_catalog_and_index_cache[“built_at”] = datetime.utcnow().isoformat()
return catalog, index

logger.info(“Precargando catalogo e indice FAISS…”)
_ = get_catalog_and_index()
logger.info(“Catalogo e indice listos.”)

def fuzzy_search(query: str, limit: int = 20):
catalog, _ = get_catalog_and_index()
if not catalog:
return []
names = [p[“name”] for p in catalog]
matches = process.extract(query, names, scorer=fuzz.WRatio, limit=limit)
return [(catalog[i], score) for _, score, i in matches if score >= 60]

def semantic_search(query: str, top_k: int = 20):
catalog, index = get_catalog_and_index()
if not catalog or index is None or not query:
return []
try:
resp = client.embeddings.create(input=[query], model=“text-embedding-3-small”, timeout=REQUESTS_TIMEOUT)
emb = np.array([resp.data[0].embedding]).astype(“float32”)
D, I = index.search(emb, top_k)
results = []
for dist, idx in zip(D[0], I[0]):
if 0 <= idx < len(catalog):
score = 1.0 / (1.0 + float(dist))
results.append((catalog[idx], score))
return results
except Exception as e:
logger.error(f”Error en busqueda semantica: {e}”)
return []

SEARCH_ALIASES = {
“yama”: “yamaha”, “zan”: “zanella”, “hond”: “honda”,
“acrilico”: “acrilico tablero”, “aceite 2t”: “aceite pride 2t”,
“aceite 4t”: “aceite moto 4t”, “aceite moto”: “aceite”,
“vc”: “VC”, “af”: “AF”, “nsu”: “NSU”, “gulf”: “GULF”,
“yamalube”: “YAMALUBE”, “zusuki”: “suzuki”
}

def normalize_search_query(query: str) -> str:
q = query.lower()
for alias, replacement in SEARCH_ALIASES.items():
if alias in q:
q = q.replace(alias, replacement)
return q

def hybrid_search(query: str, limit: int = 15):
query = normalize_search_query(query)
fuzzy = fuzzy_search(query, limit=limit * 2)
sem = semantic_search(query, top_k=limit * 2)
combined = {}

```
for prod, s in fuzzy:
    code = prod.get("code", f"id_{id(prod)}")
    combined.setdefault(code, {"prod": prod, "fuzzy": Decimal(0), "sem": Decimal(0)})
    combined[code]["fuzzy"] = max(combined[code]["fuzzy"], Decimal(s) / Decimal(100))

for prod, s in sem:
    code = prod.get("code", f"id_{id(prod)}")
    combined.setdefault(code, {"prod": prod, "fuzzy": Decimal(0), "sem": Decimal(0)})
    combined[code]["sem"] = max(combined[code]["sem"], Decimal(str(s)))

out = []
for _, d in combined.items():
    score = Decimal("0.6") * d["sem"] + Decimal("0.4") * d["fuzzy"]
    out.append((d["prod"], score))

out.sort(key=lambda x: x[1], reverse=True)
return [p for p, _ in out[:limit]]
```

# =========================================================

# CARRITO

# =========================================================

def cart_add(phone: str, code: str, qty: int, name: str, price_ars: Decimal, price_usd: Decimal):
qty = max(1, min(int(qty or 1), 1000))
price_ars = price_ars.quantize(Decimal(“0.01”))
price_usd = price_usd.quantize(Decimal(“0.01”))
with cart_lock, get_db_connection() as conn:
cur = conn.cursor()
cur.execute(“SELECT quantity FROM carts WHERE phone=? AND code=?”, (phone, code))
row = cur.fetchone()
now = datetime.now().isoformat()
if row:
new_qty = int(row[0]) + qty
cur.execute(“UPDATE carts SET quantity=?, created_at=? WHERE phone=? AND code=?”,
(new_qty, now, phone, code))
else:
cur.execute(
“”“INSERT INTO carts (phone, code, quantity, name, price_ars, price_usd, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?)”””,
(phone, code, qty, name, str(price_ars), str(price_usd), now))

def cart_get(phone: str, max_age_hours: int = 24):
with get_db_connection() as conn:
cur = conn.cursor()
cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat()
cur.execute(“DELETE FROM carts WHERE phone=? AND created_at < ?”, (phone, cutoff))
cur.execute(“SELECT code, quantity, name, price_ars FROM carts WHERE phone=?”, (phone,))
rows = cur.fetchall()
out = []
for r in rows:
code, q, name, price_str = r[0], int(r[1]), r[2], r[3]
try:
price_dec = Decimal(price_str)
except (InvalidOperation, TypeError):
price_dec = Decimal(“0.00”)
out.append((code, q, name, price_dec))
return out

def cart_update_qty(phone: str, code: str, qty: int):
qty = max(0, min(int(qty or 0), 999999))
with cart_lock, get_db_connection() as conn:
if qty == 0:
conn.execute(“DELETE FROM carts WHERE phone=? AND code=?”, (phone, code))
else:
now = datetime.now().isoformat()
conn.execute(“UPDATE carts SET quantity=?, created_at=? WHERE phone=? AND code=?”,
(qty, now, phone, code))

def cart_clear(phone: str):
with cart_lock, get_db_connection() as conn:
conn.execute(“DELETE FROM carts WHERE phone=?”, (phone,))

def cart_totals(phone: str):
items = cart_get(phone)
total = sum(q * price for _, q, __, price in items)
discount = Decimal(“0.05”) * total if total > Decimal(“10000000”) else Decimal(“0.00”)
final = (total - discount).quantize(Decimal(“0.01”))
return final, discount.quantize(Decimal(“0.01”))

# =========================================================

# LISTAS MASIVAS

# =========================================================

def parse_bulk_list(text: str):
text = text.replace(”,”, “\n”).replace(”;”, “\n”)
lines = text.strip().split(”\n”)
parsed = []
for line in lines:
line = line.strip()
if not line:
continue
match = re.match(r”^(\d+)\s+(.+)$”, line)
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
norm = text.replace(”,”, “\n”).replace(”;”, “\n”)
lines = [l for l in norm.split(”\n”) if l.strip()]
lines_with_qty = sum(1 for l in lines if re.match(r”^\d+\s+\w”, l.strip()))
has_quote_intent = any(kw in lower for kw in [“cotiz”, “precio”, “cuanto”, “tenes”, “stock”, “pedido”, “lista”])
is_multiline = len(lines) >= 3

```
is_bulk = (lines_with_qty >= 3) or (has_quote_intent and is_multiline and lines_with_qty >= 1)
count = len(lines) if is_bulk else 0

return is_bulk, count
```

def process_bulk_sync(phone: str, raw_list: str):
“”“Procesa listas <20 items de inmediato”””
parsed_items = parse_bulk_list(raw_list)
if not parsed_items:
return {“success”: False, “error”: “No pude interpretar la lista”}

```
results, not_found = [], []
total_quoted = Decimal("0")

for requested_qty, product_name in parsed_items:
    matches = hybrid_search(product_name, limit=3)
    if matches:
        best = matches[0]
        price_ars = Decimal(str(best["price_ars"])).quantize(Decimal("0.01"))
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
```

def create_bulk_job(phone: str, raw_list: str, item_count: int):
“”“Crea job async para listas grandes”””
job_id = f”bulk_{int(time.time())}*{phone.replace(’:’, ’*’)}”
try:
with get_db_connection() as conn:
conn.execute(
“”“INSERT INTO bulk_jobs (job_id, phone, raw_list, total_items, processed_items, found_items, results_json, status, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)”””,
(job_id, phone, raw_list, item_count, 0, 0, “[]”, “processing”, datetime.now().isoformat()))

```
    # Agregar a cola
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
```

def process_bulk_async(job):
“”“Worker que procesa listas grandes en background”””
try:
job_id = job[“job_id”]
phone = job[“phone”]
raw_list = job[“raw_list”]

```
    logger.info(f"Procesando job {job_id}")
    
    parsed_items = parse_bulk_list(raw_list)
    results, not_found = [], []
    total_quoted = Decimal("0")
    
    # Procesar de a 10
    for i, (requested_qty, product_name) in enumerate(parsed_items):
        matches = hybrid_search(product_name, limit=3)
        if matches:
            best = matches[0]
            price_ars = Decimal(str(best["price_ars"])).quantize(Decimal("0.01"))
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
        
        # Actualizar progreso cada 10
        if (i + 1) % 10 == 0:
            with get_db_connection() as conn:
                conn.execute(
                    "UPDATE bulk_jobs SET processed_items=?, found_items=? WHERE job_id=?",
                    (i + 1, len(results), job_id))
    
    # Guardar resultados finales
    final_results = {
        "results": results,
        "not_found": not_found,
        "total_quoted": float(total_quoted),
        "found_count": len(results),
        "not_found_count": len(not_found)
    }
    
    with get_db_connection() as conn:
        conn.execute(
            """UPDATE bulk_jobs SET processed_items=?, found_items=?, results_json=?, status=?, completed_at=?
               WHERE job_id=?""",
            (len(parsed_items), len(results), json.dumps(final_results), "completed", datetime.now().isoformat(), job_id))
    
    # Guardar en last_search para "dale"
    products_for_save = [
        {"code": r["code"], "name": r["found"], "price_ars": r["price_unit"],
         "price_usd": float(Decimal(str(r["price_unit"])) / DEFAULT_EXCHANGE),
         "qty": int(r["quantity"])}
        for r in results
    ]
    save_last_search(phone, products_for_save, "Lista async")
    save_to_search_history(phone, products_for_save, "Lista async")
    
    # Notificar cliente
    send_bulk_completion(phone, final_results)
    
    logger.info(f"Job {job_id} completado: {len(results)}/{len(parsed_items)} encontrados")
    
except Exception as e:
    logger.error(f"Error procesando job: {e}", exc_info=True)
    try:
        with get_db_connection() as conn:
            conn.execute("UPDATE bulk_jobs SET status=? WHERE job_id=?", ("failed", job["job_id"]))
    except:
        pass
```

def bulk_worker():
“”“Worker daemon que procesa cola async”””
while True:
try:
job = bulk_queue.get(timeout=1)
process_bulk_async(job)
bulk_queue.task_done()
except:
continue

# Iniciar worker

threading.Thread(target=bulk_worker, daemon=True).start()

def send_bulk_completion(phone: str, results: dict):
“”“Envía notificación cuando termina job async”””
if not twilio_rest_client:
logger.warning(“No se puede enviar notificación (Twilio no disponible)”)
return

```
try:
    found = results.get("found_count", 0)
    not_found_count = results.get("not_found_count", 0)
    total = results.get("total_quoted", 0)
    
    message = f"""Listo! ✅ Procesé tu lista:
```

📊 {found} productos encontrados
❌ {not_found_count} sin coincidencia exacta

💰 TOTAL: {format_price(Decimal(str(total)))}

¿Los agregamos al carrito? Decime: dale”””

```
    twilio_rest_client.messages.create(
        from_=TWILIO_WHATSAPP_FROM,
        body=message,
        to=phone
    )
    
    logger.info(f"Notificación enviada a {phone}")
except Exception as e:
    logger.error(f"Error enviando notificación: {e}")
```

# =========================================================

# COMANDOS SIMPLES (sin IA)

# =========================================================

SIMPLE_COMMANDS = {
“dale”, “ok”, “si”, “sí”, “agregá”, “agregalos”, “metelos”, “sumalos”, “confirmá”,
“ver carrito”, “mostrar carrito”, “mi carrito”,
“vaciar carrito”, “limpiar carrito”, “borrar carrito”
}

def is_simple_command(message: str) -> bool:
“”“Detecta comandos que NO necesitan IA”””
lower = message.lower().strip()

```
# Comandos exactos
if lower in SIMPLE_COMMANDS:
    return True

# Confirmaciones cortas
if len(lower.split()) <= 2 and lower in ["dale", "si", "ok", "agregá"]:
    return True

return False
```

# =========================================================

# IA INTELIGENTE (por default)

# =========================================================

BUSINESS_CONTEXT = “””
TERCOM - Mayorista Motopartes Argentina

ENVÍOS:

- CABA: 24-48hs
- Interior: 3-5 días
- Gratis CABA >$100.000

PAGOS:

- Transferencia
- Efectivo (retiro local)
- Cheque (clientes habituales)

HORARIOS:

- Lun-Vie: 9-18hs
- Sáb: 9-13hs
  “””

SMART_SYSTEM_PROMPT = f””“Sos Fran, vendedor mayorista de TERCOM (motopartes, Argentina).

=== TU TRABAJO ===

1. SIEMPRE buscá primero en el catálogo que te paso
1. Si encontrás productos, dáselos con PRECIO del catálogo
1. Si NO están en catálogo, usá tu conocimiento general
1. Ampliá info técnica que NO esté en catálogo:
- Compatibilidades (qué motos)
- Especificaciones (recorrido, amperaje, viscosidad)
- Comparaciones (diferencias entre productos)
- Recomendaciones de uso

=== CONTEXTO QUE RECIBÍS ===
Te paso:

- Mensaje del cliente
- Productos encontrados en catálogo (si hay)
- Historial reciente de la conversación
- Sesión del cliente (marcas/productos mencionados)
- Búsquedas anteriores (puede referirse a ellas)

=== REGLAS DURAS ===
✅ Precios SIEMPRE del catálogo (NUNCA inventes)
✅ Si no tenés el precio, NO lo menciones
✅ Info técnica SÍ podés ampliarla con tu conocimiento
✅ Preguntá detalles para asesorar mejor (modelo, año, uso)
✅ Si no está en catálogo, ofrecé alternativa similar

❌ NUNCA inventes precios
❌ NUNCA digas “tengo stock” si no está en catálogo
❌ NUNCA garantices compatibilidad sin estar seguro

=== PERSONALIDAD ===

- Argentino auténtico: che, dale, mirá, vos, boludo (con cariño)
- Directo y claro
- Respuestas CORTAS (2-4 líneas)
- Emojis sutiles (👍 ✅ 📋 💰)

=== FORMATO ===
Si encontrás en catálogo:
“Sí! Tengo el [PRODUCTO] a $[PRECIO].
[INFO TÉCNICA ADICIONAL]
¿Lo agregamos?”

Si NO está:
“Ese modelo específico no lo tengo.
Te puedo ofrecer [ALTERNATIVA] que va bien.
¿Te sirve?”

Si es pregunta técnica:
“[RESPUESTA CON TU CONOCIMIENTO]
Tengo [PRODUCTOS del catálogo] que te pueden servir.”

=== EJEMPLOS ===

Catálogo: {{Aceite Yamalube 10W40 - $15.200}}
Usuario: “Sirve para Zanella RX 150?”
Vos: “Sí, perfecto! El Yamalube 10W40 va bien en la RX 150 (es 4T monocilíndrica que pide 10W-40 o 20W-50). Está $15.200. ¿Lo agregamos?”

Catálogo: {{}}
Usuario: “Tenes amortiguadores Ohlins?”
Vos: “Ohlins no tengo en este momento. Tengo YSS que es calidad similar y muy buena. ¿Para qué moto es? Te busco opciones.”

Usuario: “Diferencia entre 10W40 y 20W50?”
Vos: “El 10W40 es más fluido en frío (mejor arranque), ideal ciudad. El 20W50 es más espeso, para motos viejas o mucho calor. ¿Para qué moto es?”

{BUSINESS_CONTEXT}

Sos vendedor que SABE de motos, no un robot.
“””

def build_enhanced_context(phone: str, user_message: str, history: list):
“”“Construye contexto automático para IA”””
session = get_session_summary(phone)
search_hist = get_search_history(phone, limit=5)
customer = get_customer_data(phone)

```
context_parts = []

# Sesión
if session and session.get("count", 0) > 0:
    context_parts.append(f"[SESIÓN: {session['count']} mensajes")
    if session.get("brands"):
        context_parts.append(f", marcas: {', '.join(session['brands'][:3])}")
    if session.get("products"):
        context_parts.append(f", productos: {', '.join(session['products'][:3])}")
    context_parts.append("]")

# Búsquedas anteriores
if search_hist:
    context_parts.append(f"\n[BÚSQUEDAS PREVIAS: {len(search_hist)} cotizaciones")
    for i, s in enumerate(search_hist[:3], 1):
        prods = s.get("products", [])
        if prods:
            context_parts.append(f"\n  {i}. {s.get('query', '')}: {len(prods)} items")
    context_parts.append("]")

# Cliente
if customer and customer.get("name"):
    context_parts.append(f"\n[CLIENTE: {customer['name']}")
    if customer.get("address"):
        context_parts.append(f", {customer['address']}")
    context_parts.append("]")

# Detectar marcas/productos en mensaje actual
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

# Actualizar sesión
if brands_mentioned or products_mentioned:
    intent = "search" if any(x in lower_msg for x in ["busca", "tenes", "precio"]) else "chat"
    update_session_summary(phone, products_mentioned, brands_mentioned, intent)

return "".join(context_parts) if context_parts else ""
```

def generate_smart_ai_reply(phone: str, user_message: str, catalog_products: list) -> str:
“”“IA con contexto enriquecido - búsqueda incluida”””
try:
history = get_history_since(phone, days=3, limit=20)
context = build_enhanced_context(phone, user_message, history)

```
    # Construir mensajes
    msgs = [{"role": "system", "content": SMART_SYSTEM_PROMPT}]
    
    # Historial
    for h in history[-20:]:
        role = "assistant" if h["role"] == "assistant" else "user"
        msgs.append({"role": role, "content": h["content"]})
    
    # Mensaje actual con contexto
    if context:
        msgs.append({"role": "user", "content": f"{context}\n\nMensaje: {user_message}"})
    else:
        msgs.append({"role": "user", "content": f"Mensaje: {user_message}"})
    
    # Productos del catálogo (si hay)
    if catalog_products:
        catalog_text = "\n".join([
            f"• {p['name']} (Cod: {p.get('code', 'N/A')}) - {format_price(Decimal(str(p['price_ars'])))}"
            for p in catalog_products[:10]
        ])
        msgs.append({
            "role": "assistant",
            "content": f"(Productos encontrados en catálogo - usá estos precios)\n{catalog_text}"
        })
    
    # Llamada IA
    resp = client.chat.completions.create(
        model=MODEL_NAME,
        messages=msgs,
        temperature=0.3,
        max_tokens=600,
        timeout=REQUESTS_TIMEOUT
    )
    
    txt = (resp.choices[0].message.content or "").strip()
    
    if not txt or len(txt) < 10:
        return "Uy, tuve un problema. ¿Me repetís?"
    
    return txt
    
except Exception as e:
    logger.error(f"IA falló: {e}", exc_info=True)
    return "Uy, tuve un problema técnico. Probá de nuevo en un ratito."
```

# =========================================================

# AGENTE PRINCIPAL

# =========================================================

def run_agent(phone: str, user_message: str) -> str:
“””
Fran 3.6:
- IA por default (lógica invertida)
- Búsqueda automática en catálogo
- Contexto enriquecido
- Async para listas grandes
“””
save_message(phone, user_message, “user”)

```
# 1. Detectar lista masiva
is_bulk, item_count = is_bulk_list_request(user_message)

if is_bulk:
    if item_count < INSTANT_THRESHOLD:
        # Procesar sync (<20 items)
        result = process_bulk_sync(phone, user_message)
        if result.get("success") and result.get("results"):
            # Guardar para "dale"
            products_for_save = [
                {"code": r["code"], "name": r["found"], "price_ars": r["price_unit"],
                 "price_usd": float(Decimal(str(r["price_unit"])) / DEFAULT_EXCHANGE),
                 "qty": int(r["quantity"])}
                for r in result["results"]
            ]
            save_last_search(phone, products_for_save, "Lista")
            save_to_search_history(phone, products_for_save, "Lista")
        
        # Formatear respuesta
        found = result.get("found_count", 0)
        not_found = result.get("not_found_count", 0)
        total = result.get("total_quoted", 0)
        
        lines = [f"Listo! Acá está tu cotización:\n"]
        lines.append(f"📦 {found} productos encontrados")
        if not_found > 0:
            lines.append(f"❌ {not_found} sin stock")
        lines.append(f"\n💰 TOTAL: {format_price(Decimal(str(total)))}")
        lines.append(f"\n¿Los agregamos? Decime: dale")
        
        final = "\n".join(lines)
    
    else:
        # Procesar async (>20 items)
        if item_count < ASYNC_QUICK:
            wait_msg = f"Dale! Son {item_count} productos, te preparo la cotización y vuelvo con vos en un minuto 👍"
        elif item_count < ASYNC_MEDIUM:
            wait_msg = f"Uh, lista grande! Son {item_count} productos 📋\nDame 2-3 minutos que te armo todo y te aviso."
        else:
            wait_msg = f"Tremenda lista che! {item_count} productos 😅\nMe va a llevar unos 4-5 minutos.\nSeguí navegando tranqui, te aviso."
        
        job_id = create_bulk_job(phone, user_message, item_count)
        
        if job_id:
            final = wait_msg
        else:
            final = "Uy, tuve un problema. ¿Me mandás la lista de nuevo?"
    
    save_message(phone, final, "assistant")
    return final

# 2. Comando simple (sin IA)
if is_simple_command(user_message):
    lower = user_message.lower().strip()
    
    # "dale" - agregar última búsqueda
    if any(trig in lower for trig in ["dale", "ok", "si", "sí", "agregá", "agregalos"]):
        last = get_last_search(phone)
        if not last or not last.get("products"):
            final = "No tengo productos recientes para agregar. Buscá algo primero."
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
                final = f"Listo! Agregué {added_count} items al carrito por {format_price(total_added)}. Pasame tus datos para el presupuesto: nombre, dirección y teléfono."
            else:
                final = "No pude agregar los productos al carrito."
    
    # Ver carrito
    elif "carrito" in lower and ("ver" in lower or "mostrar" in lower or lower == "carrito"):
        items = cart_get(phone)
        if not items:
            final = "Tu carrito está vacío. Nota: se limpia automáticamente cada 24hs."
        else:
            total, discount = cart_totals(phone)
            lines = ["TU CARRITO:\n"]
            for code, q, name, price in items:
                subtotal = (price * q).quantize(Decimal("0.01"))
                lines.append(f"• {q}x {name} = {format_price(subtotal)}")
            lines.append(f"\nTOTAL: {format_price(total)}")
            final = "\n".join(lines)
    
    # Vaciar carrito
    elif "vaciar" in lower or "limpiar" in lower or "borrar" in lower:
        cart_clear(phone)
        final = "Listo! Vacié tu carrito."
    
    else:
        final = "Hola! Soy Fran de Tercom. ¿Qué estás buscando?"
    
    save_message(phone, final, "assistant")
    return final

# 3. DEFAULT: IA con búsqueda automática
# Buscar en catálogo primero
catalog_products = hybrid_search(user_message, limit=10)

# Guardar búsqueda si hay resultados
if catalog_products:
    save_last_search(phone, [
        {"code": p["code"], "name": p["name"], "price_ars": p["price_ars"],
         "price_usd": p["price_usd"], "qty": 1}
        for p in catalog_products
    ], user_message)
    save_to_search_history(phone, [
        {"code": p["code"], "name": p["name"], "price_ars": p["price_ars"],
         "price_usd": p["price_usd"], "qty": 1}
        for p in catalog_products
    ], user_message)

# Generar respuesta con IA
final = generate_smart_ai_reply(phone, user_message, catalog_products)

save_message(phone, final, "assistant")
return final
```

# =========================================================

# HTTP - WEBHOOK TWILIO

# =========================================================

@app.before_request
def validate_twilio_signature():
if request.path.rstrip(”/”) == “/webhook” and twilio_validator:
signature = request.headers.get(“X-Twilio-Signature”, “”)
url = request.url.replace(“http://”, “https://”)
params = request.form.to_dict()
if not twilio_validator.validate(url, params, signature):
logger.warning(f”Firma Twilio inválida desde {request.remote_addr}”)
return Response(“Forbidden”, status=403)

@app.route(”/webhook”, methods=[“POST”])
def whatsapp_webhook():
from_number = request.form.get(“From”, “”)
message_body = request.form.get(“Body”, “”).strip()

```
if not from_number or not message_body:
    logger.warning("Webhook sin From o Body")
    resp = MessagingResponse()
    resp.message("Error: mensaje vacío")
    return str(resp)

# Rate limit
if not rate_limit_check(from_number):
    logger.warning(f"Rate limit excedido para {from_number}")
    resp = MessagingResponse()
    resp.message("Ey, esperá un toque que me saturaste. Probá en un minuto.")
    return str(resp)

logger.info(f"Mensaje recibido de {from_number}: {message_body[:100]}")

try:
    reply = run_agent(from_number, message_body)
except Exception as e:
    logger.error(f"Error ejecutando agente: {e}", exc_info=True)
    reply = "Uy, tuve un problema técnico. Probá de nuevo en un ratito."

twiml = MessagingResponse()
twiml.message(reply)

logger.info(f"Respuesta enviada a {from_number}: {reply[:100]}")

return str(twiml)
```

@app.route(”/health”, methods=[“GET”])
def health():
catalog, index = get_catalog_and_index()
return {
“ok”: True,
“service”: “fran36”,
“model”: MODEL_NAME,
“catalog_size”: len(catalog) if catalog else 0,
“faiss_ready”: index is not None,
“timestamp”: datetime.now().isoformat()
}, 200

@app.route(”/”, methods=[“GET”])
def root():
return Response(“Fran 3.6 - Bot Mayorista Inteligente”, status=200, mimetype=“text/plain”)

if **name** == “**main**”:
port = int(os.environ.get(“PORT”, 5000))
logger.info(f”Iniciando Fran 3.6 en puerto {port}”)
logger.info(f”Modelo LLM: {MODEL_NAME}”)
catalog, _ = get_catalog_and_index()
logger.info(f”Catalogo: {len(catalog) if catalog else 0} productos”)
app.run(host=“0.0.0.0”, port=port, debug=False)
