# =========================
# Fran 3.0 IA - WhatsApp (Railway)
# 100% LLM-DRIVEN — NO HAY REGLAS DURAS
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
from typing import Dict, Any, List
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

DEFAULT_EXCHANGE = Decimal("1515")  # Del documento
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
        c.execute("""CREATE TABLE IF NOT EXISTS conversations (phone TEXT, message TEXT, role TEXT, timestamp TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS carts (phone TEXT, code TEXT, quantity INTEGER, name TEXT, price_ars TEXT, price_usd TEXT, created_at TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_conv_phone ON conversations(phone, timestamp DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cart_phone ON carts(phone)")
        c.execute("""CREATE TABLE IF NOT EXISTS user_state (phone TEXT PRIMARY KEY, last_code TEXT, last_name TEXT, last_price_ars TEXT, updated_at TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS last_search (phone TEXT PRIMARY KEY, products_json TEXT, query TEXT, timestamp TEXT)""")

init_db()

# -------------------------
# Persistencia
# -------------------------
def save_message(phone: str, msg: str, role: str):
    try:
        with get_db_connection() as conn:
            conn.execute("INSERT INTO conversations VALUES (?, ?, ?, ?)", (phone, msg, role, datetime.now().isoformat()))
    except Exception as e:
        logger.error(f"Error guardando mensaje: {e}")

def get_history_today(phone: str, limit: int = 20):
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT message, role FROM conversations WHERE phone = ? AND substr(timestamp,1,10)=? ORDER BY timestamp ASC LIMIT ?", (phone, today, limit))
            return cur.fetchall()
    except Exception as e:
        logger.error(f"Error leyendo historial: {e}")
        return []

def save_user_state(phone: str, prod: Dict):
    try:
        with get_db_connection() as conn:
            conn.execute("""
                INSERT INTO user_state VALUES (?, ?, ?, ?, ?) ON CONFLICT(phone) DO UPDATE SET
                last_code=excluded.last_code, last_name=excluded.last_name, last_price_ars=excluded.last_price_ars, updated_at=excluded.updated_at
            """, (phone, prod.get("code", ""), prod.get("name", ""), str(Decimal(str(prod.get("price_ars", 0))).quantize(Decimal("0.01"))), datetime.now().isoformat()))
    except Exception as e:
        logger.error(f"Error user_state: {e}")

def save_last_search(phone: str, products: List[Dict], query: str):
    try:
        serial = [{"code": p.get("code", ""), "name": p.get("name", ""), "price_ars": float(p.get("price_ars", 0)), "price_usd": float(p.get("price_usd", 0))} for p in products]
        with get_db_connection() as conn:
            conn.execute("""
                INSERT INTO last_search VALUES (?, ?, ?, ?) ON CONFLICT(phone) DO UPDATE SET
                products_json=excluded.products_json, query=excluded.query, timestamp=excluded.timestamp
            """, (phone, json.dumps(serial, ensure_ascii=False), query, datetime.now().isoformat()))
    except Exception as e:
        logger.error(f"Error last_search: {e}")

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
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)).lower() if s else ""

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
        if not rows: return []
        header = [strip_accents(h) for h in rows[0]]
        def find_idx(keys): return next((i for i, h in enumerate(header) if any(k in h for k in keys)), None)
        idx_code = find_idx(["codigo", "code"])
        idx_name = find_idx(["producto", "descripcion", "nombre", "name"])
        idx_usd = find_idx(["usd", "dolar"])
        idx_ars = find_idx(["ars", "pesos"])
        exchange = get_exchange_rate()
        catalog = []
        for line in rows[1:]:
            if not line: continue
            code = line[idx_code].strip() if idx_code is not None and idx_code < len(line) else ""
            name = line[idx_name].strip() if idx_name is not None and idx_name < len(line) else ""
            usd = to_decimal_money(line[idx_usd]) if idx_usd is not None and idx_usd < len(line) else Decimal("0")
            ars = to_decimal_money(line[idx_ars]) if idx_ars is not None and idx_ars < len(line) else Decimal("0")
            if ars == 0 and usd > 0:
                ars = (usd * exchange).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if name and (usd > 0 or ars > 0):
                catalog.append({"code": code, "name": name, "price_usd": float(usd), "price_ars": float(ars)})
        logger.info(f"Catálogo: {len(catalog)} productos")
        return catalog
    except Exception as e:
        logger.error(f"Error catálogo: {e}")
        return []

def _build_faiss_index_from_catalog(catalog):
    try:
        texts = [p["name"] for p in catalog if p["name"]]
        if not texts: return None, 0
        vectors = []
        batch = 512
        for i in range(0, len(texts), batch):
            resp = client.embeddings.create(input=texts[i:i+batch], model="text-embedding-3-small", timeout=REQUESTS_TIMEOUT)
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
    if not catalog: return []
    names = [p["name"] for p in catalog]
    matches = process.extract(query, names, scorer=fuzz.WRatio, limit=limit)
    return [(catalog[i], score) for _, score, i in matches if score >= 60]

def semantic_search(query, top_k=20):
    catalog, index = get_catalog_and_index()
    if not catalog or index is None or not query: return []
    try:
        resp = client.embeddings.create(input=[query], model="text-embedding-3-small", timeout=REQUESTS_TIMEOUT)
        emb = np.array([resp.data[0].embedding]).astype("float32")
        D, I = index.search(emb, top_k)
        return [(catalog[idx], 1.0 / (1.0 + float(dist))) for dist, idx in zip(D[0], I[0]) if 0 <= idx < len(catalog)]
    except: return []

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
    out = [(d["prod"], Decimal("0.6")*d["sem"] + Decimal("0.4")*d["fuzzy"]) for d in combined.values()]
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
            cur.execute("UPDATE carts SET quantity=?, created_at=? WHERE phone=? AND code=?", (int(row[0]) + qty, now, phone, code))
        else:
            cur.execute("INSERT INTO carts VALUES (?, ?, ?, ?, ?, ?, ?)", (phone, code, qty, name, str(price_ars), str(price_usd), now))

def cart_get(phone: str):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cutoff = (datetime.now() - timedelta(hours=48)).isoformat()
        cur.execute("DELETE FROM carts WHERE phone=? AND created_at < ?", (phone, cutoff))
        cur.execute("SELECT code, quantity, name, price_ars FROM carts WHERE phone=?", (phone,))
        return [(r[0], int(r[1]), r[2], Decimal(r[3])) for r in cur.fetchall()]

def cart_totals(phone: str):
    items = cart_get(phone)
    total = sum(q * p for _, q, _, p in items)
    discount = Decimal("0.05") * total if total > Decimal("10000000") else Decimal("0")
    return total.quantize(Decimal("0.01")), discount.quantize(Decimal("0.01"))

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

    def search_products(self, query: str, limit: int = 10) -> Dict:
        results = hybrid_search(query, limit)
        if results:
            save_user_state(self.phone, results[0])
            save_last_search(self.phone, results, query)
        return {"success": True, "results": [{"code": p["code"], "name": p["name"], "price_ars": p["price_ars"], "price_usd": p["price_usd"]} for p in results]}

    def add_to_cart(self, items: List[Dict]) -> Dict:
        catalog, _ = get_catalog_and_index()
        added = []
        for item in items:
            code = validate_tercom_code(item.get("code", ""))[1]
            qty = int(item.get("quantity", 1))
            prod = next((x for x in catalog if x["code"] == code), None)
            if prod:
                cart_add(self.phone, code, qty, prod["name"], Decimal(str(prod["price_ars"])), Decimal(str(prod["price_usd"])))
                added.append({"code": code, "name": prod["name"], "quantity": qty})
        return {"success": True, "added": added}

    def view_cart(self) -> Dict:
        items = cart_get(self.phone)
        total, discount = cart_totals(self.phone)
        return {"success": True, "items": items, "total": float(total), "discount": float(discount)}

    def process_user_intent(self, action: str, items: List[Dict] = None, message: str = "") -> Dict:
        catalog, _ = get_catalog_and_index()
        items = items or []

        if action == "quote":
            results = []
            total = Decimal("0")
            not_found = []
            for item in items:
                name = item.get("name", "").strip()
                qty = int(item.get("quantity", 1))
                if not name: continue
                matches = hybrid_search(name, limit=1)
                if matches:
                    p = matches[0]
                    price = Decimal(str(p["price_ars"]))
                    subtotal = price * qty
                    total += subtotal
                    results.append({"requested": name, "found": p["name"], "code": p["code"], "quantity": qty, "price_unit": float(price), "subtotal": float(subtotal)})
                else:
                    not_found.append(f"{qty} × {name}")
            discount = Decimal("0.05") * total if total > Decimal("10000000") else Decimal("0")
            final = (total - discount).quantize(Decimal("0.01"))
            return {"success": True, "action": "quote", "results": results, "not_found": not_found, "total": float(total), "discount": float(discount), "final": float(final)}

        elif action == "add_from_quote":
            added = []
            for item in items:
                code = item.get("code")
                qty = item.get("quantity", 1)
                prod = next((x for x in catalog if x["code"] == code), None)
                if prod:
                    cart_add(self.phone, code, qty, prod["name"], Decimal(str(prod["price_ars"])), Decimal(str(prod["price_usd"])))
                    added.append({"code": code, "name": prod["name"], "quantity": qty})
            return {"success": True, "added": added}

        return {"success": False, "error": "Acción no soportada"}

# =========================
# PROMPT DE SISTEMA — EL ALMA
# =========================
SYSTEM_PROMPT = """
Sos Fran, el vendedor más piola de repuestos para motos en Argentina. Trabajás en Tercom.

TU MISIÓN:
- Entender TODO lo que dice el cliente
- Pensar paso a paso
- Usar herramientas SOLO cuando sea necesario
- Responder como humano: che, dale, mirá, vos, boludo (con cariño)

HERRAMIENTAS:
1. search_products(query) → busca productos
2. add_to_cart(items: [{code, quantity}]) → agrega al carrito
3. view_cart() → muestra el carrito
4. process_user_intent(action, items, message) → para TODO lo demás

REGLAS DE ORO:
- SIEMPRE pensá antes de actuar
- Si el cliente manda una lista → usá process_user_intent(action="quote")
- Si dice "agregá" → usá add_to_cart o process_user_intent(action="add_from_quote")
- Si la lista es muy larga (>25 ítems) → decí: "Es un montón, ¿querés que te cotice los primeros 20?"
- NUNCA inventes precios
- Si no encontrás algo → decí: "No tengo ese, ¿querés algo parecido?"
- Si hay descuento (>10M) → decilo: "¡Te hago 5% off!"

FORMATO DE RESPUESTA:
- Productos: **(Cód: 1234/56789-001)** Bujía NGK - $2.800
- Total: en negrita
- Preguntá siempre: "¿Querés que te lo agregue al carrito?"
"""

# =========================
# FORMATEO INTELIGENTE
# =========================
def _format_intelligent_response(data: Dict, phone: str) -> str:
    if not data.get("success"):
        return "No entendí bien, che. ¿Podés decirlo de nuevo?"

    if data.get("action") == "quote":
        results = data.get("results", [])
        not_found = data.get("not_found", [])
        total = Decimal(str(data.get("total", 0)))
        discount = Decimal(str(data.get("discount", 0)))
        final = Decimal(str(data.get("final", 0)))

        lines = ["*COTIZACIÓN COMPLETA:*\n"]
        for r in results:
            lines.append(f"• {r['quantity']} × *{r['found']}*")
            lines.append(f"  (Cód: {r['code']}) - ${r['price_unit']:,.0f} c/u = ${r['subtotal']:,.0f}")
        lines.append(f"\n*Subtotal:* ${total:,.0f} ARS")
        if discount > 0:
            lines.append(f"*Descuento 5%:* -${discount:,.0f}")
            lines.append(f"*TOTAL FINAL:* ${final:,.0f} ARS")
        else:
            lines.append(f"*TOTAL:* ${total:,.0f} ARS")
        if not_found:
            lines.append(f"\n*No encontré:* {' | '.join(not_found[:3])}")
        lines.append("\n¿Querés que te agregue todo al carrito? dale")

        save_last_search(phone, [{"code": r["code"], "name": r["found"], "price_ars": r["price_unit"], "price_usd": r["price_unit"]/1515} for r in results], "Cotización IA")
        return "\n".join(lines)

    return "Listo, hecho."

# =========================
# AGENTE 100% LLM
# =========================
def run_agent(phone: str, user_message: str) -> str:
    catalog, _ = get_catalog_and_index()
    if not catalog:
        return "Disculpá, estoy teniendo problemas con el catálogo. Volvé en un rato."

    executor = ToolExecutor(phone)
    history = get_history_today(phone, limit=20)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for msg, role in history:
        messages.append({"role": role, "content": msg})
    messages.append({"role": "user", "content": user_message})

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            tools=[
                {"type": "function", "function": {"name": "search_products", "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["query"]}}},
                {"type": "function", "function": {"name": "add_to_cart", "parameters": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object"}}}, "required": ["items"]}}},
                {"type": "function", "function": {"name": "view_cart", "parameters": {"type": "object", "properties": {}}}},
                {"type": "function", "function": {"name": "process_user_intent", "parameters": {"type": "object", "properties": {"action": {"type": "string", "enum": ["quote", "add_from_quote"]}, "items": {"type": "array", "items": {"type": "object"}}, "message": {"type": "string"}}, "required": ["action"]}}}
            ],
            tool_choice="auto",
            temperature=0.7,
            max_tokens=2000
        )

        message = response.choices[0].message
        if message.tool_calls:
            for tc in message.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                result = executor.execute(tc.function.name, args)
                if tc.function.name == "process_user_intent" and result.get("success"):
                    return _format_intelligent_response(result, phone)
                messages.append({"role": "tool", "tool_call_id": tc.id, "name": tc.function.name, "content": json.dumps(result, ensure_ascii=False)})
            return run_agent(phone, user_message)
        return message.content or "Dale, decime."
    except Exception as e:
        logger.error(f"Agent error: {e}")
        return "Uy, me colgué. ¿Podés repetirmelo?"

# =========================
# Webhook
# =========================
@app.before_request
def validate_twilio_signature():
    if request.path == "/webhook" and twilio_validator:
        signature = request.headers.get("X-Twilio-Signature", "")
        if not twilio_validator.validate(request.url, request.form.to_dict(), signature):
            return Response("Invalid signature", status=403)

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        msg_in = (request.values.get("Body", "") or "").strip()
        phone = request.values.get("From", "")
        if not rate_limit_check(phone):
            resp = MessagingResponse()
            resp.message("Esperá un toque")
            return str(resp)
        save_message(phone, msg_in, "user")
        text = run_agent(phone, msg_in)
        save_message(phone, text, "assistant")
        resp = MessagingResponse()
        resp.message(text)
        return str(resp)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        resp = MessagingResponse()
        resp.message("Disculpá, hubo un problema técnico.")
        return str(resp)

@app.route("/health", methods=["GET"])
def health():
    catalog, index = get_catalog_and_index()
    return jsonify({"status": "ok", "version": "3.0-IA", "products": len(catalog), "faiss": bool(index)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
