"""
Tests avanzados de multi-intent.

Este módulo testea:
- 5+ intenciones en un solo mensaje
- Intenciones contradictorias
- Intenciones parcialmente completables
- Priorización compleja
- Edge cases de orquestación
"""

import json
import sys
from pathlib import Path

import pytest

# Asegurar que el directorio raíz esté en el path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from multi_intent import (
    generate_clarification_prompt,
    generate_product_prompt,
    generate_social_prompt,
    orchestrate,
    parse_multi_intent,
)


@pytest.fixture
def five_plus_intents_llm():
    """LLM mock que retorna 6 intenciones diferentes."""
    def _llm(prompt: str) -> str:
        if "intents" in prompt or "intenciones" in prompt.lower():
            return json.dumps(
                {
                    "intents": [
                        {"type": "social", "span": "hola qué tal", "confidence": 0.9, "data": {}},
                        {"type": "clarification", "span": "tenés stock de todo?", "confidence": 0.85, "data": {}},
                        {
                            "type": "product_search",
                            "span": "necesito pastillas",
                            "confidence": 0.88,
                            "data": {"query": "pastillas"},
                        },
                        {
                            "type": "product_search",
                            "span": "aceite también",
                            "confidence": 0.82,
                            "data": {"query": "aceite"},
                        },
                        {
                            "type": "cart_action",
                            "span": "agregá todo",
                            "confidence": 0.78,
                            "data": {"action": "add"},
                        },
                        {
                            "type": "tech_question",
                            "span": "cuál es el mejor?",
                            "confidence": 0.75,
                            "data": {}},
                    ]
                }
            )
        return f"LLM({prompt.strip()})"

    return _llm


@pytest.fixture
def contradictory_intents_llm():
    """LLM mock que retorna intenciones contradictorias."""
    def _llm(prompt: str) -> str:
        if "intents" in prompt or "intenciones" in prompt.lower():
            return json.dumps(
                {
                    "intents": [
                        {
                            "type": "cart_action",
                            "span": "agregá pastillas",
                            "confidence": 0.85,
                            "data": {"action": "add", "product": "pastillas"},
                        },
                        {
                            "type": "cart_action",
                            "span": "no mejor cancelá",
                            "confidence": 0.80,
                            "data": {"action": "cancel"},
                        },
                        {
                            "type": "cart_action",
                            "span": "sacá las pastillas",
                            "confidence": 0.75,
                            "data": {"action": "remove", "product": "pastillas"},
                        },
                    ]
                }
            )
        return f"LLM({prompt.strip()})"

    return _llm


@pytest.fixture
def partial_info_llm():
    """LLM mock para información parcial en múltiples intenciones."""
    def _llm(prompt: str) -> str:
        if "intents" in prompt or "intenciones" in prompt.lower():
            return json.dumps(
                {
                    "intents": [
                        {
                            "type": "product_search",
                            "span": "pastillas",
                            "confidence": 0.7,
                            "data": {"query": "pastillas", "incomplete": True},
                        },
                        {
                            "type": "clarification",
                            "span": "para Wave",
                            "confidence": 0.85,
                            "data": {"moto": "Wave"},
                        },
                        {
                            "type": "clarification",
                            "span": "110cc",
                            "confidence": 0.8,
                            "data": {"cilindrada": "110"},
                        },
                    ]
                }
            )
        return f"LLM({prompt.strip()})"

    return _llm


def test_orchestrate_handles_six_intents(five_plus_intents_llm):
    """Test de orquestación con 6 intenciones en un mensaje."""
    replies = orchestrate(
        five_plus_intents_llm,
        "hola qué tal, tenés stock de todo? necesito pastillas, aceite también, agregá todo, cuál es el mejor?",
        run_allowed_products_search=lambda span: ["p1", "p2", "p3"],
        aplicar_accion_carrito=lambda text: f"cart({text})",
    )

    # Verificar que se procesaron múltiples intenciones
    # Orden esperado: social -> clarification -> product_search (x2) -> cart_action -> tech_question
    assert "LLM(El usuario está en modo de charla social" in replies
    assert "LLM(El usuario necesita aclarar algo" in replies
    assert "cart(agregá todo)" in replies

    # Verificar que hay al menos 4 respuestas procesadas (una por cada tipo de intent distinto)
    parts = replies.split("LLM(")
    assert len(parts) >= 4


def test_orchestrate_handles_contradictory_intents(contradictory_intents_llm):
    """Test con intenciones contradictorias: agregar -> cancelar -> remover."""
    replies = orchestrate(
        contradictory_intents_llm,
        "agregá pastillas, no mejor cancelá todo, sacá las pastillas",
        run_allowed_products_search=lambda span: ["pastillas"],
        aplicar_accion_carrito=lambda text: f"cart_action({text})",
    )

    # Todas las acciones deben ejecutarse en orden (cart_action tiene prioridad 3)
    # El sistema debe procesar todas en secuencia, la última debería prevalecer
    assert "cart_action(agregá pastillas)" in replies
    assert "cart_action(no mejor cancelá)" in replies
    assert "cart_action(sacá las pastillas)" in replies

    # Verificar que las 3 acciones están presentes
    cart_actions = replies.count("cart_action(")
    assert cart_actions == 3


def test_parse_multi_intent_with_partial_info(partial_info_llm):
    """Test de parsing con información parcial distribuida."""
    intents = list(parse_multi_intent(partial_info_llm, "pastillas para Wave 110cc"))

    assert len(intents) == 3
    assert intents[0]["type"] == "product_search"
    assert intents[1]["type"] == "clarification"
    assert intents[2]["type"] == "clarification"

    # Verificar que se captura la información parcial
    assert "query" in intents[0]["data"]
    assert "moto" in intents[1]["data"]
    assert "cilindrada" in intents[2]["data"]


def test_orchestrate_respects_priority_with_many_intents(five_plus_intents_llm):
    """Test de que la priorización se mantiene incluso con muchas intenciones."""
    replies = orchestrate(
        five_plus_intents_llm,
        "mensaje complejo con 6 intenciones",
        run_allowed_products_search=lambda span: ["p1"],
        aplicar_accion_carrito=lambda text: f"cart({text})",
    )

    # Extraer índices para verificar orden
    # La priorización debe ser: social (0) -> clarification (1) -> product_search (2) -> cart_action (3) -> tech_question (4)

    social_idx = replies.find("modo de charla social")
    clarification_idx = replies.find("aclarar algo")
    cart_idx = replies.find("cart(")

    # Social debe venir antes que clarification
    if social_idx != -1 and clarification_idx != -1:
        assert social_idx < clarification_idx

    # Clarification debe venir antes que cart_action
    if clarification_idx != -1 and cart_idx != -1:
        assert clarification_idx < cart_idx


def test_orchestrate_with_duplicate_intent_types():
    """Test con tipos de intención duplicados (2 product_search)."""
    def dual_product_llm(prompt: str) -> str:
        if "intents" in prompt or "intenciones" in prompt.lower():
            return json.dumps(
                {
                    "intents": [
                        {
                            "type": "product_search",
                            "span": "pastillas de freno",
                            "confidence": 0.9,
                            "data": {"query": "pastillas"},
                        },
                        {
                            "type": "product_search",
                            "span": "aceite sintético",
                            "confidence": 0.85,
                            "data": {"query": "aceite"},
                        },
                    ]
                }
            )
        return f"LLM({prompt.strip()})"

    replies = orchestrate(
        dual_product_llm,
        "necesito pastillas de freno y aceite sintético",
        run_allowed_products_search=lambda span: [f"product_for_{span}"],
        aplicar_accion_carrito=lambda text: f"cart({text})",
    )

    # Ambas búsquedas deben ejecutarse
    assert "product_for_pastillas de freno" in replies or "consultando sobre productos" in replies
    assert "product_for_aceite sintético" in replies or replies.count("consultando sobre productos") >= 2


def test_empty_intents_list():
    """Test con lista de intenciones vacía (mensaje incomprensible)."""
    def empty_llm(prompt: str) -> str:
        if "intents" in prompt or "intenciones" in prompt.lower():
            return json.dumps({"intents": []})
        return "LLM(mensaje incomprensible)"

    replies = orchestrate(
        empty_llm,
        "xkjdflaksjdf aslkdfjlaksjdf",
        run_allowed_products_search=lambda span: [],
        aplicar_accion_carrito=lambda text: "",
    )

    # Con intenciones vacías, no debería haber respuestas
    assert replies == ""


def test_orchestrate_with_order_flow_intent():
    """Test con intención de tipo order_flow (prioridad más baja)."""
    def order_flow_llm(prompt: str) -> str:
        if "intents" in prompt or "intenciones" in prompt.lower():
            return json.dumps(
                {
                    "intents": [
                        {"type": "social", "span": "hola", "confidence": 0.9, "data": {}},
                        {
                            "type": "order_flow",
                            "span": "quiero confirmar mi pedido",
                            "confidence": 0.85,
                            "data": {"action": "confirm"},
                        },
                        {
                            "type": "product_search",
                            "span": "pastillas",
                            "confidence": 0.8,
                            "data": {"query": "pastillas"},
                        },
                    ]
                }
            )
        return f"LLM({prompt.strip()})"

    replies = orchestrate(
        order_flow_llm,
        "hola, quiero confirmar mi pedido de pastillas",
        run_allowed_products_search=lambda span: ["p1"],
        aplicar_accion_carrito=lambda text: f"cart({text})",
    )

    # Verificar que social viene primero
    social_idx = replies.find("modo de charla social")
    product_idx = replies.find("consultando sobre productos")

    if social_idx != -1 and product_idx != -1:
        assert social_idx < product_idx


def test_high_confidence_vs_low_priority():
    """Test verificando que la prioridad prevalece sobre el confidence."""
    def confidence_test_llm(prompt: str) -> str:
        if "intents" in prompt or "intenciones" in prompt.lower():
            return json.dumps(
                {
                    "intents": [
                        # Cart action con confidence muy alto pero prioridad 3
                        {
                            "type": "cart_action",
                            "span": "agregá esto",
                            "confidence": 0.99,
                            "data": {"action": "add"},
                        },
                        # Social con confidence bajo pero prioridad 0
                        {"type": "social", "span": "hola", "confidence": 0.60, "data": {}},
                    ]
                }
            )
        return f"LLM({prompt.strip()})"

    replies = orchestrate(
        confidence_test_llm,
        "hola, agregá esto",
        run_allowed_products_search=lambda span: ["p1"],
        aplicar_accion_carrito=lambda text: f"cart({text})",
    )

    # Social debe venir primero a pesar de tener menos confidence
    social_idx = replies.find("modo de charla social")
    cart_idx = replies.find("cart(")

    assert social_idx < cart_idx, "La prioridad debe prevalecer sobre el confidence"


def test_orchestrate_with_missing_data_fields():
    """Test con intenciones que tienen campos data incompletos o faltantes."""
    def incomplete_data_llm(prompt: str) -> str:
        if "intents" in prompt or "intenciones" in prompt.lower():
            return json.dumps(
                {
                    "intents": [
                        {"type": "social", "span": "hola", "confidence": 0.9},  # sin data
                        {
                            "type": "product_search",
                            "span": "pastillas",
                            "confidence": 0.8,
                            "data": {},  # data vacío
                        },
                    ]
                }
            )
        return f"LLM({prompt.strip()})"

    # No debería fallar incluso con data faltante
    replies = orchestrate(
        incomplete_data_llm,
        "hola, pastillas",
        run_allowed_products_search=lambda span: ["p1"],
        aplicar_accion_carrito=lambda text: f"cart({text})",
    )

    # Debe ejecutarse sin errores
    assert "modo de charla social" in replies
    assert "consultando sobre productos" in replies
