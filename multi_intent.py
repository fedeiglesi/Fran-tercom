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

    template = r'''
    Analizá el siguiente mensaje y dividilo en fragmentos coherentes según su INTENCIÓN.
    NO te bases en palabras sueltas; identificá el acto comunicativo del usuario.

    Intenciones posibles:
      - "social": saludo, agradecimiento, comentario, charla humana.
      - "clarification": pide detalles sin intención de compra.
      - "product_search": el usuario está pidiendo repuestos, opciones,
        disponibilidad o iniciando una compra.
      - "cart_action": agregar, sacar, cambiar cantidad, cerrar pedido.

    IMPORTANTE:
    - El mensaje puede tener más de una intención.
    - NO inventes spans: cada 'span' debe ser una porción EXACTA del mensaje original.
    - El orden de aparición importa.
    - Ejemplo esperado:
      {{
        "intents": [
          {{"type": "social", "span": "hola Fran, genio, como estas? Me salvaste el otro día."}},
          {{"type": "product_search", "span": "Sabes q ahora ando buscando amortiguadores."}}
        ]
      }}

    Mensaje:
    """{mensaje}"""
    '''

    llm_response = llm(template.format(mensaje=message))
    parsed = json.loads(llm_response)
    return parsed["intents"]


def generate_social_response_prompt(text: str) -> str:
    return f'''
    Actuá como vendedor humano. Este fragmento es SOCIAL.
    Respondé en tono cálido, 1–2 líneas, sin catálogo, sin allowed_products,
    sin pedir marca/modelo.
    Texto del usuario: "{text}"
    '''


def generate_clarification_prompt(text: str) -> str:
    return f'''
    El usuario necesita una aclaración. NO es búsqueda todavía.
    Pedí solo la información mínima (marca, modelo, año).
    Texto: "{text}"
    '''


def generate_product_response_prompt(text: str, allowed_products: Any) -> str:
    return f'''
    Este fragmento es una consulta TÉCNICA de productos.
    Trabajá EXCLUSIVAMENTE con estos productos permitidos:
    {json.dumps(allowed_products)}

    Respondé como vendedor mayorista:
    - pocas líneas
    - comparaciones claras
    - preguntar si quiere agregar al carrito
    Texto: "{text}"
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
            respuestas.append(llm(generate_social_response_prompt(span)))
            continue

        if intent_type == "clarification":
            respuestas.append(llm(generate_clarification_prompt(span)))
            continue

        if intent_type == "product_search":
            allowed_products = run_allowed_products_search(span)
            respuestas.append(llm(generate_product_response_prompt(span, allowed_products)))
            continue

        if intent_type == "cart_action":
            respuestas.append(process_cart_action(span, aplicar_accion_carrito))
            continue

    return " ".join(respuestas)
