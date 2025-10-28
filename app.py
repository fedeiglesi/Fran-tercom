import os, json, csv, sqlite3, logging, time
from datetime import datetime
from threading import Lock
import numpy as np, faiss, requests
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
from openai import OpenAI

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

_catalog_cache, _index_cache = None, None
_catalog_lock = Lock()

# ------------------------------------------------------------
# BASE DE DATOS
# ------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT, message TEXT, response TEXT, timestamp TEXT
        )
    """)
    conn.commit(); conn.close()
init_db()

# ------------------------------------------------------------
# CATÁLOGO Y FAISS
# ------------------------------------------------------------
def load_catalog():
    catalog = []
    with open(CATALOG_PATH, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            catalog.append({
                "code": row.get("code", "").strip(),
                "name": row.get("name", "").strip(),
                "price_ars": row.get("price_ars", "").strip()
            })
    logger.info(f"Catálogo: {len(catalog)} productos cargados.")
    return catalog

def build_faiss_index(catalog):
    logger.info("Creando embeddings...")
    textos = [f"{c['code']} {c['name']}" for c in catalog]
    emb = []
    for i in range(0, len(textos), 100):
        r = client.embeddings.create(input=textos[i:i+100], model="text-embedding-3-small")
        emb += [d.embedding for d in r.data]
    mat = np.array(emb, dtype="float32")
    index = faiss.IndexFlatL2(mat.shape[1]); index.add(mat)
    logger.info(f"FAISS: {len(catalog)} vectores cargados.")
    return index

def get_catalog_and_index():
    global _catalog_cache, _index_cache
    with _catalog_lock:
        if _catalog_cache is None:
            _catalog_cache = load_catalog()
            _index_cache = build_faiss_index(_catalog_cache)
            logger.info("✅ Catálogo FAISS inicializado.")
        return _catalog_cache, _index_cache

def search_products(query: str, limit=8):
    cat, idx = get_catalog_and_index()
    try:
        emb = client.embeddings.create(input=query, model="text-embedding-3-small")
        qv = np.array(emb.data[0].embedding, dtype="float32")
        D, I = idx.search(np.array([qv]), limit)
        res = []
        for i in I[0]:
            if i < len(cat): res.append(cat[i])
        return res
    except Exception as e:
        logger.error(f"Error en búsqueda: {e}")
        return []

# ------------------------------------------------------------
# MOTOR DE RESPUESTA
# ------------------------------------------------------------
def ask_fran(user_input):
    # detección rápida de intención de búsqueda
    keywords = ["bujía", "faro", "cadena", "cable", "cubierta", "lampara", "kit", "embrague"]
    if any(k in user_input.lower() for k in keywords):
        results = search_products(user_input, 5)
        if results:
            reply = "Encontré algunas opciones que podrían servirte:\n"
            for r in results:
                reply += f"- ({r['code']}) {r['name']} – ${r['price_ars']} ARS\n"
            return reply
        else:
            return "No encontré ese producto exacto. ¿Podrías darme más detalles (marca o modelo)?"

    # si no es búsqueda, pasa por el modelo
    system_prompt = (
        "Sos Fran, asistente de ventas mayorista de Tercom. "
        "Sos amable, profesional y respondés siempre en español. "
        "Tu función es ayudar a encontrar repuestos de motos, confirmar precios o derivar a cotización."
    )

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input}
        ]
    )
    return response.choices[0].message.content.strip()

# ------------------------------------------------------------
# TWILIO
# ------------------------------------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    msg = request.values.get("Body", "").strip()
    user = request.values.get("From", "")
    logger.info(f"Mensaje: {msg}")

    try:
        ans = ask_fran(msg)
    except Exception as e:
        logger.error(f"Error en ask_fran: {e}")
        ans = "Hubo un problema al procesar tu solicitud. Estoy revisando el sistema."

    # guarda conversación
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO conversations (user, message, response, timestamp) VALUES (?,?,?,?)",
                 (user, msg, ans, datetime.now().isoformat()))
    conn.commit(); conn.close()

    tw = MessagingResponse()
    tw.message(ans)
    return str(tw)

# ------------------------------------------------------------
# INICIO
# ------------------------------------------------------------
if __name__ == "__main__":
    logger.info("Precargando catálogo...")
    get_catalog_and_index()
    app.run(host="0.0.0.0", port=5000)
