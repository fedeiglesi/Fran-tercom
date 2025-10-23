import os
import json
import sqlite3
import requests
import pandas as pd
from io import StringIO
from datetime import datetime
from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from rapidfuzz import process, fuzz
from functools import lru_cache

# -------------------------------
# CONFIGURACIÓN INICIAL
# -------------------------------

app = Flask(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
CATALOG_URL = "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/refs/heads/main/LISTA_TERCOM_LIMPIA.csv"
EXCHANGE_API_URL = "https://dolarapi.com/v1/dolares/oficial"

client = OpenAI(api_key=OPENAI_API_KEY)


# -------------------------------
# BASE DE DATOS LOCAL
# -------------------------------

def init_db():
    conn = sqlite3.connect("tercom.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS conversations
                 (phone TEXT, message TEXT, role TEXT, timestamp TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS carts
                 (phone TEXT, product_code TEXT, quantity INTEGER, 
                  product_name TEXT, price_usd REAL, price_ars REAL)""")
    conn.commit()
    conn.close()

init_db()


# -------------------------------
# UTILIDADES
# -------------------------------

def get_exchange_rate():
    """Obtiene tipo de cambio oficial"""
    try:
        data = requests.get(EXCHANGE_API_URL, timeout=5).json()
        return float(data["venta"])
    except:
        return 1200.0


@lru_cache(maxsize=1)
def load_catalog():
    """Descarga el CSV desde GitHub y lo parsea con pandas"""
    try:
        print("📦 Cargando catálogo desde GitHub...")
        response = requests.get(CATALOG_URL, timeout=10)
        response.encoding = "utf-8"
        df = pd.read_csv(StringIO(response.text))
        df.columns = [c.strip().lower() for c in df.columns]
        df.fillna("", inplace=True)
        return df
    except Exception as e:
        print(f"Error cargando catálogo: {e}")
        return pd.DataFrame()


def search_catalog(query, limit=10):
    """Busca productos similares al texto del cliente"""
    df = load_catalog()
    productos = df["descripcion"].astype(str).tolist()
    results = process.extract(query, productos, scorer=fuzz.WRatio, limit=limit)
    matched_rows = [df.iloc[idx] for _, _, idx in results if results]
    return matched_rows


def save_message(phone, message, role):
    conn = sqlite3.connect("tercom.db")
    c = conn.cursor()
    c.execute("INSERT INTO conversations VALUES (?, ?, ?, ?)",
              (phone, message, role, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def get_cart(phone):
    conn = sqlite3.connect("tercom.db")
    c = conn.cursor()
    c.execute("SELECT product_code, quantity, product_name, price_ars FROM carts WHERE phone = ?", (phone,))
    items = c.fetchall()
    conn.close()
    return items


def add_to_cart(phone, code, quantity, name, price_usd, price_ars):
    conn = sqlite3.connect("tercom.db")
    c = conn.cursor()
    c.execute("INSERT INTO carts VALUES (?, ?, ?, ?, ?, ?)",
              (phone, code, quantity, name, price_usd, price_ars))
    conn.commit()
    conn.close()


def clear_cart(phone):
    conn = sqlite3.connect("tercom.db")
    c = conn.cursor()
    c.execute("DELETE FROM carts WHERE phone = ?", (phone,))
    conn.commit()
    conn.close()


def calculate_total(phone):
    items = get_cart(phone)
    total = sum(item[3] * item[1] for item in items)
    discount = total * 0.05 if total > 10_000_000 else 0
    return total - discount, discount


# -------------------------------
# PROMPT BASE
# -------------------------------

PROMPT_FRAN = """
Sos Fran, el agente de ventas de Tercom, una empresa de motopartes.
Sos amable, profesional y respondés como una persona, no como un bot.
Tu tarea es ayudar al cliente a encontrar los productos que necesita y gestionar su carrito.

REGLAS:
1. No digas que sos una inteligencia artificial.
2. No inventes precios ni productos. Usá solo los datos del catálogo.
3. Si hay errores de escritura, interpretalos correctamente (ej: 'amort' = 'amortiguador', 'yamak' = 'yamaha').
4. Mantené un tono cálido, claro y humano.
5. Si el producto no está, ofrecé alternativas del catálogo.
6. Si el cliente pide varios productos, podés agregarlos todos en una sola acción.
7. Si entendés que el cliente quiere ver el carrito, confirmarlo o limpiarlo, usá uno de estos JSON:
   - Agregar: {"action": "add_to_cart", "products": [{"code": "ABC123", "quantity": 2}]}
   - Ver carrito: {"action": "show_cart"}
   - Confirmar: {"action": "confirm_order"}
   - Limpiar: {"action": "clear_cart"}
8. Si no se necesita acción, respondé naturalmente.
"""


# -------------------------------
# CHATBOT
# -------------------------------

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        incoming_msg = request.values.get("Body", "").strip()
        from_number = request.values.get("From", "")

        save_message(from_number, incoming_msg, "user")

        # Buscar coincidencias locales primero
        candidates = search_catalog(incoming_msg)
        if candidates:
            catalog_text = "\n".join(
                [f"{r['codigo']} | {r['descripcion']} | USD {r['usd']} | ARS {r['ars']}" for r in candidates]
            )
        else:
            catalog_text = "No se encontraron coincidencias locales."

        system_prompt = f"""{PROMPT_FRAN}

CATÁLOGO (fragmento relevante):
{catalog_text}
"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": incoming_msg}
        ]

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.7,
            max_tokens=500
        )

        bot_response = response.choices[0].message.content

        resp = MessagingResponse()
        resp.message(bot_response)

        save_message(from_number, bot_response, "assistant")

        return str(resp)

    except Exception as e:
        print(f"❌ Error en webhook: {e}")
        resp = MessagingResponse()
        resp.message("Tuve un problema técnico, ¿podés repetirlo?")
        return str(resp)


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
