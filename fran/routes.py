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
from fran.search import search_products
from fran.cart import cart_add, cart_get, cart_clear, cart_summary_text
from fran.ai import detect_intent, rule_based_reply, generate_llm_reply, summarize_message_for_log
from fran.config import logger, INSTANT_THRESHOLD

import time

# =========================================================
# APP PRINCIPAL
# =========================================================

app = Flask(__name__)


# =========================================================
# ENDPOINT /api/quote
# =========================================================

@app.route("/api/quote", methods=["POST"])
def api_quote():
    """
    Cotiza un producto individual o varios a la vez.
    Request JSON:
    {
        "phone": "112233",
        "message": "tapa valvula ybr"
    }
    """
    data = request.get_json(silent=True) or {}
    phone = data.get("phone", "").strip()
    user_message = data.get("message", "").strip()

    if not phone or not user_message:
        return jsonify({"error": "Faltan parámetros"}), 400

    start_time = time.time()
    save_message(phone, user_message, "user")
    intent = detect_intent(user_message)

    # Procesamos listas masivas
    is_bulk, item_count = is_bulk_list_request(user_message)
    if is_bulk and item_count < INSTANT_THRESHOLD:
        result = process_bulk_sync(phone, user_message)
        text = format_bulk_response(result)
        save_message(phone, text, "bot")
        log_performance(phone, "bulk_sync", start_time)
        return jsonify({"response": text, "intent": "bulk_quote"})

    # Búsqueda individual
    results = search_products(user_message, top_k=5)
    if not results:
        reply = "No encontré ese producto en el catálogo. ¿Podés darme más detalles?"
    else:
        lines = [f"{r['name']} ({r['code']}) — USD {r['price']:.2f}" for r in results[:3]]
        reply = "🔍 Resultados:\n" + "\n".join(lines)

    save_message(phone, reply, "bot")
    log_performance(phone, "quote", start_time)
    return jsonify({"response": reply, "intent": intent})


# =========================================================
# ENDPOINT /api/analytics
# =========================================================

@app.route("/api/analytics", methods=["GET"])
def api_analytics():
    """
    Devuelve métricas simples: intenciones detectadas y mensajes más frecuentes.
    """
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
    """
    Endpoint principal del bot WhatsApp.
    Recibe mensajes del cliente y responde en tiempo real.
    """
    start_time = time.time()

    # Datos entrantes
    phone = request.form.get("From", "").replace("whatsapp:", "")
    user_message = request.form.get("Body", "").strip()

    if not phone or not user_message:
        return Response("Faltan datos", status=400)

    # Evitamos procesar duplicados
    if is_duplicate_message(phone, user_message):
        return Response("Mensaje duplicado ignorado", status=200)

    save_message(phone, user_message, "user")
    intent = detect_intent(user_message)

    # Intent rule-based (respuestas rápidas)
    fast_reply = rule_based_reply(intent, user_message)
    if fast_reply:
        resp = MessagingResponse()
        resp.message(fast_reply)
        save_message(phone, fast_reply, "bot")
        log_performance(phone, "rule_based", start_time)
        return str(resp)

    # Intent lista masiva
    is_bulk, count = is_bulk_list_request(user_message)
    if is_bulk and count < INSTANT_THRESHOLD:
        result = process_bulk_sync(phone, user_message)
        reply_text = format_bulk_response(result)
        resp = MessagingResponse()
        resp.message(reply_text)
        save_message(phone, reply_text, "bot")
        log_interaction(phone, user_message, "bulk_quote", count)
        log_performance(phone, "bulk_sync", start_time)
        return str(resp)

    # Intent búsqueda individual
    if intent == "search":
        results = search_products(user_message, top_k=5)
        if results:
            lines = [f"{r['name']} ({r['code']}) — USD {r['price']:.2f}" for r in results[:3]]
            reply_text = "🔍 Resultados:\n" + "\n".join(lines)
        else:
            reply_text = "No encontré ese producto en el catálogo. ¿Podés darme más detalles?"
        save_message(phone, reply_text, "bot")
        log_interaction(phone, user_message, "search")
        log_performance(phone, "search", start_time)
        resp = MessagingResponse()
        resp.message(reply_text)
        return str(resp)

    # Intent carrito
    if intent == "cart":
        reply_text = cart_summary_text(phone)
        resp = MessagingResponse()
        resp.message(reply_text)
        save_message(phone, reply_text, "bot")
        log_interaction(phone, user_message, "cart")
        log_performance(phone, "cart", start_time)
        return str(resp)

    # Intent general o desconocido (IA)
    llm_reply = generate_llm_reply(phone, user_message)
    save_message(phone, llm_reply, "bot")
    log_interaction(phone, user_message, intent)
    log_performance(phone, "llm_reply", start_time)

    resp = MessagingResponse()
    resp.message(llm_reply)
    logger.info(summarize_message_for_log(phone, user_message, intent, llm_reply))
    return str(resp)
