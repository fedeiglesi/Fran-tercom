# coding: utf-8
"""
Módulo: ai.py
Lógica de inteligencia artificial y respuestas del asistente Fran 3.8
"""

import re
import time
from openai import OpenAI
from fran.config import MODEL_NAME, logger
from fran.search import search_products

client = OpenAI()

# =========================================================
# PROMPT BASE (IA EMPÁTICA)
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
"""

# =========================================================
# INTENT DETECTION
# =========================================================
def detect_intent(user_message: str) -> str:
    text = user_message.lower()
    if any(k in text for k in ["carrito", "total", "pedido"]):
        return "cart"
    if any(k in text for k in ["cotizar", "precio", "cuánto", "vale"]):
        return "quote"
    if any(k in text for k in ["hola", "buenas", "gracias", "chau", "adiós"]):
        return "greeting"
    if any(k in text for k in ["lista", "cantidad", "códigos", "bulk"]):
        return "bulk_quote"
    if any(k in text for k in ["buscar", "tapa", "valvula", "filtro", "cadena", "bulbo"]):
        return "search"
    return "general"

# =========================================================
# RULE-BASED RESPONSES
# =========================================================
def rule_based_reply(intent: str, msg: str) -> str:
    if intent == "greeting":
        if "gracias" in msg.lower():
            return "¡De nada! 😊 ¿Querés que te ayude con otro repuesto?"
        return "¡Hola! 👋 Soy Fran. Mandame el nombre o código del producto y te paso precio enseguida."
    if intent == "cart":
        return "🛒 Mostrame qué productos querés y te armo el total."
    return ""

# =========================================================
# IA GENERAL (LLM)
# =========================================================
def generate_llm_reply(phone: str, user_message: str) -> str:
    """Genera una respuesta contextual usando OpenAI."""
    try:
        logger.info(f"🤖 LLM Generando respuesta para {phone}...")
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": SMART_SYSTEM_PROMPT},
                {"role": "user", "content": user_message}
            ],
            temperature=0.5,
            max_tokens=250
        )
        reply = response.choices[0].message.content.strip()
        return reply
    except Exception as e:
        logger.error(f"❌ Error en generate_llm_reply: {e}")
        return "Tuve un problema para responder esa consulta, ¿podés repetirla?"

# =========================================================
# IA CON CONTEXTO ENCATENADO
# =========================================================
def generate_smart_ai_reply(user_message: str, context: str, products: list) -> str:
    """
    Variante más empática con contexto extendido.
    Se usa en versiones avanzadas o futuras.
    """
    try:
        message_context = context or "Cliente pide cotización o información general."
        prompt = f"""
{SMART_SYSTEM_PROMPT}

Contexto adicional:
{message_context}

Productos encontrados:
{products}

Mensaje del cliente:
{user_message}
"""
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": SMART_SYSTEM_PROMPT},
                {"role": "user", "content": prompt}
            ],
            temperature=0.6,
            max_tokens=300
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"❌ Error en generate_smart_ai_reply: {e}")
        return "Estoy teniendo un problema para analizar el pedido. Probá de nuevo."

# =========================================================
# LOG SUMMARY
# =========================================================
def summarize_message_for_log(phone, msg, intent, reply):
    short = re.sub(r"\s+", " ", msg)[:60]
    return f"[{phone}] INTENT={intent} → {short} → RESP={reply[:60]}"
