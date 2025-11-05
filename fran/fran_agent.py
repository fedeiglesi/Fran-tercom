# coding: utf-8
"""
Modulo: fran_agent.py
Logica central del asistente Fran 3.8
-------------------------------------------------
Responsable de:
- Interpretar mensajes entrantes
- Detectar intencion (bulk, search, cart, IA)
- Coordinar catalogo, carrito y OpenAI
- Guardar logs y rendimiento
"""

import time
from fran.db import save_message, log_interaction, log_performance
from fran.bulk import is_bulk_list_request, process_bulk_sync, format_bulk_response
from fran.cart import cart_summary_text
from fran.ai import detect_intent, rule_based_reply, generate_llm_reply, generate_product_based_reply
from fran.config import INSTANT_THRESHOLD, logger


# =========================================================
# FUNCION PRINCIPAL
# =========================================================

def run_agent(phone: str, user_message: str) -> str:
    """
    Ejecuta todo el flujo de decision de Fran:
    1. Guarda el mensaje
    2. Detecta intencion
    3. Procesa segun tipo (bulk, search, cart, IA)
    4. Devuelve respuesta lista para enviar
    """
    if not phone or not user_message:
        return "⚠️ Error: mensaje vacio."

    start_time = time.time()
    save_message(phone, user_message, "user")
    intent = "unknown"

    # =========================================================
    # 1. DETECTAR LISTAS MASIVAS (PRIORIDAD MAXIMA)
    # =========================================================
    is_bulk, item_count = is_bulk_list_request(user_message)
    if is_bulk:
        intent = "bulk_quote"
        log_interaction(phone, user_message, intent, item_count)

        if item_count < INSTANT_THRESHOLD:
            result = process_bulk_sync(phone, user_message)
            text = format_bulk_response(result)
            save_message(phone, text, "bot")
            log_performance(phone, "bulk_sync", start_time)
            return text
        else:
            text = "📦 Tu lista es muy larga. Estoy procesandola, esto puede tardar unos segundos..."
            save_message(phone, text, "bot")
            return text

    # =========================================================
    # 2. DETECTAR INTENCION GENERAL
    # =========================================================
    intent = detect_intent(user_message)
    logger.info(f"🎯 Intent detectado: {intent}")

    # =========================================================
    # 3. RESPUESTAS RAPIDAS (RULE-BASED)
    # =========================================================
    quick = rule_based_reply(intent, user_message)
    if quick:
        save_message(phone, quick, "bot")
        log_performance(phone, "rule_based", start_time)
        return quick

    # =========================================================
    # 4. INTENCION DE CARRITO
    # =========================================================
    if intent == "cart":
        reply = cart_summary_text(phone)
        save_message(phone, reply, "bot")
        log_interaction(phone, user_message, intent)
        log_performance(phone, "cart", start_time)
        return reply

    # =========================================================
    # 5. INTENCION DE BUSQUEDA TECNICA (USANDO CATALOGO + FRAN)
    # =========================================================
    if intent == "search":
        reply = generate_product_based_reply(phone, user_message)
        save_message(phone, reply, "bot")
        log_interaction(phone, user_message, intent)
        log_performance(phone, "search_rag", start_time)
        return reply

    # =========================================================
    # 6. FALLBACK IA (para dudas o mensajes naturales)
    # =========================================================
    try:
        reply = generate_llm_reply(phone, user_message)
        save_message(phone, reply, "bot")
        log_interaction(phone, user_message, intent)
        log_performance(phone, "llm_reply", start_time)
        return reply
    except Exception as e:
        logger.error(f"❌ Error en run_agent fallback IA: {e}")
        return "⚠️ No pude procesar tu mensaje. Intenta de nuevo en unos segundos."
