"""
Integración opcional de multi-intent en Fran v3.17.

Fix #7: Activación de multi-intent handling.

Para activar: SET ENABLE_MULTI_INTENT=true en env
"""

from __future__ import annotations
import os
import logging
from typing import Any, Callable, Dict, List

logger = logging.getLogger("fran313")


def is_multi_intent_enabled() -> bool:
    """
    Verifica si multi-intent está habilitado vía variable de entorno.

    Returns:
        True si ENABLE_MULTI_INTENT=true
    """
    return os.environ.get("ENABLE_MULTI_INTENT", "false").lower() in {"true", "1", "yes", "on"}


def should_use_multi_intent(message: str, llm_classification: Dict[str, Any]) -> bool:
    """
    Decide si un mensaje debe procesarse con multi-intent.

    Criterios:
    1. Multi-intent habilitado globalmente
    2. Mensaje largo (>= 20 palabras) o con múltiples cláusulas
    3. Clasificación LLM indica multi_intent con alta confianza

    Args:
        message: Mensaje del usuario
        llm_classification: Output del clasificador LLM

    Returns:
        True si debe usar multi-intent
    """
    if not is_multi_intent_enabled():
        return False

    # Criterio 1: Mensaje largo
    word_count = len(message.split())
    if word_count >= 20:
        return True

    # Criterio 2: Múltiples oraciones o cláusulas
    sentence_markers = [". ", "! ", "? ", ", y ", " y tambien", " y también", " ademas", " además"]
    if any(marker in message.lower() for marker in sentence_markers):
        return True

    # Criterio 3: LLM detectó multi-intent
    multi_intent_data = llm_classification.get("multi_intent", [])
    if isinstance(multi_intent_data, list) and len(multi_intent_data) >= 2:
        # Al menos 2 intents con confianza >= 0.6
        high_confidence = [
            intent for intent in multi_intent_data
            if intent.get("confidence", 0.0) >= 0.6
        ]
        if len(high_confidence) >= 2:
            return True

    return False


def execute_multi_intent_pipeline(
    message: str,
    llm_client: Any,
    search_function: Callable,
    cart_function: Callable,
    history: List[Dict[str, str]] | None = None,
) -> Dict[str, Any]:
    """
    Ejecuta pipeline de multi-intent para un mensaje.

    Args:
        message: Mensaje del usuario
        llm_client: Cliente LLM
        search_function: Función de búsqueda (signature: (query: str) -> List[dict])
        cart_function: Función de carrito (signature: (action: str, data: dict) -> dict)
        history: Historial de conversación

    Returns:
        Dict con:
            - intents: Lista de intents detectados
            - responses: Lista de respuestas por intent
            - combined_response: Respuesta final combinada
            - metadata: Metadata del proceso
    """
    try:
        from multi_intent import parse_multi_intent, orchestrate

        logger.info(f"[multi-intent] Processing message with {len(message)} chars")

        # Parse intents
        intents = list(parse_multi_intent(
            llm=lambda prompt: llm_client.chat.completions.create(
                model=os.environ.get("MODEL_NAME", "gpt-4o-mini"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
            ).choices[0].message.content,
            message=message,
            history=[f"{msg['role']}: {msg['content']}" for msg in (history or [])],
            min_confidence=0.55,
        ))

        if not intents:
            logger.warning("[multi-intent] No intents detected, falling back to single-intent")
            return {
                "intents": [],
                "responses": [],
                "combined_response": None,
                "metadata": {"fallback": "no_intents"},
            }

        logger.info(f"[multi-intent] Detected {len(intents)} intents: {[i['type'] for i in intents]}")

        # Orchestrate execution
        combined_response = orchestrate(
            llm=lambda prompt: llm_client.chat.completions.create(
                model=os.environ.get("MODEL_NAME", "gpt-4o-mini"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
            ).choices[0].message.content,
            message=message,
            run_allowed_products_search=search_function,
            aplicar_accion_carrito=cart_function,
            history=[f"{msg['role']}: {msg['content']}" for msg in (history or [])],
        )

        return {
            "intents": intents,
            "responses": [],  # orchestrate() retorna respuesta combinada directamente
            "combined_response": combined_response,
            "metadata": {
                "multi_intent_used": True,
                "intent_count": len(intents),
                "intent_types": [i["type"] for i in intents],
            },
        }

    except ImportError:
        logger.error("[multi-intent] multi_intent module not available")
        return {
            "intents": [],
            "responses": [],
            "combined_response": None,
            "metadata": {"error": "module_not_found"},
        }
    except Exception as e:
        logger.exception(f"[multi-intent] Error in pipeline: {e}")
        return {
            "intents": [],
            "responses": [],
            "combined_response": None,
            "metadata": {"error": str(e)},
        }


def integrate_with_orchestrator(
    orchestrator_result: Dict[str, Any],
    multi_intent_result: Dict[str, Any] | None,
) -> Dict[str, Any]:
    """
    Integra resultado de multi-intent con resultado del orquestador standard.

    Args:
        orchestrator_result: Resultado del orquestador single-intent
        multi_intent_result: Resultado de multi-intent (None si no se usó)

    Returns:
        Dict con resultado combinado
    """
    if multi_intent_result is None or not multi_intent_result.get("combined_response"):
        # No multi-intent o falló → usar resultado standard
        return orchestrator_result

    # Multi-intent exitoso → usar su respuesta
    return {
        **orchestrator_result,
        "response": multi_intent_result["combined_response"],
        "metadata": {
            **orchestrator_result.get("metadata", {}),
            "multi_intent": multi_intent_result["metadata"],
        },
    }
