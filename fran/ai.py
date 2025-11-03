# coding: utf-8
"""
Módulo: ai.py
Interfaz entre Fran 3.8 y OpenAI (modelo GPT)

Funciones:
- Generar respuestas empáticas o explicativas
- Reescribir texto para enviar al cliente
- Manejar errores y rate limits
"""

import time
import json
from typing import Dict, Optional
from openai import OpenAI, RateLimitError
from fran.config import OPENAI_API_KEY, MODEL_NAME, logger
from fran.utils import ellipsis

# =========================================================
# CLIENTE GLOBAL
# =========================================================

client = OpenAI(api_key=OPENAI_API_KEY)


# =========================================================
# GENERADOR DE RESPUESTAS
# =========================================================

def generate_llm_reply(phone: str, user_message: str, structured_data: Optional[Dict[str, str]] = None) -> str:
    """
    Usa el modelo GPT para generar una respuesta amigable.
    Puede reformular resultados del catálogo o responder consultas directas.
    """
    try:
        # Preparamos el contexto base
        system_prompt = (
            "Sos Fran, vendedor experto en repuestos y accesorios para motos. "
            "Respondés con precisión, sin inventar precios, y mantenés un tono humano, técnico y claro. "
            "Usás oraciones cortas, tono cordial argentino y sin emojis repetitivos."
        )

        # Si viene info estructurada (por ej. cotización), la pasamos en formato legible
        if structured_data:
            structured_text = json.dumps(structured_data, ensure_ascii=False, indent=2)
            user_message = f"Estos son los datos que obtuve:\n{structured_text}\n\nRedactá una respuesta clara para el cliente."

        start_time = time.time()
        response = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": system_prompt},
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

    if any(x in msg for x in ["lista", "\n", "cotiza", "presupuesto", "bulk"]):
        return "bulk_quote"
    if any(x in msg for x in ["carrito", "mi pedido", "total", "agregá", "sacá", "vaciar"]):
        return "cart"
    if any(x in msg for x in ["precio", "cuánto", "vale", "tienen", "stock"]):
        return "search"
    if any(x in msg for x in ["hola", "buenas", "gracias", "ok", "dale", "perfecto"]):
        return "greeting"
    if any(x in msg for x in ["chau", "gracias", "nos vemos"]):
        return "goodbye"
    return "unknown"


# =========================================================
# RESPUESTAS AUTOMÁTICAS SEGÚN INTENCIÓN
# =========================================================

def rule_based_reply(intent: str, message: str) -> Optional[str]:
    """Responde mensajes simples sin usar IA (más rápido)."""
    if intent == "greeting":
        return "¡Hola! Soy Fran 👋, vendedor de repuestos. ¿Qué producto querés cotizar hoy?"
    if intent == "goodbye":
        return "¡Gracias por tu consulta! Si necesitás algo más, escribime cuando quieras."
    if intent == "cart":
        return "Podés decirme *vaciar carrito*, *mostrar carrito* o *agregar producto* para gestionarlo."
    if intent == "unknown":
        return None
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
