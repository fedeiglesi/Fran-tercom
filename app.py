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
CATALOG_URL = 'https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/refs/heads/main/LISTA_TERCOM_LIMPIA.csv'
EXCHANGE_API_URL = 'https://dolarapi.com/v1/dolares/oficial'

client = OpenAI(api_key=OPENAI_API_KEY)

# Base de datos local
def init_db():
    conn = sqlite3.connect('tercom.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS conversations
                 (phone TEXT, message TEXT, role TEXT, timestamp TEXT)''')
    conn.commit()
    conn.close()

init_db()

def load_catalog_text():
    """Descarga el CSV del catálogo y lo devuelve como texto completo"""
    try:
        response = requests.get(CATALOG_URL, timeout=10)
        if response.status_code == 200:
            return response.text[:15000]  # límite para no saturar tokens
        else:
            print(f"Error cargando catálogo: {response.status_code}")
            return ""
    except Exception as e:
        print(f"Error al descargar catálogo: {e}")
        return ""

def get_exchange_rate():
    try:
        r = requests.get(EXCHANGE_API_URL, timeout=5)
        return float(r.json().get("venta", 1200))
    except:
        return 1200.0

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
    data = c.fetchall()
    conn.close()
    return list(reversed(data))

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        incoming_msg = request.values.get('Body', '').strip()
        from_number = request.values.get('From', '')

        save_message(from_number, incoming_msg, 'user')

        catalog_text = load_catalog_text()
        exchange_rate = get_exchange_rate()

        system_prompt = f"""
Sos Fran, un vendedor humano de la empresa Tercom.
Vendés motopartes de forma amable y profesional, ayudando paso a paso al cliente.

Tenés acceso a este catálogo (puede tener errores o abreviaciones):
{catalog_text}

REGLAS:
- Si el cliente escribe con errores (ej: "amotrr", "onda", "110"), entendé el sentido.
- Buscá coincidencias aproximadas dentro del catálogo.
- Si un producto parece estar en otra marca, mencioná ambas opciones.
- Mostrá precios en pesos argentinos (ARS), calculando con tipo de cambio: {exchange_rate:.2f}.
- Si no encontrás coincidencia exacta, proponé similares.
- Nunca digas que sos una IA, siempre actuá como un vendedor humano.
"""

        history = get_conversation_history(from_number)
        messages = [{"role": "system", "content": system_prompt}]
        for msg, role in history:
            messages.append({"role": role, "content": msg})
        messages.append({"role": "user", "content": incoming_msg})

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.7,
            max_tokens=600
        )

        bot_reply = response.choices[0].message.content
        save_message(from_number, bot_reply, 'assistant')

        twilio_resp = MessagingResponse()
        twilio_resp.message(bot_reply)
        return str(twilio_resp)

    except Exception as e:
        print(f"Error general: {e}")
        resp = MessagingResponse()
        resp.message("Tuve un problema técnico, pero ya estoy revisando el catálogo. ¿Podés repetir tu consulta?")
        return str(resp)

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
