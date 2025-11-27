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
    Sos un modelo experto en analizar lenguaje natural en conversaciones de WhatsApp.
    Dado un mensaje, debés identificar TODAS las intenciones presentes.

    Tu tarea:
    - Dividir el mensaje en fragmentos (spans) si contiene más de una intención.
    - Cada fragmento debe ser texto EXACTO del usuario (sin inventar).
    - Cada fragmento debe tener solo UNA intención.
    - Mantener el orden original.

    Intenciones posibles (no agregues otras):
    - "social": saludos, agradecer, cómo estás, charla humana no comercial.
    - "product_search": cuando el usuario expresa interés en buscar, ver, consultar, comparar o analizar cualquier producto, parte, repuesto o catálogo.
    - "clarification": cuando el usuario necesita aclarar o ampliar lo anterior.
    - "cart_action": agregar, sacar, confirmar, cambiar cantidades, cerrar compra.

    NO uses reglas fijas. NO asumas palabras clave. NO dependas de un diccionario.
    Analizá el significado y contexto general, como haría ChatGPT.

    Respondé SOLO con JSON en este formato:
    {{
      "intents": [
        {{"type": "<intent>", "span": "<texto_original>"}},
        ...
      ]
    }}

    Mensaje del usuario:
    """{message}"""
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

    The orchestrator keeps priority stable (social → clarification →
    product_search → cart_action) to ensure human rapport is handled
    first, followed by technical guidance.
    """

    intents_detected = parse_multi_intent(llm, message)

    prioridad = ["social", "clarification", "product_search", "cart_action"]
    intents_sorted = sorted(intents_detected, key=lambda x: prioridad.index(x["type"]))

    respuestas = []

    for intent_item in intents_sorted:
        intent_type = intent_item["type"]
        span = intent_item["span"]

        if intent_type == "social":
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

    return " ".join(respuestas)
