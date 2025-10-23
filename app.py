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
from openai import OpenAI
from rapidfuzz import process, fuzz

# -----------------------------------
# Configuración de Logging
# -----------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# -----------------------------------
# Configuración general
# -----------------------------------
app = Flask(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
CATALOG_URL = "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/refs/heads/main/LISTA_TERCOM_LIMPIA.csv"
EXCHANGE_API_URL = "https://dolarapi.com/v1/dolares/oficial"

client = OpenAI(api_key=OPENAI_API_KEY)

user_requests = defaultdict(list)
RATE_LIMIT = 10
RATE_WINDOW = 60

# -----------------------------------
# Base de datos SQLite
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
        c.execute('''CREATE TABLE IF NOT EXISTS carts
                     (phone TEXT, product_code TEXT, quantity INTEGER, 
                      product_name TEXT, price_usd REAL, price_ars REAL)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_conversations_phone 
                     ON conversations(phone, timestamp DESC)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_carts_phone 
                     ON carts(phone)''')

init_db()

# -----------------------------------
# Utilidades
# -----------------------------------
def rate_limit_check(phone, limit=RATE_LIMIT, window=RATE_WINDOW):
    now = time()
    user_requests[phone] = [t for t in user_requests[phone] if now - t < window]
    if len(user_requests[phone]) >= limit:
        return False
    user_requests[phone].append(now)
    return True

def get_exchange_rate():
    try:
        r = requests.get(EXCHANGE_API_URL, timeout=5)
        data = r.json()
        return float(data.get("venta", 1200.0))
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

@lru_cache(maxsize=1)
def load_catalog_text():
    try:
        resp = requests.get(CATALOG_URL, timeout=15)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        logger.error(f"Error leyendo catálogo texto: {e}")
        return ""

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
        return (
            "Sos Fran, el agente de ventas de Tercom, una empresa de motopartes.\n"
            "Sos amable, profesional y respondés como una persona real, no como un bot.\n"
            "Tu tarea es ayudar al cliente a encontrar los productos que necesita y gestionar su carrito.\n\n"
            "REGLAS DE CONVERSACIÓN:\n"
            "1. No digas que sos una inteligencia artificial o un asistente.\n"
            "2. No inventes precios ni productos. Usá SOLO los datos del catálogo que recibas.\n"
            "3. Mantené un tono cálido, claro y humano.\n"
            "4. Si el producto no está, ofrecé alternativas similares del catálogo.\n"
            "5. Si el cliente pide varios productos, podés agregarlos todos en una sola acción.\n"
            "6. Cuando quieras ejecutar una acción (agregar al carrito, ver carrito, confirmar o limpiar), "
            "devolvé un JSON con este formato:\n"
            '   {"action": "add_to_cart", "products": [{"code": "ABC123", "quantity": 2}]}\n'
            '   {"action": "show_cart"}\n'
            '   {"action": "confirm_order"}\n'
            '   {"action": "clear_cart"}\n'
            "7. Si no se necesita ninguna acción, simplemente conversá normalmente.\n"
        )

# -----------------------------------
# Webhook
# -----------------------------------
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        # --- NUEVA LECTURA ROBUSTA PARA TWILIO ---
        data = request.get_json(silent=True)
        if data:
            incoming_msg = data.get('Body', '').strip()
            from_number = data.get('From', '')
        else:
            incoming_msg = request.values.get('Body', '').strip()
            from_number = request.values.get('From', '')

        if not incoming_msg:
            logger.warning("No se recibió texto en Body o JSON.")
            resp = MessagingResponse()
            resp.message("Disculpá, no entendí tu mensaje. ¿Podés repetirlo?")
            return str(resp)

        # Rate limiting
        if not rate_limit_check(from_number):
            resp = MessagingResponse()
            resp.message("Por favor esperá un momento antes de enviar más mensajes. 😊")
            return str(resp)

        logger.info(f"Mensaje de {from_number}: {incoming_msg}")

        base_prompt = load_prompt()
        candidates = fuzzy_candidates(incoming_msg)

        if candidates:
            frag = "\n".join([
                f"{p['code']} | {p['name']} | ARS {p['price_ars']:.2f}"
                for p in candidates
            ])
            system_prompt = f"""{base_prompt}\n\nCATÁLOGO RELEVANTE:\n{frag}"""
        else:
            raw = load_catalog_text()
            system_prompt = f"""{base_prompt}\n\nCATÁLOGO COMPLETO (parcial):\n{raw[:50000]}"""

        messages = [{"role": "system", "content": system_prompt},
                    {"role": "user", "content": incoming_msg}]

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.7,
            max_tokens=600
        )

        answer = response.choices[0].message.content.strip()
        logger.info(f"Respuesta IA: {answer[:100]}...")

        twilio_resp = MessagingResponse()
        twilio_resp.message(answer)
        return str(twilio_resp)

    except Exception as e:
        logger.error(f"Error general en webhook: {e}", exc_info=True)
        resp = MessagingResponse()
        resp.message("Disculpá, tuve un problema técnico. ¿Podés repetir tu consulta?")
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

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
