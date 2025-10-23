import os
import json
import sqlite3
import requests
import logging
from datetime import datetime
from flask import Flask, request, jsonify
from twilio.twiml.messaging_response import MessagingResponse
import openai
from memory_vector import add_memory, query_memory

# --- CONFIGURACIÓN BASE ---
app = Flask(__name__)

openai.api_key = os.environ.get("OPENAI_API_KEY")
CATALOG_URL = os.environ.get("CATALOG_URL", "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/main/LISTA_TERCOM_LIMPIA.csv")
EXCHANGE_API_URL = os.environ.get("EXCHANGE_API_URL", "https://dolarapi.com/v1/dolares/oficial")

# --- LOGGING ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Fran")

# --- BASE DE DATOS ---
def init_db():
    conn = sqlite3.connect("tercom.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS conversations
                 (phone TEXT, message TEXT, role TEXT, timestamp TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS carts
                 (phone TEXT, product_code TEXT, quantity INTEGER, 
                  product_name TEXT, price_usd REAL, price_ars REAL)''')
    conn.commit()
    conn.close()

init_db()

def save_message(phone, message, role):
    conn = sqlite3.connect("tercom.db")
    c = conn.cursor()
    c.execute("INSERT INTO conversations VALUES (?, ?, ?, ?)", 
              (phone, message, role, datetime.now().isoformat()))
    conn.commit()
    conn.close()

def get_conversation_history(phone, limit=10):
    conn = sqlite3.connect("tercom.db")
    c = conn.cursor()
    c.execute("SELECT message, role FROM conversations WHERE phone = ? ORDER BY timestamp DESC LIMIT ?", (phone, limit))
    data = list(reversed(c.fetchall()))
    conn.close()
    return data

# --- CARGA DE CATÁLOGO ---
def load_catalog():
    try:
        response = requests.get(CATALOG_URL, timeout=10)
        lines = response.text.strip().split("\n")
        data = []
        for i, line in enumerate(lines[1:]):
            parts = line.split(",")
            if len(parts) >= 3:
                code = parts[0].strip()
                name = parts[1].strip()
                price = parts[2].strip()
                data.append({"code": code, "name": name, "price": price})
        return data
    except Exception as e:
        logger.error(f"Error cargando catálogo: {e}")
        return []

# --- PROMPT BASE ---
def load_prompt():
    try:
        with open("prompt_fran.txt", "r", encoding="utf-8") as f:
            return f.read()
    except:
        return "Sos Fran, el agente de ventas de Tercom. Respondé como una persona real."

# --- WHATSAPP WEBHOOK ---
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        incoming_msg = request.values.get("Body", "").strip()
        from_number = request.values.get("From", "")
        save_message(from_number, incoming_msg, "user")

        catalog = load_catalog()
        base_prompt = load_prompt()
        history = get_conversation_history(from_number)
        context_memory = query_memory(from_number, incoming_msg)
        add_memory(from_number, incoming_msg)

        messages = [{"role": "system", "content": base_prompt}]
        if context_memory:
            messages.append({"role": "system", "content": "\n".join(context_memory[-5:])})
        for msg, role in history:
            messages.append({"role": role, "content": msg})
        messages.append({"role": "user", "content": incoming_msg})

        response = openai.ChatCompletion.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.7,
            max_tokens=600
        )

        reply = response["choices"][0]["message"]["content"]
        save_message(from_number, reply, "assistant")

        resp = MessagingResponse()
        resp.message(reply)
        return str(resp)

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        resp = MessagingResponse()
        resp.message("Disculpá, tuve un problema técnico. ¿Podés repetir tu consulta?")
        return str(resp)

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now().isoformat()})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
