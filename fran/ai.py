# coding: utf-8

"""
Módulo: ai.py (VERSIÓN CON TIMEOUT OPTIMIZADO)
Interfaz entre Fran 3.8 y OpenAI (modelo GPT)

MEJORAS:
- Timeout de 5s en OpenAI (evita 499)
- Retries automáticos
- Fallback rápido si falla
"""

import time
import json
import re
from typing import Dict, List, Optional
from openai import OpenAI, RateLimitError, APITimeoutError
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
✅ SIEMPRE considerá el contexto de la conversación previa.
"""

# =========================================================
# GENERADOR DE RESPUESTAS CON TIMEOUT OPTIMIZADO
# =========================================================

def generate_llm_reply(
    phone: str, 
    user_message: str, 
    structured_data: Optional[Dict[str, str]] = None,
    max_retries: int = 2
) -> str:
    """
    Usa el modelo GPT para generar una respuesta amigable.
    ✅ Timeout de 5s para evitar 499
    ✅ Retries automáticos
    """
    if not client:
        logger.warning("⚠️ OpenAI client no disponible")
        return "⚠️ El sistema de IA no está disponible en este momento."

    # Construir mensaje
    if structured_data:
        structured_text = json.dumps(structured_data, ensure_ascii=False, indent=2)
        full_message = f"Estos son los datos que obtuve:\n{structured_text}\n\nRedactá una respuesta clara para el cliente."
    else:
        full_message = user_message

    # Intentar con retries
    for attempt in range(max_retries):
        try:
            start_time = time.time()
            
            response = client.chat.completions.create(
                model=MODEL_NAME,
                messages=[
                    {"role": "system", "content": SMART_SYSTEM_PROMPT},
                    {"role": "user", "content": full_message}
                ],
                temperature=0.7,
                max_tokens=300,  # Reducido de 400 a 300
                timeout=5,  # ✅ TIMEOUT DE 5 SEGUNDOS
            )
            
            reply = response.choices[0].message.content.strip()
            duration = round(time.time() - start_time, 2)
            
            logger.info(f"🤖 Respuesta IA generada en {duration}s (intento {attempt + 1})")
            
            return reply

        except APITimeoutError:
            logger.warning(f"⏱️ Timeout OpenAI (intento {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return "⚠️ Estoy un poco lento. Intentá de nuevo en unos segundos."

        except RateLimitError:
            logger.warning(f"⚠️ Rate limit OpenAI (intento {attempt + 1}/{max_retries})")
            if attempt < max_retries - 1:
                time.sleep(2)
                continue
            return "⚠️ Límite de uso alcanzado. Esperá un momento."

        except Exception as e:
            logger.error(f"❌ Error en generate_llm_reply: {e}")
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return "⚠️ Tuve un problema técnico. Intentá de nuevo."

    return "⚠️ No pude procesar tu mensaje. Intentá de nuevo."


# =========================================================
# DETECTOR DE INTENCIÓN (MEJORADO)
# =========================================================

def detect_intent(message: str) -> str:
    """
    Detecta la intención principal del usuario.
    PRIORIDAD: cart_add > búsqueda técnica > saludo > otros
    """
    msg = message.lower().strip()

    # Comandos de carrito
    cart_commands = ["agrega", "agregá", "añade", "añadí", "poneme", "al carrito", "agregar"]
    if any(cmd in msg for cmd in cart_commands):
        return "cart_add"

    technical_terms = [
        "batería", "bateria", "amortiguador", "filtro", "tapa", "valvula", "cadena",
        "bulbo", "aceite", "carter", "embrague", "piston", "camisa", "precio",
        "cuánto", "vale", "tienen", "stock", "compatible", "modelo", "año",
        "delantero", "trasero", "adelante", "atras",
        "2010", "2011", "2012", "2013", "2014", "2015", "2016", "2017", "2018",
        "2019", "2020", "2021", "2022", "2023", "2024", "2025"
    ]
    if any(term in msg for term in technical_terms):
        return "search"

    if any(x in msg for x in ["lista", "cotiza", "presupuesto", "bulk"]) or "\n" in message:
        return "bulk_quote"

    if any(x in msg for x in ["carrito", "mi pedido", "total", "sacá", "vaciar"]):
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
    """
    Genera respuesta basada en productos del catálogo.
    ✅ Optimizado para responder en <5s
    """
    if products is None:
        from fran.search import search_products
        products = search_products(query, top_k=10, phone=phone)

    if not products:
        return generate_llm_reply(
            phone=phone,
            user_message=f"El usuario preguntó: '{query}'. Pero NO se encontró ningún producto relacionado en el catálogo. Responde como Fran: amable, técnico, y sin inventar."
        )

    # Construir contexto de productos (solo top 5 para ser rápido)
    context_lines = []
    for p in products[:5]:
        line = f"- Código: {p['code']} | Nombre: {p['name']}"
        if p.get("models"):
            line += f" | Compatible: {p['models']}"
        if p.get("brand"):
            line += f" | Marca: {p['brand']}"
        if p.get("price"):
            line += f" | Precio: ${p['price']}"
        context_lines.append(line)

    context = "\n".join(context_lines)

    user_prompt = f"""El cliente preguntó: "{query}"

Productos disponibles (reales, del catálogo):
{context}

Redactá una respuesta técnica, útil y empática como Fran.
- Si hay un producto claramente compatible, destacalo.
- Si hay varios, mostrá las mejores opciones.
- Si no estás seguro, pedí más datos (modelo exacto, año, etc.).
- Nunca inventes información fuera de esta lista.
- SÉ BREVE (máximo 4-5 líneas).
"""
    
    return generate_llm_reply(phone=phone, user_message=user_prompt)


# =========================================================
# MEMORIA CONVERSACIONAL PROGRESIVA
# =========================================================

def get_conversation_summary(phone: str) -> str:
    from fran.db import get_db_connection
    try:
        with get_db_connection() as conn:
            cur = conn.execute("SELECT summary FROM conversation_summary WHERE phone = ?", (phone,))
            row = cur.fetchone()
            return row["summary"] if row and row["summary"] else ""
    except Exception as e:
        logger.error(f"Error leyendo resumen: {e}")
        return ""


def update_conversation_summary(phone: str, user_message: str, bot_reply: str):
    """
    Actualiza resumen conversacional de forma inteligente.
    ✅ Timeout de 5s máximo
    """
    if not client:
        return

    try:
        current_summary = get_conversation_summary(phone)
        new_exchange = f"Usuario: {user_message}\nFran: {bot_reply}"

        prompt = f"""Actualizá el resumen de la conversación con el nuevo intercambio.

REGLAS:
- Mantenelo breve (máximo 2-3 oraciones)
- Incluí: producto/moto mencionado, marca, modelo
- Si se agregó algo al carrito, mencionarlo
- Usá tercera persona
- NO incluyas saludos/despedidas

Resumen actual:
{current_summary if current_summary else "(vacío)"}

Nuevo intercambio:
{new_exchange}

Resumen actualizado (breve):"""

        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=100,
            timeout=5  # ✅ Timeout de 5s
        )
        
        new_summary = response.choices[0].message.content.strip()

        # Evitar resúmenes vacíos
        if len(new_summary) < 10:
            logger.info("Resumen muy corto, manteniendo anterior")
            return

        from fran.db import get_db_connection
        with get_db_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO conversation_summary (phone, summary, updated_at)
                VALUES (?, ?, datetime('now'))
            """, (phone, new_summary))
            conn.commit()
        
        logger.info(f"📝 Resumen actualizado: {ellipsis(new_summary, 50)}")
        
    except APITimeoutError:
        logger.warning("⏱️ Timeout actualizando resumen (ignorado)")
    except Exception as e:
        logger.warning(f"⚠️ Error actualizando resumen: {e}")


# =========================================================
# LOGGING
# =========================================================

def summarize_message_for_log(phone: str, user_message: str, intent: str, response: str) -> str:
    user_preview = ellipsis(user_message, 50)
    response_preview = ellipsis(response, 80)
    return f"{phone} | {intent} | U: {user_preview} → R: {response_preview}"
