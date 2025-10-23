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
# CONFIGURACIÓN GENERAL
# -----------------------------------
app = Flask(__name__)
openai.api_key = os.environ.get("OPENAI_API_KEY")

CATALOG_URL = "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/refs/heads/main/LISTA_TERCOM_LIMPIA.csv"
EXCHANGE_API_URL = "https://dolarapi.com/v1/dolares/oficial"

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Rate limiting
user_requests = defaultdict(list)
RATE_LIMIT = 10
RATE_WINDOW = 60

# Memorias temporales
last_context = {}
last_product = {}
last_moto = {}

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

def save_message(phone, message, role):
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute('INSERT INTO conversations VALUES (?, ?, ?, ?)',
                      (phone, message, role, datetime.now().isoformat()))
    except Exception as e:
        logger.error(f"Error guardando mensaje: {e}")

def get_conversation_history(phone, limit=10):
    try:
        with get_db_connection() as conn:
            c = conn.cursor()
            c.execute(
                'SELECT message, role FROM conversations WHERE phone = ? ORDER BY timestamp DESC LIMIT ?',
                (phone, limit)
            )
            history = c.fetchall()
        return list(reversed(history))
    except Exception as e:
        logger.error(f"Error obteniendo historial: {e}")
        return []

# -----------------------------------
# UTILIDADES
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

def fuzzy_candidates(query, limit=10, threshold=60):
    catalog = load_catalog_structured()
    if not catalog:
        return []
    names = [p["name"] for p in catalog]
    results = process.extract(query, names, scorer=fuzz.WRatio, limit=limit)
    return [catalog[idx] for name, score, idx in results if score >= threshold]

def get_context_summary(phone):
    """Resume los últimos mensajes del cliente"""
    history = get_conversation_history(phone, limit=8)
    if not history:
        return ""
    msgs = " ".join([m for m, r in history if r == 'user'])
    return msgs[-700:]

def infer_context(incoming_msg, phone):
    """Agrega el contexto del último producto o moto mencionada"""
    if "mismo" in incoming_msg.lower() and phone in last_moto:
        return incoming_msg + " " + last_moto[phone]
    return incoming_msg

# -----------------------------------
# PROMPT BASE
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
            "Recordá mantener siempre el contexto de la conversación (modelo de moto, marca, etc.) "
            "y ofrecer solo productos reales del catálogo.\n"
        )

# -----------------------------------
# WEBHOOK
# -----------------------------------
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        incoming_msg = request.values.get('Body', '').strip()
        from_number = request.values.get('From', '')
        logger.info(f"Mensaje de {from_number}: {incoming_msg}")

        if not rate_limit_check(from_number):
            resp = MessagingResponse()
            resp.message("📨 Estás enviando muchos mensajes. Esperá unos segundos y seguimos 😉")
            return str(resp)

        # Guardar mensaje del usuario
        save_message(from_number, incoming_msg, 'user')

        # Aplicar contexto previo
        incoming_msg = infer_context(incoming_msg, from_number)
        base_prompt = load_prompt()
        context_summary = get_context_summary(from_number)

        # Búsqueda en catálogo
        candidates = fuzzy_candidates(incoming_msg)
        catalog_text = ""
        if candidates:
            catalog_text = "\n".join([
                f"{p['code']} | {p['name']} | ${p['price_ars']:.2f}" for p in candidates[:8]
            ])
            last_product[from_number] = candidates[0]['name']

            # Detectar si menciona moto
            for c in candidates:
                if any(word in c['name'].lower() for word in ['honda', 'yamaha', 'biz', 'crypton', 'cg', 'cbf']):
                    last_moto[from_number] = c['name']

        # Armar prompt completo
        system_prompt = f"""{base_prompt}

Contexto previo del cliente:
{context_summary}

Productos relevantes encontrados:
{catalog_text if catalog_text else "Ninguno exacto, pero buscá alternativas."}
"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": incoming_msg}
        ]

        # Llamada a OpenAI
        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.6,
            max_tokens=600
        )

        answer = response.choices[0].message.content.strip()

        # Guardar respuesta
        save_message(from_number, answer, 'assistant')

        # Enviar a WhatsApp
        twilio_resp = MessagingResponse()
        twilio_resp.message(answer)
        return str(twilio_resp)

    except Exception as e:
        logger.error(f"Error en webhook: {e}", exc_info=True)
        resp = MessagingResponse()
        resp.message("Tuve un problema técnico. ¿Podés repetir tu consulta?")
        return str(resp)

# -----------------------------------
# HEALTHCHECK
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
