import os
import json
import csv
import io
import sqlite3
import logging
from datetime import datetime
from collections import defaultdict
from functools import lru_cache
from contextlib import contextmanager
from time import time

import requests
from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from rapidfuzz import process, fuzz
import faiss
import numpy as np

# -----------------------------------
# CONFIGURACIÓN GENERAL
# -----------------------------------
app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
CATALOG_URL = os.environ.get(
    "CATALOG_URL",
    "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/main/LISTA_TERCOM_LIMPIA.csv"
)
EXCHANGE_API_URL = os.environ.get(
    "EXCHANGE_API_URL", "https://dolarapi.com/v1/dolares/oficial"
)

client = OpenAI(api_key=OPENAI_API_KEY)

# -----------------------------------
# BASE DE DATOS LOCAL
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
def get_exchange_rate():
    try:
        res = requests.get(EXCHANGE_API_URL, timeout=5)
        data = res.json()
        return float(data.get("venta", 1200.0))
    except Exception as e:
        logger.warning(f"Fallo tasa cambio: {e}")
        return 1200.0


@lru_cache(maxsize=1)
def load_catalog():
    """Lee el catálogo CSV desde GitHub."""
    try:
        r = requests.get(CATALOG_URL, timeout=15)
        r.raise_for_status()
        csv_data = io.StringIO(r.text)
        reader = csv.reader(csv_data)
        rows = list(reader)
        header = [h.lower().strip() for h in rows[0]]

        def find_index(keys):
            for i, h in enumerate(header):
                if any(k in h for k in keys):
                    return i
            return None

        idx_code = find_index(["codigo", "code"])
        idx_name = find_index(["producto", "descripcion", "description"])
        idx_usd = find_index(["usd", "dolar"])
        idx_ars = find_index(["ars", "peso"])

        exchange = get_exchange_rate()
        catalog = []
        for line in rows[1:]:
            code = (line[idx_code] if idx_code is not None else "").strip()
            name = (line[idx_name] if idx_name is not None else "").strip()
            try:
                usd = float(str(line[idx_usd]).replace(",", ".").replace("$", "")) if idx_usd is not None else 0.0
                ars = float(str(line[idx_ars]).replace(",", ".").replace("$", "")) if idx_ars is not None else usd * exchange
            except:
                usd, ars = 0.0, 0.0
            if name:
                catalog.append({"code": code, "name": name, "price_usd": usd, "price_ars": ars})
        return catalog
    except Exception as e:
        logger.error(f"Error cargando catálogo: {e}")
        return []


def fuzzy_search(query, limit=10):
    catalog = load_catalog()
    if not catalog:
        return []
    names = [p["name"] for p in catalog]
    matches = process.extract(query, names, scorer=fuzz.WRatio, limit=limit)
    results = [catalog[i] for _, score, i in matches if score > 60]
    return results


def save_message(phone, msg, role):
    try:
        with get_db_connection() as conn:
            conn.execute(
                'INSERT INTO conversations VALUES (?, ?, ?, ?)',
                (phone, msg, role, datetime.now().isoformat())
            )
    except Exception as e:
        logger.error(f"Error guardando mensaje: {e}")


def get_history(phone, limit=6):
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


# -----------------------------------
# EMBEDDINGS (para memoria vectorial FAISS)
# -----------------------------------
@lru_cache(maxsize=1)
def build_faiss_index():
    catalog = load_catalog()
    if not catalog:
        return None, None
    texts = [p["name"] for p in catalog]
    emb = client.embeddings.create(input=texts, model="text-embedding-3-small").data
    vecs = np.array([e.embedding for e in emb]).astype("float32")
    index = faiss.IndexFlatL2(vecs.shape[1])
    index.add(vecs)
    return index, catalog


def semantic_search(query, top_k=8):
    index, catalog = build_faiss_index()
    if not index:
        return []
    emb = client.embeddings.create(input=[query], model="text-embedding-3-small").data[0].embedding
    emb_np = np.array([emb]).astype("float32")
    D, I = index.search(emb_np, top_k)
    return [catalog[i] for i in I[0] if i < len(catalog)]


# -----------------------------------
# PROMPT BASE
# -----------------------------------
def load_prompt():
    try:
        with open("prompt_fran.txt", "r", encoding="utf-8") as f:
            return f.read()
    except:
        return "Sos Fran, el agente de ventas de Tercom. Respondé como una persona real, amable y profesional."


# -----------------------------------
# ACCIONES DEL CARRITO
# -----------------------------------
def add_to_cart(phone, code, qty, name, usd, ars):
    with get_db_connection() as conn:
        conn.execute(
            'INSERT INTO carts VALUES (?, ?, ?, ?, ?, ?)',
            (phone, code, qty, name, usd, ars)
        )


def get_cart(phone):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute('SELECT code, name, quantity, price_ars FROM carts WHERE phone=?', (phone,))
        return cur.fetchall()


def clear_cart(phone):
    with get_db_connection() as conn:
        conn.execute('DELETE FROM carts WHERE phone=?', (phone,))


# -----------------------------------
# WEBHOOK DE TWILIO
# -----------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        msg_in = request.values.get("Body", "").strip()
        phone = request.values.get("From", "")

        if not rate_limit_check(phone):
            resp = MessagingResponse()
            resp.message("Esperá un momento antes de enviar más mensajes 😊")
            return str(resp)

        save_message(phone, msg_in, "user")

        # --- buscar coincidencias
        results = fuzzy_search(msg_in)
        if not results:
            results = semantic_search(msg_in)

        catalog_text = "\n".join(
            [f"{r['code']} | {r['name']} | ${r['price_ars']:.2f}" for r in results]
        )

        system_prompt = f"""{load_prompt()}

Productos similares encontrados:
{catalog_text}
"""

        history = get_history(phone)
        messages = [{"role": "system", "content": system_prompt}]
        for m, r in history:
            messages.append({"role": r, "content": m})
        messages.append({"role": "user", "content": msg_in})

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.7,
            max_tokens=600
        )

        text = response.choices[0].message.content
        save_message(phone, text, "assistant")

        resp = MessagingResponse()
        resp.message(text)
        return str(resp)

    except Exception as e:
        logger.error(f"Error en webhook: {e}", exc_info=True)
        resp = MessagingResponse()
        resp.message("Disculpá, tuve un problema técnico. ¿Podés repetir tu consulta?")
        return str(resp)


# -----------------------------------
# HEALTH CHECK
# -----------------------------------
@app.route("/health", methods=["GET"])
def health():
    try:
        catalog = load_catalog()
        return jsonify({"status": "ok", "products": len(catalog)})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500


# -----------------------------------
# INICIO APP
# -----------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
