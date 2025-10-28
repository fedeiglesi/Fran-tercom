# ============================================================
# Fran – versión de prueba FAISS + Tool
# ============================================================

import os
import json
import csv
import sqlite3
import logging
import time
from datetime import datetime
from threading import Lock

import numpy as np
import faiss
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
from twilio.twiml.messaging_response import MessagingResponse

# ------------------------------------------------------------
# CONFIGURACIÓN GENERAL
# ------------------------------------------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
client = OpenAI(api_key=OPENAI_API_KEY)

CATALOG_PATH = "LISTA_TERCOM_LIMPIA.csv"
DB_PATH = "conversations.db"

_catalog_lock = Lock()
_catalog_cache = None
_index_cache = None

# ------------------------------------------------------------
# BASE DE DATOS DE CONVERSACIONES
# ------------------------------------------------------------
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT,
            message TEXT,
            response TEXT,
            timestamp TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ------------------------------------------------------------
# CARGA Y PROCESAMIENTO DEL CATÁLOGO
# ------------------------------------------------------------
def load_catalog():
    catalog = []
    with open(CATALOG_PATH, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            catalog.append({
                "code": row.get("code", "").strip(),
                "name": row.get("name", "").strip(),
                "price_ars": row.get("price_ars", "").strip()
            })
    logger.info(f"Catálogo: {len(catalog)} productos cargados.")
    return catalog

def build_faiss_index(catalog):
    logger.info("Construyendo índice FAISS...")
    texts = [f"{item['code']} {item['name']}" for item in catalog]
    embeddings = []
    for i in range(0, len(texts), 100):
        chunk = texts[i:i + 100]
        response = client.embeddings.create(
            input=chunk, model="text-embedding-3-small"
        )
        for d in response.data:
            embeddings.append(d.embedding)

    vectors = np.array(embeddings, dtype="float32")
    index = faiss.IndexFlatL2(vectors.shape[1])
    index.add(vectors)
    logger.info(f"FAISS: {len(catalog)} vectores creados.")
    return index

def get_catalog_and_index():
    global _catalog_cache, _index_cache
    with _catalog_lock:
        if _catalog_cache is None or _index_cache is None:
            catalog = load_catalog()
            index = build_faiss_index(catalog)
            _catalog_cache = catalog
            _index_cache = index
            logger.info("Catálogo precargado correctamente.")
        return _catalog_cache, _index_cache

# ------------------------------------------------------------
# FUNCIÓN TOOL: BÚSQUEDA EN CATÁLOGO
# ------------------------------------------------------------
def search_products(query: str, limit: int = 10):
    catalog, index = get_catalog_and_index()
    try:
        response = client.embeddings.create(
            input=query, model="text-embedding-3-small"
        )
        qv = np.array(response.data[0].embedding, dtype="float32")
        distances, indices = index.search(np.array([qv]), limit)
        results = []
        for i, idx in enumerate(indices[0]):
            if idx < len(catalog):
                item = catalog[idx]
                results.append({
                    "code": item["code"],
                    "name": item["name"],
                    "price_ars": item["price_ars"],
                    "distance": float(distances[0][i])
                })
        logger.info(f"Búsqueda FAISS completada: {len(results)} resultados.")
        return results
    except Exception as e:
        logger.error(f"Error en búsqueda FAISS: {e}")
        return []

# ------------------------------------------------------------
# TOOLS DISPONIBLES PARA EL ASISTENTE
# ------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "Busca productos en el catálogo interno de Tercom según la consulta del cliente (marca, modelo o tipo de repuesto).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Texto de búsqueda (por ejemplo 'bujía NGK' o 'faro Honda Wave')."
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Número máximo de resultados a devolver.",
                        "default": 10
                    }
                },
                "required": ["query"]
            }
        }
    }
]

# ------------------------------------------------------------
# ASISTENTE OPENAI
# ------------------------------------------------------------
def ask_fran(user_input: str):
    messages = [
        {"role": "system", "content": (
            "Sos Fran, vendedor de Tercom, empresa mayorista de motopartes. "
            "Tu estilo es profesional y cordial. Siempre respondé en español claro. "
            "Si el usuario menciona una marca, modelo o tipo de repuesto, usá la función search_products "
            "para buscar en el catálogo y devolver resultados con precios en ARS. "
            "Si no hay resultados, pedí más detalles del producto."
        )},
        {"role": "user", "content": user_input}
    ]

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        tools=TOOLS,
        tool_choice="auto"
    )

    message = response.choices[0].message

    # Si el modelo pidió usar una tool
    if hasattr(message, "tool_calls") and message.tool_calls:
        for tool_call in message.tool_calls:
            if tool_call.function.name == "search_products":
                args = json.loads(tool_call.function.arguments)
                query = args.get("query", "")
                limit = args.get("limit", 10)
                results = search_products(query, limit)
                if not results:
                    return "No encontré coincidencias exactas, ¿podrías especificar el modelo o marca?"
                reply = "He encontrado algunas opciones que podrían interesarte:\n"
                for r in results[:5]:
                    reply += f"- ({r['code']}) {r['name']} - ${r['price_ars']} ARS\n"
                return reply
    else:
        return message.content

# ------------------------------------------------------------
# TWILIO WEBHOOK
# ------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.values.get("Body", "").strip()
    from_number = request.values.get("From", "")
    logger.info(f"Mensaje recibido de {from_number}: {incoming_msg}")

    response_text = ask_fran(incoming_msg)
    logger.info(f"Respuesta generada: {response_text[:100]}...")

    conn = get_db_connection()
    conn.execute(
        "INSERT INTO conversations (user, message, response, timestamp) VALUES (?, ?, ?, ?)",
        (from_number, incoming_msg, response_text, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

    twilio_resp = MessagingResponse()
    twilio_resp.message(response_text)
    return str(twilio_resp)

# ------------------------------------------------------------
# INICIO DE LA APP
# ------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Precargando catálogo e índice FAISS...")
    get_catalog_and_index()
    app.run(host="0.0.0.0", port=5000)
