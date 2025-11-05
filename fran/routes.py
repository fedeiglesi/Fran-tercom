# coding: utf-8
"""
Módulo: routes.py
Endpoints principales de Fran 3.8 (Flask)
-------------------------------------------------
Incluye:
- /api/quote
- /api/analytics
- /webhook (Twilio con firma, timeout y background)
- /health (ping)
"""

from flask import Flask, request, jsonify, Response
from twilio.twiml.messaging_response import MessagingResponse
from fran.utils import is_duplicate_message
from fran.db import save_message, log_interaction, log_performance
from fran.bulk import is_bulk_list_request, process_bulk_sync, format_bulk_response
from fran.cart import cart_summary_text
from fran.ai import (
    detect_intent,
    rule_based_reply,
    generate_product_based_reply,
    summarize_message_for_log,
    update_conversation_summary,
)
from fran.config import logger, INSTANT_THRESHOLD, TWILIO_AUTH_TOKEN
from fran.catalog import eager_warmup

import os
import time
import threading
import concurrent.futures

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
        
        # Actualización asíncrona de resumen
        def _update():
            try:
                update_conversation_summary(phone, user_message, text)
            except:
                pass
        threading.Thread(target=_update, daemon=True).start()
        
        return jsonify({"response": text, "intent": "bulk_quote"})

    # Búsqueda con timeout
    try:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(generate_product_based_reply, phone, user_message)
            reply = future.result(timeout=10)
    except Exception:
        reply = "⚠️ Estoy un poco lento ahora. Intentá de nuevo en unos segundos."

    save_message(phone, reply, "bot")
    log_performance(phone, "search_rag", start_time)
    
    # Actualización asíncrona
    def _update():
        try:
            update_conversation_summary(phone, user_message, reply)
        except:
            pass
    threading.Thread(target=_update, daemon=True).start()
    
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
    """Endpoint principal del bot WhatsApp con manejo robusto de errores y timeout."""
    start_time = time.time()
    
    # ✅ VALIDACIÓN DE FIRMA TWILIO
    try:
        from twilio.request_validator import RequestValidator
        validator = RequestValidator(TWILIO_AUTH_TOKEN)
        if not validator.validate(
            request.url,
            request.form,
            request.headers.get("X-Twilio-Signature", "")
        ):
            logger.warning("⚠️ Solicitud no autorizada a /webhook")
            return Response("Forbidden", status=403)
    except Exception as e:
        logger.error(f"❌ Error validando firma Twilio: {e}")
        return Response("Forbidden", status=403)

    try:
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
            return str(resp)

        # Lógica principal
        is_bulk, count = is_bulk_list_request(user_message)
        if is_bulk and count < INSTANT_THRESHOLD:
            result = process_bulk_sync(phone, user_message)
            reply_text = format_bulk_response(result)
        elif intent == "cart":
            reply_text = cart_summary_text(phone)
        else:
            # ✅ TIMEOUT EN IA (máx 10s)
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(generate_product_based_reply, phone, user_message)
                try:
                    reply_text = future.result(timeout=10)
                except Exception:
                    reply_text = "⚠️ Estoy un poco lento ahora. Intentá de nuevo en unos segundos."

        # Guardar y loggear
        save_message(phone, reply_text, "bot")
        if intent != "cart":
            log_interaction(phone, user_message, intent)
        log_performance(phone, "webhook", start_time)

        # ✅ ACTUALIZAR RESUMEN EN BACKGROUND
        def _update_summary():
            try:
                update_conversation_summary(phone, user_message, reply_text)
            except Exception as e:
                logger.warning(f"⚠️ Error en resumen background: {e}")

        threading.Thread(target=_update_summary, daemon=True).start()

        # Responder
        resp = MessagingResponse()
        resp.message(reply_text)
        logger.info(summarize_message_for_log(phone, user_message, intent, reply_text))
        return str(resp)

    except Exception as e:
        # ✅ MANEJO GENERAL DE ERRORES
        logger.error(f"❌ Error no manejado en webhook: {e}")
        resp = MessagingResponse()
        resp.message("⚠️ Perdón, tuve un problema técnico. Intentá de nuevo.")
        return str(resp)
