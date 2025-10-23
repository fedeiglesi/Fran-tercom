import os
import json
import sqlite3
from datetime import datetime
from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
import requests
from openai import OpenAI

app = Flask(__name__)

# Configuración
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')
CATALOG_URL = 'https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/main/LISTA_TERCOM_LIMPIA.csv'
EXCHANGE_API_URL = 'https://dolarapi.com/v1/dolares/oficial'

client = OpenAI(api_key=OPENAI_API_KEY)

# Base de datos
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

# Cargar prompt externo
def load_prompt():
    try:
        with open("prompt_fran.txt", "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"Error al leer prompt_fran.txt: {e}")
        return "Sos Fran, el vendedor humano de Tercom. Tu tarea es ayudar a los clientes a comprar motopartes."

# Obtener tipo de cambio
def get_exchange_rate():
    try:
        response = requests.get(EXCHANGE_API_URL, timeout=5)
        data = response.json()
        return float(data['venta'])
    except:
        return 1200.0

# Cargar catálogo desde GitHub
def load_catalog():
    try:
        response = requests.get(CATALOG_URL, timeout=10)
        lines = response.text.strip().split('\n')
        catalog = []
        exchange_rate = get_exchange_rate()

        for i, line in enumerate(lines[1:]):
            parts = line.split(',')
            if len(parts) >= 4:
                code = parts[0].strip()
                name = parts[1].strip()
                price_usd = parts[2].strip()
                price_ars = parts[3].strip()

                if price_usd and price_usd != '' and price_usd != '0':
                    final_price = float(price_usd) * exchange_rate
                    currency = 'USD'
                else:
                    final_price = float(price_ars) if price_ars else 0
                    currency = 'ARS'

                catalog.append({
                    'code': code,
                    'name': name,
                    'price_usd': float(price_usd) if price_usd else 0,
                    'price_ars': final_price,
                    'currency': currency
                })
        return catalog, exchange_rate
    except Exception as e:
        print(f"Error cargando catálogo: {e}")
        return [], 1200.0

# Funciones de conversación
def get_conversation_history(phone):
    conn = sqlite3.connect('tercom.db')
    c = conn.cursor()
    c.execute('SELECT message, role FROM conversations WHERE phone = ? ORDER BY timestamp DESC LIMIT 10', (phone,))
    history = c.fetchall()
    conn.close()
    return list(reversed(history))

def save_message(phone, message, role):
    conn = sqlite3.connect('tercom.db')
    c = conn.cursor()
    c.execute('INSERT INTO conversations VALUES (?, ?, ?, ?)',
              (phone, message, role, datetime.now().isoformat()))
    conn.commit()
    conn.close()

# Funciones del carrito
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

# Crear prompt con catálogo
def create_system_prompt(catalog, exchange_rate):
    base_prompt = load_prompt()
    catalog_text = "\n".join([
        f"- Código: {p['code']} | {p['name']} | ${p['price_ars']:,.2f} ARS"
        for p in catalog[:50]  # Limita a 50 productos para no saturar el modelo
    ])
    return f"""{base_prompt}

CATÁLOGO (TC: ${exchange_rate:.2f}):
{catalog_text}"""

# Webhook principal
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        incoming_msg = request.values.get('Body', '').strip()
        from_number = request.values.get('From', '')
        save_message(from_number, incoming_msg, 'user')

        catalog, exchange_rate = load_catalog()
        history = get_conversation_history(from_number)
        messages = [{"role": "system", "content": create_system_prompt(catalog, exchange_rate)}]

        for msg, role in history:
            messages.append({"role": role, "content": msg})

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.7,
            max_tokens=500
        )

        bot_response = response.choices[0].message.content
        response_text = process_actions(bot_response, from_number, catalog)

        save_message(from_number, response_text, 'assistant')
        resp = MessagingResponse()
        resp.message(response_text)
        return str(resp)

    except Exception as e:
        print(f"Error general: {e}")
        resp = MessagingResponse()
        resp.message("Tuve un problema técnico, ¿podés repetir tu consulta?")
        return str(resp)

# Procesar acciones del bot
def process_actions(bot_response, phone, catalog):
    try:
        if '{"action"' in bot_response:
            start = bot_response.index('{"action"')
            end = bot_response.index('}', start) + 1
            action_json = json.loads(bot_response[start:end])

            action = action_json.get('action')

            if action == 'add_to_cart':
                products = action_json.get('products', [])
                for prod in products:
                    code = prod['code']
                    qty = prod['quantity']
                    product = next((p for p in catalog if p['code'] == code), None)
                    if product:
                        add_to_cart(phone, code, qty, product['name'], product['price_usd'], product['price_ars'])
                return f"Listo! Agregué los productos a tu carrito. ¿Querés ver el resumen?"

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
                    order_text += f" (con descuento del 5%)"
                order_text += "\n\nTe contactamos por este medio para coordinar el pago y envío. ¡Gracias por tu compra!"
                clear_cart(phone)
                return order_text

            elif action == 'clear_cart':
                clear_cart(phone)
                return "Carrito limpiado. ¿En qué más puedo ayudarte?"

        return bot_response
    except:
        return bot_response

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
