# coding: utf-8

"""
Módulo: ai.py (VERSIÓN UNIFICADA + SOPORTE PARA CATÁLOGO)
Interfaz entre Fran 3.8 y OpenAI (modelo GPT)

Funciones:

- Generar respuestas empáticas o explicativas
- Detectar intención del usuario
- Respuestas rule-based rápidas
- Responder consultas técnicas usando SOLO el catálogo
- Manejo de errores y rate limits
"""

import time
import json
import re
from typing import Dict, List, Optional
from openai import OpenAI, RateLimitError
from fran.config import OPENAI_API_KEY, MODEL_NAME, logger
from fran.utils import ellipsis

# =========================================================
# VALIDACIÓN Y CLIENTE GLOBAL
# =========================================================

if not OPENAI_API_KEY:
    logger.error("❌ OPENAI_API_KEY no configurada. Las funciones de IA no funcionarán.")
    client = None
else:
    client = OpenAI(api_key=OPENAI_API_KEY)

# =========================================================
# PROMPT BASE DEL SISTEMA
# =========================================================

SMART_SYSTEM_PROMPT = """
Sos Fran, vendedor experto en repuestos y accesorios para motos.

Tu personalidad:
- Técnico pero accesible (explicás simple, sin condescender)
- Proactivo y consultivo (anticipás lo que el cliente puede necesitar)
- Profesional pero cercano (argentino neutral)
- Honesto: si no sabés algo o no lo tenés, lo decís claramente.

Reglas críticas:
❌ Nunca inventes códigos ni precios.
✅ Siempre basate en el catálogo que se te pasó.
✅ Si no encontrás el producto, ofrecé alternativas o pedí más detalles.
✅ Usá emojis moderadamente (🔍, ✅, 📦, 💬).
✅ Oraciones cortas, tono cordial argentino.
"""

# =========================================================
# GENERADOR DE RESPUESTAS
# =========================================================

def generate_llm_reply(phone: str, user_message: str, structured_data: Optional[Dict[str, str]] = None) -> str:
    """
    Usa el modelo GPT para generar una respuesta amigable.
    Puede reformular resultados del catálogo o responder consultas directas.
    """
    if not client:
        logger.warning("⚠️ OpenAI client no disponible")
        return "⚠️ El sistema de IA no está disponible en este momento."

    try:
        if structured_data:
            structured_text = json.dumps(structured_data, ensure_ascii=False, indent=2)
            user_message = f"Estos son los datos que obtuve:\n{structured_text}\n\nRedactá una respuesta clara para el cliente."

        start_time = time.time()
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": SMART_SYSTEM_PROMPT},
                {"role": "user", "content": user_message}
            ],
            temperature=0.7,
            max_tokens=400,
        )
        reply = response.choices[0].message.content.strip()
        duration = round(time.time() - start_time, 2)
        logger.info(f"🤖 Respuesta IA generada en {duration}s ({len(reply)} chars)")
        return reply

    except RateLimitError:
        logger.warning("⚠️ Límite de uso OpenAI alcanzado, reintentando en 5s...")
        time.sleep(5)
        return generate_llm_reply(phone, user_message, structured_data)

    except Exception as e:
        logger.error(f"❌ Error en generate_llm_reply: {e}")
        return "⚠️ Estoy teniendo un problema para responder ahora. Intentá de nuevo en unos segundos."

# =========================================================
# DETECTOR DE INTENCIÓN (RULE-BASED)
# =========================================================

def detect_intent(message: str) -> str:
    """
    Detecta la intención principal del usuario.
    PRIORIDAD: búsqueda técnica > saludo > otros
    """
    msg = message.lower().strip()

    technical_terms = [
        "batería", "bateria", "amortiguador", "filtro", "tapa", "valvula", "cadena",
        "bulbo", "aceite", "carter", "embrague", "piston", "camisa", "precio",
        "cuánto", "vale", "tienen", "stock", "compatible", "modelo", "año",
        "2010", "2011", "2012", "2013", "2014", "2015", "2016", "2017", "2018",
        "2019", "2020", "2021", "2022", "2023", "2024", "2025"
    ]
    if any(term in msg for term in technical_terms):
        return "search"

    if any(x in msg for x in ["lista", "cotiza", "presupuesto", "bulk"]) or "\n" in message:
        return "bulk_quote"

    if any(x in msg for x in ["carrito", "mi pedido", "total", "agregá", "sacá", "vaciar"]):
        return "cart"

    if any(x in msg for x in ["hola", "buenas", "buen día", "buenos días"]):
        return "greeting"

    if any(x in msg for x in ["gracias", "ok", "dale", "perfecto"]):
        return "thanks"

    if any(x in msg for x in ["chau", "adiós", "nos vemos", "hasta luego"]):
        return "goodbye"

    return "unknown"

# =========================================================
# RESPUESTAS AUTOMÁTICAS SEGÚN INTENCIÓN
# =========================================================

def rule_based_reply(intent: str, message: str) -> Optional[str]:
    if intent == "greeting":
        return "¡Hola! Soy Fran 👋, vendedor de repuestos. ¿Qué producto necesitás cotizar hoy?"

    if intent == "thanks":
        return "¡De nada! 😊 ¿Necesitás algo más?"

    if intent == "goodbye":
        return "¡Gracias por tu consulta! Si necesitás algo más, escribime cuando quieras."

    return None

# =========================================================
# RESPUESTA CON PRODUCTOS DEL CATÁLOGO
# =========================================================

def generate_product_based_reply(phone: str, query: str, products: List[Dict[str, str]] = None) -> str:
    if products is None:
        from fran.search import search_products
        products = search_products(query, top_k=10)

    if not products:
        return generate_llm_reply(
            phone=phone,
            user_message=f"El usuario preguntó: '{query}'. Pero NO se encontró ningún producto relacionado en el catálogo. Responde como Fran: amable, técnico, y sin inventar."
        )

    context_lines = []
    for p in products:
        line = f"- Código: {p['code']} | Nombre: {p['name']}"
        if p.get("models"):
            line += f" | Compatible con: {p['models']}"
        if p.get("brand"):
            line += f" | Marca: {p['brand']}"
        context_lines.append(line)

    context = "\n".join(context_lines)

    user_prompt = f"""El cliente preguntó: "{query}"

Productos disponibles (reales, del catálogo):
{context}

Redactá una respuesta técnica, útil y empática como Fran.
- Si hay un producto claramente compatible, destacadlo.
- Si hay varios, mostrá las mejores opciones.
- Si no estás seguro, pedí más datos (modelo exacto, año, etc.).
- Nunca inventes información fuera de esta lista.
"""
    return generate_llm_reply(phone=phone, user_message=user_prompt)

# =========================================================
# MEMORIA CONVERSACIONAL PROGRESIVA
# =========================================================

def get_conversation_summary(phone: str) -> str:
    from fran.db import get_db_connection
    with get_db_connection() as conn:
        cur = conn.execute("SELECT summary FROM conversation_summary WHERE phone = ?", (phone,))
        row = cur.fetchone()
        return row["summary"] if row and row["summary"] else ""

def update_conversation_summary(phone: str, user_message: str, bot_reply: str):
    if not client:
        return

    current_summary = get_conversation_summary(phone)
    new_exchange = f"Usuario: {user_message}\nFran: {bot_reply}"

    prompt = f"""Actualizá el resumen de la conversación con el nuevo intercambio.
- Mantenelo breve (1-2 oraciones).
- Incluí solo información relevante: productos, precios, decisiones, dudas técnicas.
- Usá tercera persona y lenguaje neutral.
- Si el intercambio es un saludo o despedida, mantené el resumen anterior.

Resumen actual:
{current_summary}

Nuevo intercambio:
{new_exchange}

Resumen actualizado:"""

    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=120,
            timeout=5
        )
        new_summary = response.choices[0].message.content.strip()

        from fran.db import get_db_connection
        with get_db_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO conversation_summary (phone, summary, updated_at)
                VALUES (?, ?, datetime('now'))
            """, (phone, new_summary))
            conn.commit()
    except Exception as e:
        logger.warning(f"⚠️ No se pudo actualizar resumen para {phone}: {e}")

# =========================================================
# LOGGING
# =========================================================

def summarize_message_for_log(phone: str, user_message: str, intent: str, response: str) -> str:
    user_preview = ellipsis(user_message, 50)
    response_preview = ellipsis(response, 80)
    return f"{phone} | {intent} | U: {user_preview} → R: {response_preview}"
