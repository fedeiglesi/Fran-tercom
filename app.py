# coding: utf-8
# Fran 3.1 IA - Asistente de ventas mayorista Tercom
# Versión profesional completa y estable

import os
import json
import csv
import io
import sqlite3
import logging
import re
import unicodedata
import time as time_mod
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

# =====================================
# CONFIGURACIÓN INICIAL
# =====================================

load_dotenv()

try:
    from twilio.rest import Client as TwilioClient
    from twilio.request_validator import RequestValidator
except Exception:
    TwilioClient = None
    RequestValidator = None

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("fran_ia")

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

client = OpenAI(api_key=OPENAI_API_KEY)

DB_PATH = os.environ.get("DB_PATH", "fran_ia.db")
cart_lock = Lock()

# =====================================
# BASE DE DATOS
# =====================================

@contextmanager
def get_db_connection():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
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
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            phone TEXT,
            message TEXT,
            role TEXT,
            timestamp TEXT
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS carts (
            phone TEXT,
            code TEXT,
            quantity INTEGER,
            name TEXT,
            price_ars TEXT,
            price_usd TEXT,
            created_at TEXT
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            phone TEXT PRIMARY KEY,
            last_code TEXT,
            last_name TEXT,
            last_price_ars TEXT,
            updated_at TEXT
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS last_search (
            phone TEXT PRIMARY KEY,
            products_json TEXT,
            query TEXT,
            timestamp TEXT
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS conversation_context (
            phone TEXT PRIMARY KEY,
            context_summary TEXT,
            updated_at TEXT
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_conv_phone ON conversations(phone, timestamp DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cart_phone ON carts(phone)")


init_db()

# =====================================
# FUNCIONES AUXILIARES
# =====================================

def save_message(phone: str, msg: str, role: str):
    try:
        with get_db_connection() as conn:
            conn.execute(
                "INSERT INTO conversations VALUES (?, ?, ?, ?)",
                (phone, msg, role, datetime.now().isoformat())
            )
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
    except Exception:
        return Decimal("0")

def format_price(price: Decimal) -> str:
    return f"${price:,.0f}".replace(",", ".")

def get_exchange_rate() -> Decimal:
    try:
        res = requests.get(EXCHANGE_API_URL, timeout=REQUESTS_TIMEOUT)
        res.raise_for_status()
        return to_decimal_money(res.json().get("venta", DEFAULT_EXCHANGE))
    except Exception:
        return DEFAULT_EXCHANGE

# =====================================
# CATALOGO + FAISS
# =====================================

_catalog_cache = {"catalog": None, "index": None}
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

        logger.info(f"Catálogo: {len(catalog)} productos cargados.")
        return catalog
    except Exception as e:
        logger.error(f"Error catálogo: {e}")
        return []

def _build_faiss_index(catalog):
    try:
        texts = [p["name"] for p in catalog if p["name"]]
        if not texts:
            return None, 0

        vectors = []
        batch = 512
        for i in range(0, len(texts), batch):
            resp = client.embeddings.create(
                input=texts[i:i+batch],
                model="text-embedding-3-small"
            )
            vectors.extend([d.embedding for d in resp.data])

        vecs = np.array(vectors).astype("float32")
        index = faiss.IndexFlatL2(vecs.shape[1])
        index.add(vecs)

        logger.info(f"FAISS: {vecs.shape[0]} vectores creados.")
        return index, vecs.shape[0]
    except Exception as e:
        logger.error(f"Error FAISS: {e}")
        return None, 0

def get_catalog_and_index():
    with _catalog_lock:
        if _catalog_cache["catalog"] is not None:
            return _catalog_cache["catalog"], _catalog_cache["index"]
        catalog = load_catalog()
        index, _ = _build_faiss_index(catalog)
        _catalog_cache["catalog"] = catalog
        _catalog_cache["index"] = index
        return catalog, index

logger.info("Precargando catálogo e índice FAISS...")
get_catalog_and_index()
logger.info("Catálogo precargado correctamente.")

# =====================================
# PROMPT AJUSTADO PROFESIONAL
# =====================================

def get_system_prompt(phone: str) -> str:
    return """Sos Fran, asesor comercial de Tercom, empresa mayorista especializada en motopartes.
Tu estilo es profesional, claro y amable. 
Ofrecés atención personalizada a clientes del rubro, sin usar expresiones informales.
Respondés de forma natural, con empatía y precisión técnica cuando corresponde.

Reglas:
- Mostrá los precios en pesos argentinos con punto como separador de miles (ejemplo: $2.800 ARS).
- Si no entendés algo, pedí amablemente una aclaración.
- Nunca confirmes disponibilidad sin buscar el producto en el catálogo.
- Siempre cerrá con una pregunta amable, por ejemplo:
  "¿Querés que te pase opciones similares?" o "¿Deseás que te lo agregue al presupuesto?".
- Evitá palabras informales o modismos locales.
""".strip()

# =====================================
# WEBHOOK
# =====================================

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        msg_in = (request.values.get("Body", "") or "").strip()
        phone = request.values.get("From", "")

        if not msg_in or not phone:
            resp = MessagingResponse()
            resp.message("No recibí ningún mensaje. Podrías repetirlo, por favor?")
            return str(resp)

        logger.info(f"Mensaje recibido de {phone}: {msg_in}")
        save_message(phone, msg_in, "user")

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": get_system_prompt(phone)},
                {"role": "user", "content": msg_in}
            ],
            temperature=0.6,
            max_tokens=800
        )

        text = response.choices[0].message.content.strip()
        save_message(phone, text, "assistant")

        logger.info(f"Respuesta generada: {text[:100]}...")
        resp = MessagingResponse()
        resp.message(text)
        return str(resp)

    except Exception as e:
        logger.error(f"Error en webhook: {e}", exc_info=True)
        resp = MessagingResponse()
        resp.message("Tuve un inconveniente técnico, pero ya lo estoy revisando. Podés intentar nuevamente en unos segundos.")
        return str(resp)

# =====================================
# SALUD DEL SERVIDOR
# =====================================

@app.route("/health", methods=["GET"])
def health():
    catalog, index = get_catalog_and_index()
    return jsonify({
        "status": "ok",
        "version": "3.1",
        "productos": len(catalog),
        "faiss_index": bool(index),
        "timestamp": datetime.now().isoformat()
    })

@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "name": "Fran 3.1 IA",
        "description": "Asistente comercial inteligente de Tercom para ventas mayoristas.",
        "endpoints": {
            "/webhook": "POST - Webhook de Twilio",
            "/health": "GET - Estado del sistema"
        }
    })

# =====================================
# MAIN
# =====================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Iniciando Fran 3.1 IA en puerto {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
