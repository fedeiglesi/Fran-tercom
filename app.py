# =========================
# Fran 2.4 - WhatsApp (Railway)
# Parte 1/4 - Imports, Config, DB y Utilidades
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

# -------------------------
# Cargar variables de entorno
# -------------------------
load_dotenv()

# -------------------------
# Twilio (REST opcional)
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
logger = logging.getLogger("fran24")

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

DEFAULT_EXCHANGE = Decimal(os.environ.get("DEFAULT_EXCHANGE", "1600.0"))
REQUESTS_TIMEOUT = int(os.environ.get("REQUESTS_TIMEOUT", "20"))

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
DB_PATH = os.environ.get("DB_PATH", "tercom.db")
cart_lock = Lock()

@contextmanager
def get_db_connection():
    """Context manager para conexión SQLite con manejo de errores y commit/rollback."""
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

        c.execute("CREATE INDEX IF NOT EXISTS idx_conv_phone ON conversations(phone, timestamp DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cart_phone ON carts(phone)")

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

# Inicializar DB al importar
init_db()

# -------------------------
# Persistencia de mensajes y estados
# -------------------------
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
        today_prefix = datetime.now().strftime("%Y-%m-%d")
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT message, role FROM conversations "
                "WHERE phone = ? AND substr(timestamp,1,10)=? "
                "ORDER BY timestamp ASC LIMIT ?",
                (phone, today_prefix, limit)
            )
            return cur.fetchall()
    except Exception as e:
        logger.error(f"Error leyendo historial: {e}")
        return []

def save_user_state(phone: str, prod: Dict[str, Any]):
    try:
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO user_state (phone, last_code, last_name, last_price_ars, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                  last_code=excluded.last_code,
                  last_name=excluded.last_name,
                  last_price_ars=excluded.last_price_ars,
                  updated_at=excluded.updated_at
                """,
                (
                    phone,
                    prod.get("code", ""),
                    prod.get("name", ""),
                    str(Decimal(str(prod.get("price_ars", 0))).quantize(Decimal("0.01"))),
                    datetime.now().isoformat()
                )
            )
    except Exception as e:
        logger.error(f"Error guardando user_state: {e}")

def save_last_search(phone: str, products: List[Dict[str, Any]], query: str):
    try:
        serializable = []
        for p in products:
            serializable.append({
                "code": p.get("code", ""),
                "name": p.get("name", ""),
                "price_ars": float(p.get("price_ars", 0)),
                "price_usd": float(p.get("price_usd", 0))
            })
        with get_db_connection() as conn:
            conn.execute(
                """
                INSERT INTO last_search (phone, products_json, query, timestamp)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(phone) DO UPDATE SET
                  products_json=excluded.products_json,
                  query=excluded.query,
                  timestamp=excluded.timestamp
                """,
                (phone, json.dumps(serializable, ensure_ascii=False), query, datetime.now().isoformat())
            )
    except Exception as e:
        logger.error(f"Error guardando last_search: {e}")

def get_last_search(phone: str):
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT products_json, query, timestamp FROM last_search WHERE phone=?", (phone,))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "products": json.loads(row[0]),
                "query": row[1],
                "timestamp": row[2]
            }
    except Exception as e:
        logger.error(f"Error leyendo last_search: {e}")
        return None

# -------------------------
# Rate limit simple (en proceso)
# -------------------------
user_requests = defaultdict(list)
RATE_LIMIT = 15
RATE_WINDOW = 60  # segundos

def rate_limit_check(phone: str) -> bool:
    now = time()
    user_requests[phone] = [t for t in user_requests[phone] if now - t < RATE_WINDOW]
    if len(user_requests[phone]) >= RATE_LIMIT:
        return False
    user_requests[phone].append(now)
    return True

# -------------------------
# Utils generales
# -------------------------
def strip_accents(s: str) -> str:
    if not s:
        return ""
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch)).lower()

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

# =========================
# Parte 2/4 - Catálogo, FAISS, fuzzy + híbrido
# =========================

def get_exchange_rate() -> Decimal:
    """Obtiene la tasa de cambio desde API externa o valor por defecto."""
    try:
        res = requests.get(EXCHANGE_API_URL, timeout=REQUESTS_TIMEOUT)
        res.raise_for_status()
        venta = res.json().get("venta", None)
        return to_decimal_money(venta) if venta is not None else DEFAULT_EXCHANGE
    except Exception as e:
        logger.warning(f"Fallo tasa cambio: {e}")
        return DEFAULT_EXCHANGE


# Caché de catálogo e índice
_catalog_and_index_cache = {"catalog": None, "index": None, "built_at": None}
_catalog_lock = Lock()


@lru_cache(maxsize=1)
def _load_raw_csv():
    r = requests.get(CATALOG_URL, timeout=REQUESTS_TIMEOUT)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def load_catalog():
    """Carga y normaliza el catálogo CSV remoto."""
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
        idx_name = find_idx(["producto", "descripcion", "description", "nombre", "name"])
        idx_usd = find_idx(["usd", "dolar", "precio en dolares"])
        idx_ars = find_idx(["ars", "pesos", "precio en pesos"])

        exchange = get_exchange_rate()
        catalog = []
        for line in rows[1:]:
            if not line:
                continue
            code = (line[idx_code].strip() if idx_code is not None and idx_code < len(line) else "")
            name = (line[idx_name].strip() if idx_name is not None and idx_name < len(line) else "")
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
    """Crea un índice FAISS de embeddings."""
    try:
        if not catalog:
            return None, 0
        texts = [str(p.get("name", "")).strip() for p in catalog if str(p.get("name", "")).strip()]
        if not texts:
            return None, 0
        vectors = []
        batch = 512
        for i in range(0, len(texts), batch):
            chunk = texts[i:i+batch]
            resp = client.embeddings.create(input=chunk, model="text-embedding-3-small", timeout=REQUESTS_TIMEOUT)
            vectors.extend([d.embedding for d in resp.data])
        if not vectors:
            return None, 0
        vecs = np.array(vectors).astype("float32")
        if vecs.ndim != 2 or vecs.shape[0] == 0 or vecs.shape[1] == 0:
            return None, 0
        index = faiss.IndexFlatL2(vecs.shape[1])
        index.add(vecs)
        logger.info(f"✅ Índice FAISS: {vecs.shape[0]} vectores")
        return index, vecs.shape[0]
    except Exception as e:
        logger.error(f"Error construyendo FAISS: {e}", exc_info=True)
        return None, 0


def get_catalog_and_index():
    """Devuelve catálogo e índice FAISS, cacheados."""
    with _catalog_lock:
        if _catalog_and_index_cache["catalog"] is not None:
            return _catalog_and_index_cache["catalog"], _catalog_and_index_cache["index"]
        catalog = load_catalog()
        index, _ = _build_faiss_index_from_catalog(catalog)
        _catalog_and_index_cache["catalog"] = catalog
        _catalog_and_index_cache["index"] = index
        _catalog_and_index_cache["built_at"] = datetime.utcnow().isoformat()
        return catalog, index


# -------------------------
# Motores de búsqueda
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
        resp = client.embeddings.create(input=[query], model="text-embedding-3-small", timeout=REQUESTS_TIMEOUT)
        emb = np.array([resp.data[0].embedding]).astype("float32")
        D, I = index.search(emb, top_k)
        results = []
        for dist, idx in zip(D[0], I[0]):
            if 0 <= idx < len(catalog):
                score = 1.0 / (1.0 + float(dist))
                results.append((catalog[idx], score))
        return results
    except Exception as e:
        logger.error(f"Error en búsqueda semántica: {e}")
        return []


SEARCH_ALIASES = {
    "yama": "yamaha", "gilera": "gilera", "zan": "zanella", "hond": "honda",
    "acrilico": "acrilico tablero", "aceite 2t": "aceite pride 2t",
    "aceite 4t": "aceite moto 4t", "aceite moto": "aceite",
    "vc": "VC", "af": "AF", "nsu": "NSU", "gulf": "GULF",
    "yamalube": "YAMALUBE", "suzuki": "suzuki", "zusuki": "suzuki"
}

def normalize_search_query(query):
    q = query.lower()
    for alias, replacement in SEARCH_ALIASES.items():
        if alias in q:
            q = q.replace(alias, replacement)
    return q


def hybrid_search(query, limit=15):
    """Combina fuzzy y semántico con pesos."""
    query = normalize_search_query(query)
    fuzzy = fuzzy_search(query, limit=limit*2)
    sem = semantic_search(query, top_k=limit*2)
    combined = {}
    for prod, s in fuzzy:
        code = prod.get("code", f"id_{id(prod)}")
        combined.setdefault(code, {"prod": prod, "fuzzy": Decimal(0), "sem": Decimal(0)})
        combined[code]["fuzzy"] = max(combined[code]["fuzzy"], Decimal(s)/Decimal(100))
    for prod, s in sem:
        code = prod.get("code", f"id_{id(prod)}")
        combined.setdefault(code, {"prod": prod, "fuzzy": Decimal(0), "sem": Decimal(0)})
        combined[code]["sem"] = max(combined[code]["sem"], Decimal(str(s)))
    out = []
    for _, d in combined.items():
        score = Decimal("0.6")*d["sem"] + Decimal("0.4")*d["fuzzy"]
        out.append((d["prod"], score))
    out.sort(key=lambda x: x[1], reverse=True)
    return [p for p, _ in out[:limit]]

# =========================
# Parte 3/4 - Carrito, Tools, Cotización masiva, Formato
# =========================

def validate_tercom_code(code):
    pattern = r'^\d{4}/\d{5}-\d{3}$'
    if re.match(pattern, str(code).strip()):
        return True, str(code).strip()
    code_clean = re.sub(r'[^0-9]', '', str(code))
    if len(code_clean) == 12:
        normalized = f"{code_clean[:4]}/{code_clean[4:9]}-{code_clean[9:12]}"
        return True, normalized
    return False, code


# -------------------------
# Carrito
# -------------------------
def cart_add(phone: str, code: str, qty: int, name: str, price_ars: Decimal, price_usd: Decimal):
    qty = max(1, min(int(qty or 1), 100))
    price_ars = price_ars.quantize(Decimal("0.01"))
    price_usd = price_usd.quantize(Decimal("0.01"))
    with cart_lock, get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT quantity FROM carts WHERE phone=? AND code=?", (phone, code))
        row = cur.fetchone()
        now = datetime.now().isoformat()
        if row:
            new_qty = int(row[0]) + qty
            cur.execute(
                "UPDATE carts SET quantity=?, created_at=? WHERE phone=? AND code=?",
                (new_qty, now, phone, code)
            )
        else:
            cur.execute(
                """INSERT INTO carts (phone, code, quantity, name, price_ars, price_usd, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (phone, code, qty, name, str(price_ars), str(price_usd), now)
            )


def cart_get(phone: str, max_age_hours: int = 24):
    with get_db_connection() as conn:
        cur = conn.cursor()
        cutoff = (datetime.now() - timedelta(hours=max_age_hours)).isoformat()
        cur.execute("DELETE FROM carts WHERE phone=? AND created_at < ?", (phone, cutoff))
        cur.execute("SELECT code, quantity, name, price_ars FROM carts WHERE phone=?", (phone,))
        rows = cur.fetchall()
        out = []
        for r in rows:
            code, q, name, price_str = r[0], int(r[1]), r[2], r[3]
            try:
                price_dec = Decimal(price_str)
            except (InvalidOperation, TypeError):
                price_dec = Decimal("0.00")
            out.append((code, q, name, price_dec))
        return out


def cart_update_qty(phone: str, code: str, qty: int):
    qty = max(0, min(int(qty or 0), 999))
    with cart_lock, get_db_connection() as conn:
        if qty == 0:
            conn.execute("DELETE FROM carts WHERE phone=? AND code=?", (phone, code))
        else:
            now = datetime.now().isoformat()
            conn.execute(
                "UPDATE carts SET quantity=?, created_at=? WHERE phone=? AND code=?",
                (qty, now, phone, code)
            )


def cart_clear(phone: str):
    with cart_lock, get_db_connection() as conn:
        conn.execute("DELETE FROM carts WHERE phone=?", (phone,))


def cart_totals(phone: str):
    items = cart_get(phone)
    total = sum(q * price for _, q, __, price in items)
    discount = Decimal("0.05") * total if total > Decimal("10000000") else Decimal("0.00")
    final = (total - discount).quantize(Decimal("0.01"))
    return final, discount.quantize(Decimal("0.01"))


# -------------------------
# Procesamiento de listas grandes
# -------------------------
def parse_bulk_list(text: str) -> List[Tuple[int, str]]:
    """Parsea listas como '10 BUJIA NGK B7ES' o '5 FILTRO AIRE HONDA'"""
    lines = text.strip().split("\n")
    parsed = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        match = re.match(r'^(\d+)\s+(.+)$', line)
        if match:
            qty = int(match.group(1))
            product_name = match.group(2).strip()
            parsed.append((qty, product_name))
        else:
            parsed.append((1, line))
    return parsed


def is_bulk_list_request(text: str) -> bool:
    lower = text.lower()
    quote_keywords = ["cotiz", "precio", "cuanto", "tenes", "stock", "pedido", "lista"]
    lines = text.strip().split("\n")
    lines_with_qty = sum(1 for l in lines if re.match(r'^\d+\s+\w', l.strip()))
    has_quote_intent = any(kw in lower for kw in quote_keywords)
    is_multiline = len(lines) >= 3
    return (lines_with_qty >= 3) or (has_quote_intent and is_multiline and lines_with_qty >= 1)


# -------------------------
# Tools del agente
# -------------------------

class ToolExecutor:
    def __init__(self, phone: str):
        self.phone = phone

    def execute(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Ejecuta cualquier tool y repara argumentos incompatibles."""
        method = getattr(self, tool_name, None)
        if not method:
            return {"error": f"Tool '{tool_name}' no encontrada"}

        # Normalizar argumentos según tool
        if tool_name == "quote_bulk_list":
            # Aceptar tanto "list" como "raw_list"
            if "list" in arguments and "raw_list" not in arguments:
                arguments["raw_list"] = arguments.pop("list")

        try:
            logger.info(f"🔧 Ejecutando tool: {tool_name} con args: {arguments.keys()}")
            return method(**arguments)
        except TypeError as e:
            logger.error(f"❌ Error en {tool_name}: {e}", exc_info=True)
            return {"error": f"Argumentos inválidos para {tool_name}: {e}"}
        except Exception as e:
            logger.error(f"❌ Error inesperado en {tool_name}: {e}", exc_info=True)
            return {"error": str(e)}

    def quote_bulk_list(self, raw_list: str) -> Dict[str, Any]:
        """
        Recibe una lista de productos en formato libre (por ejemplo:
        '10 bujia NGK, 5 cableado Zanella RX150') y devuelve cotización aproximada.
        """
        if not raw_list:
            return {"success": False, "error": "Lista vacía"}

        catalog, _ = get_catalog_and_index()
        if not catalog:
            return {"success": False, "error": "Catálogo no disponible"}

        # Separar por coma o salto de línea
        items = [i.strip() for i in re.split(r"[,\n]", raw_list) if i.strip()]
        resultados = []
        for item in items:
            # Detectar cantidad inicial (ej: "10 bujía...")
            match = re.match(r"^(\d+)\s+(.*)$", item)
            qty = int(match.group(1)) if match else 1
            query = match.group(2) if match else item
            found = hybrid_search(query, limit=1)
            if found:
                p = found[0]
                price = Decimal(str(p.get("price_ars", 0))) * qty
                resultados.append({
                    "query": query,
                    "code": p.get("code", ""),
                    "name": p.get("name", ""),
                    "qty": qty,
                    "price_total": float(price)
                })
            else:
                resultados.append({
                    "query": query,
                    "not_found": True
                })

        total = sum(r["price_total"] for r in resultados if "price_total" in r)
        return {
            "success": True,
            "count": len(resultados),
            "total": float(total),
            "items": resultados
        }


# === BÚSQUEDA DE PRODUCTOS ===
    def search_products(self, query: str, limit: int = 15) -> Dict:
        results = hybrid_search(query, limit=limit)
        if results:
            save_user_state(self.phone, results[0])
            save_last_search(self.phone, results, query)
        return {
            "success": True,
            "query": query,
            "results": [
                {"code": p["code"], "name": p["name"], "price_ars": p["price_ars"], "price_usd": p["price_usd"]}
                for p in results
            ],
            "count": len(results)
        }

    # === AGREGAR AL CARRITO ===
    def add_to_cart(self, items: List[Dict]) -> Dict:
        catalog, _ = get_catalog_and_index()
        added, not_found = [], []
        for item in items:
            code = str(item.get("code", "")).strip()
            qty = int(item.get("quantity", 1))
            ok, norm = validate_tercom_code(code)
            if not ok:
                not_found.append(code)
                continue
            prod = next((x for x in catalog if x["code"] == norm), None)
            if prod:
                price_ars = to_decimal_money(prod["price_ars"])
                price_usd = to_decimal_money(prod["price_usd"])
                cart_add(self.phone, norm, qty, prod["name"], price_ars, price_usd)
                added.append({
                    "code": norm,
                    "name": prod["name"],
                    "quantity": qty,
                    "subtotal": str((price_ars * qty).quantize(Decimal("0.01")))
                })
            else:
                not_found.append(code)
        if added:
            total = sum(Decimal(a["subtotal"]) for a in added)
            return {"success": True, "added": added, "message": f"Agregué {len(added)} productos por ${total:,.2f} ARS"}
        return {"success": False, "message": "No pude agregar productos"}

    def view_cart(self) -> Dict:
        items = cart_get(self.phone)
        total, discount = cart_totals(self.phone)
        return {
            "success": True,
            "items": [
                {
                    "code": c,
                    "name": n,
                    "quantity": q,
                    "price_unit": float(p),
                    "price_total": float((p * q).quantize(Decimal("0.01")))
                }
                for c, q, n, p in items
            ],
            "total": float(total),
            "item_count": len(items)
        }

    def update_cart_item(self, code: str, quantity: int) -> Dict:
        cart_update_qty(self.phone, code, quantity)
        return {"success": True, "message": "Actualizado"}

    def clear_cart(self) -> Dict:
        cart_clear(self.phone)
        return {"success": True}

    def confirm_order(self) -> Dict:
        items = cart_get(self.phone)
        if not items:
            return {"success": False, "error": "Carrito vacío"}
        total, _ = cart_totals(self.phone)
        cart_clear(self.phone)
        return {"success": True, "message": f"Pedido confirmado por ${total:,.2f} ARS"}

    def get_last_search_results(self) -> Dict:
        search = get_last_search(self.phone)
        if not search:
            return {"success": False}
        return {
            "success": True,
            "query": search["query"],
            "products": search["products"],
            "product_codes": [p["code"] for p in search["products"]]
        }

    # === NUEVO: COTIZACIÓN MASIVA ===
    def quote_bulk_list(self, raw_list: str) -> Dict:
        catalog, _ = get_catalog_and_index()
        parsed_items = parse_bulk_list(raw_list)
        if not parsed_items:
            return {"success": False, "error": "No pude interpretar la lista"}
        results, not_found = [], []
        total_quoted = Decimal("0")
        for requested_qty, product_name in parsed_items:
            matches = hybrid_search(product_name, limit=3)
            if matches:
                best = matches[0]
                price_ars = Decimal(str(best["price_ars"]))
                subtotal = price_ars * requested_qty
                total_quoted += subtotal
                results.append({
                    "requested": product_name,
                    "found": best["name"],
                    "code": best["code"],
                    "quantity": requested_qty,
                    "price_unit": float(price_ars),
                    "subtotal": float(subtotal)
                })
            else:
                not_found.append({"requested": product_name, "quantity": requested_qty})
        return {
            "success": True,
            "found_count": len(results),
            "not_found_count": len(not_found),
            "results": results,
            "not_found": not_found,
            "total_quoted": float(total_quoted),
            "message": f"Encontré {len(results)} de {len(parsed_items)} productos solicitados"
        }

# =========================
# Parte 4/4 - Agente, Prompt, Webhook y Flask endpoints
# =========================

def _format_list(products, max_items=15) -> str:
    if not products:
        return "No encontré productos."
    lines = []
    for p in products[:max_items]:
        code = p.get("code", "").strip() or "s/c"
        name = p.get("name", "").strip()
        ars = Decimal(str(p.get("price_ars", 0))).quantize(Decimal("0.01"))
        lines.append(f"• **(Cód: {code})** {name} - ${ars:,.0f} ARS")
    return "\n".join(lines)


def _format_bulk_quote_response(data: Dict) -> str:
    """Formatea respuesta de cotización masiva de manera clara."""
    if not data.get("success"):
        return "No pude procesar la lista. ¿Podrías reformularla?"

    results = data.get("results", [])
    not_found = data.get("not_found", [])
    total = Decimal(str(data.get("total_quoted", 0)))

    lines = ["📋 *COTIZACIÓN DE TU LISTA:*\n"]

    if results:
        lines.append(f"✅ *Encontré {len(results)} productos:*\n")
        for item in results:
            qty = item["quantity"]
            name = item["found"]
            code = item["code"]
            price = Decimal(str(item["price_unit"]))
            subtotal = Decimal(str(item["subtotal"]))
            lines.append(f"• {qty} × *{name}*")
            lines.append(f"  (Cód: {code}) - ${price:,.0f} c/u = ${subtotal:,.0f}")
        lines.append(f"\n💰 *TOTAL: ${total:,.2f} ARS*")

    if not_found:
        lines.append(f"\n⚠️ *No encontré {len(not_found)} productos:*")
        for item in not_found[:5]:
            lines.append(f"• {item['quantity']} × {item['requested']}")
        if len(not_found) > 5:
            lines.append(f"... y {len(not_found) - 5} más")

    lines.append("\n¿Querés que agregue los productos encontrados al carrito? 🛒")
    return "\n".join(lines)


def _intent_needs_basics(user_message: str) -> bool:
    t = user_message.lower()
    triggers = [
        "surtido", "básico", "basico", "abrir mi local", "recomendar", "proponer",
        "lista de productos", "lo básico", "lo basico", "catalogo", "catálogo",
        "empezar", "comenzar", "inicial", "necesito productos"
    ]
    return any(x in t for x in triggers)


def _force_search_and_reply(phone: str, query: str) -> str:
    results = hybrid_search(query, limit=15)
    if not results:
        results = hybrid_search("repuestos basicos moto", limit=15)
    if not results:
        return "Disculpá, tengo problemas con el catálogo."
    save_last_search(phone, results, query)
    save_user_state(phone, results[0])
    listado = _format_list(results, max_items=len(results))
    return f"Acá tenés {len(results)} productos sugeridos:\n\n{listado}\n\n¿Querés que agregue alguno?"


# -------------------------
# Lógica principal del agente
# -------------------------
def run_agent(phone: str, user_message: str, max_iterations: int = 8) -> str:
    catalog, _ = get_catalog_and_index()
    if not catalog:
        return "No puedo acceder al catálogo."

    # Si el usuario envía una lista masiva de productos:
    if is_bulk_list_request(user_message):
        logger.info(f"🔍 Lista masiva detectada para {phone}")
        executor = ToolExecutor(phone)
        result = executor.quote_bulk_list(raw_list=user_message)

        if result.get("success") and result.get("results"):
            products_for_save = [
                {
                    "code": r["code"],
                    "name": r["found"],
                    "price_ars": r["price_unit"],
                    "price_usd": r["price_unit"] / 1600
                }
                for r in result["results"]
            ]
            save_last_search(phone, products_for_save, "Lista masiva")
        return _format_bulk_quote_response(result)

    executor = ToolExecutor(phone)
    history = get_history_today(phone, limit=20)
    logger.info(f"📱 {phone}: {user_message[:100]}")

    system_prompt = """Sos Fran, vendedor de Tercom (mayorista de repuestos para motos en Argentina).

REGLAS CRÍTICAS:
1. Si el usuario envía una LISTA CON CANTIDADES (ej: '10 BUJIA NGK'), ejecutá quote_bulk_list con todo el texto.
2. NUNCA digas “te pasé la lista” si no ejecutaste una búsqueda o cotización en este mensaje.
3. Si pide “surtido/básico/catálogo”, ejecutá search_products primero.
4. Antes de decir que agregaste algo, ejecutá add_to_cart.
5. Si pregunta por carrito, ejecutá view_cart.
6. Confirmar pedidos con confirm_order.
7. Respondé como humano argentino (usá 'vos', 'dale', 'che', 'mirá').

Formato de respuesta:
- Para productos: **(Cód: XXXX/XXXXX-XXX)** Nombre - $precio ARS
- Para listas: usá quote_bulk_list y mostrá resultados organizados."""

    messages = [{"role": "system", "content": system_prompt}]
    for msg, role in history:
        messages.append({"role": role, "content": msg})
    messages.append({"role": "user", "content": user_message})

    last_text = ""
    for iteration in range(max_iterations):
        try:
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=messages,
                tools=[
                    {"type": "function", "function": {"name": "search_products"}},
                    {"type": "function", "function": {"name": "add_to_cart"}},
                    {"type": "function", "function": {"name": "view_cart"}},
                    {"type": "function", "function": {"name": "quote_bulk_list"}}
                ],
                tool_choice="auto",
                temperature=0.7,
                max_tokens=2000,
                timeout=REQUESTS_TIMEOUT
            )
            message = response.choices[0].message

            if getattr(message, "tool_calls", None):
                messages.append({"role": "assistant", "content": message.content or "", "tool_calls": message.tool_calls})
                for tc in message.tool_calls:
                    result = executor.execute(tc.function.name, json.loads(tc.function.arguments or "{}"))
                    if tc.function.name == "quote_bulk_list":
                        return _format_bulk_quote_response(result)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.function.name,
                        "content": json.dumps(result, ensure_ascii=False)
                    })
                continue

            final_response = (message.content or "").strip()
            last_text = final_response
            if _intent_needs_basics(user_message):
                has_codes = bool(re.search(r"\(Cód:\s*\d{4}/\d{5}-\d{3}\)", final_response))
                if not has_codes:
                    return _force_search_and_reply(phone, "surtido basico repuestos moto")
            if final_response:
                return final_response
            messages.append({"role": "system", "content": "Respondé con una frase clara."})
        except Exception as e:
            logger.error(f"❌ Iteración {iteration}: {e}")
            return "Disculpá, hubo un problema. ¿Podés reintentar?"
    return last_text or _force_search_and_reply(phone, "surtido basico")


# -------------------------
# Webhook de Twilio
# -------------------------
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
            resp.message("Esperá un momento 😊")
            return str(resp)

        save_message(phone, msg_in, "user")
        text = run_agent(phone, msg_in)
        save_message(phone, text, "assistant")

        resp = MessagingResponse()
        resp.message(text)
        return str(resp)
    except Exception as e:
        logger.error(f"❌ Webhook: {e}")
        resp = MessagingResponse()
        resp.message("Disculpá, hubo un problema técnico.")
        return str(resp)


# -------------------------
# Healthcheck
# -------------------------
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


# -------------------------
# Main (para Railway)
# -------------------------
if __name__ == "__main__":
    logger.info("🚀 Iniciando Fran 2.4 FULL (modo desarrollo)")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
    
