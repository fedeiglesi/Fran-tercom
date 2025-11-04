# coding: utf-8

"""
Módulo: ai.py (VERSIÓN UNIFICADA Y CORREGIDA)
Interfaz entre Fran 3.8 y OpenAI (modelo GPT)

Funciones:

- Generar respuestas empáticas o explicativas
- Detectar intención del usuario
- Respuestas rule-based rápidas
- Manejo de errores y rate limits
"""

import time
import json
import re
from typing import Dict, Optional
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
        # Si viene info estructurada (por ej. cotización), la pasamos en formato legible
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
    Detecta la intención principal del usuario de manera simple (sin IA).
    Usado para elegir ruta lógica antes de llamar al modelo.
    """
    msg = message.lower()

    # Búsquedas masivas (prioridad alta)
    if any(x in msg for x in ["lista", "cotiza", "presupuesto", "bulk"]) or "\n" in message:
        return "bulk_quote"

    # Carrito
    if any(x in msg for x in ["carrito", "mi pedido", "total", "agregá", "sacá", "vaciar"]):
        return "cart"

    # Búsqueda de productos
    if any(x in msg for x in ["precio", "cuánto", "vale", "tienen", "stock", "buscar", "tapa", "valvula", "filtro", "cadena", "bulbo"]):
        return "search"

    # Saludos
    if any(x in msg for x in ["hola", "buenas", "buen día", "buenos días"]):
        return "greeting"

    # Agradecimientos
    if any(x in msg for x in ["gracias", "ok", "dale", "perfecto"]):
        return "thanks"

    # Despedidas
    if any(x in msg for x in ["chau", "adiós", "nos vemos", "hasta luego"]):
        return "goodbye"

    return "unknown"

# =========================================================
# RESPUESTAS AUTOMÁTICAS SEGÚN INTENCIÓN
# =========================================================

def rule_based_reply(intent: str, message: str) -> Optional[str]:
    """Responde mensajes simples sin usar IA (más rápido)."""

    if intent == "greeting":
        return "¡Hola! Soy Fran 👋, vendedor de repuestos. ¿Qué producto necesitás cotizar hoy?"

    if intent == "thanks":
        return "¡De nada! 😊 ¿Necesitás algo más?"

    if intent == "goodbye":
        return "¡Gracias por tu consulta! Si necesitás algo más, escribime cuando quieras."

    if intent == "cart":
        return None  # Se maneja en el módulo cart

    if intent == "unknown":
        return None  # Se derivará a IA

    return None

# =========================================================
# PREPARACIÓN DE MENSAJE PARA LOGS / DEBUG
# =========================================================

def summarize_message_for_log(phone: str, user_message: str, intent: str, response: str) -> str:
    """
    Genera un resumen compacto de interacción para logging.
    """
    user_preview = ellipsis(user_message, 50)
    response_preview = ellipsis(response, 80)
    return f"{phone} | {intent} | U: {user_preview} → R: {response_preview}"
