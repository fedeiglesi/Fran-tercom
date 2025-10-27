# -*- coding: utf-8 -*-
"""
FRAN 2.2 FULL (patched)
Arreglos clave:
- Anti “lista fantasma”: si el modelo no ejecuta tool_call y afirma que envió listado,
  se fuerza una búsqueda real y se responde con productos del catálogo.
- get_last_search_results devuelve product_codes explícitos (para “X de cada uno”).
- ReAct loop con control de iteraciones y logging de tool calls.
- Fallback si FAISS/embeddings fallan (sigue funcionando con fuzzy).
- Timeouts consistentes en requests/embeddings.
- Validación de formato de código (XXXX/XXXXX-XXX).
- Memoria diaria: historial, último producto visto, última búsqueda, carrito.

Requisitos (compatibles con tu requirements.txt):
flask, gunicorn, twilio, openai==1.54.0, httpx, rapidfuzz, faiss-cpu, numpy, requests, python-dotenv
"""

import os
import json
import csv
import io
import sqlite3
import logging
import re
import unicodedata
import random
import threading
import time as time_mod
from datetime import datetime
from collections import defaultdict
from functools import lru_cache
from contextlib import contextmanager
from time import time
from threading import Lock
from typing import Dict, Any, List, Optional

import requests
from flask import Flask, request, jsonify, Response
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from rapidfuzz import process, fuzz
import faiss
import numpy as np
from dotenv import load_dotenv

# ============================================================
# Carga .env
# ============================================================
load_dotenv()

# ============================================================
# Twilio REST y validador (opcional)
# ============================================================
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
logger = logging.getLogger("fran22")

# Variables de entorno
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
if not OPENAI_API_KEY:
    raise RuntimeError("Falta OPENAI_API_KEY")

CATALOG_URL = (os.environ.get("CATALOG_URL",
    "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/main/LISTA_TERCOM_LIMPIA.csv") or "").strip()

EXCHANGE_API_URL = (os.environ.get("EXCHANGE_API_URL",
    "https://dolarapi.com/v1/dolares/oficial") or "").strip()

DEFAULT_EXCHANGE = float(os.environ.get("DEFAULT_EXCHANGE", "1600.0"))
REQUESTS_TIMEOUT = 12

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")

twilio_rest_available = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and TwilioClient)
twilio_rest_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if twilio_rest_available else None
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN) if (RequestValidator and TWILIO_AUTH_TOKEN) else None

# OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

# Human-like typing notice (opcional)
DELAY_SECONDS = 12
delay_messages = ["Dale 👌", "Ok, ya te ayudo…", "Un seg…", "No hay drama, esperá un toque", "Ya vuelvo con vos 😉"]

# =================
# BASE DE DATOS
# =================
DB_PATH = "tercom.db"

@contextmanager
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
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
        c.execute('''CREATE TABLE IF NOT EXISTS conversations
                     (phone TEXT, message TEXT, role TEXT, timestamp TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS carts
                     (phone TEXT, code TEXT, quantity INTEGER, name TEXT, price_usd REAL, price_ars REAL)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_conv_phone ON conversations(phone, timestamp DESC)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_cart_phone ON carts(phone)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_state
                     (phone TEXT PRIMARY KEY, last_code TEXT, last_name TEXT, 
                      last_price_ars REAL, updated_at TEXT)''')
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
                 float(prod.get("price_ars", 0.0)), datetime.now().isoformat())
            )
    except Exception as e:
        logger.error(f"Error guardando user_state: {e}")

def get_user_state(phone):
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute('SELECT last_code, last_name, last_price_ars FROM user_state WHERE phone=?', (phone,))
            row = cur.fetchone()
            if not row:
                return None
            return {"last_code": row[0], "last_name": row[1], "last_price_ars": row[2]}
    except Exception as e:
        logger.error(f"Error leyendo user_state: {e}")
        return None

def save_last_search(phone, products, query):
    try:
        with get_db_connection() as conn:
            conn.execute(
                '''INSERT INTO last_search (phone, products_json, query, timestamp)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(phone) DO UPDATE SET
                       products_json=excluded.products_json,
                       query=excluded.query,
                       timestamp=excluded.timestamp''',
                (phone, json.dumps(products, ensure_ascii=False), query, datetime.now().isoformat())
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
RATE_LIMIT = 10
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

def to_float(s: str) -> float:
    """Convierte texto a float manejando formatos AR/US comunes."""
    if s is None:
        return 0.0
    s = str(s).strip()
    if s in ("", "-", "—", "–"):
        return 0.0
    s = s.replace("USD", "").replace("ARS", "").replace("$", "").replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        val = float(s)
        if val > 1_000_000 and len(s) <= 7:
            val = val / 100
        return round(val, 2)
    except Exception:
        return 0.0

def get_exchange_rate():
    try:
        res = requests.get(EXCHANGE_API_URL, timeout=REQUESTS_TIMEOUT)
        res.raise_for_status()
        return float(res.json().get("venta", DEFAULT_EXCHANGE))
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
            code = line[idx_code].strip() if idx_code is not None and idx_code < len(line) else ""
            name = line[idx_name].strip() if idx_name is not None and idx_name < len(line) else ""
            usd = to_float(line[idx_usd]) if idx_usd is not None and idx_usd < len(line) else 0.0
            ars = to_float(line[idx_ars]) if idx_ars is not None and idx_ars < len(line) else 0.0
            if ars == 0.0 and usd > 0.0:
                ars = round(usd * exchange, 2)
            if name and (usd > 0.0 or ars > 0.0):
                catalog.append({"code": code, "name": name, "price_usd": usd, "price_ars": ars})
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
def fuzzy_search(query, limit=12):
    catalog, _ = get_catalog_and_index()
    if not catalog:
        return []
    names = [p["name"] for p in catalog]
    matches = process.extract(query, names, scorer=fuzz.WRatio, limit=limit)
    return [(catalog[i], score) for _, score, i in matches if score >= 60]

def semantic_search(query, top_k=12):
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

def hybrid_search(query, limit=8):
    query = normalize_search_query(query)
    fuzzy = fuzzy_search(query, limit=limit*2)
    sem = semantic_search(query, top_k=limit*2)
    combined = {}
    for prod, s in fuzzy:
        code = prod.get("code", f"id_{id(prod)}")
        combined.setdefault(code, {"prod": prod, "fuzzy": 0.0, "sem": 0.0})
        combined[code]["fuzzy"] = max(combined[code]["fuzzy"], float(s)/100.0)
    for prod, s in sem:
        code = prod.get("code", f"id_{id(prod)}")
        combined.setdefault(code, {"prod": prod, "fuzzy": 0.0, "sem": 0.0})
        combined[code]["sem"] = max(combined[code]["sem"], float(s))
    out = []
    for _, d in combined.items():
        score = 0.6*d["sem"] + 0.4*d["fuzzy"]
        out.append((d["prod"], score))
    out.sort(key=lambda x: x[1], reverse=True)
    return [p for p, _ in out[:limit]]

# ==================================
# CARRITO (funciones base)
# ==================================
cart_lock = Lock()

def cart_add(phone, code, qty, name, usd, ars):
    qty = max(1, min(int(qty or 1), 100))
    with cart_lock, get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute('SELECT quantity FROM carts WHERE phone=? AND code=?', (phone, code))
        row = cur.fetchone()
        if row:
            newq = row[0] + qty
            conn.execute('UPDATE carts SET quantity=? WHERE phone=? AND code=?', (newq, phone, code))
        else:
            conn.execute('INSERT INTO carts VALUES (?, ?, ?, ?, ?, ?)',
                        (phone, code, qty, name, float(usd or 0.0), float(ars or 0.0)))

def cart_get(phone):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute('SELECT code, quantity, name, price_ars FROM carts WHERE phone=?', (phone,))
        return cur.fetchall()

def cart_update_qty(phone, code, qty):
    qty = max(0, min(int(qty or 0), 999))
    with cart_lock, get_db_connection() as conn:
        if qty == 0:
            conn.execute('DELETE FROM carts WHERE phone=? AND code=?', (phone, code))
        else:
            conn.execute('UPDATE carts SET quantity=? WHERE phone=? AND code=?', (qty, phone, code))

def cart_clear(phone):
    with cart_lock, get_db_connection() as conn:
        conn.execute('DELETE FROM carts WHERE phone=?', (phone,))

def cart_totals(phone):
    items = cart_get(phone)
    total = sum(q * price for _, q, __, price in items)
    discount = 0.05 * total if total > 10_000_000 else 0.0
    return total - discount, discount

# ============================================================
# 🤖 DEFINICIÓN DE HERRAMIENTAS (FUNCTION CALLING)
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
            "description": "Busca productos en el catálogo por nombre, características o descripción",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "default": 8}
                },
                "required": ["query"]
            }
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
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string"},
                                "quantity": {"type": "integer", "default": 1}
                            },
                            "required": ["code"]
                        }
                    }
                },
                "required": ["items"]
            }
        }
    },
    {
        "type": "function",
        "function": {"name": "view_cart", "description": "Muestra el carrito", "parameters": {"type": "object", "properties": {}}}
    },
    {
        "type": "function",
        "function": {
            "name": "update_cart_item",
            "description": "Modifica cantidad (0 elimina)",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "quantity": {"type": "integer"}
                },
                "required": ["code", "quantity"]
            }
        }
    },
    {
        "type": "function",
        "function": {"name": "clear_cart", "description": "Vacía el carrito", "parameters": {"type": "object", "properties": {}}}
    },
    {
        "type": "function",
        "function": {"name": "confirm_order", "description": "Confirma y procesa el pedido", "parameters": {"type": "object", "properties": {}}}
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_details",
            "description": "Detalle por código",
            "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "compare_products",
            "description": "Compara múltiples códigos",
            "parameters": {"type": "object", "properties": {"codes": {"type": "array", "items": {"type": "string"}}}, "required": ["codes"]}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_recommendations",
            "description": "Recomendaciones basadas en last_viewed o carrito",
            "parameters": {
                "type": "object",
                "properties": {"based_on": {"type": "string", "default": "last_viewed"}, "limit": {"type": "integer", "default": 5}}
            }
        }
    },
    {
        "type": "function",
        "function": {"name": "get_last_search_results", "description": "Últimos resultados y códigos", "parameters": {"type": "object", "properties": {}}}
    }
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

    # ---------- Implementaciones ----------
    def search_products(self, query: str, limit: int = 8) -> Dict:
        results = hybrid_search(query, limit=limit)
        if results:
            save_user_state(self.phone, results[0])
            save_last_search(self.phone, results, query)
        return {
            "success": True,
            "query": query,
            "results": [{"code": p["code"], "name": p["name"], "price_ars": p["price_ars"], "price_usd": p["price_usd"]} for p in results],
            "count": len(results),
            "message": f"Encontré {len(results)} productos para '{query}'." if results else f"No encontré productos para '{query}'."
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
                cart_add(self.phone, code, qty, prod["name"], prod["price_usd"], prod["price_ars"])
                added.append({
                    "code": code, "name": prod["name"], "quantity": qty,
                    "price_unit": prod["price_ars"], "subtotal": round(prod["price_ars"] * qty, 2)
                })
                save_user_state(self.phone, prod)
            else:
                not_found.append(code)
        if added and not (not_found or invalid_codes):
            total_items = sum(x["quantity"] for x in added)
            total_price = sum(x["subtotal"] for x in added)
            return {
                "success": True,
                "added": added,
                "total_items": total_items,
                "total_price": total_price,
                "message": f"¡Listo! Agregué {len(added)} productos ({total_items} unidades) por ${total_price:,.2f} ARS"
            }
        elif added:
            errors = []
            if not_found:
                errors.append(f"No encontré: {', '.join(not_found)}")
            if invalid_codes:
                errors.append(f"Códigos inválidos: {', '.join(invalid_codes)} (formato: XXXX/XXXXX-XXX)")
            return {"success": True, "added": added, "message": f"Agregué {len(added)} productos. {' | '.join(errors)}"}
        else:
            return {"success": False, "added": [], "message": "No pude agregar productos. Hacé una búsqueda primero para obtener códigos válidos."}

    def view_cart(self) -> Dict:
        items = cart_get(self.phone)
        total, discount = cart_totals(self.phone)
        return {
            "success": True,
            "items": [{"code": code, "name": name, "quantity": qty, "price_unit": price, "price_total": round(price * qty, 2)} for code, qty, name, price in items],
            "subtotal": round(total + discount, 2),
            "discount": round(discount, 2),
            "total": round(total, 2),
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
        return {"success": True, "message": "Carrito vaciado"}

    def confirm_order(self) -> Dict:
        items = cart_get(self.phone)
        if not items:
            return {"success": False, "error": "Carrito vacío"}
        total, discount = cart_totals(self.phone)
        order_details = {
            "items": [{"code": code, "name": name, "quantity": qty, "price": price, "subtotal": round(price * qty, 2)} for code, qty, name, price in items],
            "subtotal": round(total + discount, 2),
            "discount": round(discount, 2),
            "total": round(total, 2),
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
        return {"success": True, "product": prod, "message": f"**(Cód: {prod['code']})** {prod['name']} - ${prod['price_ars']:,.2f} ARS"}

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
            "price_difference": round(sorted_by_price[-1]["price_ars"] - sorted_by_price[0]["price_ars"], 2),
            "comparison": f"Más barato: {sorted_by_price[0]['name']} (${sorted_by_price[0]['price_ars']:,.2f}). Más caro: {sorted_by_price[-1]['name']} (${sorted_by_price[-1]['price_ars']:,.2f})"
        }

    def get_recommendations(self, based_on: str = "last_viewed", limit: int = 5) -> Dict:
        if based_on == "last_viewed":
            state = get_user_state(self.phone)
            if state and state.get("last_name"):
                results = semantic_search(state["last_name"], top_k=limit+1)
                recommendations = [p for p, _ in results if p.get("code") != state.get("last_code")][:limit]
                return {"success": True, "recommendations": recommendations, "reason": f"Basado en tu interés en: {state['last_name']}"}
        elif based_on == "cart":
            items = cart_get(self.phone)
            if items:
                first_item_name = items[0][2]
                results = semantic_search(first_item_name, top_k=limit+len(items))
                cart_codes = [item[0] for item in items]
                recommendations = [p for p, _ in results if p.get("code") not in cart_codes][:limit]
                return {"success": True, "recommendations": recommendations, "reason": "Productos complementarios a tu carrito"}
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
            "message": (
                f"Última búsqueda: '{search['query']}' con {len(search['products'])} productos. "
                f"Códigos: {', '.join(product_codes[:5])}{'...' if len(product_codes) > 5 else ''}"
            )
        }

# ============================================================
# 🧠 AGENTE AUTÓNOMO (ReAct) — with anti “lista fantasma”
# ============================================================
def _format_list(products, max_items=8) -> str:
    if not products:
        return "No encontré productos."
    lines = []
    for p in products[:max_items]:
        code = p.get("code", "").strip() or "s/c"
        name = p.get("name", "").strip()
        ars = p.get("price_ars", 0.0)
        lines.append(f"• (Cód: {code}) {name} - ${ars:,.0f} ARS")
    return "\n".join(lines)

def _intent_needs_basics(user_message: str) -> bool:
    text = user_message.lower()
    triggers = ["surtido", "básico", "basico", "abrir mi local", "abrir un local", "recomendar", "proponer", "lista de productos", "lo básico", "lo basico"]
    return any(t in text for t in triggers)

def _force_search_and_reply(phone: str, query: str) -> str:
    # Búsqueda directa por servidor para garantizar que exista el listado
    results = hybrid_search(query, limit=10)
    if not results:
        # Segundo intento más genérico
        results = hybrid_search("repuestos moto", limit=10)
    if not results:
        return "No encontré productos para armar un surtido inicial en este momento."
    save_last_search(phone, results, query)
    save_user_state(phone, results[0])
    listado = _format_list(results, max_items=8)
    return f"Acá tenés un surtido sugerido ({query}):\n{listado}"

def run_agent(phone: str, user_message: str, max_iterations: int = 5) -> str:
    catalog, _ = get_catalog_and_index()
    if not catalog:
        return "No puedo acceder al catálogo en este momento. Probá más tarde."

    executor = ToolExecutor(phone)
    history = get_history_today(phone, limit=20)
    state = get_user_state(phone)
    cart_items = cart_get(phone)

    system_prompt = f"""Sos Fran, vendedor experto de Tercom (mayorista de repuestos para motos en Argentina).
- Usá lenguaje argentino natural (vos, che, dale).
- No inventes productos ni códigos. Siempre usá lo que devuelven las herramientas.
- Formato por ítem: **(Cód: XXXX/XXXXX-XXX)** Nombre - $precio ARS
- Protocolo “X de cada uno”: get_last_search_results → add_to_cart con esos códigos → confirmar.
- Confirmación: si el usuario dice “confirmo” → primero view_cart (preview), preguntar “¿Confirmo?” y solo si afirma, confirm_order.
- Referencias implícitas: “agregala/lo” = último producto con código; “esos” = últimos productos (get_last_search_results).
- Si pedís “surtido”, “básico”, “recomendá”: llamá search_products con algo como “surtido inicial moto” o “repuestos básicos moto”.
- Nunca digas “te pasé la lista” si no ejecutaste una búsqueda que devuelva resultados reales.
Estado:
- Carrito: {len(cart_items)} ítems
- Último visto: {state.get('last_name', 'ninguno') if state else 'ninguno'}
"""

    messages = [{"role": "system", "content": system_prompt}]
    for msg, role in history:
        messages.append({"role": role, "content": msg})
    messages.append({"role": "user", "content": user_message})

    # ReAct loop
    for iteration in range(max_iterations):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.7,
                max_tokens=1000,
                timeout=REQUESTS_TIMEOUT
            )
            message = response.choices[0].message

            # 1) Si hay tool_calls → ejecutarlas y seguir
            if getattr(message, "tool_calls", None):
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
                # sigue el loop para que el modelo use los resultados
                continue

            # 2) Sin tool_calls: respuesta final del modelo
            final_response = (message.content or "").strip()

            # Anti “lista fantasma”: si pide surtido y no hay bullets/códigos, forzar búsqueda de servidor
            if _intent_needs_basics(user_message):
                # ¿La respuesta contiene un listado con códigos?
                looks_like_list = bool(re.search(r"\(Cód:\s*\d{4}/\d{5}-\d{3}\)", final_response))
                if not looks_like_list:
                    logger.info("⚠️ Anti-lista-fantasma activado: forzando búsqueda real de surtido.")
                    return _force_search_and_reply(phone, query="surtido inicial moto")

            logger.info(f"✅ Agente respondió en iteración {iteration + 1}")
            return final_response or "¿Podés repetirlo? No encontré productos."
        except Exception as e:
            logger.error(f"Error en iteración {iteration}: {e}", exc_info=True)
            return "Disculpá, tuve un problema procesando tu pedido. ¿Podés intentar de nuevo?"

    logger.warning(f"⚠️ Agente alcanzó {max_iterations} iteraciones — devolviendo fallback")
    # Fallback final: si pedía surtido, asegurar listado real
    if _intent_needs_basics(user_message):
        return _force_search_and_reply(phone, query="surtido inicial moto")
    return "Estoy procesando tu pedido pero está tomando más tiempo del esperado. ¿Podés reformular tu consulta?"

# ===========================================
# 📱 WEBHOOK PRINCIPAL
# ===========================================
def send_out_of_band_message(to_number: str, body: str):
    if not twilio_rest_available:
        return
    try:
        twilio_rest_client.messages.create(from_=TWILIO_WHATSAPP_FROM, to=to_number, body=body)
        logger.info(f"Mensaje fuera de banda enviado a {to_number}")
    except Exception as e:
        logger.error(f"Error enviando mensaje fuera de banda: {e}")

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
    cancel_event = threading.Event()
    try:
        msg_in = (request.values.get("Body", "") or "").strip()
        phone = request.values.get("From", "")

        # Rate limiting
        if not rate_limit_check(phone):
            resp = MessagingResponse()
            resp.message("Esperá un momento antes de enviar más mensajes 😊")
            return str(resp)

        save_message(phone, msg_in, "user")

        # Aviso humano en paralelo
        def delayed_notice():
            waited = 0
            while waited < DELAY_SECONDS and not cancel_event.is_set():
                time_mod.sleep(0.2)
                waited += 0.2
            if not cancel_event.is_set():
                send_out_of_band_message(phone, random.choice(delay_messages))

        if twilio_rest_available:
            threading.Thread(target=delayed_notice, daemon=True).start()

        # Ejecuta agente
        text = run_agent(phone, msg_in)

        save_message(phone, text, "assistant")
        cancel_event.set()

        elapsed = time_mod.time() - start_ts
        logger.info(f"⏱️ Webhook procesado en {elapsed:.2f}s")

        # Si tardó mucho, refuerzo fuera de banda
        if elapsed > (DELAY_SECONDS - 0.5) and twilio_rest_available:
            send_out_of_band_message(phone, text)

        resp = MessagingResponse()
        resp.message(text)
        return str(resp)

    except Exception as e:
        logger.error(f"❌ Error en webhook: {e}", exc_info=True)
        cancel_event.set()
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
            "version": "2.2-full-patched",
            "products": len(catalog) if catalog else 0,
            "faiss": bool(index),
            "built_at": _catalog_and_index_cache["built_at"],
            "tools": len(TOOLS),
            "changelog": {
                "anti_lista_fantasma": True,
                "product_codes_fix": True,
                "preview_before_confirm": True,
                "fallback_semantico": True,
                "timeouts_uniformes": True
            }
        })
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

# =========
# MAIN
# =========
if __name__ == "__main__":
    logger.info("🚀 Iniciando FRAN 2.2 FULL (patched)")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
