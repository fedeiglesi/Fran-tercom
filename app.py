import os
import json
import csv
import io
import sqlite3
from datetime import datetime
from functools import lru_cache

import requests
from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI
from rapidfuzz import process, fuzz

# -----------------------------------
# Configuración
# -----------------------------------
app = Flask(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
CATALOG_URL = "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/refs/heads/main/LISTA_TERCOM_LIMPIA.csv"
EXCHANGE_API_URL = "https://dolarapi.com/v1/dolares/oficial"

client = OpenAI(api_key=OPENAI_API_KEY)

# -----------------------------------
# Base de datos
# -----------------------------------
def init_db():
    conn = sqlite3.connect('tercom.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS conversations
                 (phone TEXT, message TEXT, role TEXT, timestamp TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS carts
                 (phone TEXT, product_code TEXT, quantity INTEGER, 
                  product_name TEXT, price_usd REAL, price_ars REAL)''')
    conn.commit()
    conn.close()

init_db()

# -----------------------------------
# Utilidades
# -----------------------------------
def get_exchange_rate():
    try:
        data = requests.get(EXCHANGE_API_URL, timeout=5).json()
        return float(data.get("venta", 1200.0))
    except:
        return 1200.0

@lru_cache(maxsize=1)
def load_catalog_structured():
    """
    Lee el CSV desde GitHub y lo devuelve como lista de dicts:
    {code, name, price_usd, price_ars}
    El parser es tolerante a encabezados distintos.
    """
    try:
        resp = requests.get(CATALOG_URL, timeout=15)
        resp.encoding = 'utf-8'
        csv_data = io.StringIO(resp.text)
        reader = csv.reader(csv_data)

        rows = list(reader)
        if not rows:
            return []

        header = [h.strip().lower() for h in rows[0]]

        # Normalizamos nombres de columnas
        def col_idx(keys):
            for i, h in enumerate(header):
                h_norm = h.replace("ó","o").replace("á","a").replace("é","e").replace("í","i").replace("ú","u")
                if any(k in h_norm for k in keys):
                    return i
            return None

        idx_code = col_idx(["codigo", "código"])
        idx_name = col_idx(["producto", "descripcion", "descripción"])
        idx_usd  = col_idx(["usd", "dolar", "precio en dolares"])
        idx_ars  = col_idx(["ars", "pesos", "precio en pesos"])

        exchange = get_exchange_rate()
        catalog = []

        for line in rows[1:]:
            # proteger longitudes cortas
            if len(line) < 2:
                continue

            code = (line[idx_code] if idx_code is not None else "").strip() if idx_code is not None and idx_code < len(line) else ""
            name = (line[idx_name] if idx_name is not None else "").strip() if idx_name is not None and idx_name < len(line) else ""

            # Limpieza de números: quita símbolos, miles y unifica decimales
            def to_float(s):
                s = (s or "").strip()
                s = s.replace("$", "").replace("USD", "").replace("ARS", "")
                s = s.replace(" ", "")
                # primero quitamos separador de miles (puntos), luego cambiamos coma a punto
                s = s.replace(".", "").replace(",", ".")
                try:
                    return float(s)
                except:
                    return 0.0

            price_usd = to_float(line[idx_usd]) if idx_usd is not None and idx_usd < len(line) else 0.0
            price_ars = to_float(line[idx_ars]) if idx_ars is not None and idx_ars < len(line) else 0.0

            # si no hay ARS pero hay USD, convertir
            if price_ars == 0.0 and price_usd > 0.0:
                price_ars = round(price_usd * exchange, 2)

            if name:
                catalog.append({
                    "code": code,
                    "name": name,
                    "price_usd": price_usd,
                    "price_ars": price_ars
                })

        return catalog
    except Exception as e:
        print(f"Error cargando catálogo estructurado: {e}")
        return []

@lru_cache(maxsize=1)
def load_catalog_text():
    """Devuelve el CSV crudo como texto (para fallback IA completa)."""
    try:
        resp = requests.get(CATALOG_URL, timeout=15)
        resp.encoding = 'utf-8'
        return resp.text
    except Exception as e:
        print(f"Error leyendo catálogo texto: {e}")
        return ""

def fuzzy_candidates(query, limit=12):
    """
    Devuelve hasta 'limit' filas del catálogo por similitud de nombre.
    """
    catalog = load_catalog_structured()
    if not catalog:
        return []

    names = [p["name"] for p in catalog]
    results = process.extract(
        query, names, scorer=fuzz.WRatio, limit=limit
    )
    # results = [(matched_string, score, index), ...]
    matches = []
    for _, score, idx in results:
        # umbral flexible
        if score >= 55:
            matches.append(catalog[idx])
    return matches

def save_message(phone, message, role):
    conn = sqlite3.connect('tercom.db')
    c = conn.cursor()
    c.execute('INSERT INTO conversations VALUES (?, ?, ?, ?)',
              (phone, message, role, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_conversation_history(phone):
    conn = sqlite3.connect('tercom.db')
    c = conn.cursor()
    c.execute('SELECT message, role FROM conversations WHERE phone = ? ORDER BY timestamp DESC LIMIT 10', (phone,))
    history = c.fetchall()
    conn.close()
    return list(reversed(history))

def get_cart(phone):
    conn = sqlite3.connect('tercom.db')
    c = conn.cursor()
    c.execute('SELECT product_code, quantity, product_name, price_ars FROM carts WHERE phone = ?', (phone,))
    items = c.fetchall()
    conn.close()
    return items

def add_to_cart(phone, product_code, quantity, product_name, price_usd, price_ars):
    conn = sqlite3.connect('tercom.db')
    c = conn.cursor()
    c.execute('INSERT INTO carts VALUES (?, ?, ?, ?, ?, ?)',
              (phone, product_code, quantity, product_name, price_usd, price_ars))
    conn.commit()
    conn.close()

def clear_cart(phone):
    conn = sqlite3.connect('tercom.db')
    c = conn.cursor()
    c.execute('DELETE FROM carts WHERE phone = ?', (phone,))
    conn.commit()
    conn.close()

def calculate_total(phone):
    items = get_cart(phone)
    total = sum(item[3] * item[1] for item in items)
    discount = 0
    if total > 10000000:
        discount = total * 0.05
        total = total * 0.95
    return total, discount

# -----------------------------------
# Prompt base (archivo externo si existe)
# -----------------------------------
def load_prompt():
    try:
        with open("prompt_fran.txt", "r", encoding="utf-8") as f:
            return f.read()
    except:
        return (
            "Sos Fran, el agente de ventas de Tercom, una empresa de motopartes.\n"
            "Sos amable, profesional y respondés como una persona, no como un bot.\n"
            "Tu tarea es ayudar al cliente a encontrar los productos que necesita y gestionar su carrito.\n\n"
            "REGLAS DE CONVERSACIÓN:\n"
            "1. No digas que sos una inteligencia artificial.\n"
            "2. No inventes precios ni productos. Usá solo los datos del catálogo que recibas.\n"
            "3. Mantené un tono cálido, claro y humano.\n"
            "4. Si el producto no está, ofrecé alternativas del catálogo.\n"
            "5. Si el cliente pide varios productos, podés agregarlos todos en una sola acción.\n"
            "6. Si entendés que el cliente quiere ver el carrito, confirmarlo o limpiarlo, usá JSON de acciones.\n"
            "7. Si no se necesita ninguna acción, simplemente conversá normalmente.\n"
        )

# -----------------------------------
# Webhook
# -----------------------------------
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        incoming_msg = request.values.get('Body', '').strip()
        from_number = request.values.get('From', '')
        save_message(from_number, incoming_msg, 'user')

        # 1) Buscador local rápido (no gasta tokens)
        candidates = fuzzy_candidates(incoming_msg)

        base_prompt = load_prompt()

        if candidates:
            frag = "\n".join([
                f"{p['code']} | {p['name']} | USD {p['price_usd']:.2f} | ARS {p['price_ars']:.2f}"
                for p in candidates
            ])
            system_prompt = f"""{base_prompt}

CATÁLOGO (coincidencias relevantes, precios reales):
{frag}

Si el cliente quiere agregar algo al carrito, devolvé el JSON de acción correspondiente.
"""
        else:
            # 2) Fallback IA completa: le damos el CSV crudo (acotado)
            raw = load_catalog_text()
            raw_cut = raw[:60000]  # límite de seguridad
            system_prompt = f"""{base_prompt}

No encontré coincidencias locales. A continuación tenés el CATÁLOGO COMPLETO (texto crudo CSV).
Leelo y recomendá los mejores candidatos, devolviendo los códigos y precios reales.

CATÁLOGO CRUDO (parcial):
{raw_cut}
"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": incoming_msg}
        ]

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.7,
            max_tokens=600
        )

        bot_response = response.choices[0].message.content
        response_text = process_actions(bot_response, from_number, load_catalog_structured())

        save_message(from_number, response_text, 'assistant')
        resp = MessagingResponse()
        resp.message(response_text)
        return str(resp)

    except Exception as e:
        print(f"Error general: {e}")
        resp = MessagingResponse()
        resp.message("Tuve un problema técnico, ¿podés repetir tu consulta?")
        return str(resp)

# -----------------------------------
# Acciones de carrito (JSON en la respuesta del modelo)
# -----------------------------------
def process_actions(bot_response, phone, catalog):
    try:
        if '{"action"' in bot_response:
            start = bot_response.index('{"action"')
            # buscar el cierre correcto del JSON (puede venir con productos)
            # estrategia simple: hasta el último '}' de la cadena
            end = bot_response.rfind('}') + 1
            maybe = bot_response[start:end]
            try:
                action_json = json.loads(maybe)
            except:
                # si falla, intentar hasta el primer cierre
                end = bot_response.index('}', start) + 1
                action_json = json.loads(bot_response[start:end])

            action = action_json.get('action')

            if action == 'add_to_cart':
                products = action_json.get('products', [])
                for prod in products:
                    code = str(prod.get('code', '')).strip()
                    qty  = int(prod.get('quantity', 1))
                    # buscar en catálogo estructurado por code
                    product = next((p for p in catalog if p['code'] == code), None)
                    if product:
                        add_to_cart(phone, code, qty, product['name'], product['price_usd'], product['price_ars'])
                return "Listo! Agregué los productos a tu carrito. ¿Querés ver el resumen?"

            elif action == 'show_cart':
                items = get_cart(phone)
                if not items:
                    return "Tu carrito está vacío. ¿Qué motopartes necesitás?"
                cart_text = "*Tu Carrito:*\n"
                for code, qty, name, price in items:
                    cart_text += f"• {name} (x{qty}) - ${price * qty:,.2f}\n"
                total, discount = calculate_total(phone)
                cart_text += f"\n*Subtotal:* ${total + discount:,.2f}"
                if discount > 0:
                    cart_text += f"\n*Descuento 5%:* -${discount:,.2f}"
                cart_text += f"\n*TOTAL:* ${total:,.2f}\n\n¿Confirmamos el pedido?"
                return cart_text

            elif action == 'confirm_order':
                items = get_cart(phone)
                if not items:
                    return "No tenés productos en el carrito."
                total, discount = calculate_total(phone)
                order_text = "*Pedido confirmado!*\n\n"
                for code, qty, name, price in items:
                    order_text += f"• {name} (x{qty})\n"
                order_text += f"\n*Total:* ${total:,.2f}"
                if discount > 0:
                    order_text += " (con descuento del 5%)"
                order_text += "\n\nTe contactamos por este medio para coordinar el pago y envío. ¡Gracias por tu compra!"
                clear_cart(phone)
                return order_text

            elif action == 'clear_cart':
                clear_cart(phone)
                return "Carrito limpiado. ¿En qué más puedo ayudarte?"

        return bot_response
    except Exception as e:
        print(f"Error process_actions: {e}")
        return bot_response

# -----------------------------------
# Healthcheck
# -----------------------------------
@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
