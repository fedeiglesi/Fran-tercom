# coding: utf-8

"""
Modulo: ai.py (VERSION UNIFICADA + SOPORTE PARA CATALOGO)
Interfaz entre Fran 3.8 y OpenAI (modelo GPT)

Funciones:

- Generar respuestas empaticas o explicativas
- Detectar intencion del usuario
- Respuestas rule-based rapidas
- Responder consultas tecnicas usando SOLO el catalogo
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
# VALIDACION Y CLIENTE GLOBAL
# =========================================================

if not OPENAI_API_KEY:
    logger.error("❌ OPENAI_API_KEY no configurada. Las funciones de IA no funcionaran.")
    client = None
else:
    client = OpenAI(api_key=OPENAI_API_KEY)

# =========================================================
# PROMPT BASE DEL SISTEMA
# =========================================================

SMART_SYSTEM_PROMPT = """
Sos Fran, vendedor experto en repuestos y accesorios para motos.

Tu personalidad:
- Tecnico pero accesible (explicas simple, sin condescender)
- Proactivo y consultivo (anticipas lo que el cliente puede necesitar)
- Profesional pero cercano (argentino neutral)
- Honesto: si no sabes algo o no lo tenes, lo decis claramente.

Reglas criticas:
❌ Nunca inventes codigos ni precios.
✅ Siempre basate en el catalogo que se te paso.
✅ Si no encontras el producto, ofrece alternativas o pedi mas detalles.
✅ Usa emojis moderadamente (🔍, ✅, 📦, 💬).
✅ Oraciones cortas, tono cordial argentino.
"""

# =========================================================
# GENERADOR DE RESPUESTAS
# =========================================================

def generate_llm_reply(phone: str, user_message: str, structured_data: Optional[Dict[str, str]] = None) -> str:
    """
    Usa el modelo GPT para generar una respuesta amigable.
    Puede reformular resultados del catalogo o responder consultas directas.
    """
    if not client:
        logger.warning("⚠️ OpenAI client no disponible")
        return "⚠️ El sistema de IA no esta disponible en este momento."

    try:
        if structured_data:
            structured_text = json.dumps(structured_data, ensure_ascii=False, indent=2)
            user_message = f"Estos son los datos que obtuve:\n{structured_text}\n\nRedacta una respuesta clara para el cliente."

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
        logger.warning("⚠️ Limite de uso OpenAI alcanzado, reintentando en 5s...")
        time.sleep(5)
        return generate_llm_reply(phone, user_message, structured_data)

    except Exception as e:
        logger.error(f"❌ Error en generate_llm_reply: {e}")
        return "⚠️ Estoy teniendo un problema para responder ahora. Intenta de nuevo en unos segundos."

# =========================================================
# DETECTOR DE INTENCION (RULE-BASED)
# =========================================================

def detect_intent(message: str) -> str:
    """
    Detecta la intencion principal del usuario de manera simple (sin IA).
    Usado para elegir ruta logica antes de llamar al modelo.
    """
    msg = message.lower()

    if any(x in msg for x in ["lista", "cotiza", "presupuesto", "bulk"]) or "\n" in message:
        return "bulk_quote"

    if any(x in msg for x in ["carrito", "mi pedido", "total", "agrega", "saca", "vaciar"]):
        return "cart"

    if any(x in msg for x in ["precio", "cuanto", "vale", "tienen", "stock", "buscar", "tapa", "valvula", "filtro", "cadena", "bulbo"]):
        return "search"

    if any(x in msg for x in ["hola", "buenas", "buen dia", "buenos dias"]):
        return "greeting"

    if any(x in msg for x in ["gracias", "ok", "dale", "perfecto"]):
        return "thanks"

    if any(x in msg for x in ["chau", "adios", "nos vemos", "hasta luego"]):
        return "goodbye"

    return "unknown"

# =========================================================
# RESPUESTAS AUTOMATICAS SEGUN INTENCION
# =========================================================

def rule_based_reply(intent: str, message: str) -> Optional[str]:
    """Responde mensajes simples sin usar IA (mas rapido)."""

    if intent == "greeting":
        return "Hola! Soy Fran 👋, vendedor de repuestos. Que producto necesitas cotizar hoy?"

    if intent == "thanks":
        return "De nada! 😊 Necesitas algo mas?"

    if intent == "goodbye":
        return "Gracias por tu consulta! Si necesitas algo mas, escribime cuando quieras."

    if intent == "cart":
        return None

    if intent == "unknown":
        return None

    return None

# =========================================================
# RESPUESTA CON PRODUCTOS DEL CATALOGO
# =========================================================

def generate_product_based_reply(phone: str, query: str, products: List[Dict[str, str]] = None) -> str:
    """
    Responde preguntas tecnicas usando SOLO productos del catalogo.
    Usa la personalidad de Fran y evita alucinaciones.
    """
    if products is None:
        from fran.catalog import get_relevant_products_for_query
        products = get_relevant_products_for_query(query, top_k=10)

    if not products:
        return generate_llm_reply(
            phone=phone,
            user_message=f"El usuario pregunto: '{query}'. Pero NO se encontro ningun producto relacionado en el catalogo. Responde como Fran: amable, tecnico, y sin inventar."
        )

    context_lines = []
    for p in products:
        line = f"- Codigo: {p['code']} | Nombre: {p['name']}"
        if p.get("models"):
            line += f" | Compatible con: {p['models']}"
        if p.get("brand"):
            line += f" | Marca: {p['brand']}"
        context_lines.append(line)
    context = "\n".join(context_lines)

    user_prompt = f"""El cliente pregunto: "{query}"

Productos disponibles (reales, del catalogo):
{context}

Redacta una respuesta tecnica, util y empatica como Fran.
- Si hay un producto claramente compatible, destacalo.
- Si hay varios, mostra las mejores opciones.
- Si no estas seguro, pedi mas datos (modelo exacto, año, etc.).
- Nunca inventes informacion fuera de esta lista.
"""
    return generate_llm_reply(phone=phone, user_message=user_prompt)

# =========================================================
# PREPARACION PARA LOGS
# =========================================================

def summarize_message_for_log(phone: str, user_message: str, intent: str, response: str) -> str:
    """
    Genera un resumen compacto de interaccion para logging.
    """
    user_preview = ellipsis(user_message, 50)
    response_preview = ellipsis(response, 80)
    return f"{phone} | {intent} | U: {user_preview} → R: {response_preview}"
