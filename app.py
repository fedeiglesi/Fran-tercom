import os
import json
import csv
import io
import sqlite3
import logging
import re
import unicodedata
from datetime import datetime
from collections import defaultdict
from functools import lru_cache
from contextlib import contextmanager
from time import time
import threading
import requests
import faiss
import numpy as np

from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from openai import OpenAI
from rapidfuzz import process, fuzz

# -----------------------------------
# CONFIGURACIÓN GENERAL
# -----------------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
CATALOG_URL = "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/main/LISTA_TERCOM_LIMPIA.csv"
EXCHANGE_API_URL = "https://dolarapi.com/v1/dolares/oficial"
DEFAULT_EXCHANGE = float(os.environ.get("DEFAULT_EXCHANGE", 1600.0))

TWILIO_SID = os.environ.get("TWILIO_SID")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN")
TWILIO_NUMBER = "whatsapp:+14155238886"

client = OpenAI(api_key=OPENAI_API_KEY)

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

def to_float(s: str) -> float:
    if s is None:
        return 0.0
    s = str(s).replace("USD", "").replace("ARS", "").replace("$", "").replace(" ", "")
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except:
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

@lru_cache(maxsize=1)
def load_catalog():
    try:
        r = requests.get(CATALOG_URL, timeout=15)
        r.raise_for_status()
        reader = csv.reader(io.StringIO(r.text))
        rows = list(reader)
        if not rows:
            return []
        header = [strip_accents(h) for h in rows[0]]

        def find_index(keys):
            for i, h in enumerate(header):
                if any(k in h for k in keys):
                    return i
            return None

        idx_code = find_index(["codigo", "code"])
        idx_name = find_index(["producto", "descripcion", "nombre"])
        idx_usd = find_index(["usd", "dolar"])
        idx_ars = find_index(["ars", "pesos"])

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
            if name:
                catalog.append({"code": code, "name": name, "price_usd": usd, "price_ars": ars})
        return catalog
    except Exception as e:
        logger.error(f"Error cargando catálogo: {e}", exc_info=True)
        return []

# -----------------------------------
# HISTORIAL
# -----------------------------------
def save_message(phone, msg, role):
    try:
        with get_db_connection() as conn:
            conn.execute('INSERT INTO conversations VALUES (?, ?, ?, ?)',
                         (phone, msg, role, datetime.now().isoformat()))
    except Exception as e:
        logger.error(f"Error guardando mensaje: {e}")

def get_history(phone, limit=8):
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute('SELECT message, role FROM conversations WHERE phone=? ORDER BY timestamp DESC LIMIT ?',
                        (phone, limit))
            return list(reversed(cur.fetchall()))
    except Exception as e:
        logger.error(f"Error leyendo historial: {e}")
        return []

# -----------------------------------
# BÚSQUEDAS
# -----------------------------------
def fuzzy_search(query, limit=10):
    catalog = load_catalog()
    if not catalog:
        return []
    names = [p["name"] for p in catalog]
    matches = process.extract(query, names, scorer=fuzz.WRatio, limit=limit)
    results = [catalog[i] for _, score, i in matches if score >= 60]
    return results

@lru_cache(maxsize=1)
def build_faiss_index():
    catalog = load_catalog()
    if not catalog:
        return None, None
    texts = [p["name"] for p in catalog]
    vectors = []
    for i in range(0, len(texts), 512):
        chunk = texts[i:i+512]
        resp = client.embeddings.create(input=chunk, model="text-embedding-3-small")
        vectors.extend([d.embedding for d in resp.data])
    vecs = np.array(vectors).astype("float32")
    index = faiss.IndexFlatL2(vecs.shape[1])
    index.add(vecs)
    return index, catalog

def semantic_search(query, top_k=8):
    if not query:
        return []
    index, catalog = build_faiss_index()
    if not index:
        return []
    resp = client.embeddings.create(input=[query], model="text-embedding-3-small")
    emb = np.array([resp.data[0].embedding]).astype("float32")
    D, I = index.search(emb, top_k)
    return [catalog[i] for i in I[0] if 0 <= i < len(catalog)]

# -----------------------------------
# PROMPT
# -----------------------------------
def load_prompt():
    try:
        with open("prompt_fran.txt", "r", encoding="utf-8") as f:
            return f.read()
    except:
        return "Sos Fran, agente de ventas de Tercom."

# -----------------------------------
# WEBHOOK TWILIO (Optimizado)
# -----------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        msg_in = request.values.get("Body", "").strip()
        phone = request.values.get("From", "")
        logger.info(f"Mensaje entrante de {phone}: {msg_in}")

        resp = MessagingResponse()
        resp.message("Recibí tu mensaje 👌. Estoy procesándolo, dame unos segundos...")
        response_str = str(resp)

        def procesar():
            try:
                save_message(phone, msg_in, "user")

                results = fuzzy_search(msg_in, 12) or semantic_search(msg_in, 12)
                frag = "\n".join([f"{r['code']} | {r['name']} | ${r['price_ars']:,.2f}" for r in results[:10]]) or "—"

                system_prompt = f"""{load_prompt()}

CATÁLOGO (coincidencias relevantes):
{frag}
"""
                history = get_history(phone, 8)
                messages = [{"role": "system", "content": system_prompt}]
                for m, r in history:
                    messages.append({"role": r, "content": m})
                messages.append({"role": "user", "content": msg_in})

                res = client.chat.completions.create(model="gpt-4o", messages=messages, temperature=0.6)
                text = res.choices[0].message.content
                save_message(phone, text, "assistant")

                tw = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
                tw.messages.create(from_=TWILIO_NUMBER, to=phone, body=text)
            except Exception as e:
                logger.error(f"Error en hilo Twilio: {e}", exc_info=True)

        threading.Thread(target=procesar).start()
        return response_str

    except Exception as e:
        logger.error(f"Error en webhook principal: {e}", exc_info=True)
        r = MessagingResponse()
        r.message("Hubo un error interno. Intentá nuevamente.")
        return str(r)

# -----------------------------------
# HEALTH CHECK
# -----------------------------------
@app.route("/health", methods=["GET"])
def health():
    try:
        return jsonify({"status": "ok", "productos": len(load_catalog())})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

# -----------------------------------
# RUN
# -----------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
