# coding: utf-8
"""
Módulo: routes.py
Endpoints principales de Fran 3.8 (Flask)
-------------------------------------------------
Incluye:
- /api/quote
- /api/analytics
- /webhook (Twilio)
- /health (ping)
"""

from flask import Flask, request, jsonify, Response
from twilio.twiml.messaging_response import MessagingResponse
from fran.utils import is_duplicate_message
from fran.db import save_message, log_interaction, log_performance
from fran.bulk import is_bulk_list_request, process_bulk_sync, format_bulk_response
from fran.cart import cart_add, cart_get, cart_clear, cart_summary_text
from fran.ai import (
    detect_intent,
    rule_based_reply,
    generate_llm_reply,
    generate_product_based_reply,
    summarize_message_for_log,
    update_conversation_summary,  # ✅ Importado
)
from fran.config import logger, INSTANT_THRESHOLD
from fran.catalog import eager_warmup

import os
import time

# =========================================================
# APP PRINCIPAL
# =========================================================

app = Flask(__name__)

# =========================================================
# WARMUP DEL CATÁLOGO Y FAISS (al arrancar)
# =========================================================
try:
    if os.environ.get("EAGER_CATALOG", "1") == "1":
        logger.info("🚀 Iniciando warmup de catálogo FAISS al arrancar servidor...")
        eager_warmup()
        logger.info("✅ Warmup de catálogo completado correctamente.")
except Exception as e:
    logger.error(f"❌ Error en warmup inicial: {e}")

# =========================================================
# ENDPOINT /api/quote
# =========================================================

@app.route("/api/quote", methods=["POST"])
def api_quote():
    """Cotiza un producto individual o varios a la vez."""
    data = request.get_json(silent=True) or {}
    phone = data.get("phone", "").strip()
    user_message = data.get("message", "").strip()

    if not phone or not user_message:
        return jsonify({"error": "Faltan parámetros"}), 400

    start_time = time.time()
    save_message(phone, user_message, "user")
    intent = detect_intent(user_message)

    # Listas masivas
    is_bulk, item_count = is_bulk_list_request(user_message)
    if is_bulk and item_count < INSTANT_THRESHOLD:
        result = process_bulk_sync(phone, user_message)
        text = format_bulk_response(result)
        save_message(phone, text, "bot")
        log_performance(phone, "bulk_sync", start_time)

        # ✅ ACTUALIZAR RESUMEN CONVERSACIONAL
        update_conversation_summary(phone, user_message, text)

        return jsonify({"response": text, "intent": "bulk_quote"})

    # Búsqueda técnica usando Fran + Catálogo + FAISS
    if intent == "search":
        reply = generate_product_based_reply(phone, user_message)
        save_message(phone, reply, "bot")
        log_performance(phone, "search_rag", start_time)

        # ✅ ACTUALIZAR RESUMEN CONVERSACIONAL
        update_conversation_summary(phone, user_message, reply)

        return jsonify({"response": reply, "intent": intent})
    else:
        reply = generate_llm_reply(phone, user_message)
        save_message(phone, reply, "bot")
        log_performance(phone, "llm_reply", start_time)

        # ✅ ACTUALIZAR RESUMEN CONVERSACIONAL
        update_conversation_summary(phone, user_message, reply)

        return jsonify({"response": reply, "intent": intent})


# =========================================================
# ENDPOINT /api/analytics
# =========================================================

@app.route("/api/analytics", methods=["GET"])
def api_analytics():
    """Devuelve métricas simples: intenciones detectadas y mensajes más frecuentes."""
    from fran.db import get_db_connection
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()

            cur.execute("""
                SELECT intent_detected, COUNT(*) as count 
                FROM interactions 
                WHERE timestamp >= datetime('now', '-7 days')
                GROUP BY intent_detected
            """)
            intents = [{"intent": r[0], "count": r[1]} for r in cur.fetchall()]

            cur.execute("""
                SELECT message, COUNT(*) as count 
                FROM interactions 
                WHERE intent_detected = 'search' AND timestamp >= datetime('now', '-7 days')
                GROUP BY message 
                ORDER BY count DESC 
                LIMIT 10
            """)
            top_searches = [{"message": r[0], "count": r[1]} for r in cur.fetchall()]

        return jsonify({"intents": intents, "top_searches": top_searches})
    except Exception as e:
        logger.error(f"❌ Error en /api/analytics: {e}")
        return jsonify({"error": str(e)}), 500


# =========================================================
# ENDPOINT /health
# =========================================================

@app.route("/health", methods=["GET"])
def health():
    """Verifica que la app esté viva."""
    return jsonify({"status": "ok"})

# =========================================================
# ENDPOINT /webhook (Twilio WhatsApp)
# =========================================================

@app.route("/webhook", methods=["POST"])
def webhook():
    """Endpoint principal del bot WhatsApp."""
    start_time = time.time()

    phone = request.form.get("From", "").replace("whatsapp:", "")
    user_message = request.form.get("Body", "").strip()

    if not phone or not user_message:
        return Response("Faltan datos", status=400)

    if is_duplicate_message(phone, user_message):
        return Response("Mensaje duplicado ignorado", status=200)

    save_message(phone, user_message, "user")
    intent = detect_intent(user_message)

    # Respuestas rápidas
    fast_reply = rule_based_reply(intent, user_message)
    if fast_reply:
        resp = MessagingResponse()
        resp.message(fast_reply)
        save_message(phone, fast_reply, "bot")
        log_performance(phone, "rule_based", start_time)
        return str(resp)  # ❌ No actualizamos resumen en saludos

    # Listas masivas
    is_bulk, count = is_bulk_list_request(user_message)
    if is_bulk and count < INSTANT_THRESHOLD:
        result = process_bulk_sync(phone, user_message)
        reply_text = format_bulk_response(result)
        resp = MessagingResponse()
        resp.message(reply_text)
        save_message(phone, reply_text, "bot")
        log_interaction(phone, user_message, "bulk_quote", count)
        log_performance(phone, "bulk_sync", start_time)

        # ✅ ACTUALIZAR RESUMEN CONVERSACIONAL
        update_conversation_summary(phone, user_message, reply_text)

        return str(resp)

    # Búsqueda técnica usando RAG + Catálogo
    if intent == "search":
        reply_text = generate_product_based_reply(phone, user_message)
        save_message(phone, reply_text, "bot")
        log_interaction(phone, user_message, "search")
        log_performance(phone, "search_rag", start_time)

        # ✅ ACTUALIZAR RESUMEN CONVERSACIONAL
        update_conversation_summary(phone, user_message, reply_text)

        resp = MessagingResponse()
        resp.message(reply_text)
        return str(resp)

    # Carrito
    if intent == "cart":
        reply_text = cart_summary_text(phone)
        resp = MessagingResponse()
        resp.message(reply_text)
        save_message(phone, reply_text, "bot")
        log_interaction(phone, user_message, "cart")
        log_performance(phone, "cart", start_time)

        # ✅ ACTUALIZAR RESUMEN CONVERSACIONAL
        update_conversation_summary(phone, user_message, reply_text)

        return str(resp)

    # IA general
    llm_reply = generate_llm_reply(phone, user_message)
    save_message(phone, llm_reply, "bot")
    log_interaction(phone, user_message, intent)
    log_performance(phone, "llm_reply", start_time)

    # ✅ ACTUALIZAR RESUMEN CONVERSACIONAL
    update_conversation_summary(phone, user_message, llm_reply)

    resp = MessagingResponse()
    resp.message(llm_reply)
    logger.info(summarize_message_for_log(phone, user_message, intent, llm_reply))
    return str(resp)
