# ============================================================
# Fran 4.0 – Integración con Google Vertex AI (Estilo NotebookLM)
# Proyecto: Fran-google-1.0
# ============================================================

import os
import json
import sqlite3
import logging
import re
from flask import Flask, request, Response
from datetime import datetime

from config import (
    PROJECT_ID,
    REGION,
    EMBED_MODEL,
    CHAT_MODEL,
    INDEX_ENDPOINT_ID,
    DEPLOYED_INDEX_ID,
    DB_PATH
)

from google.cloud import aiplatform
from vertexai.generative_models import GenerativeModel
from vertexai.language_models import TextEmbeddingModel
from twilio.twiml.messaging_response import MessagingResponse

# ------------------------------------------
# LOGGING
# ------------------------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fran-google")

# ------------------------------------------
# GOOGLE VERTEX AI INIT
# ------------------------------------------
aiplatform.init(project=PROJECT_ID, location=REGION)

index_endpoint = aiplatform.MatchingEngineIndexEndpoint(
    index_endpoint_name=INDEX_ENDPOINT_ID
)

# ------------------------------------------
# BASE DE DATOS LOCAL – CARRITO + HISTORIAL
# ------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS conversation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT,
            message TEXT,
            timestamp TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS cart (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT,
            code TEXT,
            qty INTEGER,
            timestamp TEXT
        )
    """)

    conn.commit()
    conn.close()

init_db()

# ------------------------------------------
# UTILIDADES
# ------------------------------------------
def save_message(user, msg):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO conversation (user, message, timestamp) VALUES (?, ?, ?)",
        (user, msg, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def add_to_cart(user, code, qty):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO cart (user, code, qty, timestamp) VALUES (?, ?, ?, ?)",
        (user, code, qty, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def get_cart(user):
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT code, qty FROM cart WHERE user=?", (user,)).fetchall()
    conn.close()
    return [{"code": r[0], "qty": r[1]} for r in rows]

def clear_cart(user):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM cart WHERE user=?", (user,))
    conn.commit()
    conn.close()

# ------------------------------------------
# EMBEDDINGS
# ------------------------------------------
def embed(text):
    model = TextEmbeddingModel.from_pretrained(EMBED_MODEL)
    r = model.get_embeddings([text])
    return r[0].values

# ------------------------------------------
# RETRIEVER (Vector Search)
# ------------------------------------------
def search_catalog(query, top_k=12):
    log.info(f"Searching catalog for query: {query}")
    vector = embed(query)

    results = index_endpoint.match(
        queries=[vector],
        num_neighbors=top_k,
        deployed_index_id=DEPLOYED_INDEX_ID
    )

    productos = []
    for r in results[0]:
        metadata = json.loads(r["metadata"])
        productos.append(metadata)

    return productos

# ------------------------------------------
# MODELO DE CHAT
# ------------------------------------------
chat_model = GenerativeModel(model_name=CHAT_MODEL)

with open("prompt_fran.txt", "r") as f:
    SYSTEM_PROMPT = f.read()

# ------------------------------------------
# FLASK – TWILIO
# ------------------------------------------
app = Flask(__name__)

@app.route("/sms", methods=["POST"])
def sms_reply():
    user = request.form.get("From")
    msg = request.form.get("Body", "").strip()

    save_message(user, msg)

    productos = search_catalog(msg, top_k=12)

    context = {
        "user_query": msg,
        "productos": productos,
        "carrito": get_cart(user)
    }

    prompt = SYSTEM_PROMPT + "\n\nContexto:\n" + json.dumps(context, ensure_ascii=False, indent=2)

    response = chat_model.generate_content(prompt)
    answer = response.text

    # Detectar acciones
    acciones = re.findall(r"AGREGAR\s+(\d+)\s+UNIDADES\s+DE\s+([\w/.-]+)", answer.upper())
    for qty, code in acciones:
        add_to_cart(user, code, int(qty))

    eliminar = re.findall(r"ELIMINAR\s+PRODUCTO\s+([\w/.-]+)", answer.upper())
    for code in eliminar:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM cart WHERE user=? AND code=?", (user, code))
        conn.commit()
        conn.close()

    resp = MessagingResponse()
    resp.message(answer)
    return Response(str(resp), mimetype="application/xml")

# ------------------------------------------
# MAIN
# ------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
