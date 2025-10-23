import os
import json
import csv
import io
import sqlite3
import re
import logging
from datetime import datetime
from functools import lru_cache
from collections import defaultdict
from time import time
from contextlib import contextmanager

import requests
from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
import openai
from rapidfuzz import process, fuzz

# -----------------------------------
# Logging
# -----------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# -----------------------------------
# Configuración general
# -----------------------------------
app = Flask(__name__)

openai.api_key = os.environ.get("OPENAI_API_KEY")
CATALOG_URL = "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/refs/heads/main/LISTA_TERCOM_LIMPIA.csv"
EXCHANGE_API_URL = "https://dolarapi.com/v1/dolares/oficial"

user_requests = defaultdict(list)
RATE_LIMIT = 10
RATE_WINDOW = 60

# -----------------------------------
# Base de datos
# -----------------------------------
@contextmanager
def get_db_connection():
    conn = sqlite3.connect('tercom.db')
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"Error en DB: {e}")
        raise
    finally:
        conn.close()

def init_db():
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS conversations
                     (phone TEXT, message TEXT, role TEXT, timestamp TEXT)''')
init_db()

# -----------------------------------
# Utilidades
# -----------------------------------
def rate_limit_check(phone):
    now = time()
    user_requests[phone] = [t for t in user_requests[phone] if now - t < RATE_WINDOW]
    if len(user_requests[phone]) >= RATE_LIMIT:
        return False
    user_requests[phone].append(now)
    return True

def get_exchange_rate():
    try:
        r = requests.get(EXCHANGE_API_URL, timeout=5)
        return float(r.json().get("venta", 1200.0))
    except:
        return 1200.0

@lru_cache(maxsize=1)
def load_catalog_structured():
    try:
        resp = requests.get(CATALOG_URL, timeout=15)
        resp.raise_for_status()
        csv_data = io.StringIO(resp.text)
        reader = csv.reader(csv_data)
        rows = list(reader)
        if not rows:
            return []

        header = [h.strip().lower() for h in rows[0]]
        def col_idx(keys):
            for i, h in enumerate(header):
                h_norm = h.replace("ó","o").replace("á","a").replace("é","e").replace("í","i").replace("ú","u")
                if any(k in h_norm for k in keys):
                    return i
            return None

        idx_code = col_idx(["codigo", "código", "code"])
        idx_name = col_idx(["producto", "descripcion", "name"])
        idx_usd  = col_idx(["usd", "dolar", "price"])
        idx_ars  = col_idx(["ars", "pesos", "precio en pesos"])

        exchange = get_exchange_rate()
        catalog = []

        for line in rows[1:]:
            if len(line) < 2:
                continue

            code = (line[idx_code] if idx_code is not None and idx_code < len(line) else "").strip()
            name = (line[idx_name] if idx_name is not None and idx_name < len(line) else "").strip()

            def to_float(s):
                if not s:
                    return 0.0
                s = str(s).replace("$", "").replace("USD", "").replace("ARS", "")
                s = s.replace(" ", "").replace(".", "").replace(",", ".")
                try:
                    return float(s)
                except:
                    return 0.0

            price_usd = to_float(line[idx_usd]) if idx_usd is not None and idx_usd < len(line) else 0.0
            price_ars = to_float(line[idx_ars]) if idx_ars is not None and idx_ars < len(line) else 0.0
            if price_ars == 0.0 and price_usd > 0.0:
                price_ars = round(price_usd * exchange, 2)

            if name and (price_usd > 0 or price_ars > 0):
                catalog.append({
                    "code": code,
                    "name": name,
                    "price_usd": price_usd,
                    "price_ars": price_ars
                })

        logger.info(f"Catálogo cargado: {len(catalog)} productos")
        return catalog
    except Exception as e:
        logger.error(f"Error cargando catálogo: {e}")
        return []

def fuzzy_candidates(query, limit=12, threshold=55):
    catalog = load_catalog_structured()
    if not catalog:
        return []
    names = [p["name"] for p in catalog]
    results = process.extract(query, names, scorer=fuzz.WRatio, limit=limit)
    return [catalog[idx] for name, score, idx in results if score >= threshold]

# -----------------------------------
# Prompt base
# -----------------------------------
def load_prompt():
    try:
        with open("prompt_fran.txt", "r", encoding="utf-8") as f:
            return f.read()
    except:
        return "Sos Fran, el agente de ventas de Tercom. Ayudá al cliente con el catálogo."

# -----------------------------------
# Webhook
# -----------------------------------
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json(silent=True)
        if data:
            incoming_msg = data.get('Body', '').strip()
            from_number = data.get('From', '')
        else:
            incoming_msg = request.values.get('Body', '').strip()
            from_number = request.values.get('From', '')

        if not incoming_msg:
            resp = MessagingResponse()
            resp.message("Disculpá, no entendí tu mensaje. ¿Podés repetirlo?")
            return str(resp)

        if not rate_limit_check(from_number):
            resp = MessagingResponse()
            resp.message("Por favor esperá un momento antes de enviar más mensajes 😊")
            return str(resp)

        base_prompt = load_prompt()
        candidates = fuzzy_candidates(incoming_msg)
        catalog_text = "\n".join([f"{p['code']} | {p['name']} | ${p['price_ars']:.2f}" for p in candidates]) if candidates else "No se encontraron coincidencias."

        messages = [
            {"role": "system", "content": f"{base_prompt}\n\n{catalog_text}"},
            {"role": "user", "content": incoming_msg}
        ]

        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.7,
            max_tokens=500
        )

        answer = response.choices[0].message.content.strip()
        logger.info(f"Respuesta: {answer[:100]}")

        twilio_resp = MessagingResponse()
        twilio_resp.message(answer)
        return str(twilio_resp)

    except Exception as e:
        logger.error(f"Error en webhook: {e}", exc_info=True)
        resp = MessagingResponse()
        resp.message("Tuve un problema técnico. ¿Podés repetir tu consulta?")
        return str(resp)

# -----------------------------------
# Healthcheck
# -----------------------------------
@app.route('/health', methods=['GET'])
def health():
    try:
        catalog = load_catalog_structured()
        return jsonify({"status": "ok", "productos": len(catalog)})
    except Exception as e:
        logger.error(f"Healthcheck error: {e}")
        return jsonify({"status": "error"}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
