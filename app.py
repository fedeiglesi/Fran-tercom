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

import requests
from flask import Flask, request, jsonify, Response
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from rapidfuzz import process, fuzz
import faiss
import numpy as np

# Twilio REST (para mensajes fuera de banda) y validador de firma
try:
    from twilio.rest import Client as TwilioClient
    from twilio.request_validator import RequestValidator
except Exception:
    TwilioClient = None
    RequestValidator = None

# -----------------------------------
# CONFIGURACIÓN GENERAL
# -----------------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")

# URLs sin espacios finales (además aplicamos .strip() por seguridad)
CATALOG_URL = (os.environ.get(
    "CATALOG_URL",
    "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/main/LISTA_TERCOM_LIMPIA.csv"
) or "").strip()

EXCHANGE_API_URL = (os.environ.get(
    "EXCHANGE_API_URL",
    "https://dolarapi.com/v1/dolares/oficial"
) or "").strip()

# Fallback configurable del dólar
DEFAULT_EXCHANGE = float(os.environ.get("DEFAULT_EXCHANGE", 1600.0))

# Twilio (opcional: para delay-msgs y validación de firma)
TWILIO_ACCOUNT_SID   = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN    = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")  # ej: "whatsapp:+14155238886"

twilio_rest_available = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and TwilioClient)
twilio_rest_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if twilio_rest_available else None
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN) if (RequestValidator and TWILIO_AUTH_TOKEN) else None

client = OpenAI(api_key=OPENAI_API_KEY)

# -----------------------------------
# MENSAJE DE ESPERA (delay inteligente)
# -----------------------------------
DELAY_SECONDS = 12
delay_messages = [
    "Dale 👌",
    "Ok, ya te ayudo…",
    "Un seg…",
    "No hay drama, esperá un toque",
    "Ya vuelvo con vos 😉"
]

def send_out_of_band_message(to_number: str, body: str):
    if not twilio_rest_available:
        logger.info("Twilio REST no disponible. No se envía mensaje fuera de banda.")
        return
    try:
        twilio_rest_client.messages.create(
            from_=TWILIO_WHATSAPP_FROM,
            to=to_number,
            body=body
        )
        logger.info(f"Mensaje fuera de banda enviado a {to_number}: {body}")
    except Exception as e:
        logger.error(f"Error enviando mensaje fuera de banda: {e}")

# -----------------------------------
# VALIDACIÓN DE FIRMA TWILIO (seguridad opcional)
# -----------------------------------
@app.before_request
def validate_twilio_signature():
    if request.path == "/webhook" and twilio_validator:
        signature = request.headers.get("X-Twilio-Signature", "")
        url = request.url
        params = request.form.to_dict()
        if not twilio_validator.validate(url, params, signature):
            logger.warning("⚠️ Solicitud rechazada: firma Twilio inválida")
            return Response("Invalid signature", status=403)

# -----------------------------------
# BASE DE DATOS
# -----------------------------------
@contextmanager
def get_db_connection():
    conn = sqlite3.connect('tercom.db')
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"DB error: {e}")
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
        # Estado del usuario (último producto mencionado)
        c.execute('''CREATE TABLE IF NOT EXISTS user_state
                     (phone TEXT PRIMARY KEY,
                      last_code TEXT,
                      last_name TEXT,
                      last_price_ars REAL,
                      updated_at TEXT)''')
init_db()

# -----------------------------------
# RATE LIMIT
# -----------------------------------
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

# -----------------------------------
# UTILIDADES
# -----------------------------------
def strip_accents(s: str) -> str:
    if not s:
        return ""
    return ''.join(ch for ch in unicodedata.normalize('NFKD', s) if not unicodedata.combining(ch)).lower()

# (1) CORRECCIÓN: to_float robusto (formato AR, evita multiplicaciones erróneas)
def to_float(s: str) -> float:
    """Convierte texto a float manejando formatos argentinos y evita multiplicaciones erróneas."""
    if s is None:
        return 0.0
    s = str(s).strip()
    if s in ("", "-", "—", "–"):
        return 0.0
    s = s.replace("USD", "").replace("ARS", "").replace("$", "").replace(" ", "")
    if "," in s and "." in s:
        # Caso "51.499,31" → "51499.31"
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        # Caso "51499,31" → "51499.31"
        s = s.replace(",", ".")
    try:
        val = float(s)
        # Corrige errores de escala tipo 5149931 → 51499.31
        if val > 1_000_000 and len(s) <= 7:
            val = val / 100
        return round(val, 2)
    except Exception:
        return 0.0

def get_exchange_rate():
    try:
        res = requests.get(EXCHANGE_API_URL, timeout=5)
        res.raise_for_status()
        data = res.json()
        return float(data.get("venta", DEFAULT_EXCHANGE))
    except Exception as e:
        logger.warning(f"Fallo tasa cambio: {e}")
        return DEFAULT_EXCHANGE

# -----------------------------------
# CATÁLOGO + FAISS (carga atómica y segura)
# -----------------------------------
_catalog_and_index_cache = {
    "catalog": None,
    "index": None,
    "built_at": None
}
_catalog_lock = Lock()

def _build_faiss_index_from_catalog(catalog):
    """
    Construye índice FAISS de manera segura.
    Devuelve (index, size) o (None, 0) si no hay datos suficientes.
    """
    try:
        if not catalog:
            logger.warning("Catálogo vacío: no se construye FAISS.")
            return None, 0

        texts = [str(p.get("name", "")).strip() for p in catalog if str(p.get("name", "")).strip()]
        if not texts:
            logger.warning("No hay nombres válidos en catálogo para embeddings.")
            return None, 0

        # Embeddings por lotes
        vectors = []
        batch = 512
        for i in range(0, len(texts), batch):
            chunk = texts[i:i+batch]
            resp = client.embeddings.create(input=chunk, model="text-embedding-3-small")
            vectors.extend([d.embedding for d in resp.data])

        # (2) CORRECCIÓN: validaciones robustas de embeddings
        if not vectors or len(vectors) == 0:
            logger.warning("⚠️ No se generaron embeddings. FAISS no se construye.")
            return None, 0

        vecs = np.array(vectors).astype("float32")
        if vecs.ndim != 2 or vecs.shape[0] == 0 or vecs.shape[1] == 0:
            logger.warning(f"⚠️ Dimensión inválida de embeddings: {vecs.shape}")
            return None, 0

        index = faiss.IndexFlatL2(vecs.shape[1])
        index.add(vecs)
        logger.info(f"Índice FAISS creado: {vecs.shape[0]} vectores, dim={vecs.shape[1]}")
        return index, vecs.shape[0]
    except Exception as e:
        logger.error(f"Error construyendo FAISS: {e}", exc_info=True)
        return None, 0

@lru_cache(maxsize=1)
def _load_raw_csv():
    """
    Carga raw del CSV para minimizar llamadas y permitir reuso.
    """
    r = requests.get(CATALOG_URL, timeout=20)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text

def load_catalog():
    """
    Parsea el CSV crudo a objetos normalizados.
    Cacheado por proceso (si el CSV no cambia).
    """
    try:
        text = _load_raw_csv()
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            logger.warning("CSV sin filas.")
            return []

        header_raw = rows[0]
        header = [strip_accents(h) for h in header_raw]

        def find_index(keys):
            for i, h in enumerate(header):
                if any(k in h for k in keys):
                    return i
            return None

        idx_code = find_index(["codigo", "code"])
        idx_name = find_index(["producto", "descripcion", "description", "nombre"])
        idx_usd  = find_index(["usd", "dolar", "precio en dolares"])
        idx_ars  = find_index(["ars", "pesos", "precio en pesos"])

        exchange = get_exchange_rate()
        catalog = []
        for line in rows[1:]:
            if not line:
                continue
            code = line[idx_code].strip() if idx_code is not None and idx_code < len(line) else ""
            name = line[idx_name].strip() if idx_name is not None and idx_name < len(line) else ""
            usd  = to_float(line[idx_usd]) if idx_usd is not None and idx_usd < len(line) else 0.0
            ars  = to_float(line[idx_ars]) if idx_ars is not None and idx_ars < len(line) else 0.0
            if ars == 0.0 and usd > 0.0:
                ars = round(usd * exchange, 2)
            if name and (usd > 0.0 or ars > 0.0):
                catalog.append({
                    "code": code,
                    "name": name,
                    "price_usd": usd,
                    "price_ars": ars
                })
        logger.info(f"Catálogo cargado: {len(catalog)} productos")
        return catalog
    except Exception as e:
        logger.error(f"Error cargando catálogo: {e}", exc_info=True)
        return []

def get_catalog_and_index():
    """
    Devuelve (catalog, index) de forma atómica y cacheada.
    Solo reconstruye si el cache está vacío o si se fuerza manualmente.
    """
    with _catalog_lock:
        if _catalog_and_index_cache["catalog"] is not None and _catalog_and_index_cache["index"] is not None:
            return _catalog_and_index_cache["catalog"], _catalog_and_index_cache["index"]

        catalog = load_catalog()
        index, _ = _build_faiss_index_from_catalog(catalog)
        _catalog_and_index_cache["catalog"] = catalog
        _catalog_and_index_cache["index"] = index
        _catalog_and_index_cache["built_at"] = datetime.utcnow().isoformat()
        return catalog, index

# -----------------------------------
# MEMORIA: GUARDADO Y ESTADO
# -----------------------------------
def save_message(phone, msg, role):
    try:
        with get_db_connection() as conn:
            conn.execute(
                'INSERT INTO conversations VALUES (?, ?, ?, ?)',
                (phone, msg, role, datetime.now().isoformat())
            )
    except Exception as e:
        logger.error(f"Error guardando mensaje: {e}")

def get_history(phone, limit=8):
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                'SELECT message, role FROM conversations WHERE phone = ? ORDER BY timestamp DESC LIMIT ?',
                (phone, limit)
            )
            rows = cur.fetchall()
            return list(reversed(rows))
    except Exception as e:
        logger.error(f"Error leyendo historial: {e}")
        return []

def get_history_today(phone, limit=12):
    """
    Memoria del día (portable en SQLite): filtramos por prefijo de fecha (YYYY-MM-DD)
    """
    try:
        today_prefix = datetime.now().strftime("%Y-%m-%d")
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                'SELECT message, role FROM conversations WHERE phone = ? AND substr(timestamp,1,10)=? ORDER BY timestamp ASC LIMIT ?',
                (phone, today_prefix, limit)
            )
            rows = cur.fetchall()
            return rows
    except Exception as e:
        logger.error(f"Error leyendo historial diario: {e}")
        return []

def save_user_state(phone, prod):
    try:
        with get_db_connection() as conn:
            conn.execute(
                '''INSERT INTO user_state (phone, last_code, last_name, last_price_ars, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(phone) DO UPDATE SET
                       last_code=excluded.last_code,
                       last_name=excluded.last_name,
                       last_price_ars=excluded.last_price_ars,
                       updated_at=excluded.updated_at''',
                (phone, prod.get("code",""), prod.get("name",""), float(prod.get("price_ars",0.0)), datetime.now().isoformat())
            )
    except Exception as e:
        logger.error(f"Error guardando user_state: {e}")

def get_user_state(phone):
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute('SELECT last_code, last_name, last_price_ars, updated_at FROM user_state WHERE phone=?', (phone,))
            row = cur.fetchone()
            if not row:
                return None
            return {"last_code": row[0], "last_name": row[1], "last_price_ars": row[2], "updated_at": row[3]}
    except Exception as e:
        logger.error(f"Error leyendo user_state: {e}")
        return None

# -----------------------------------
# BÚSQUEDA: FUZZY + SEMÁNTICA + HÍBRIDA
# -----------------------------------
def fuzzy_search(query, limit=12):
    catalog, _ = get_catalog_and_index()
    if not catalog:
        return []
    names = [p["name"] for p in catalog]
    matches = process.extract(query, names, scorer=fuzz.WRatio, limit=limit)
    results = [(catalog[i], score) for _, score, i in matches if score >= 60]
    return results  # (producto, score_fuzzy)

def semantic_search(query, top_k=12):
    catalog, index = get_catalog_and_index()
    if not catalog or index is None or not query:
        return []
    try:
        resp = client.embeddings.create(input=[query], model="text-embedding-3-small")
        emb = np.array([resp.data[0].embedding]).astype("float32")
        if emb.ndim != 2 or emb.shape[1] != index.d:
            logger.warning("Embedding query con dimensión incompatible para FAISS.")
            return []
        D, I = index.search(emb, top_k)
        sem_results = []
        for dist, idx in zip(D[0], I[0]):
            if 0 <= idx < len(catalog):
                score = 1.0 / (1.0 + float(dist))
                sem_results.append((catalog[idx], score))
        return sem_results
    except Exception as e:
        logger.error(f"Error en búsqueda semántica: {e}", exc_info=True)
        return []

def hybrid_search(query, limit=8):
    fuzzy = fuzzy_search(query, limit=limit*2)
    sem   = semantic_search(query, top_k=limit*2)
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
    for code, d in combined.items():
        score = 0.6*d["sem"] + 0.4*d["fuzzy"]
        out.append((d["prod"], score))
    out.sort(key=lambda x: x[1], reverse=True)
    return [p for p, _ in out[:limit]]

# -----------------------------------
# PROMPT
# -----------------------------------
def load_prompt():
    try:
        with open("prompt_fran.txt", "r", encoding="utf-8") as f:
            txt = f.read().strip()
            if not txt:
                logger.warning("prompt_fran.txt está vacío. Usando prompt por defecto.")
                raise Exception("empty")
            return txt
    except Exception:
        logger.warning("No se encontró prompt_fran.txt o está vacío. Usando prompt por defecto.")
        return (
            "Sos Fran, el agente de ventas de Tercom. Respondé como una persona real, amable y profesional.\n"
            "No inventes productos ni precios: usá sólo el catálogo provisto en el contexto.\n"
            "Si tenés que operar el carrito, devolvé SOLO uno de estos JSON:\n"
            '{"action":"add_to_cart","products":[{"code":"ABC123","quantity":2}]}\n'
            '{"action":"show_cart"}\n'
            '{"action":"confirm_order"}\n'
            '{"action":"clear_cart"}\n'
            '{"action":"update_qty","items":[{"code":"ABC123","quantity":3}]}\n'
        )

# -----------------------------------
# CARRITO + ACCIONES (con lock)
# -----------------------------------
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
            conn.execute(
                'INSERT INTO carts VALUES (?, ?, ?, ?, ?, ?)',
                (phone, code, qty, name, float(usd or 0.0), float(ars or 0.0))
            )

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
    discount = 0.0
    if total > 10_000_000:
        discount = total * 0.05
    return total - discount, discount

# Parser robusto de JSON acción (sin regex)
def extract_json_action(text):
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    stack = []
    buf = ""
    for ch in text[start:]:
        if ch == "{":
            stack.append("{")
        if stack:
            buf += ch
        if ch == "}":
            if stack:
                stack.pop()
            if not stack:
                try:
                    obj = json.loads(buf)
                    if isinstance(obj, dict) and "action" in obj:
                        return obj
                except Exception:
                    pass
                buf = ""
    return None

def process_actions(bot_response, phone):
    action_json = extract_json_action(bot_response)
    if not action_json:
        return bot_response

    action = action_json.get("action")
    if action == "add_to_cart":
        products = action_json.get("products", [])
        if not products:
            return "No entendí qué producto agregar. ¿Me pasás el código y la cantidad?"
        catalog, _ = get_catalog_and_index()
        added = []
        for p in products:
            code = str(p.get("code", "")).strip()
            qty = int(p.get("quantity", 1))
            prod = next((x for x in catalog if x["code"] == code), None)
            if prod:
                add_to_cart(phone, prod["code"], qty, prod["name"], prod["price_usd"], prod["price_ars"])
                added.append(f"{prod['name']} (x{qty})")
                save_user_state(phone, prod)
        if added:
            return "✅ Agregué al carrito:\n• " + "\n• ".join(added) + "\n\n¿Querés ver el carrito?"
        return "No encontré esos códigos en el catálogo. ¿Podés revisarlos?"

    elif action == "update_qty":
        items = action_json.get("items", [])
        if not items:
            return "Decime el código y la nueva cantidad."
        catalog, _ = get_catalog_and_index()
        changed = []
        for it in items:
            code = str(it.get("code", "")).strip()
            qty = int(it.get("quantity", 0))
            prod = next((x for x in catalog if x["code"] == code), None)
            if prod:
                update_cart_qty(phone, code, qty)
                if qty == 0:
                    changed.append(f"❌ {prod['name']} eliminado")
                else:
                    changed.append(f"✏️ {prod['name']} ahora x{qty}")
                save_user_state(phone, prod)
        if changed:
            return "Actualicé el carrito:\n• " + "\n• ".join(changed) + "\n\n¿Querés ver el total?"
        return "No pude actualizar cantidades. ¿Podés chequear los códigos?"

    elif action == "show_cart":
        items = get_cart(phone)
        if not items:
            return "Tu carrito está vacío. ¿Te muestro opciones?"
        lines = ["*🛒 Tu Carrito:*", ""]
        for code, qty, name, price in items:
            lines.append(f"• {name} (cód {code}) x{qty} — ${price*qty:,.2f}")
        total, disc = cart_totals(phone)
        if disc > 0:
            lines.append(f"\n*Descuento 5%:* -${disc:,.2f}")
        lines.append(f"*TOTAL:* ${total:,.2f}\n")
        lines.append("¿Confirmamos el pedido?")
        return "\n".join(lines)

    elif action == "confirm_order":
        items = get_cart(phone)
        if not items:
            return "No tenés productos en el carrito."
        total, disc = cart_totals(phone)
        clear_cart(phone)
        line = f"*✅ Pedido confirmado.* Total: ${total:,.2f}"
        if disc > 0:
            line += " (incluye 5% de descuento)"
        line += "\nTe escribimos por acá para coordinar pago y envío. ¡Gracias!"
        return line

    elif action == "clear_cart":
        clear_cart(phone)
        return "🗑️ Listo, limpié tu carrito. ¿Qué más necesitás?"

    return bot_response

# -----------------------------------
# WEBHOOK (con delay inteligente y envío fuera de banda si tarda)
# -----------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    start_ts = time_mod.time()
    cancel_event = threading.Event()
    try:
        msg_in = (request.values.get("Body", "") or "").strip()
        phone = request.values.get("From", "")

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

        # Fast-path: si el usuario manda sólo un número, interpretarlo como cantidad del último producto
        if re.fullmatch(r"\d+", msg_in):
            qty = int(msg_in)
            state = get_user_state(phone)
            if state and state.get("last_code"):
                catalog, _ = get_catalog_and_index()
                prod = next((x for x in catalog if x["code"] == state["last_code"]), None)
                if prod:
                    add_to_cart(phone, prod["code"], qty, prod["name"], prod["price_usd"], prod["price_ars"])
                    text = f"✅ Agregué *{prod['name']}* (x{qty}) al carrito.\n¿Querés ver el resumen?"
                    save_message(phone, text, "assistant")
                    cancel_event.set()
                    resp = MessagingResponse()
                    resp.message(text)
                    return str(resp)

        # Búsqueda híbrida y estado
        results = hybrid_search(msg_in, limit=8)
        if results:
            save_user_state(phone, results[0])

        frag = "\n".join(
            [f"{r['code']} | {r['name']} | ARS ${r['price_ars']:,.2f}" for r in results]
        ) if results else "— (sin coincidencias directas)"

        # Estado/Carrito resumido para el prompt
        state = get_user_state(phone)
        state_line = ""
        if state and state.get("last_code"):
            state_line = f"- Último producto mencionado: {state['last_name']} (cód {state['last_code']}) — ${state['last_price_ars']:,.2f}"
        cart_items = get_cart(phone)
        cart_line = f"- Carrito: {sum(q for _, q, __, ___ in cart_items)} ítems." if cart_items else "- Carrito: vacío."

        system_prompt = f"""{load_prompt()}

ESTADO ACTUAL:
{cart_line}
{state_line}

CATÁLOGO (coincidencias relevantes, máx 8):
{frag}

Recordá: si tenés que agregar/ver/confirmar/limpiar/actualizar cantidades del carrito, devolvés SOLO el JSON de acción.
"""

        # historial del día (contexto selectivo)
        history = get_history_today(phone, limit=12)
        messages = [{"role": "system", "content": system_prompt}]
        for m, r in history:
            messages.append({"role": r, "content": m})
        messages.append({"role": "user", "content": msg_in})

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.6,
            max_tokens=600
        )

        raw = response.choices[0].message.content
        logger.info(f"LLM: {raw[:200].replace(chr(10),' ')}...")
        # (3) CORRECCIÓN: logueo de longitud y tiempo de respuesta del LLM
        logger.info(f"LLM respondió ({len(raw)} chars, {round(time_mod.time()-start_ts,2)}s)")

        text = process_actions(raw, phone)
        save_message(phone, text, "assistant")

        cancel_event.set()

        elapsed = time_mod.time() - start_ts
        if elapsed > 11.5 and twilio_rest_available:
            # En caso de tardanza, mandamos el texto por fuera también
            send_out_of_band_message(phone, text)

        resp = MessagingResponse()
        resp.message(text)
        return str(resp)

    except Exception as e:
        logger.error(f"Error en webhook: {e}", exc_info=True)
        try:
            cancel_event.set()
        except Exception:
            pass
        err_msg = "Disculpá, tuve un problema técnico. ¿Podés repetir tu consulta?"
        if twilio_rest_available:
            try:
                send_out_of_band_message(request.values.get("From", ""), err_msg)
            except Exception:
                pass
        resp = MessagingResponse()
        resp.message(err_msg)
        return str(resp)

# -----------------------------------
# HEALTH
# -----------------------------------
@app.route("/health", methods=["GET"])
def health():
    try:
        catalog, index = get_catalog_and_index()
        return jsonify({
            "status": "ok",
            "products": len(catalog) if catalog else 0,
            "faiss": bool(index),
            "built_at": _catalog_and_index_cache["built_at"]
        })
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

# -----------------------------------
# INICIO
# -----------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
