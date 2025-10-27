"""
FRAN 2.2 - Agente de Ventas Autónomo con IA
Mejoras: Fix "X de cada uno" + Preview pedidos + Ejecutar recomendaciones
Changelog v2.2:
- ✅ get_last_search_results devuelve product_codes explícitos
- ✅ Logging detallado de tool calls
- ✅ System prompt optimizado con instrucciones #6, #7, #8
- ✅ Mejor manejo de errores con mensajes contextuales
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

# ============================================================
# Twilio REST y validador
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
logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
CATALOG_URL = (os.environ.get("CATALOG_URL", 
    "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/main/LISTA_TERCOM_LIMPIA.csv") or "").strip()
EXCHANGE_API_URL = (os.environ.get("EXCHANGE_API_URL", 
    "https://dolarapi.com/v1/dolares/oficial") or "").strip()
DEFAULT_EXCHANGE = float(os.environ.get("DEFAULT_EXCHANGE", 1600.0))

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")

twilio_rest_available = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and TwilioClient)
twilio_rest_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if twilio_rest_available else None
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN) if (RequestValidator and TWILIO_AUTH_TOKEN) else None

# Inicialización de OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

DELAY_SECONDS = 12
delay_messages = ["Dale 👌", "Ok, ya te ayudo…", "Un seg…", "No hay drama, esperá un toque", "Ya vuelvo con vos 😉"]

# ===========================
# HELPERS DE TWILIO
# ===========================
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
    finally:
        conn.close()

def validate_tercom_code(code):
    """
    Valida que un código tenga el formato correcto de Tercom: XXXX/XXXXX-XXX
    Returns: (is_valid, normalized_code)
    """
    import re
    
    # Formato esperado: 1548/00016-566
    pattern = r'^\d{4}/\d{5}-\d{3}$'
    
    if re.match(pattern, str(code).strip()):
        return True, str(code).strip()
    
    # Intentar normalizar (por si viene sin barras o guiones)
    # Ej: "154800016566" → "1548/00016-566"
    code_clean = re.sub(r'[^0-9]', '', str(code))
    if len(code_clean) == 12:
        normalized = f"{code_clean[:4]}/{code_clean[4:9]}-{code_clean[9:12]}"
        return True, normalized
    
    return False, code

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
    """Convierte texto a float manejando formatos argentinos."""
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
        res = requests.get(EXCHANGE_API_URL, timeout=5)
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
            resp = client.embeddings.create(input=chunk, model="text-embedding-3-small")
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

@lru_cache(maxsize=1)
def _load_raw_csv():
    r = requests.get(CATALOG_URL, timeout=20)
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
        idx_name = find_idx(["producto", "descripcion", "description", "nombre"])
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

# ============================
# MEMORIA Y ESTADO
# ============================
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
    """Guarda la última lista de productos mostrada al usuario"""
    try:
        with get_db_connection() as conn:
            conn.execute(
                '''INSERT INTO last_search (phone, products_json, query, timestamp)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(phone) DO UPDATE SET
                       products_json=excluded.products_json,
                       query=excluded.query,
                       timestamp=excluded.timestamp''',
                (phone, json.dumps(products), query, datetime.now().isoformat())
            )
    except Exception as e:
        logger.error(f"Error guardando last_search: {e}")

def get_last_search(phone):
    """Recupera la última lista de productos mostrada"""
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
        resp = client.embeddings.create(input=[query], model="text-embedding-3-small")
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

# Aliases y correcciones de búsqueda comunes
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
    "yamalube": "YAMALUBE"
}

def normalize_search_query(query):
    """Normaliza queries de búsqueda aplicando aliases"""
    query_lower = query.lower()
    for alias, replacement in SEARCH_ALIASES.items():
        if alias in query_lower:
            query_lower = query_lower.replace(alias, replacement)
    return query_lower

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

def add_to_cart(phone, code, qty, name, usd, ars):
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

def get_cart(phone):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute('SELECT code, quantity, name, price_ars FROM carts WHERE phone=?', (phone,))
        return cur.fetchall()

def update_cart_qty(phone, code, qty):
    qty = max(0, min(int(qty or 0), 999))
    with cart_lock, get_db_connection() as conn:
        if qty == 0:
            conn.execute('DELETE FROM carts WHERE phone=? AND code=?', (phone, code))
        else:
            conn.execute('UPDATE carts SET quantity=? WHERE phone=? AND code=?', (qty, phone, code))

def clear_cart(phone):
    with cart_lock, get_db_connection() as conn:
        conn.execute('DELETE FROM carts WHERE phone=?', (phone,))

def cart_totals(phone):
    items = get_cart(phone)
    total = sum(q * price for _, q, __, price in items)
    discount = 0.05 * total if total > 10_000_000 else 0.0
    return total - discount, discount

# ============================================================
# 🤖 DEFINICIÓN DE HERRAMIENTAS (FUNCTION CALLING)
# ============================================================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "Busca productos en el catálogo por nombre, características o descripción",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Texto de búsqueda (ej: 'cable hdmi', 'mouse inalambrico', 'teclado mecánico')"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Cantidad máxima de resultados a mostrar",
                        "default": 5
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "add_to_cart",
            "description": "Agrega uno o varios productos al carrito del usuario",
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "Lista de productos a agregar",
                        "items": {
                            "type": "object",
                            "properties": {
                                "code": {"type": "string", "description": "Código del producto"},
                                "quantity": {"type": "integer", "description": "Cantidad a agregar", "default": 1}
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
        "function": {
            "name": "view_cart",
            "description": "Muestra el contenido actual del carrito con precios y totales",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_cart_item",
            "description": "Modifica la cantidad de un producto en el carrito (0 para eliminar)",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Código del producto"},
                    "quantity": {"type": "integer", "description": "Nueva cantidad (0 elimina el item)"}
                },
                "required": ["code", "quantity"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "clear_cart",
            "description": "Vacía completamente el carrito del usuario",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_order",
            "description": "Confirma y procesa el pedido actual",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_product_details",
            "description": "Obtiene información detallada de un producto específico por código",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Código del producto"}
                },
                "required": ["code"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "compare_products",
            "description": "Compara precios y características de múltiples productos",
            "parameters": {
                "type": "object",
                "properties": {
                    "codes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lista de códigos de productos a comparar (mínimo 2)"
                    }
                },
                "required": ["codes"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_recommendations",
            "description": "Obtiene recomendaciones de productos basadas en el contexto del usuario",
            "parameters": {
                "type": "object",
                "properties": {
                    "based_on": {
                        "type": "string",
                        "description": "Base para recomendación: 'last_viewed', 'cart', o un código de producto",
                        "default": "last_viewed"
                    },
                    "limit": {"type": "integer", "default": 5}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_last_search_results",
            "description": "Obtiene los resultados de la última búsqueda realizada por el usuario (útil cuando dice 'esos', 'de cada uno', etc.)",
            "parameters": {"type": "object", "properties": {}}
        }
    }
]

# ============================================================
# 🛠️ EJECUTOR DE HERRAMIENTAS - VERSIÓN 2.2 MEJORADA
# ============================================================

class ToolExecutor:
    """
    Ejecuta las herramientas que el modelo solicita
    V2.2 - Con logging detallado y mejoras críticas
    """
    
    def __init__(self, phone: str):
        self.phone = phone
    
    def execute(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Router principal de herramientas con logging mejorado"""
        method = getattr(self, tool_name, None)
        if not method:
            logger.error(f"❌ Tool not found: {tool_name}")
            return {"error": f"Herramienta '{tool_name}' no encontrada"}
        
        try:
            # 🔍 LOGGING DETALLADO (v2.2)
            logger.info(f"🔧 Tool: {tool_name}")
            args_preview = json.dumps(arguments, ensure_ascii=False)[:200]
            logger.info(f"   Args: {args_preview}{'...' if len(str(arguments)) > 200 else ''}")
            
            result = method(**arguments)
            
            # Log resultado (truncado)
            result_str = json.dumps(result, ensure_ascii=False)
            logger.info(f"   Result: {result_str[:200]}{'...' if len(result_str) > 200 else ''}")
            
            return result
        except Exception as e:
            logger.error(f"❌ {tool_name} error: {e}", exc_info=True)
            return {"error": str(e), "tool": tool_name}
    
    def search_products(self, query: str, limit: int = 5) -> Dict:
        """Búsqueda híbrida de productos"""
        results = hybrid_search(query, limit=limit)
        
        if results:
            save_user_state(self.phone, results[0])
            save_last_search(self.phone, results, query)
        
        return {
            "success": True,
            "query": query,
            "results": [
                {
                    "code": p["code"],
                    "name": p["name"],
                    "price_ars": p["price_ars"],
                    "price_usd": p["price_usd"]
                }
                for p in results
            ],
            "count": len(results),
            "message": f"Encontré {len(results)} productos para '{query}'." if results else f"No encontré productos para '{query}'. Probá con otro término."
        }
    
    def add_to_cart(self, items: List[Dict]) -> Dict:
        """
        Agrega productos al carrito
        V2.2 - Mejor logging y mensajes contextuales
        """
        catalog, _ = get_catalog_and_index()
        added = []
        not_found = []
        invalid_codes = []
        
        for item in items:
            code = str(item.get("code", "")).strip()
            qty = int(item.get("quantity", 1))
            
            logger.info(f"   Procesando: {code} x{qty}")
            
            # Validar formato
            is_valid, normalized_code = validate_tercom_code(code)
            if not is_valid:
                logger.warning(f"   ⚠️ Código inválido: {code}")
                invalid_codes.append(code)
                continue
            
            code = normalized_code
            prod = next((x for x in catalog if x["code"] == code), None)
            
            if prod:
                add_to_cart(self.phone, code, qty, prod["name"], prod["price_usd"], prod["price_ars"])
                added.append({
                    "code": code,
                    "name": prod["name"],
                    "quantity": qty,
                    "price_unit": prod["price_ars"],
                    "subtotal": prod["price_ars"] * qty
                })
                save_user_state(self.phone, prod)
                logger.info(f"   ✅ Agregado: {code} x{qty}")
            else:
                logger.warning(f"   ⚠️ No encontrado: {code}")
                not_found.append(code)
        
        # Mensajes contextuales mejorados (v2.2)
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
            return {
                "success": True,
                "added": added,
                "message": f"Agregué {len(added)} productos. {' | '.join(errors)}"
            }
        else:
            suggestion = "Hacé una búsqueda primero para obtener códigos válidos"
            return {
                "success": False,
                "added": [],
                "message": f"No pude agregar productos. {suggestion}"
            }
    
    def view_cart(self) -> Dict:
        """Muestra el carrito actual"""
        items = get_cart(self.phone)
        total, discount = cart_totals(self.phone)
        
        return {
            "success": True,
            "items": [
                {
                    "code": code,
                    "name": name,
                    "quantity": qty,
                    "price_unit": price,
                    "price_total": price * qty
                }
                for code, qty, name, price in items
            ],
            "subtotal": total + discount,
            "discount": discount,
            "total": total,
            "item_count": len(items),
            "unit_count": sum(qty for _, qty, __, ___ in items)
        }
    
    def update_cart_item(self, code: str, quantity: int) -> Dict:
        """Actualiza cantidad de un item"""
        catalog, _ = get_catalog_and_index()
        prod = next((x for x in catalog if x["code"] == code), None)
        
        if not prod:
            return {"success": False, "error": f"Producto {code} no encontrado"}
        
        update_cart_qty(self.phone, code, quantity)
        
        return {
            "success": True,
            "code": code,
            "name": prod["name"],
            "new_quantity": quantity,
            "action": "removed" if quantity == 0 else "updated",
            "message": f"{'Eliminado' if quantity == 0 else f'Actualizado a {quantity} unidades'}"
        }
    
    def clear_cart(self) -> Dict:
        """Vacía el carrito"""
        clear_cart(self.phone)
        logger.info(f"🗑️ Carrito vaciado: {self.phone}")
        return {"success": True, "message": "Carrito vaciado"}
    
    def confirm_order(self) -> Dict:
        """Confirma el pedido"""
        items = get_cart(self.phone)
        if not items:
            return {"success": False, "error": "Carrito vacío"}
        
        total, discount = cart_totals(self.phone)
        
        order_details = {
            "items": [
                {"code": code, "name": name, "quantity": qty, "price": price, "subtotal": price * qty}
                for code, qty, name, price in items
            ],
            "subtotal": total + discount,
            "discount": discount,
            "total": total,
            "item_count": len(items),
            "unit_count": sum(qty for _, qty, __, ___ in items)
        }
        
        clear_cart(self.phone)
        logger.info(f"✅ Pedido confirmado: {len(items)} items, ${total:,.2f} ARS")
        
        return {
            "success": True,
            "order": order_details,
            "message": f"¡Pedido confirmado! {order_details['item_count']} productos ({order_details['unit_count']} unidades) por ${total:,.2f} ARS"
        }
    
    def get_product_details(self, code: str) -> Dict:
        """Detalles completos de un producto"""
        catalog, _ = get_catalog_and_index()
        prod = next((x for x in catalog if x["code"] == code), None)
        
        if not prod:
            return {"success": False, "error": f"Producto {code} no encontrado"}
        
        return {
            "success": True,
            "product": prod,
            "message": f"**(Cód: {prod['code']})** {prod['name']} - ${prod['price_ars']:,.2f} ARS"
        }
    
    def compare_products(self, codes: List[str]) -> Dict:
        """Compara múltiples productos"""
        catalog, _ = get_catalog_and_index()
        products = [p for p in catalog if p["code"] in codes]
        
        if len(products) < 2:
            return {
                "success": False,
                "error": f"Necesito al menos 2 productos válidos. Solo encontré {len(products)}"
            }
        
        sorted_by_price = sorted(products, key=lambda x: x["price_ars"])
        
        return {
            "success": True,
            "products": products,
            "cheapest": sorted_by_price[0],
            "most_expensive": sorted_by_price[-1],
            "price_difference": sorted_by_price[-1]["price_ars"] - sorted_by_price[0]["price_ars"],
            "comparison": f"Más barato: {sorted_by_price[0]['name']} (${sorted_by_price[0]['price_ars']:,.2f}). Más caro: {sorted_by_price[-1]['name']} (${sorted_by_price[-1]['price_ars']:,.2f})"
        }
    
    def get_recommendations(self, based_on: str = "last_viewed", limit: int = 5) -> Dict:
        """Recomendaciones personalizadas"""
        
        if based_on == "last_viewed":
            state = get_user_state(self.phone)
            if state and state.get("last_name"):
                results = semantic_search(state["last_name"], top_k=limit+1)
                recommendations = [p for p, _ in results if p.get("code") != state.get("last_code")][:limit]
                return {
                    "success": True,
                    "recommendations": recommendations,
                    "reason": f"Basado en tu interés en: {state['last_name']}"
                }
        
        elif based_on == "cart":
            items = get_cart(self.phone)
            if items:
                first_item_name = items[0][2]
                results = semantic_search(first_item_name, top_k=limit+len(items))
                cart_codes = [item[0] for item in items]
                recommendations = [p for p, _ in results if p.get("code") not in cart_codes][:limit]
                return {
                    "success": True,
                    "recommendations": recommendations,
                    "reason": "Productos complementarios a tu carrito"
                }
        
        catalog, _ = get_catalog_and_index()
        return {
            "success": True,
            "recommendations": catalog[:limit],
            "reason": "Productos destacados"
        }
    
    def get_last_search_results(self) -> Dict:
        """
        Recupera los últimos productos buscados/mostrados al usuario
        V2.2 - MEJORA CRÍTICA: Devuelve product_codes explícitamente
        """
        search = get_last_search(self.phone)
        
        if not search:
            return {
                "success": False,
                "message": "No hay búsquedas recientes. Hacé una búsqueda primero."
            }
        
        # ✅ CLAVE (v2.2): Extraer códigos en lista simple
        product_codes = [p["code"] for p in search["products"]]
        
        return {
            "success": True,
            "query": search["query"],
            "products": search["products"],
            "product_codes": product_codes,  # ← NUEVO en v2.2
            "count": len(search["products"]),
            "message": (
                f"Última búsqueda: '{search['query']}' con {len(search['products'])} productos. "
                f"Códigos: {', '.join(product_codes[:5])}"
                f"{'...' if len(product_codes) > 5 else ''}"
            )
        }

# ============================================================
# 🧠 AGENTE AUTÓNOMO (ReAct Loop) - V2.2
# ============================================================

def run_agent(phone: str, user_message: str, max_iterations: int = 5) -> str:
    """
    Loop principal del agente con ReAct (Reasoning + Acting)
    V2.2 - System prompt optimizado con fixes #1, #2, #3
    """
    
    executor = ToolExecutor(phone)
    
    # Contexto del usuario
    history = get_history_today(phone, limit=20)
    state = get_user_state(phone)
    cart_items = get_cart(phone)
    
    # ============================================================
    # 📝 SYSTEM PROMPT OPTIMIZADO - V2.2
    # ============================================================
    system_prompt = f"""Sos Fran, vendedor experto de Tercom (distribuidora de repuestos para motos en Argentina).

🎯 PERSONALIDAD:
- Amable, cercano, profesional
- Usás lenguaje argentino natural (vos, che, dale, etc.)
- Proactivo: sugerís productos complementarios cuando corresponde
- Honesto: si no sabés algo, lo admitís y buscás la info

📊 ESTADO DEL USUARIO:
- Carrito actual: {len(cart_items)} items
- Último producto visto: {state.get('last_name', 'ninguno') if state else 'ninguno'}

📦 EJEMPLOS DE PRODUCTOS REALES:

EJEMPLO 1 - Búsqueda de aceites:
Usuario: "Tenes aceites para motos?"
→ [search_products("aceite moto")]
Respuesta: "Sí, tengo estos aceites:
1. **(Cód: 1005/02102-630)** Aceite Moto 4T 20W40 x 1LT Yamalube - $7,822.36
2. **(Cód: 1003/03100-658)** Aceite Pride 2T Verde x 1 Litro Gulf - $4,572.79"

EJEMPLO 2 - Pedido mayorista "X de cada uno":
Usuario: "Dame 10 de cada uno"
→ [get_last_search_results] → obtiene product_codes
→ [add_to_cart] con esos códigos
Respuesta: "¡Listo! Agregué:
• Aceite Yamalube x10 - $78,223
• Aceite Pride 2T x10 - $45,727
Total: $123,950"

🔢 FORMATO DE CÓDIGOS: XXXX/XXXXX-XXX
Ejemplo: 1548/00016-566

⚠️ INSTRUCCIONES CRÍTICAS:

1. **NUNCA inventes códigos** - Siempre vienen de search_products
2. **"X de cada uno" protocol:**
   - PASO 1: get_last_search_results (devuelve product_codes)
   - PASO 2: add_to_cart con ESA lista
   - PASO 3: Confirmar con códigos reales

3. **Formato OBLIGATORIO:**
   **(Cód: XXXX/XXXXX-XXX)** Nombre - $precio ARS

4. **Si add_to_cart falla:**
   - NO busques productos random
   - Pedí que especifique mejor
   - NO mezcles contextos

5. **Memoria del día completa:**
   - Podés referenciar conversaciones de horas atrás
   - get_last_search_results = última búsqueda

6. **PROTOCOLO DE CONFIRMACIÓN DE PEDIDO:**
   Cuando el usuario dice "confirmo":
   - PASO 1: Llamar view_cart para mostrar resumen
   - PASO 2: Preguntar "¿Confirmo el pedido?" y ESPERAR
   - PASO 3: Solo si confirma (sí/dale), llamar confirm_order
   
   Ejemplo:
   Usuario: "confirmo"
   → view_cart → "Tenés: Espejo x1 ($9,152), Cubierta x1 ($23,315). Total: $32,468. ¿Confirmo?"
   Usuario: "sí"
   → confirm_order → "✅ Pedido confirmado!"

7. **PATRÓN DE RECOMENDACIÓN CONFIRMADA:**
   Si recomendaste un producto CON CÓDIGO y el usuario confirma:
   - Ejecutar add_to_cart directamente
   - NO preguntes de nuevo
   
   Ejemplo:
   Bot: "Batería Gel (Cód: 1079/12060-128) - $25,391"
   Usuario: "sí, agregala"
   → add_to_cart([{{code: "1079/12060-128", quantity: 1}}])
   Bot: "✅ Batería agregada!"

8. **REFERENCIAS IMPLÍCITAS:**
   - "agregala" = producto que acabás de mencionar con código
   - "ese"/"esa"/"esos" = últimos productos (usar get_last_search_results)
   - "el más barato" = analizar get_last_search_results

💬 ESTILO:
- Natural y conversacional
- Directo al grano
- Emojis con moderación (máx 2/mensaje)
- SIEMPRE mostrá códigos entre paréntesis

🚫 NO HAGAS:
- Inventar códigos
- Confirmar pedidos sin preview
- Mostrar productos irrelevantes al fallar
- Repetir el mismo error sin usar get_last_search_results
"""

    messages = [{"role": "system", "content": system_prompt}]
    
    # Agregar historial
    for msg, role in history:
        messages.append({"role": role, "content": msg})
    
    # Mensaje actual
    messages.append({"role": "user", "content": user_message})
    
    # Loop del agente
    for iteration in range(max_iterations):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.7,
                max_tokens=1000,
                timeout=15.0
            )
            
            message = response.choices[0].message
            
            # Si no quiere usar herramientas, terminamos
            if not message.tool_calls:
                final_response = message.content
                logger.info(f"✅ Agente respondió en iteración {iteration + 1}")
                return final_response
            
            # Agregar mensaje del modelo al historial
            messages.append(message)
            
            # Ejecutar herramientas solicitadas
            for tool_call in message.tool_calls:
                tool_name = tool_call.function.name
                tool_args = json.loads(tool_call.function.arguments)
                
                # Ejecutar
                result = executor.execute(tool_name, tool_args)
                
                # Agregar resultado al contexto
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_name,
                    "content": json.dumps(result, ensure_ascii=False)
                })
        
        except Exception as e:
            logger.error(f"Error en iteración {iteration}: {e}", exc_info=True)
            return "Disculpá, tuve un problema procesando tu pedido. ¿Podés intentar de nuevo?"
    
    # Límite de iteraciones alcanzado
    logger.warning(f"⚠️ Agente alcanzó {max_iterations} iteraciones")
    return "Estoy procesando tu pedido pero está tomando más tiempo del esperado. ¿Podés reformular tu consulta?"

# ===========================================
# 📱 WEBHOOK PRINCIPAL
# ===========================================

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
        
        # Delay humano asíncrono
        def delayed_notice():
            waited = 0
            while waited < DELAY_SECONDS and not cancel_event.is_set():
                time_mod.sleep(0.2)
                waited += 0.2
            if not cancel_event.is_set():
                send_out_of_band_message(phone, random.choice(delay_messages))
        
        if twilio_rest_available:
            threading.Thread(target=delayed_notice, daemon=True).start()
        
        # 🤖 EL AGENTE AUTÓNOMO MANEJA TODO (v2.2 mejorado)
        text = run_agent(phone, msg_in)
        
        save_message(phone, text, "assistant")
        cancel_event.set()
        
        elapsed = time_mod.time() - start_ts
        logger.info(f"⏱️ Webhook procesado en {elapsed:.2f}s")
        
        # Fallback fuera de banda si tardó mucho
        if elapsed > 11.5 and twilio_rest_available:
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
            "version": "2.2",  # ← Actualizado
            "products": len(catalog) if catalog else 0,
            "faiss": bool(index),
            "built_at": _catalog_and_index_cache["built_at"],
            "tools": len(TOOLS),
            "changelog": {
                "product_codes_fix": True,
                "preview_before_confirm": True,
                "execute_recommendations": True
            }
        })
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

# =========
# MAIN
# =========
if __name__ == "__main__":
    logger.info("🚀 Iniciando Fran 2.2 - Agente Autónomo Mejorado")
    logger.info("✅ Fixes incluidos:")
    logger.info("   - product_codes en get_last_search_results")
    logger.info("   - Preview antes de confirmar pedidos")
    logger.info("   - Ejecución automática de recomendaciones")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
