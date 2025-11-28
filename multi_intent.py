"""Utilities for handling multi-intent orchestration.

These helpers keep prompt templates small and focused while allowing
callers to plug in the heavy-lifting functions such as product search or
cart mutations. The logic stays intentionally simple so it can be reused
in lightweight environments (e.g., tests or sandboxes).
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable

Intent = dict[str, Any]


def parse_multi_intent(llm: Callable[[str], str], message: str) -> Iterable[Intent]:
    """Use the LLM to split a message into intent spans.

    The prompt is designed to avoid word-spotting and instead asks the
    model to identify coherent spans grouped by the communicative act.
    """

    prompt = f'''
    Sos un analista LLM-first de conversaciones. Detectá TODAS las intenciones en el
    mensaje sin usar palabras clave. Segmentar en spans exactos del usuario.

    Formato JSON obligatorio:
    {{
      "intents": [
        {{
          "type": "general_chat|product_search|cart_action|clarification|compare|checkout",
          "span": "<texto_exactamente_original>",
          "confidence": 0.0,
          "data": {{"query": "", "product": "", "action": "", "quantity": 0}}
        }}
      ]
    }}

    Mensaje del usuario: """{message}"""
    '''

    llm_response = llm(prompt)
    parsed = json.loads(llm_response)
    return parsed["intents"]


def generate_social_prompt(text: str) -> str:
    return f'''
    El usuario está en modo de charla social. Contestá brevemente (1–2 líneas), 
    cálido, humano, natural, sin catálogo ni detalles técnicos.

    Texto: "{text}"
    '''


def generate_clarification_prompt(text: str) -> str:
    return f'''
    El usuario necesita aclarar algo. Pedí más información de forma simple,
    sin recomendar productos ni buscar en catálogo todavía.

    Texto: "{text}"
    '''


def generate_product_prompt(text: str, allowed_products: Any) -> str:
    return f'''
    El usuario está consultando sobre productos o repuestos.
    Usá exclusivamente esta lista (allowed_products) proporcionada por el sistema:
    {json.dumps(allowed_products, ensure_ascii=False)}

    Respondé de forma profesional, clara y mayorista.
    Si faltan datos, pedí SOLO lo necesario.

    Texto del usuario: "{text}"
    '''


def process_cart_action(text: str, aplicar_accion_carrito: Callable[[str], str]) -> str:
    """Delegate cart updates to the provided callable."""

    return aplicar_accion_carrito(text)


def orchestrate(
    llm: Callable[[str], str],
    message: str,
    *,
    run_allowed_products_search: Callable[[str], Any],
    aplicar_accion_carrito: Callable[[str], str],
) -> str:
    """Execute multiple intents in priority order.

    The orchestrator keeps priority stable (general_chat → clarification →
    compare → product_search → cart_action → checkout) to ensure human rapport is handled
    first, followed by technical guidance.
    """

    intents_detected = parse_multi_intent(llm, message)

    normalized_intents = []
    for intent in intents_detected:
        if intent.get("type") == "social":
            intent = {**intent, "type": "general_chat"}
        normalized_intents.append(intent)

    prioridad = {
        "general_chat": 0,
        "clarification": 1,
        "compare": 2,
        "product_search": 3,
        "cart_action": 4,
        "checkout": 5,
    }
    intents_sorted = sorted(normalized_intents, key=lambda x: prioridad.get(x.get("type"), len(prioridad)))

    respuestas = []

    for intent_item in intents_sorted:
        intent_type = intent_item["type"]
        span = intent_item["span"]

        if intent_type == "general_chat":
            respuestas.append(llm(generate_social_prompt(span)))
            continue

        if intent_type == "clarification":
            respuestas.append(llm(generate_clarification_prompt(span)))
            continue

        if intent_type == "product_search":
            allowed_products = run_allowed_products_search(span)
            respuestas.append(llm(generate_product_prompt(span, allowed_products)))
            continue

        if intent_type == "cart_action":
            respuestas.append(process_cart_action(span, aplicar_accion_carrito))
            continue

        if intent_type == "compare":
            respuestas.append(llm(generate_clarification_prompt(span)))
            continue

        if intent_type == "checkout":
            respuestas.append("Cierro el pedido y preparo el total.")
            continue

    return " ".join(respuestas)
