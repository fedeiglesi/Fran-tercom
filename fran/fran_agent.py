# coding: utf-8
"""
Módulo: fran_agent.py
Lógica central del asistente Fran 3.8
-------------------------------------------------
Responsable de:
- Interpretar mensajes entrantes
- Detectar intención (bulk, search, cart, IA)
- Coordinar catálogo, carrito y OpenAI
- Guardar logs y rendimiento
"""

import time
from decimal import Decimal
from fran.db import save_message, log_interaction, log_performance
from fran.bulk import is_bulk_list_request, process_bulk_sync, format_bulk_response
from fran.search import search_products
from fran.cart import cart_summary_text
from fran.ai import detect_intent, rule_based_reply, generate_llm_reply
from fran.config import INSTANT_THRESHOLD, logger


# =========================================================
# FUNCIÓN PRINCIPAL
# =========================================================

def run_agent(phone: str, user_message: str) -> str:
    """
    Ejecuta todo el flujo de decisión de Fran:
    1. Guarda el mensaje
    2. Detecta intención
    3. Procesa según tipo (bulk, search, cart, IA)
    4. Devuelve respuesta lista para enviar
    """
    if not phone or not user_message:
        return "⚠️ Error: mensaje vacío."

    start_time = time.time()
    save_message(phone, user_message, "user")
    intent = "unknown"

    # =========================================================
    # 1. DETECTAR LISTAS MASIVAS (PRIORIDAD MÁXIMA)
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
            text = "📦 Tu lista es muy larga. Estoy procesándola, esto puede tardar unos segundos..."
            save_message(phone, text, "bot")
            return text

    # =========================================================
    # 2. DETECTAR INTENCIÓN GENERAL
    # =========================================================
    intent = detect_intent(user_message)
    logger.info(f"🎯 Intent detectado: {intent}")

    # =========================================================
    # 3. RESPUESTAS RÁPIDAS (RULE-BASED)
    # =========================================================
    quick = rule_based_reply(intent, user_message)
    if quick:
        save_message(phone, quick, "bot")
        log_performance(phone, "rule_based", start_time)
        return quick

    # =========================================================
    # 4. INTENCIÓN DE CARRITO
    # =========================================================
    if intent == "cart":
        reply = cart_summary_text(phone)
        save_message(phone, reply, "bot")
        log_interaction(phone, user_message, intent)
        log_performance(phone, "cart", start_time)
        return reply

    # =========================================================
    # 5. INTENCIÓN DE BÚSQUEDA
    # =========================================================
    if intent == "search":
        results = search_products(user_message, top_k=5)
        if not results:
            reply = "No encontré ese producto en el catálogo. ¿Podés darme más detalles?"
        else:
            lines = [f"{r['name']} ({r['code']}) — USD {r['price']:.2f}" for r in results[:3]]
            reply = "🔍 Resultados:\n" + "\n".join(lines)

        save_message(phone, reply, "bot")
        log_interaction(phone, user_message, intent)
        log_performance(phone, "search", start_time)
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
        return "⚠️ No pude procesar tu mensaje. Intentá de nuevo en unos segundos."
