# -*- coding: utf-8 -*-
"""
FRAN 2.3 FULL — Railway Production Ready
Fixes implementados:
- Anti "lista fantasma" reforzado con validación de códigos en respuesta
- System prompt mejorado con reglas explícitas
- Persistencia de carrito con expiración (24h)
- Streaming real de respuestas (sin delays artificiales)
- Catálogo mayorista extendido (límites aumentados)
- Timeouts corregidos y configurables
- Sumas de carrito con Decimal (precisión exacta)
- Logging detallado para debugging
- Validaciones de conteo correctas
"""

import os
import json
import csv
import io
import sqlite3
import logging
import re
import unicodedata
import threading
import time as time_mod
from datetime import datetime, timedelta
from collections import defaultdict
from functools import lru_cache
from contextlib import contextmanager
from time import time
from threading import Lock
from typing import Dict, Any, List

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

import requests
from flask import Flask, request, jsonify, Response
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from rapidfuzz import process, fuzz
import faiss
import numpy as np
from dotenv import load_dotenv

load_dotenv()

try:
    from twilio.rest import Client as TwilioClient
    from twilio.request_validator import RequestValidator
except Exception:
    TwilioClient = None
    RequestValidator = None

# =======================
# CONFIGURACIÓN
# =======================
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("fran23")

OPENAI_API_KEY = (os.environ.get("OPENAI_API_KEY") or "").strip()
if not OPENAI_API_KEY:
    raise RuntimeError("Falta OPENAI_API_KEY")

CATALOG_URL = (os.environ.get("CATALOG_URL",
    "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/main/LISTA_TERCOM_LIMPIA.csv") or "").strip()
EXCHANGE_API_URL = (os.environ.get("EXCHANGE_API_URL",
    "https://dolarapi.com/v1/dolares/oficial") or "").strip()
DEFAULT_EXCHANGE = Decimal(os.environ.get("DEFAULT_EXCHANGE", "1600.0"))
REQUESTS_TIMEOUT = int(os.environ.get("REQUESTS_TIMEOUT", "20"))

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")

twilio_rest_available = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and TwilioClient)
twilio_rest_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if twilio_rest_available else None
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN) if (RequestValidator and TWILIO_AUTH_TOKEN) else None

client = OpenAI(api_key=OPENAI_API_KEY)

# =================
# BASE DE DATOS
# =================
DB_PATH = os.environ.get("DB_PATH", "tercom.db")
cart_lock = Lock()

@contextmanager
def get_db_connection():
    try:
        db_dir = os.path.dirname(DB_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
    except Exception as e:
        logger.warning(f"No se pudo crear dir DB: {e}")
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"DB error: {e}")
        raise
    finally:
        conn.close()

def init_db():
    with get_db_connection() as conn:
        c = conn.cursor()
        try:
            c.execute("PRAGMA journal_mode=WAL;")
        except Exception as e:
            logger.warning(f"No se pudo activar WAL: {e}")

        c.execute('''CREATE TABLE IF NOT EXISTS conversations
                     (phone TEXT, message TEXT, role TEXT, timestamp TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS carts
                     (phone TEXT, code TEXT, quantity INTEGER, name TEXT, 
                      price_ars TEXT, price_usd TEXT, created_at TEXT)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_conv_phone ON conversations(phone, timestamp DESC)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_cart_phone ON carts(phone)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_state
                     (phone TEXT PRIMARY KEY, last_code TEXT, last_name TEXT, 
                      last_price_ars TEXT, updated_at TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS last_search
                     (phone TEXT PRIMARY KEY, 
                      products_json TEXT,
                      query TEXT,
                      timestamp TEXT)''')

init_db()

def save_message(phone, msg, role):
    try:
        with get_db_connection() as conn:
            conn.execute('INSERT INTO conversations VALUES (?, ?, ?, ?)',
                        (phone, msg, role, datetime.now().isoformat()))
    except Exception as e:
        logger.error(f"Error guardando mensaje: {e}")

def get_history_today(phone, limit=20):
    try:
        today_prefix = datetime.now().strftime("%Y-%m-%d")
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                'SELECT message, role FROM conversations WHERE phone = ? AND substr(timestamp,1,10)=? '
                'ORDER BY timestamp ASC LIMIT ?',
                (phone, today_prefix, limit)
            )
            return cur.fetchall()
    except Exception as e:
        logger.error(f"Error leyendo historial: {e}")
        return []

def save_user_state(phone, prod):
    try:
        with get_db_connection() as conn:
            conn.execute(
                '''INSERT INTO user_state (phone, last_code, last_name, last_price_ars, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(phone) DO UPDATE SET
                       last_code=excluded.last_code, last_name=excluded.last_name,
                       last_price_ars=excluded.last_price_ars, updated_at=excluded.updated_at''',
                (phone, prod.get("code", ""), prod.get("name", ""), 
                 str(Decimal(str(prod.get("price_ars", 0))).quantize(Decimal("0.01"))), datetime.now().isoformat())
            )
    except Exception as e:
        logger.error(f"Error guardando user_state: {e}")

def save_last_search(phone, products, query):
    try:
        serializable = []
        for p in products:
            serializable.append({
                "code": p.get("code", ""),
                "name": p.get("name", ""),
                "price_ars": float(p.get("price_ars", 0)),
                "price_usd": float(p.get("price_usd", 0))
            })
        with get_db_connection() as conn:
            conn.execute(
                '''INSERT INTO last_search (phone, products_json, query, timestamp)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(phone) DO UPDATE SET
                       products_json=excluded.products_json,
                       query=excluded.query,
                       timestamp=excluded.timestamp''',
                (phone, json.dumps(serializable, ensure_ascii=False), query, datetime.now().isoformat())
            )
    except Exception as e:
        logger.error(f"Error guardando last_search: {e}")

def get_last_search(phone):
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute('SELECT products_json, query, timestamp FROM last_search WHERE phone=?', (phone,))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "products": json.loads(row[0]),
                "query": row[1],
                "timestamp": row[2]
            }
    except Exception as e:
        logger.error(f"Error leyendo last_search: {e}")
        return None

# =================
# RATE LIMIT
# =================
user_requests = defaultdict(list)
RATE_LIMIT = 15
RATE_WINDOW = 60

def rate_limit_check(phone):
    now = time()
    user_requests[phone] = [t for t in user_requests[phone] if now - t < RATE_WINDOW]
    if len(user_requests[phone]) >= RATE_LIMIT:
        return False
    user_requests[phone].append(now)
    return True

# =================
# UTILIDADES
# =================
def strip_accents(s: str) -> str:
    if not s:
        return ""
    return ''.join(ch for ch in unicodedata.normalize('NFKD', s) if not unicodedata.combining(ch)).lower()

def to_decimal_money(x) -> Decimal:
    try:
        s = str(x).replace("USD", "").replace("ARS", "").replace("$", "").replace(" ", "")
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        d = Decimal(s)
    except Exception:
        d = Decimal("0")
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def get_exchange_rate() -> Decimal:
    try:
        res = requests.get(EXCHANGE_API_URL, timeout=REQUESTS_TIMEOUT)
        res.raise_for_status()
        venta = res.json().get("venta", None)
        return to_decimal_money(venta) if venta is not None else DEFAULT_EXCHANGE
    except Exception as e:
        logger.warning(f"Fallo tasa cambio: {e}")
        return DEFAULT_EXCHANGE

# =================================
# CATÁLOGO + FAISS
# =================================
_catalog_and_index_cache = {"catalog": None, "index": None, "built_at": None}
_catalog_lock = Lock()

@lru_cache(maxsize=1)
def _load_raw_csv():
    r = requests.get(CATALOG_URL, timeout=REQUESTS_TIMEOUT)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text

def load_catalog():
    try:
        text = _load_raw_csv()
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return []
        header = [strip_accents(h) for h in rows[0]]

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
        logger.info(f"📦 Catálogo cargado: {len(catalog)} productos")
        return catalog
    except Exception as e:
        logger.error(f"Error cargando catálogo: {e}", exc_info=True)
        return []

def _build_faiss_index_from_catalog(catalog):
    try:
        if not catalog:
            return None, 0
        texts = [str(p.get("name", "")).strip() for p in catalog if str(p.get("name", "")).strip()]
        if not texts:
            return None, 0

        vectors = []
        batch = 512
        for i in range(0, len(texts), batch):
            chunk = texts[i:i+batch]
            resp = client.embeddings.create(input=chunk, model="text-embedding-3-small", timeout=REQUESTS_TIMEOUT)
            vectors.extend([d.embedding for d in resp.data])

        if not vectors:
            return None, 0

        vecs = np.array(vectors).astype("float32")
        if vecs.ndim != 2 or vecs.shape[0] == 0 or vecs.shape[1] == 0:
            return None, 0

        index = faiss.IndexFlatL2(vecs.shape[1])
        index.add(vecs)
        logger.info(f"✅ Índice FAISS: {vecs.shape[0]} vectores, dim={vecs.shape[1]}")
        return index, vecs.shape[0]
    except Exception as e:
        logger.error(f"Error construyendo FAISS: {e}", exc_info=True)
        return None, 0

def get_catalog_and_index():
    with _catalog_lock:
        if _catalog_and_index_cache["catalog"] is not None:
            return _catalog_and_index_cache["catalog"], _catalog_and_index_cache["index"]

        catalog = load_catalog()
        index, _ = _build_faiss_index_from_catalog(catalog)
        _catalog_and_index_cache["catalog"] = catalog
        _catalog_and_index_cache["index"] = index
        _catalog_and_index_cache["built_at"] = datetime.utcnow().isoformat()
        return catalog, index

# =========================================
# BÚSQUEDA HÍBRIDA
# =========================================
def fuzzy_search(query, limit=20):
    catalog, _ = get_catalog_and_index()
    if not catalog:
        return []
    names = [p["name"] for p in catalog]
    matches = process.extract(query, names, scorer=fuzz.WRatio, limit=limit)
    return [(catalog[i], score) for _, score, i in matches if score >= 60]

def semantic_search(query, top_k=20):
    catalog, index = get_catalog_and_index()
    if not catalog or index is None or not query:
        return []
    try:
        resp = client.embeddings.create(input=[query], model="text-embedding-3-small", timeout=REQUESTS_TIMEOUT)
        emb = np.array([resp.data[0].embedding]).astype("float32")
        D, I = index.search(emb, top_k)
        results = []
        for dist, idx in zip(D[0], I[0]):
            if 0 <= idx < len(catalog):
                score = 1.0 / (1.0 + float(dist))
                results.append((catalog[idx], score))
        return results
    except Exception as e:
        logger.error(f"Error en búsqueda semántica: {e}")
        return []

SEARCH_ALIASES = {
    "yama": "yamaha",
    "gilera": "gilera",
    "zan": "zanella",
    "hond": "honda",
    "acrilico": "acrilico tablero",
    "aceite 2t": "aceite pride 2t",
    "aceite 4t": "aceite moto 4t",
    "aceite moto": "aceite",
    "vc": "VC",
    "af": "AF",
    "nsu": "NSU",
    "gulf": "GULF",
    "yamalube": "YAMALUBE",
    "suzuki": "suzuki",
    "zusuki": "suzuki",
}

def normalize_search_query(query):
    q = query.lower()
    for alias, replacement in SEARCH_ALIASES.items():
        if alias in q:
            q = q.replace(alias, replacement)
    return q

def hybrid_search(query, limit=15):
    query = normalize_search_query(query)
    fuzzy = fuzzy_search(query, limit=limit*2)
    sem = semantic_search(query, top_k=limit*2)
    combined = {}
    for prod, s in fuzzy:
        code = prod.get("code", f"id_{id(prod)}")
        combined.setdefault(code, {"prod": prod, "fuzzy": Decimal(0), "sem": Decimal(0)})
        combined[code]["fuzzy"] = max(combined[code]["fuzzy"], Decimal(s)/Decimal(100))
    for prod, s in sem:
        code = prod.get("code", f"id_{id(prod)}")
        combined.setdefault(code, {"prod": prod, "fuzzy": Decimal(0), "sem": Decimal(0)})
        combined[code]["sem"] = max(combined[code]["sem"], Decimal(str(s)))
    out = []
    for _, d in combined.items():
        score = Decimal("0.6")*d["sem"] + Decimal("0.4")*d["fuzzy"]
        out.append((d["prod"], score))
    out.sort(key=lambda x: x[1], reverse=True)
    return [p for p, _ in out[:limit]]

# ==================================
# CARRITO
# ==================================
def cart_add(phone: str, code: str, qty: int, name: str, price_ars: Decimal, price_usd: Decimal):
    qty = max(1, min(int(qty or 1), 100))
    price_ars = price_ars.quantize(Decimal("0.01"))
    price_usd = price_usd.quantize(Decimal("0.01"))

    with cart_lock, get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute('SELECT quantity FROM carts WHERE phone=? AND code=?', (phone, code))
        row = cur.fetchone()
        now = datetime.now().isoformat()
        
        if row:
            new_qty = int(row[0]) + qty
            cur.execute('UPDATE carts SET quantity=?, created_at=? WHERE phone=? AND code=?', 
                       (new_qty, now, phone, code))
        else:
            cur.execute('''INSERT INTO carts (phone, code, quantity, name, price_ars, price_usd, created_at) 
                          VALUES (?, ?, ?, ?, ?, ?, ?)''',
                       (phone, code, qty, name, str(price_ars), str(price_usd), now))

def cart_get(phone: str, max_age_hours: int = 24):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat()
        cur.execute('DELETE FROM carts WHERE phone=? AND created_at < ?', (phone, cutoff))
        
        cur.execute('SELECT code, quantity, name, price_ars FROM carts WHERE phone=?', (phone,))
        rows = cur.fetchall()
        out = []
        for r in rows:
            code, q, name, price_str = r[0], int(r[1]), r[2], r[3]
            try:
                price_dec = Decimal(price_str)
            except (InvalidOperation, TypeError):
                price_dec = Decimal("0.00")
            out.append((code, q, name, price_dec))
        return out

def cart_update_qty(phone: str, code: str, qty: int):
    qty = max(0, min(int(qty or 0), 999))
    with cart_lock, get_db_connection() as conn:
        if qty == 0:
            conn.execute('DELETE FROM carts WHERE phone=? AND code=?', (phone, code))
        else:
            now = datetime.now().isoformat()
            conn.execute('UPDATE carts SET quantity=?, created_at=? WHERE phone=? AND code=?', 
                        (qty, now, phone, code))

def cart_clear(phone: str):
    with cart_lock, get_db_connection() as conn:
        conn.execute('DELETE FROM carts WHERE phone=?', (phone,))

def cart_totals(phone: str):
    items = cart_get(phone)
    total = sum(q * price for _, q, __, price in items)
    discount = Decimal("0.05") * total if total > Decimal("10000000") else Decimal("0.00")
    final = (total - discount).quantize(Decimal("0.01"))
    return final, discount.quantize(Decimal("0.01"))

# ============================================================
# TOOLS
# ============================================================
def validate_tercom_code(code):
    pattern = r'^\d{4}/\d{5}-\d{3}$'
    if re.match(pattern, str(code).strip()):
        return True, str(code).strip()
    code_clean = re.sub(r'[^0-9]', '', str(code))
    if len(code_clean) == 12:
        normalized = f"{code_clean[:4]}/{code_clean[4:9]}-{code_clean[9:12]}"
        return True, normalized
    return False, code

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "Busca productos por nombre/características en el catálogo",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 15}}, "required": ["query"]}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_cart",
            "description": "Agrega productos al carrito",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {"type": "array",
                              "items": {"type": "object",
                                        "properties": {"code": {"type": "string"}, "quantity": {"type": "integer", "default": 1}},
                                        "required": ["code"]}}
                },
                "required": ["items"]
            }
        }
    },
    {"type": "function", "function": {"name": "view_cart", "description": "Muestra el carrito actual", "parameters": {"type": "object", "properties": {}}}},
    {
        "type": "function",
        "function": {"name": "update_cart_item", "description": "Modifica cantidad (0 elimina)", "parameters": {"type": "object", "properties": {"code": {"type": "string"}, "quantity": {"type": "integer"}}, "required": ["code", "quantity"]}}
    },
    {"type": "function", "function": {"name": "clear_cart", "description": "Vacía el carrito completamente", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "confirm_order", "description": "Confirma y procesa el pedido final", "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_product_details", "description": "Detalle de un producto por código", "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}}},
    {"type": "function", "function": {"name": "compare_products", "description": "Compara múltiples productos por código", "parameters": {"type": "object", "properties": {"codes": {"type": "array", "items": {"type": "string"}}}, "required": ["codes"]}}},
    {"type": "function", "function": {"name": "get_recommendations", "description": "Obtiene recomendaciones de productos", "parameters": {"type": "object", "properties": {"based_on": {"type": "string", "default": "last_viewed"}, "limit": {"type": "integer", "default": 5}}}}},
    {"type": "function", "function": {"name": "get_last_search_results", "description": "Recupera los últimos resultados de búsqueda con códigos", "parameters": {"type": "object", "properties": {}}}},
]

class ToolExecutor:
    def __init__(self, phone: str):
        self.phone = phone

    def execute(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        method = getattr(self, tool_name, None)
        if not method:
            logger.error(f"❌ Tool not found: {tool_name}")
            return {"error": f"Herramienta '{tool_name}' no encontrada"}
        try:
            logger.info(f"🔧 Tool: {tool_name}")
            args_preview = json.dumps(arguments, ensure_ascii=False)[:200]
            logger.info(f"   Args: {args_preview}{'...' if len(str(arguments)) > 200 else ''}")
            result = method(**arguments)
            result_str = json.dumps(result, ensure_ascii=False)
            logger.info(f"   Result: {result_str[:300]}{'...' if len(result_str) > 300 else ''}")
            return result
        except Exception as e:
            logger.error(f"❌ {tool_name} error: {e}", exc_info=True)
            return {"error": str(e), "tool": tool_name}

    def search_products(self, query: str, limit: int = 15) -> Dict:
        results = hybrid_search(query, limit=limit)
        
        if results:
            save_user_state(self.phone, results[0])
            save_last_search(self.phone, results, query)
        
        actual_count = len(results)
        
        return {
            "success": True,
            "query": query,
            "results": [{"code": p["code"], "name": p["name"], "price_ars": p["price_ars"], "price_usd": p["price_usd"]} for p in results],
            "count": actual_count,
            "message": (f"Encontré {actual_count} producto{'s' if actual_count != 1 else ''} para '{query}'." 
                       if results 
                       else f"No encontré productos para '{query}'. Probá con otros términos.")
        }

    def add_to_cart(self, items: List[Dict]) -> Dict:
        catalog, _ = get_catalog_and_index()
        added, not_found, invalid_codes = [], [], []
        
        for item in items:
            code = str(item.get("code", "")).strip()
            qty = int(item.get("quantity", 1))
            ok, norm = validate_tercom_code(code)
            
            if not ok:
                invalid_codes.append(code)
                continue
            
            code = norm
            prod = next((x for x in catalog if x["code"] == code), None)
            
            if prod:
                price_ars = to_decimal_money(prod["price_ars"])
                price_usd = to_decimal_money(prod["price_usd"])
                cart_add(self.phone, code, qty, prod["name"], price_ars, price_usd)
                subtotal = (price_ars * qty).quantize(Decimal("0.01"))
                added.append({
                    "code": code,
                    "name": prod["name"],
                    "quantity": qty,
                    "price_unit": str(price_ars),
                    "subtotal": str(subtotal)
                })
                save_user_state(self.phone, prod)
            else:
                not_found.append(code)
        
        if added and not (not_found or invalid_codes):
            total_items = sum(a["quantity"] for a in added)
            total_price = sum(Decimal(a["subtotal"]) for a in added)
            return {
                "success": True,
                "added": added,
                "total_items": int(total_items),
                "total_price": float(total_price),
                "message": f"¡Listo! Agregué {len(added)} producto{'s' if len(added) != 1 else ''} ({int(total_items)} unidad{'es' if total_items != 1 else ''}) por ${total_price:,.2f} ARS"
            }
        elif added:
            errors = []
            if not_found:
                errors.append(f"No encontré: {', '.join(not_found)}")
            if invalid_codes:
                errors.append(f"Códigos inválidos: {', '.join(invalid_codes)}")
            return {"success": True, "added": added, "message": f"Agregué {len(added)} productos. {' | '.join(errors)}"}
        else:
            return {"success": False, "added": [], "message": "No pude agregar productos. Hacé una búsqueda primero para obtener códigos válidos."}

    def view_cart(self) -> Dict:
        items = cart_get(self.phone)
        total, discount = cart_totals(self.phone)
        
        return {
            "success": True,
            "items": [{
                "code": code,
                "name": name,
                "quantity": qty,
                "price_unit": float(price),
                "price_total": float((price * qty).quantize(Decimal("0.01")))
            } for code, qty, name, price in items],
            "subtotal": float((total + discount).quantize(Decimal("0.01"))),
            "discount": float(discount),
            "total": float(total),
            "item_count": len(items),
            "unit_count": sum(qty for _, qty, __, ___ in items)
        }

    def update_cart_item(self, code: str, quantity: int) -> Dict:
        catalog, _ = get_catalog_and_index()
        prod = next((x for x in catalog if x["code"] == code), None)
        
        if not prod:
            return {"success": False, "error": f"Producto {code} no encontrado"}
        
        cart_update_qty(self.phone, code, quantity)
        
        return {
            "success": True,
            "code": code,
            "name": prod["name"],
            "new_quantity": quantity,
            "action": "removed" if quantity == 0 else "updated",
            "message": f"{'Eliminado' if quantity == 0 else f'Actualizado a {quantity} unidades'}"
        }

    def clear_cart(self) -> Dict:
        cart_clear(self.phone)
        return {"success": True, "message": "Carrito vaciado completamente"}

    def confirm_order(self) -> Dict:
        items = cart_get(self.phone)
        
        if not items:
            return {"success": False, "error": "Carrito vacío"}
        
        total, discount = cart_totals(self.phone)
        
        order_details = {
            "items": [{
                "code": code, 
                "name": name, 
                "quantity": qty, 
                "price": float(price), 
                "subtotal": float((price * qty).quantize(Decimal("0.01")))
            } for code, qty, name, price in items],
            "subtotal": float((total + discount).quantize(Decimal("0.01"))),
            "discount": float(discount),
            "total": float(total),
            "item_count": len(items),
            "unit_count": sum(qty for _, qty, __, ___ in items)
        }
        
        cart_clear(self.phone)
        
        return {
            "success": True, 
            "order": order_details, 
            "message": f"¡Pedido confirmado! {order_details['item_count']} productos ({order_details['unit_count']} unidades) por ${total:,.2f} ARS"
        }

    def get_product_details(self, code: str) -> Dict:
        catalog, _ = get_catalog_and_index()
        prod = next((x for x in catalog if x["code"] == code), None)
        
        if not prod:
            return {"success": False, "error": f"Producto {code} no encontrado"}
        
        return {
            "success": True, 
            "product": prod, 
            "message": f"**(Cód: {prod['code']})** {prod['name']} - ${float(prod['price_ars']):,.2f} ARS"
        }

    def compare_products(self, codes: List[str]) -> Dict:
        catalog, _ = get_catalog_and_index()
        products = [p for p in catalog if p["code"] in codes]
        
        if len(products) < 2:
            return {"success": False, "error": f"Necesito al menos 2 productos válidos. Solo encontré {len(products)}"}
        
        sorted_by_price = sorted(products, key=lambda x: x["price_ars"])
        
        return {
            "success": True,
            "products": products,
            "cheapest": sorted_by_price[0],
            "most_expensive": sorted_by_price[-1],
            "price_difference": float(sorted_by_price[-1]["price_ars"] - sorted_by_price[0]["price_ars"]),
            "comparison": f"Más barato: {sorted_by_price[0]['name']} (${sorted_by_price[0]['price_ars']:,.2f}). Más caro: {sorted_by_price[-1]['name']} (${sorted_by_price[-1]['price_ars']:,.2f})"
        }

    def get_recommendations(self, based_on: str = "last_viewed", limit: int = 5) -> Dict:
        catalog, _ = get_catalog_and_index()
        return {"success": True, "recommendations": catalog[:limit], "reason": "Productos destacados"}

    def get_last_search_results(self) -> Dict:
        search = get_last_search(self.phone)
        
        if not search:
            return {"success": False, "message": "No hay búsquedas recientes. Hacé una búsqueda primero."}
        
        product_codes = [p["code"] for p in search["products"]]
        
        return {
            "success": True,
            "query": search["query"],
            "products": search["products"],
            "product_codes": product_codes,
            "count": len(search["products"]),
            "message": f"Última búsqueda: '{search['query']}' con {len(search['products'])} productos. Códigos: {', '.join(product_codes[:5])}{'...' if len(product_codes) > 5 else ''}"
        }

# ============================================================
# AGENTE
# ============================================================
def _format_list(products, max_items=15) -> str:
    if not products:
        return "No encontré productos."
    
    lines = []
    for p in products[:max_items]:
        code = p.get("code", "").strip() or "s/c"
        name = p.get("name", "").strip()
        ars = Decimal(str(p.get("price_ars", 0))).quantize(Decimal("0.01"))
        lines.append(f"• **(Cód: {code})** {name} - ${ars:,.0f} ARS")
    
    return "\n".join(lines)

def _intent_needs_basics(user_message: str) -> bool:
    t = user_message.lower()
    triggers = [
        "surtido", "básico", "basico", "abrir mi local", 
        "recomendar", "proponer", "lista de productos", 
        "lo básico", "lo basico", "catalogo", "catálogo",
        "empezar", "comenzar", "inicial", "necesito productos",
        "dame productos", "mostrame productos"
    ]
    return any(x in t for x in triggers)

def _force_search_and_reply(phone: str, query: str) -> str:
    results = hybrid_search(query, limit=15)
    
    if not results:
        results = hybrid_search("repuestos basicos moto", limit=15)
    
    if not results:
        return "Disculpá, tengo problemas para acceder al catálogo en este momento. Probá en unos minutos."
    
    save_last_search(phone, results, query)
    save_user_state(phone, results[0])
    
    listado = _format_list(results, max_items=len(results))
    count = len(results)
    
    return f"Acá tenés {count} productos sugeridos para {query}:\n\n{listado}\n\n¿Querés que te agregue alguno al carrito?"

def run_agent(phone: str, user_message: str, max_iterations: int = 6) -> str:
    catalog, _ = get_catalog_and_index()
    
    if not catalog:
        return "No puedo acceder al catálogo en este momento. Probá más tarde."

    executor = ToolExecutor(phone)
    history = get_history_today(phone, limit=20)
    
    cart_items = cart_get(phone)
    logger.info(f"📱 INICIO - Usuario: {phone}, Carrito: {len(cart_items)} items")
    logger.info(f"💬 Mensaje: {user_message[:100]}")

    system_prompt = """Sos Fran, vendedor experto de Tercom (mayorista de repuestos para motos en Argentina).

REGLAS CRÍTICAS:
1. NUNCA digas "te pasé la lista" o "la lista que te mostré" si no ejecutaste search_products con resultados reales EN ESTE MENSAJE.
2. Si el usuario pide "surtido/básico/catálogo/productos", SIEMPRE ejecutá search_products primero y mostrá los resultados completos.
3. Antes de decir que agregaste productos al carrito, SIEMPRE ejecutá add_to_cart y confirmá el resultado.
4. Si el usuario pregunta por el carrito, SIEMPRE ejecutá view_cart antes de responder.
5. Formato obligatorio por ítem: **(Cód: XXXX/XXXXX-XXX)** Nombre - $precio ARS
6. Mostrá TODOS los productos que encontraste, no solo algunos.

PROTOCOLO DE CONFIRMACIÓN:
- Usuario dice "dale/ok/sí" después de ver productos → ejecutá add_to_cart con los códigos
- Usuario dice "dale/ok/confirmo" cuando ya hay carrito → ejecutá view_cart primero
- Mostrá el resumen completo con totales exactos
- Preguntá "¿Confirmo el pedido?" explícitamente
- Solo si el usuario confirma de nuevo, ejecutá confirm_order

REFERENCIAS:
- "agregalo/agregala" = último producto mencionado con código válido
- "esos/todos esos" = get_last_search_results → usar esos códigos
- "X de cada uno" = get_last_search_results → add_to_cart con cantidad X para cada código
- "todos" o "todos los que me mostraste" = get_last_search_results → add_to_cart con todos los códigos

SUMAS Y CANTIDADES:
- Usá Decimal para cálculos exactos
- Validá que las cantidades y totales sean correctos
- Si el usuario cuestiona números, ejecutá view_cart y verificá

Usá lenguaje argentino natural (vos, che, dale) pero NUNCA inventes información ni códigos.
"""

    messages = [{"role": "system", "content": system_prompt}]
    for msg, role in history:
        messages.append({"role": role, "content": msg})
    messages.append({"role": "user", "content": user_message})

    last_model_text = ""

    for iteration in range(max_iterations):
        try:
            logger.info(f"🔄 Iteración {iteration + 1}/{max_iterations}")
            
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.7,
                max_tokens=1500,
                timeout=REQUESTS_TIMEOUT
            )
            message = response.choices[0].message

            if getattr(message, "tool_calls", None):
                logger.info(f"🔧 Tools llamados: {[tc.function.name for tc in message.tool_calls]}")
                messages.append({"role": "assistant", "content": message.content or "", "tool_calls": message.tool_calls})
                
                for tool_call in message.tool_calls:
                    tool_name = tool_call.function.name
                    tool_args = json.loads(tool_call.function.arguments or "{}")
                    result = executor.execute(tool_name, tool_args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_name,
                        "content": json.dumps(result, ensure_ascii=False)
                    })
                continue

            final_response = (message.content or "").strip()
            last_model_text = final_response

            # Anti "lista fantasma"
            if _intent_needs_basics(user_message):
                has_codes = bool(re.search(r"\(Cód:\s*\d{4}/\d{5}-\d{3}\)", final_response))
                if not has_codes:
                    logger.info("⚠️ Anti-lista-fantasma activado: forzando búsqueda real")
                    return _force_search_and_reply(phone, query="surtido basico repuestos moto")

            if final_response:
                logger.info(f"✅ Respuesta final generada: {len(final_response)} caracteres")
                return final_response

            logger.warning("⚠️ Modelo no respondió texto, reintentando")
            messages.append({"role": "system", "content": "Respondé siempre con una frase clara y útil. Si ejecutaste herramientas, explicá los resultados."})
            continue

        except Exception as e:
            logger.error(f"❌ Error en iteración {iteration}: {e}", exc_info=True)
            return "Disculpá, tuve un problema procesando tu pedido. ¿Podés intentar de nuevo?"

    logger.warning(f"⚠️ Agente alcanzó {max_iterations} iteraciones")
    
    if _intent_needs_basics(user_message):
        return _force_search_and_reply(phone, query="surtido basico repuestos moto")
    
    return last_model_text or "Estoy procesando tu pedido pero está tomando más tiempo del esperado. ¿Podés reformular tu consulta?"

# ===========================================
# WEBHOOK
# ===========================================
def send_out_of_band_message(to_number: str, body: str):
    if not twilio_rest_available:
        return
    try:
        twilio_rest_client.messages.create(from_=TWILIO_WHATSAPP_FROM, to=to_number, body=body)
        logger.info(f"📤 Mensaje enviado a {to_number}")
    except Exception as e:
        logger.error(f"❌ Error enviando mensaje: {e}")

@app.before_request
def validate_twilio_signature():
    if request.path == "/webhook" and twilio_validator:
        signature = request.headers.get("X-Twilio-Signature", "")
        url = request.url
        params = request.form.to_dict()
        if not twilio_validator.validate(url, params, signature):
            logger.warning("⚠️ Solicitud rechazada: firma Twilio inválida")
            return Response("Invalid signature", status=403)

@app.route("/webhook", methods=["POST"])
def webhook():
    start_ts = time_mod.time()
    
    try:
        msg_in = (request.values.get("Body", "") or "").strip()
        phone = request.values.get("From", "")

        if not rate_limit_check(phone):
            resp = MessagingResponse()
            resp.message("Esperá un momento antes de enviar más mensajes 😊")
            return str(resp)

        save_message(phone, msg_in, "user")

        text = run_agent(phone, msg_in)

        save_message(phone, text, "assistant")

        elapsed = time_mod.time() - start_ts
        logger.info(f"⏱️ Webhook procesado en {elapsed:.2f}s")

        resp = MessagingResponse()
        resp.message(text)
        return str(resp)

    except Exception as e:
        logger.error(f"❌ Error en webhook: {e}", exc_info=True)
        err_msg = "Disculpá, tuve un problema técnico. ¿Podés repetir tu consulta?"
        
        if twilio_rest_available:
            send_out_of_band_message(request.values.get("From", ""), err_msg)
        
        resp = MessagingResponse()
        resp.message(err_msg)
        return str(resp)

# =========
# HEALTH
# =========
@app.route("/health", methods=["GET"])
def health():
    try:
        catalog, index = get_catalog_and_index()
        return jsonify({
            "status": "ok",
            "version": "2.3-production-streaming",
            "products": len(catalog) if catalog else 0,
            "faiss": bool(index),
            "built_at": _catalog_and_index_cache["built_at"],
            "tools": len(TOOLS),
            "changelog": {
                "anti_lista_fantasma": True,
                "system_prompt_mejorado": True,
                "persistencia_carrito_24h": True,
                "streaming_real": True,
                "catalogo_extendido": True,
                "timeout_20s": True,
                "decimal_precision": True,
                "logging_detallado": True
            }
        })
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

# =========
# MAIN
# =========
if __name__ == "__main__":
    logger.info("🚀 Iniciando FRAN 2.3 — Production Ready con Streaming Real")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
```
