# ============================================================
# Fran 4.0 – Bot Mayorista Inteligente (Arquitectura NotebookLM)
# ============================================================
# Este sistema utiliza:
# - Google Vertex AI (Gemini 2.0 Pro/Flash)
# - Vector Search (similar a embeddings persistentes de NotebookLM)
# - Recuperación semántica avanzada
# - Carrito inteligente con pending actions
# - Corrección contextual automática
# - Conversación fluida como ChatGPT
# - Integración con Twilio WhatsApp
# - Servidor Flask para Railway
# ============================================================

import os
import json
import sqlite3
import logging
import re
from flask import Flask, request, Response
from datetime import datetime
from google.cloud import aiplatform

# ==============================================
# CONFIGURACIÓN: GOOGLE VERTEX AI
# ==============================================
PROJECT_ID = os.getenv("GOOGLE_PROJECT_ID")
REGION = os.getenv("GOOGLE_REGION", "us-central1")
EMBED_MODEL = "text-embedding-004"
CHAT_MODEL = "gemini-2.0-pro"
INDEX_ID = os.getenv("CATALOGO_INDEX_ID")

aiplatform.init(project=PROJECT_ID, location=REGION)

retriever = aiplatform.MatchingEngineIndexEndpoint(
    index_endpoint_name=INDEX_ID,
    deployed_index_id="catalogo-deploy"
)

# ==============================================
# BASE DE DATOS LOCAL PARA CONVERSACIONES + CARRITO
# ==============================================
DB_PATH = "fran.db"

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

# ==============================================
# UTILIDADES DE LOGICA
# ==============================================

def emb(text):
    """Generar embedding usando Vertex AI."""
    from vertexai.language_models import TextEmbeddingModel
    model = TextEmbeddingModel.from_pretrained(EMBED_MODEL)
    r = model.get_embeddings([text])
    return r[0].values


def retrieve_catalog_results(query, top_k=10):
    """Busca productos relevantes utilizando Vector Search (NotebookLM style)."""
    vector = emb(query)
    results = retriever.match(
        queries=[vector], num_neighbors=top_k
    )

    productos = []
    for r in results[0]:
        metadata = json.loads(r["metadata"])
        productos.append({
            "code": metadata["code"],
            "family": metadata["family"],
            "brand_model": metadata["brand_model"],
            "description": metadata["description"],
            "price_usd": metadata["price_usd"]
        })

    return productos


def save_message(user, msg):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO conversation (user, message, timestamp) VALUES (?, ?, ?)",
                 (user, msg, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def add_to_cart(user, code, qty):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO cart (user, code, qty, timestamp) VALUES (?, ?, ?, ?)",
                 (user, code, qty, datetime.now().isoformat()))
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


# ==============================================
# MODELO DE CHAT (Gemini 2.0)
# ==============================================
from vertexai.generative_models import GenerativeModel

chat_model = GenerativeModel(model_name=CHAT_MODEL)

SYSTEM_PROMPT = """
Sos *Fran 4.0*, un vendedor mayorista profesional de repuestos para motos.
Tenés acceso a un Catálogo cargado en un índice semántico (como NotebookLM).
Reglas:
1. Siempre respondé en tono humano, amable, argentino.
2. Entendé abreviaciones: amot = amortiguador; tras = trasero; del = delantero; reg = registro.
3. Entendé “2 de cada”, “todos x5”, “3 más de los anteriores”.
4. Gestioná un carrito interno: agregar, sacar, cambiar cantidades.
5. Si la búsqueda es genérica (“tengo honda wave 110”), recomendá los más vendidos.
6. Si no hay coincidencia exacta, sugerí alternativas.
7. Mostrá el resultado así:
   - Código
   - Descripción limpia
   - Precio USD
   - Compatibilidad (marca/modelo)
8. Llevá contexto conversacional real como ChatGPT.
9. NUNCA inventes códigos que no existen en el catálogo indexado.
"""

# ==============================================
# SERVIDOR FLASK – WHATSAPP
# ==============================================
app = Flask(__name__)

@app.route("/sms", methods=["POST"])
def sms_reply():
    user = request.form.get("From")
    msg = request.form.get("Body", "").strip()

    save_message(user, msg)

    # ---- BÚSQUEDA SEMÁNTICA ----
    productos = retrieve_catalog_results(msg, top_k=12)

    # ---- CONTEXTO COMPLETO PARA GEMINI ----
    context = {
        "user_query": msg,
        "productos": productos,
        "carrito": get_cart(user)
    }

    prompt = SYSTEM_PROMPT + "\n\nContexto:\n" + json.dumps(context, ensure_ascii=False, indent=2)

    # ---- RESPUESTA DEL LLM ----
    response = chat_model.generate_content(prompt)
    answer = response.text

    # ---- PROCESAR ACCIONES DETECTADAS ----
    # (Fran indica en texto lo que debe hacerse. Ej: "agregar 3 unidades de 1333/00040")
    acciones = re.findall(r"AGREGAR\s+(\d+)\s+UNIDADES\s+DE\s+([\w/.-]+)", answer.upper())
    for qty, code in acciones:
        add_to_cart(user, code, int(qty))

    borrar = re.findall(r"ELIMINAR\s+PRODUCTO\s+([\w/.-]+)", answer.upper())
    for code in borrar:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM cart WHERE user=? AND code=?", (user, code))
        conn.commit()
        conn.close()

    # ---- ENVÍO A WHATSAPP ----
    from twilio.twiml.messaging_response import MessagingResponse
    resp = MessagingResponse()
    resp.message(answer)
    return Response(str(resp), mimetype="application/xml")


# ==============================================
# MAIN
# ==============================================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
