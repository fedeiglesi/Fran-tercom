import json
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from multi_intent import (
    generate_clarification_prompt,
    generate_product_prompt,
    generate_social_prompt,
    orchestrate,
    parse_multi_intent,
)


@pytest.fixture
def dummy_llm():
    def _llm(prompt: str) -> str:
        # detect prompts that expect JSON vs plain text
        if "intents" in prompt or "intenciones" in prompt.lower():
            return json.dumps(
                {
                    "intents": [
                        {"type": "social", "span": "hola", "confidence": 0.9, "data": {}},
                        {"type": "product_search", "span": "necesito pastillas", "confidence": 0.8, "data": {"query": "pastillas"}},
                        {"type": "cart_action", "span": "sumalas", "confidence": 0.7, "data": {"action": "add"}},
                    ]
                }
            )
        return f"LLM({prompt.strip()})"

    return _llm


def test_parse_multi_intent_returns_intents(dummy_llm):
    intents = list(parse_multi_intent(dummy_llm, "hola, necesito pastillas"))

    assert len(intents) == 3
    assert intents[0]["type"] == "social"
    assert intents[0]["span"] == "hola"
    assert "confidence" in intents[0]


def test_generate_prompts_are_human_focused():
    social_prompt = generate_social_prompt("hola")
    clarification_prompt = generate_clarification_prompt("que modelos?")
    product_prompt = generate_product_prompt("pastillas", ["a", "b"])

    assert "modo de charla social" in social_prompt
    assert "aclarar" in clarification_prompt.lower()
    assert "allowed_products" in product_prompt


def test_orchestrate_respects_priority(dummy_llm):
    replies = orchestrate(
        dummy_llm,
        "hola, necesito pastillas",
        run_allowed_products_search=lambda span: ["p1"],
        aplicar_accion_carrito=lambda text: f"cart({text})",
    )

    # Order: social -> product_search -> cart_action
    assert "LLM(El usuario está en modo de charla social" in replies
    assert replies.index("LLM(El usuario está consultando sobre productos") > replies.index("LLM(El usuario está en modo de charla social")
    assert replies.index("cart(sumalas)") > replies.index("LLM(El usuario está consultando sobre productos")


@pytest.fixture
def four_intents_llm():
    def _llm(prompt: str) -> str:
        if "intents" in prompt or "intenciones" in prompt.lower():
            return json.dumps(
                {
                    "intents": [
                        {"type": "social", "span": "hola buen día", "confidence": 0.9, "data": {}},
                        {"type": "clarification", "span": "qué motos tenés", "confidence": 0.85, "data": {}},
                        {
                            "type": "product_search",
                            "span": "necesito pastillas de freno",
                            "confidence": 0.8,
                            "data": {"query": "pastillas"},
                        },
                        {
                            "type": "cart_action",
                            "span": "sumalas al carrito",
                            "confidence": 0.75,
                            "data": {"action": "add"},
                        },
                    ]
                }
            )
        return f"LLM({prompt.strip()})"

    return _llm


def test_orchestrate_handles_four_intents_same_message(four_intents_llm):
    replies = orchestrate(
        four_intents_llm,
        "hola buen día, necesito pastillas de freno, sumalas al carrito y decime qué motos tenés",
        run_allowed_products_search=lambda span: ["p1", "p2"],
        aplicar_accion_carrito=lambda text: f"cart({text})",
    )

    # Priority: social -> clarification -> product_search -> cart_action
    assert replies.index("LLM(El usuario está en modo de charla social") == 0
    assert replies.index("LLM(El usuario necesita aclarar algo") > replies.index("LLM(El usuario está en modo de charla social")
    assert replies.index("LLM(El usuario está consultando sobre productos") > replies.index("LLM(El usuario necesita aclarar algo")
    assert replies.endswith("cart(sumalas al carrito)")
