¡Ah! El problema es que incluí emojis (❌ y ✅) en un comentario dentro del código Python, y Python no permite esos caracteres Unicode en el código fuente sin estar dentro de strings.

Voy a arreglarlo removiendo esa sección de comentarios:

```python
# =========================
# Fran 3.0 IA - WhatsApp (Railway)
# 100% LLM-DRIVEN — SUPER INTELIGENTE
# =========================

import os
import json
import csv
import io
import sqlite3
import logging
import re
import unicodedata
import threading
import time
from datetime import datetime, timedelta
from collections import defaultdict
from functools import lru_cache
from contextlib import contextmanager
from threading import Lock
from typing import Dict, Any, List, Optional
from decimal import Decimal, ROUND_HALF_UP

import requests
from flask import Flask, request, jsonify, Response
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from rapidfuzz import process, fuzz
import faiss
import numpy as np
from dotenv import load_dotenv

# -------------------------
# Cargar variables de entorno
# -------------------------
load_dotenv()

# -------------------------
# Twilio (opcional)
# -------------------------
try:
    from twilio.rest import Client as TwilioClient
    from twilio.request_validator import RequestValidator
except Exception:
    TwilioClient = None
    RequestValidator = None

# -------------------------
# Flask + Logging
# -------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("fran_ia")

# -------------------------
# Variables de entorno
# -------------------------
OPENAI_API_KEY = (os.environ.get("OPENAI_API_KEY") or "").strip()
if not OPENAI_API_KEY:
    raise RuntimeError("Falta OPENAI_API_KEY")

CATALOG_URL = (os.environ.get(
    "CATALOG_URL",
    "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/main/LISTA_TERCOM_LIMPIA.csv"
) or "").strip()

EXCHANGE_API_URL = (os.environ.get(
    "EXCHANGE_API_URL",
    "https://dolarapi.com/v1/dolares/oficial"
) or "").strip()

DEFAULT_EXCHANGE = Decimal("1515")
REQUESTS_TIMEOUT = int(os.environ.get("REQUESTS_TIMEOUT", "30"))

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")
twilio_rest_available = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and TwilioClient)
twilio_rest_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if twilio_rest_available else None
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN) if (RequestValidator and TWILIO_AUTH_TOKEN) else None

# -------------------------
# OpenAI Client
# -------------------------
client = OpenAI(api_key=OPENAI_API_KEY)

# -------------------------
# Base de datos
# -------------------------
DB_PATH = os.environ.get("DB_PATH", "fran_ia.db")
cart_lock = Lock()

@contextmanager
def get_db_connection():
    try:
        db_dir = os.path.dirname(DB_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
    except Exception as e:
        logger.warning(f"No se pudo crear dir DB: {e}")
    conn = sqlite3.connect(DB_PATH, timeout=15)
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
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("""CREATE TABLE IF NOT EXISTS conversations (
            phone TEXT, 
            message TEXT, 
            role TEXT, 
            timestamp TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS carts (
            phone TEXT, 
            code TEXT, 
            quantity INTEGER, 
            name TEXT, 
            price_ars TEXT, 
            price_usd TEXT, 
            created_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS user_state (
            phone TEXT PRIMARY KEY, 
            last_code TEXT, 
            last_name TEXT, 
            last_price_ars TEXT, 
            updated_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS last_search (
            phone TEXT PRIMARY KEY, 
            products_json TEXT, 
            query TEXT, 
            timestamp TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS feedback (
            phone TEXT,
            user_message TEXT,
            bot_response TEXT,
            was_helpful INTEGER,
            timestamp TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS conversation_context (
            phone TEXT PRIMARY KEY,
            context_summary TEXT,
            updated_at TEXT
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_conv_phone ON conversations(phone, timestamp DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cart_phone ON carts(phone)")

init_db()

# -------------------------
# Persistencia
# -------------------------
def save_message(phone: str, msg: str, role: str):
    try:
        with get_db_connection() as conn:
            conn.execute("INSERT INTO conversations VALUES (?, ?, ?, ?)", 
                        (phone, msg, role, datetime.now().isoformat()))
    except Exception as e:
        logger.error(f"Error guardando mensaje: {e}")

def get_history_today(phone: str, limit: int = 20):
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT message, role FROM conversations WHERE phone = ? AND substr(timestamp,1,10)=? ORDER BY timestamp ASC LIMIT ?", 
                (phone, today, limit)
            )
            return cur.fetchall()
    except Exception as e:
        logger.error(f"Error leyendo historial: {e}")
        return []

def save_user_state(phone: str, prod: Dict):
    try:
        with get_db_connection() as conn:
            conn.execute("""
                INSERT INTO user_state VALUES (?, ?, ?, ?, ?) 
                ON CONFLICT(phone) DO UPDATE SET
                last_code=excluded.last_code, 
                last_name=excluded.last_name, 
                last_price_ars=excluded.last_price_ars, 
                updated_at=excluded.updated_at
            """, (
                phone, 
                prod.get("code", ""), 
                prod.get("name", ""), 
                str(Decimal(str(prod.get("price_ars", 0))).quantize(Decimal("0.01"))), 
                datetime.now().isoformat()
            ))
    except Exception as e:
        logger.error(f"Error user_state: {e}")

def save_last_search(phone: str, products: List[Dict], query: str):
    try:
        serial = [
            {
                "code": p.get("code", ""), 
                "name": p.get("name", ""), 
                "price_ars": float(p.get("price_ars", 0)), 
                "price_usd": float(p.get("price_usd", 0))
            } 
            for p in products
        ]
        with get_db_connection() as conn:
            conn.execute("""
                INSERT INTO last_search VALUES (?, ?, ?, ?) 
                ON CONFLICT(phone) DO UPDATE SET
                products_json=excluded.products_json, 
                query=excluded.query, 
                timestamp=excluded.timestamp
            """, (phone, json.dumps(serial, ensure_ascii=False), query, datetime.now().isoformat()))
    except Exception as e:
        logger.error(f"Error last_search: {e}")

def save_conversation_context(phone: str, context: str):
    """Guarda resumen del contexto de conversación"""
    try:
        with get_db_connection() as conn:
            conn.execute("""
                INSERT INTO conversation_context VALUES (?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                context_summary=excluded.context_summary,
                updated_at=excluded.updated_at
            """, (phone, context, datetime.now().isoformat()))
    except Exception as e:
        logger.error(f"Error guardando contexto: {e}")

def get_conversation_context(phone: str) -> str:
    """Obtiene el resumen de contexto guardado"""
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT context_summary FROM conversation_context WHERE phone = ?", 
                (phone,)
            )
            row = cur.fetchone()
            return row[0] if row else ""
    except Exception as e:
        logger.error(f"Error obteniendo contexto: {e}")
        return ""

def save_interaction_feedback(phone: str, user_msg: str, bot_response: str, was_helpful: Optional[bool] = None):
    """Guarda feedback para mejorar futuras interacciones"""
    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO feedback VALUES (?, ?, ?, ?, ?)",
                (phone, user_msg, bot_response, 1 if was_helpful else 0, datetime.now().isoformat())
            )
    except Exception as e:
        logger.error(f"Error guardando feedback: {e}")

# -------------------------
# Rate limit
# -------------------------
user_requests = defaultdict(list)
RATE_LIMIT = 20
RATE_WINDOW = 60

def rate_limit_check(phone: str) -> bool:
    now = time.time()
    user_requests[phone] = [t for t in user_requests[phone] if now - t < RATE_WINDOW]
    if len(user_requests[phone]) >= RATE_LIMIT:
        return False
    user_requests[phone].append(now)
    return True

# -------------------------
# Utils
# -------------------------
def strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) 
        if not unicodedata.combining(c)
    ).lower() if s else ""

def to_decimal_money(x) -> Decimal:
    try:
        s = str(x).replace("USD", "").replace("ARS", "").replace("$", "").replace(" ", "")
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        return Decimal(s).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except:
        return Decimal("0")

def format_price(price: Decimal) -> str:
    """Formatea precio con separador de miles"""
    return f"${price:,.0f}".replace(",", ".")

# =========================
# Catálogo + FAISS
# =========================
def get_exchange_rate() -> Decimal:
    try:
        res = requests.get(EXCHANGE_API_URL, timeout=REQUESTS_TIMEOUT)
        res.raise_for_status()
        return to_decimal_money(res.json().get("venta", DEFAULT_EXCHANGE))
    except:
        return DEFAULT_EXCHANGE

_catalog_and_index_cache = {"catalog": None, "index": None}
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
            return next((i for i, h in enumerate(header) if any(k in h for k in keys)), None)
        
        idx_code = find_idx(["codigo", "code"])
        idx_name = find_idx(["producto", "descripcion", "nombre", "name"])
        idx_usd = find_idx(["usd", "dolar"])
        idx_ars = find_idx(["ars", "pesos"])
        
        exchange = get_exchange_rate()
        catalog = []
        
        for line in rows[1:]:
            if not line: 
                continue
            
            code = line[idx_code].strip() if idx_code is not None and idx_code < len(line) else ""
            name = line[idx_name].strip() if idx_name is not None and idx_name < len(line) else ""
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
        
        logger.info(f"Catálogo: {len(catalog)} productos")
        return catalog
    except Exception as e:
        logger.error(f"Error catálogo: {e}")
        return []

def _build_faiss_index_from_catalog(catalog):
    try:
        texts = [p["name"] for p in catalog if p["name"]]
        if not texts: 
            return None, 0
        
        vectors = []
        batch = 512
        for i in range(0, len(texts), batch):
            resp = client.embeddings.create(
                input=texts[i:i+batch], 
                model="text-embedding-3-small", 
                timeout=REQUESTS_TIMEOUT
            )
            vectors.extend([d.embedding for d in resp.data])
        
        vecs = np.array(vectors).astype("float32")
        index = faiss.IndexFlatL2(vecs.shape[1])
        index.add(vecs)
        
        logger.info(f"FAISS: {vecs.shape[0]} vectores")
        return index, vecs.shape[0]
    except Exception as e:
        logger.error(f"Error FAISS: {e}")
        return None, 0

def get_catalog_and_index():
    with _catalog_lock:
        if _catalog_and_index_cache["catalog"] is not None:
            return _catalog_and_index_cache["catalog"], _catalog_and_index_cache["index"]
        catalog = load_catalog()
        index, _ = _build_faiss_index_from_catalog(catalog)
        _catalog_and_index_cache["catalog"] = catalog
        _catalog_and_index_cache["index"] = index
        return catalog, index

logger.info("Precargando catálogo e índice FAISS...")
_ = get_catalog_and_index()
logger.info("Catálogo precargado.")

# -------------------------
# Búsqueda híbrida
# -------------------------
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
        resp = client.embeddings.create(
            input=[query], 
            model="text-embedding-3-small", 
            timeout=REQUESTS_TIMEOUT
        )
        emb = np.array([resp.data[0].embedding]).astype("float32")
        D, I = index.search(emb, top_k)
        return [
            (catalog[idx], 1.0 / (1.0 + float(dist))) 
            for dist, idx in zip(D[0], I[0]) 
            if 0 <= idx < len(catalog)
        ]
    except: 
        return []

def hybrid_search(query, limit=15):
    query = query.lower()
    fuzzy = fuzzy_search(query, limit*2)
    sem = semantic_search(query, limit*2)
    combined = {}
    
    for prod, s in fuzzy:
        code = prod.get("code", f"id_{id(prod)}")
        combined.setdefault(code, {"prod": prod, "fuzzy": Decimal(0), "sem": Decimal(0)})
        combined[code]["fuzzy"] = max(combined[code]["fuzzy"], Decimal(s)/100)
    
    for prod, s in sem:
        code = prod.get("code", f"id_{id(prod)}")
        combined.setdefault(code, {"prod": prod, "fuzzy": Decimal(0), "sem": Decimal(0)})
        combined[code]["sem"] = max(combined[code]["sem"], Decimal(str(s)))
    
    out = [
        (d["prod"], Decimal("0.6")*d["sem"] + Decimal("0.4")*d["fuzzy"]) 
        for d in combined.values()
    ]
    out.sort(key=lambda x: x[1], reverse=True)
    return [p for p, _ in out[:limit]]

# -------------------------
# Carrito
# -------------------------
def validate_tercom_code(code):
    pattern = r'^\d{4}/\d{5}-\d{3}$'
    if re.match(pattern, str(code).strip()):
        return True, str(code).strip()
    code_clean = re.sub(r'[^0-9]', '', str(code))
    if len(code_clean) == 12:
        return True, f"{code_clean[:4]}/{code_clean[4:9]}-{code_clean[9:12]}"
    return False, code

def cart_add(phone: str, code: str, qty: int, name: str, price_ars: Decimal, price_usd: Decimal):
    qty = max(1, min(int(qty), 100))
    price_ars = price_ars.quantize(Decimal("0.01"))
    price_usd = price_usd.quantize(Decimal("0.01"))
    
    with cart_lock, get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT quantity FROM carts WHERE phone=? AND code=?", (phone, code))
        row = cur.fetchone()
        now = datetime.now().isoformat()
        
        if row:
            cur.execute(
                "UPDATE carts SET quantity=?, created_at=? WHERE phone=? AND code=?", 
                (int(row[0]) + qty, now, phone, code)
            )
        else:
            cur.execute(
                "INSERT INTO carts VALUES (?, ?, ?, ?, ?, ?, ?)", 
                (phone, code, qty, name, str(price_ars), str(price_usd), now)
            )

def cart_get(phone: str):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cutoff = (datetime.now() - timedelta(hours=48)).isoformat()
        cur.execute("DELETE FROM carts WHERE phone=? AND created_at < ?", (phone, cutoff))
        cur.execute(
            "SELECT code, quantity, name, price_ars FROM carts WHERE phone=?", 
            (phone,)
        )
        return [(r[0], int(r[1]), r[2], Decimal(r[3])) for r in cur.fetchall()]

def cart_clear(phone: str):
    """Vacía el carrito del usuario"""
    with cart_lock, get_db_connection() as conn:
        conn.execute("DELETE FROM carts WHERE phone=?", (phone,))

def cart_totals(phone: str):
    items = cart_get(phone)
    total = sum(q * p for _, q, _, p in items)
    discount = Decimal("0.05") * total if total > Decimal("10000000") else Decimal("0")
    return total.quantize(Decimal("0.01")), discount.quantize(Decimal("0.01"))

# =========================
# CONTEXTO INTELIGENTE
# =========================
def generate_conversation_summary(phone: str) -> str:
    """Genera un resumen inteligente de la conversación usando LLM"""
    history = get_history_today(phone, limit=30)
    if not history:
        return "Nueva conversación sin historial previo."
    
    messages = [{"role": r[1], "content": r[0]} for r in history[-15:]]
    
    context_prompt = """Resume esta conversación de WhatsApp enfocándote ÚNICAMENTE en:
1. Productos específicos mencionados o buscados (nombres, códigos)
2. Intención actual del cliente (cotizar, comprar, consultar stock)
3. Estado del pedido/carrito (si mencionó agregar algo)
4. Preferencias del cliente (marcas, tipos de producto)

Formato: 2-3 líneas máximo, directo y relevante.
Ejemplo: "Cliente busca repuestos para Yamaha MT-07. Consultó por bujías NGK y kit de transmisión. Tiene 3 productos en carrito."
"""
    
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": context_prompt},
                {"role": "user", "content": json.dumps(messages, ensure_ascii=False)}
            ],
            max_tokens=200,
            temperature=0.3
        )
        summary = response.choices[0].message.content.strip()
        save_conversation_context(phone, summary)
        return summary
    except Exception as e:
        logger.error(f"Error generando resumen: {e}")
        return "Conversación activa sobre repuestos para motos."

# =========================
# TOOL EXECUTOR 100% IA
# =========================
class ToolExecutor:
    def __init__(self, phone: str):
        self.phone = phone

    def execute(self, tool_name: str, arguments: Dict) -> Dict:
        method = getattr(self, tool_name, None)
        if not method:
            return {"error": f"Tool '{tool_name}' no existe"}
        try:
            return method(**arguments)
        except Exception as e:
            logger.error(f"Tool error: {e}")
            return {"error": str(e)}

    def analyze_intent(self, message: str) -> Dict:
        """Analiza la intención del usuario antes de ejecutar acciones"""
        prompt = f"""Analiza este mensaje de WhatsApp y clasifica la intención del cliente.

Mensaje: "{message}"

Clasifica en UNA categoría principal:
1. "search" - Buscar un producto específico
2. "quote_list" - Cotizar una lista completa de productos (múltiples ítems)
3. "add_cart" - Agregar productos al carrito (frases como "agregalo", "lo quiero")
4. "view_cart" - Ver carrito actual
5. "clear_cart" - Vaciar carrito
6. "chitchat" - Charla general, saludos
7. "help" - Necesita ayuda o no entiende algo

Responde SOLO con este JSON (sin markdown):
{{"intent": "...", "confidence": 0.95, "entities": ["producto1", "producto2"]}}

Entities debe contener nombres de productos mencionados si los hay.
"""
        
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=150,
                temperature=0.2
            )
            return json.loads(response.choices[0].message.content)
        except Exception as e:
            logger.error(f"Error analizando intención: {e}")
            return {"intent": "unknown", "confidence": 0.5, "entities": []}

    def _extract_products_from_text(self, text: str) -> List[Dict]:
        """Extrae productos de texto libre usando IA"""
        prompt = f"""Extrae TODOS los productos de esta lista. Cada línea puede tener:
- Cantidad (número al inicio)
- Nombre del producto (descripción completa)
- Código Tercom opcional (formato: 1234/56789-001)

IMPORTANTE:
- Si NO hay cantidad explícita, asume 1
- Mantén nombres de productos completos
- Si hay código, inclúyelo

Texto:
{text}

Devuelve SOLO JSON (sin markdown):
{{"products": [{{"name": "Filtro de aceite Honda", "quantity": 2, "code": "1234/56789-001"}}]}}
"""
        
        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                max_tokens=1500,
                temperature=0.2
            )
            data = json.loads(response.choices[0].message.content)
            return data.get("products", [])
        except Exception as e:
            logger.error(f"Error extrayendo productos: {e}")
            return []

    def search_products(self, query: str, limit: int = 10) -> Dict:
        """Busca productos en el catálogo"""
        results = hybrid_search(query, limit)
        if results:
            save_user_state(self.phone, results[0])
            save_last_search(self.phone, results, query)
        
        return {
            "success": True, 
            "results": [
                {
                    "code": p["code"], 
                    "name": p["name"], 
                    "price_ars": p["price_ars"], 
                    "price_usd": p["price_usd"]
                } 
                for p in results
            ]
        }

    def add_to_cart(self, items: List[Dict]) -> Dict:
        """Agrega productos al carrito"""
        catalog, _ = get_catalog_and_index()
        added = []
        not_found = []
        
        for item in items:
            code = validate_tercom_code(item.get("code", ""))[1]
            qty = int(item.get("quantity", 1))
            prod = next((x for x in catalog if x["code"] == code), None)
            
            if prod:
                cart_add(
                    self.phone, 
                    code, 
                    qty, 
                    prod["name"], 
                    Decimal(str(prod["price_ars"])), 
                    Decimal(str(prod["price_usd"]))
                )
                added.append({"code": code, "name": prod["name"], "quantity": qty})
            else:
                not_found.append(code)
        
        return {"success": True, "added": added, "not_found": not_found}

    def view_cart(self) -> Dict:
        """Muestra el carrito actual"""
        items = cart_get(self.phone)
        total, discount = cart_totals(self.phone)
        
        return {
            "success": True, 
            "items": [
                {
                    "code": code, 
                    "quantity": qty, 
                    "name": name, 
                    "price_unit": float(price),
                    "subtotal": float(qty * price)
                }
                for code, qty, name, price in items
            ],
            "total": float(total), 
            "discount": float(discount),
            "final": float(total - discount)
        }

    def clear_cart(self) -> Dict:
        """Vacía el carrito"""
        cart_clear(self.phone)
        return {"success": True, "message": "Carrito vaciado"}

    def process_user_intent(self, action: str, items: List[Dict] = None, message: str = "") -> Dict:
        """Procesa intenciones complejas del usuario"""
        catalog, _ = get_catalog_and_index()
        items = items or []

        if action == "quote":
            # Usar IA para extraer productos de texto libre
            if not items and message:
                items = self._extract_products_from_text(message)
            
            # Limitar a 30 productos para no saturar
            if len(items) > 30:
                return {
                    "success": True,
                    "action": "too_many",
                    "message": f"Uf, son {len(items)} productos. ¿Querés que te cotice los primeros 30? Después seguimos con el resto, dale.",
                    "items_count": len(items),
                    "first_batch": items[:30]
                }
            
            results = []
            total = Decimal("0")
            not_found = []
            
            for item in items:
                name = item.get("name", "").strip()
                qty = int(item.get("quantity", 1))
                if not name: 
                    continue
                
                # Búsqueda inteligente - tomar el mejor match
                matches = hybrid_search(name, limit=1)
                if matches:
                    p = matches[0]
                    price = Decimal(str(p["price_ars"]))
                    subtotal = price * qty
                    total += subtotal
                    results.append({
                        "requested": name,
                        "found": p["name"],
                        "code": p["code"],
                        "quantity": qty,
                        "price_unit": float(price),
                        "subtotal": float(subtotal)
                    })
                else:
                    not_found.append(f"{qty} × {name}")
            
            discount = Decimal("0.05") * total if total > Decimal("10000000") else Decimal("0")
            final = (total - discount).quantize(Decimal("0.01"))
            
            # Guardar búsqueda para referencia futura
            if results:
                save_last_search(
                    self.phone, 
                    [{"code": r["code"], "name": r["found"], "price_ars": r["price_unit"], "price_usd": r["price_unit"]/1515} for r in results],
                    "Cotización masiva IA"
                )
            
            return {
                "success": True,
                "action": "quote",
                "results": results,
                "not_found": not_found,
                "total": float(total),
                "discount": float(discount),
                "final": float(final)
            }

        elif action == "add_from_quote":
            # Agregar productos de última cotización
            added = []
            for item in items:
                code = item.get("code")
                qty = item.get("quantity", 1)
                prod = next((x for x in catalog if x["code"] == code), None)
                if prod:
                    cart_add(
                        self.phone, 
                        code, 
                        qty, 
                        prod["name"], 
                        Decimal(str(prod["price_ars"])), 
                        Decimal(str(prod["price_usd"]))
                    )
                    added.append({"code": code, "name": prod["name"], "quantity": qty})
            return {"success": True, "added": added}

        return {"success": False, "error": "Acción no soportada"}

# =========================
# PROMPT DE SISTEMA
# =========================
def get_system_prompt(phone: str) -> str:
    """Genera el prompt de sistema con contexto actualizado"""
    context = get_conversation_context(phone)
    
    return f"""Sos Fran, el vendedor más piola de repuestos para motos en Argentina. Trabajás en Tercom y sos un crack.

PERSONALIDAD:
- Argentino auténtico: che, boludo (con cariño), dale, mirá, vos
- Proactivo: anticipás necesidades del cliente
- Claro: precios SIEMPRE con separador de miles (ejemplo: $2.800, nunca $2800)
- Paciente: si no entendés algo, preguntás sin drama
- Natural: hablás como en WhatsApp, no como robot

CONTEXTO DE ESTA CONVERSACIÓN:
{context if context else "Nueva conversación - cliente sin historial previo"}

HERRAMIENTAS DISPONIBLES:
1. search_products(query, limit) - buscar productos en catálogo
2. add_to_cart(items) - agregar productos al carrito
3. view_cart() - mostrar carrito actual con totales
4. clear_cart() - vaciar el carrito
5. process_user_intent(action, items, message) - para cotizaciones masivas y listas

FLUJO DE TRABAJO:
1. PENSAR - Qué quiere el cliente realmente?
2. DECIDIR - Necesito herramientas o puedo responder directo?
3. ACTUAR - Usar herramientas SOLO cuando sea necesario
4. CONFIRMAR - Siempre preguntar antes de agregar al carrito

CASOS ESPECIALES:
- Lista larga (>25 ítems) - "Uf che, son un montón. Arrancamos con los primeros 25?"
- Producto no existe - Buscar similar: "Ese no lo tengo, pero mirá este que es parecido"
- Descuento automático - Si total > $10.000.000 aplicar 5% off
- Códigos Tercom - Formato estándar: 1234/56789-001
- Cliente indeciso - Sugerir alternativas o productos relacionados

FORMATO DE SALIDA:
Productos individuales:
   (Cód: 1234/56789-001) Bujía NGK Iridium - $2.800

Listas/Cotizaciones:
   2 × Filtro de aceite Honda
     (Cód: 1234/56789-001) - $1.500 c/u = $3.000
   
   Subtotal: $125.000 ARS
   Descuento 5%: -$6.250
   TOTAL FINAL: $118.750 ARS

Precios: SIEMPRE usar separador de miles con punto: $2.800 (nunca $2800)

Siempre cerrar con pregunta:
   - "Te lo agrego al carrito?"
   - "Querés algo más?"
   - "Buscás algún otro repuesto?"

REGLAS DE ORO:
- NUNCA inventar precios
- NUNCA confirmar productos sin buscarlos primero
- NUNCA agregar al carrito sin permiso explícito del cliente
- NUNCA usar lenguaje formal o corporativo
- SIEMPRE usar herramientas para buscar productos
- SIEMPRE formatear precios correctamente
- SIEMPRE ser proactivo y anticipar necesidades
- SIEMPRE mantener el tono argentino relajado

Sos Fran, no un robot. Vendé con onda, ayudá al cliente como si fuera tu amigo.
"""

# =========================
# FORMATEO INTELIGENTE DE RESPUESTAS
# =========================
def _format_intelligent_response(data: Dict, phone: str) -> str:
    """Formatea respuestas de herramientas de manera natural"""
    if not data.get("success"):
        return "No entendí bien eso, che. Me lo decís de nuevo?"

    # Cotización completa
    if data.get("action") == "quote":
        results = data.get("results", [])
        not_found = data.get("not_found", [])
        total = Decimal(str(data.get("total", 0)))
        discount = Decimal(str(data.get("discount", 0)))
        final = Decimal(str(data.get("final", 0)))

        if not results:
            return "No encontré ninguno de esos productos, che. Tenés los nombres completos o códigos?"

        lines = ["COTIZACIÓN COMPLETA:\n"]
        for r in results:
            lines.append(f"{r['quantity']} × {r['found']}")
            lines.append(f"  (Cód: {r['code']}) - {format_price(Decimal(str(r['price_unit'])))} c/u = {format_price(Decimal(str(r['subtotal'])))}")
        
        lines.append(f"\nSubtotal: {format_price(total)} ARS")
        
        if discount > 0:
            lines.append(f"Descuento 5%: -{format_price(discount)}")
            lines.append(f"TOTAL FINAL: {format_price(final)} ARS")
        else:
            lines.append(f"TOTAL: {format_price(total)} ARS")
        
        if not_found:
            lines.append(f"\nNo encontré:")
            for nf in not_found[:5]:
                lines.append(f"  {nf}")
            if len(not_found) > 5:
                lines.append(f"  ...y {len(not_found) - 5} más")
        
        lines.append("\nQuerés que te agregue todo al carrito? Dale nomás")

        return "\n".join(lines)

    # Lista muy larga
    if data.get("action") == "too_many":
        return data.get("message", "Son muchos productos. Arrancamos con algunos?")

    return "Listo, che. Algo más?"

# =========================
# AGENTE 100% LLM CON CONTEXTO
# =========================
def run_agent(phone: str, user_message: str) -> str:
    """Ejecuta el agente conversacional con IA"""
    catalog, _ = get_catalog_and_index()
    if not catalog:
        return "Disculpá, estoy teniendo problemas con el catálogo. Probá en un ratito, dale."

    # Actualizar contexto cada 5 mensajes
    history = get_history_today(phone, limit=50)
    if len(history) % 5 == 0 and len(history) > 0:
        generate_conversation_summary(phone)

    executor = ToolExecutor(phone)
    
    # Construir mensajes con contexto
    messages = [{"role": "system", "content": get_system_prompt(phone)}]
    
    # Agregar historial reciente
    for msg, role in history[-15:]:
        messages.append({"role": role, "content": msg})
    
    messages.append({"role": "user", "content": user_message})

    # Definir herramientas disponibles
    tools = [
        {
            "type": "function",
            "function": {
                "name": "search_products",
                "description": "Busca productos en el catálogo por nombre o descripción",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Término de búsqueda"},
                        "limit": {"type": "integer", "description": "Cantidad de resultados", "default": 10}
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "add_to_cart",
                "description": "Agrega productos al carrito del cliente",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "code": {"type": "string"},
                                    "quantity": {"type": "integer"}
                                }
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
                "description": "Muestra el contenido actual del carrito",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "clear_cart",
                "description": "Vacía completamente el carrito del cliente",
                "parameters": {"type": "object", "properties": {}}
            }
        },
        {
            "type": "function",
            "function": {
                "name": "process_user_intent",
                "description": "Procesa cotizaciones masivas, listas de productos y acciones complejas",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["quote", "add_from_quote"],
                            "description": "quote: cotizar lista | add_from_quote: agregar de cotización previa"
                        },
                        "items": {
                            "type": "array",
                            "items": {"type": "object"}
                        },
                        "message": {"type": "string", "description": "Mensaje del usuario si tiene lista de productos"}
                    },
                    "required": ["action"]
                }
            }
        }
    ]

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=0.7,
            max_tokens=2000
        )

        message = response.choices[0].message
        
        # Si hay llamadas a herramientas
        if message.tool_calls:
            messages.append(message)
            
            for tc in message.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                logger.info(f"Ejecutando tool: {tc.function.name} con args: {args}")
                
                result = executor.execute(tc.function.name, args)
                
                # Si es process_user_intent con quote, formatear respuesta especial
                if tc.function.name == "process_user_intent" and result.get("success"):
                    formatted = _format_intelligent_response(result, phone)
                    if formatted != "Listo, che. Algo más?":
                        return formatted
                
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.function.name,
                    "content": json.dumps(result, ensure_ascii=False)
                })
            
            # Segunda llamada para generar respuesta final
            final_response = client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                temperature=0.7,
                max_tokens=1500
            )
            
            return final_response.choices[0].message.content or "Dale, decime qué necesitás."
        
        # Respuesta directa sin herramientas
        return message.content or "Dale, en qué te puedo ayudar?"
        
    except Exception as e:
        logger.error(f"Agent error: {e}")
        return "Uy, me colgué un toque. Me repetís lo que necesitás?"

# =========================
# WEBHOOK DE TWILIO
# =========================
@app.before_request
def validate_twilio_signature():
    if request.path == "/webhook" and twilio_validator:
        signature = request.headers.get("X-Twilio-Signature", "")
        if not twilio_validator.validate(request.url, request.form.to_dict(), signature):
            logger.warning("Invalid Twilio signature")
            return Response("Invalid signature", status=403)

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        msg_in = (request.values.get("Body", "") or "").strip()
        phone = request.values.get("From", "")
        
        if not msg_in or not phone:
            resp = MessagingResponse()
            resp.message("No recibí nada, che. Mandame algo")
            return str(resp)
        
        # Rate limiting
        if not rate_limit_check(phone):
            resp = MessagingResponse()
            resp.message("Ey, esperá un toque que me estás saturando")
            return str(resp)
        
        logger.info(f"Mensaje de {phone}: {msg_in}")
        
        # Guardar mensaje del usuario
        save_message(phone, msg_in, "user")
        
        # Ejecutar agente
        text = run_agent(phone, msg_in)
        
        # Guardar respuesta
        save_message(phone, text, "assistant")
        save_interaction_feedback(phone, msg_in, text, was_helpful=True)
        
        logger.info(f"Respuesta a {phone}: {text[:100]}...")
        
        resp = MessagingResponse()
        resp.message(text)
        return str(resp)
        
    except Exception as e:
        logger.error(f"Webhook error: {e}", exc_info=True)
        resp = MessagingResponse()
        resp.message("Uy, tuve un problema técnico. Probá de nuevo en un toque, dale.")
        return str(resp)

@app.route("/health", methods=["GET"])
def health():
    catalog, index = get_catalog_and_index()
    return jsonify({
        "status": "ok",
        "version": "3.0-IA-SUPER",
        "products": len(catalog),
        "faiss_index": bool(index),
        "timestamp": datetime.now().isoformat()
    })

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "name": "Fran 3.0 IA",
        "description": "Bot 100% inteligente para ventas de repuestos",
        "endpoints": {
            "/webhook": "POST - Webhook de Twilio",
            "/health": "GET - Estado del sistema"
        }
    })

# =========================
# INICIALIZACIÓN
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Iniciando Fran 3.0 IA en puerto {port}")
    logger.info(f"Catálogo cargado: {len(get_catalog_and_index()[0])} productos")
    app.run(host="0.0.0.0", port=port, debug=False)
```

¡Listo! Ahora el código debería funcionar sin problemas. El error era por los emojis que había incluido en un comentario de comparación. Los removí completamente y el código está listo para deploy. 🚀​​​​​​​​​​​​​​​​
