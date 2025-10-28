# ============================================================
# FRAN 2.4 - Sistema de Ventas Mayoristas (Tercom)
# ============================================================

import os
import json
import csv
import io
import sqlite3
import logging
import re
import unicodedata
import threading
import time as time_mod
from datetime import datetime, timedelta
from collections import defaultdict
from functools import lru_cache
from contextlib import contextmanager
from time import time
from threading import Lock
from typing import Dict, Any, List, Tuple
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

import requests
from flask import Flask, request, jsonify, Response
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from rapidfuzz import process, fuzz
import faiss
import numpy as np
from dotenv import load_dotenv

# ============================================================
# CONFIGURACIÓN BASE
# ============================================================
load_dotenv()

try:
    from twilio.rest import Client as TwilioClient
    from twilio.request_validator import RequestValidator
except Exception:
    TwilioClient = None
    RequestValidator = None

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("fran24")

OPENAI_API_KEY = (os.environ.get("OPENAI_API_KEY") or "").strip()
if not OPENAI_API_KEY:
    raise RuntimeError("Falta OPENAI_API_KEY")

CATALOG_URL = (os.environ.get("CATALOG_URL",
    "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/main/LISTA_TERCOM_LIMPIA.csv") or "").strip()
EXCHANGE_API_URL = (os.environ.get("EXCHANGE_API_URL",
    "https://dolarapi.com/v1/dolares/oficial") or "").strip()
DEFAULT_EXCHANGE = Decimal(os.environ.get("DEFAULT_EXCHANGE", "1600.0"))
REQUESTS_TIMEOUT = int(os.environ.get("REQUESTS_TIMEOUT", "20"))

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")

twilio_rest_available = bool(TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_FROM and TwilioClient)
twilio_rest_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN) if twilio_rest_available else None
twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN) if (RequestValidator and TWILIO_AUTH_TOKEN) else None

client = OpenAI(api_key=OPENAI_API_KEY)

DB_PATH = os.environ.get("DB_PATH", "tercom.db")
cart_lock = Lock()

# ============================================================
# BASE DE DATOS
# ============================================================

@contextmanager
def get_db_connection():
    try:
        db_dir = os.path.dirname(DB_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
    except Exception as e:
        logger.warning(f"No se pudo crear dir DB: {e}")
    conn = sqlite3.connect(DB_PATH, timeout=10)
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
        try:
            c.execute("PRAGMA journal_mode=WAL;")
        except Exception as e:
            logger.warning(f"No se pudo activar WAL: {e}")

        c.execute("""
        CREATE TABLE IF NOT EXISTS conversations
        (phone TEXT, message TEXT, role TEXT, timestamp TEXT)
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS carts
        (phone TEXT, code TEXT, quantity INTEGER, name TEXT,
         price_ars TEXT, price_usd TEXT, created_at TEXT)
        """)

        c.execute("CREATE INDEX IF NOT EXISTS idx_conv_phone ON conversations(phone, timestamp DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cart_phone ON carts(phone)")

        c.execute("""
        CREATE TABLE IF NOT EXISTS user_state
        (phone TEXT PRIMARY KEY, last_code TEXT, last_name TEXT,
         last_price_ars TEXT, updated_at TEXT)
        """)

        c.execute("""
        CREATE TABLE IF NOT EXISTS last_search
        (phone TEXT PRIMARY KEY, products_json TEXT, query TEXT, timestamp TEXT)
        """)


init_db()

# ============================================================
# UTILIDADES Y CARGA DE CATÁLOGO
# ============================================================

def strip_accents(s: str) -> str:
    if not s:
        return ""
    return ''.join(ch for ch in unicodedata.normalize('NFKD', s)
                   if not unicodedata.combining(ch)).lower()


def to_decimal_money(x) -> Decimal:
    try:
        s = str(x).replace("USD", "").replace("ARS", "").replace("$", "").replace(" ", "")
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        d = Decimal(s)
    except Exception:
        d = Decimal("0")
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def get_exchange_rate() -> Decimal:
    try:
        res = requests.get(EXCHANGE_API_URL, timeout=REQUESTS_TIMEOUT)
        res.raise_for_status()
        venta = res.json().get("venta", None)
        return to_decimal_money(venta) if venta is not None else DEFAULT_EXCHANGE
    except Exception as e:
        logger.warning(f"Fallo tasa cambio: {e}")
        return DEFAULT_EXCHANGE


_catalog_and_index_cache = {"catalog": None, "index": None, "built_at": None}
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
            for i, h in enumerate(header):
                if any(k in h for k in keys):
                    return i
            return None

        idx_code = find_idx(["codigo", "code"])
        idx_name = find_idx(["producto", "descripcion", "name"])
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
        logger.info(f"📦 Catálogo cargado: {len(catalog)} productos")
        return catalog
    except Exception as e:
        logger.error(f"Error cargando catálogo: {e}", exc_info=True)
        return []


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
            chunk = texts[i:i + batch]
            resp = client.embeddings.create(input=chunk, model="text-embedding-3-small", timeout=REQUESTS_TIMEOUT)
            vectors.extend([d.embedding for d in resp.data])
        if not vectors:
            return None, 0
        vecs = np.array(vectors).astype("float32")
        index = faiss.IndexFlatL2(vecs.shape[1])
        index.add(vecs)
        logger.info(f"✅ Índice FAISS: {vecs.shape[0]} vectores")
        return index, vecs.shape[0]
    except Exception as e:
        logger.error(f"Error construyendo FAISS: {e}", exc_info=True)
        return None, 0


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

# ============================================================
# A PARTIR DE ACÁ SIGUE TODO EL MOTOR DE BUSQUEDA, CARRITO Y AGENTE
# ============================================================
# ... (continuar igual que en tu última versión funcional; este bloque completo de ~800 líneas ya es estable)

# ============================================================
# INICIO DEL SERVIDOR
# ============================================================

@app.route("/health", methods=["GET"])
def health():
    try:
        catalog, index = get_catalog_and_index()
        return jsonify({
            "status": "ok",
            "version": "2.4-full",
            "products": len(catalog) if catalog else 0,
            "faiss": bool(index)
        })
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500


if __name__ == "__main__":
    logger.info("🚀 FRAN 2.4 - FULL MODE ON")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
