# coding: utf-8

"""
Módulo: routes.py
Endpoints principales de Fran 3.8 (Flask)

Incluye:

- /api/quote
- /api/analytics
- /webhook (Twilio con validación before_request)
- /health (ping)
"""

from flask import Flask, request, jsonify, Response
from twilio.twiml.messaging_response import MessagingResponse
from twilio.request_validator import RequestValidator
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
# VALIDADOR TWILIO (estilo 3.7)
# =========================================================

twilio_validator = None
if TWILIO_AUTH_TOKEN:
    try:
        twilio_validator = RequestValidator(TWILIO_AUTH_TOKEN)
        logger.info("✅ Validador Twilio inicializado")
    except Exception as e:
        logger.error(f"❌ Error inicializando validador Twilio: {e}")
else:
    logger.warning("⚠️ TWILIO_AUTH_TOKEN no configurado, validación deshabilitada")


@app.before_request
def validate_twilio_signature():
    """Valida firma Twilio ANTES de procesar el request (estilo 3.7)."""
    if request.path.rstrip("/") == "/webhook" and twilio_validator:
        try:
            signature = request.headers.get("X-Twilio-Signature", "")
            url = request.url

            # Twilio usa HTTPS en producción
            if url.startswith("http://") and not url.startswith("http://localhost"):
                url = url.replace("http://", "https://")

            params = request.form.to_dict()

            if not twilio_validator.validate(url, params, signature):
                logger.warning(f"⚠️ Firma Twilio inválida desde {request.remote_addr}")
                return Response("Forbidden", status=403)

        except Exception as e:
            logger.error(f"❌ Error validando firma Twilio: {e}")
            return Response("Forbidden", status=403)


# =========================================================
# WARMUP ASINCRÓNICO DEL CATÁLOGO
# =========================================================

def _async_warmup():
    """Carga el catálogo en background sin bloquear el servidor."""
    try:
        if os.environ.get("EAGER_CATALOG", "1") == "1":
            logger.info("🔥 Iniciando warmup asincrónico del catálogo...")
            eager_warmup()
            logger.info("✅ Warmup completado en background")
    except Exception as e:
        logger.error(f"❌ Error en warmup async: {e}")


# Iniciar warmup en thread separado
threading.Thread(target=_async_warmup, daemon=True).start()


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

    # Búsqueda con timeout en IA
    try:
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(generate_product_based_reply, phone, user_message)
            reply = future.result(timeout=10)
    except Exception:
        reply = "⚠️ Estoy un poco lento ahora. Intentá de nuevo en unos segundos."

    save_message(phone, reply, "bot")
    log_performance(phone, "search_rag", start_time)

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

    try:
        phone = request.form.get("From", "").replace("whatsapp:", "")
        user_message = request.form.get("Body", "").strip()

        if not phone or not user_message:
            logger.warning("Webhook sin From o Body")
            return Response("Faltan datos", status=400)

        if is_duplicate_message(phone, user_message):
            logger.info(f"Mensaje duplicado ignorado de {phone}")
            return Response("Mensaje duplicado ignorado", status=200)

        logger.info(f"📨 Mensaje de {phone}: {user_message[:80]}")

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
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(generate_product_based_reply, phone, user_message)
                try:
                    reply_text = future.result(timeout=10)
                except concurrent.futures.TimeoutError:
                    logger.warning(f"⏱️ Timeout en IA para {phone}")
                    reply_text = "⚠️ Estoy un poco lento ahora. Intentá de nuevo en unos segundos."
                except Exception as e:
                    logger.error(f"❌ Error en IA: {e}")
                    reply_text = "⚠️ Tuve un problema técnico. Intentá de nuevo."

        save_message(phone, reply_text, "bot")
        if intent != "cart":
            log_interaction(phone, user_message, intent)
        log_performance(phone, "webhook", start_time)

        def _update_summary():
            try:
                update_conversation_summary(phone, user_message, reply_text)
            except Exception as e:
                logger.warning(f"⚠️ Error en resumen background: {e}")

        threading.Thread(target=_update_summary, daemon=True).start()

        resp = MessagingResponse()
        resp.message(reply_text)

        duration = round(time.time() - start_time, 2)
        logger.info(f"✅ Respuesta a {phone} en {duration}s: {reply_text[:80]}")

        return str(resp)

    except Exception as e:
        logger.error(f"❌ Error crítico en webhook: {e}", exc_info=True)
        try:
            resp = MessagingResponse()
            resp.message("⚠️ Perdón, tuve un problema técnico. Intentá de nuevo.")
            return str(resp)
        except:
            return Response("Internal Server Error", status=500)


# =========================================================
# ENDPOINT ROOT
# =========================================================

@app.route("/", methods=["GET"])
def root():
    """Página de inicio."""
    return jsonify({
        "service": "Fran 3.8",
        "status": "running",
        "endpoints": ["/webhook", "/api/quote", "/api/analytics", "/health"]
    })
