import csv
import json
import sys
from itertools import islice
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))

from multi_intent import orchestrate


CATALOG_PATH = Path(__file__).resolve().parent.parent / "catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv"


@pytest.fixture(scope="module")
def catalog_sample():
    with CATALOG_PATH.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return list(islice(reader, 8))


@pytest.fixture
def complex_llm():
    def _llm(prompt: str) -> str:
        lower = prompt.lower()
        if "\"intents\"" in prompt or "intenciones" in lower:
            return json.dumps(
                {
                    "intents": [
                        {"type": "social", "span": "buenas fran", "confidence": 0.91, "data": {}},
                        {
                            "type": "clarification",
                            "span": "confirmame stock mayorista y descuentos",
                            "confidence": 0.82,
                            "data": {},
                        },
                        {
                            "type": "product_search",
                            "span": "necesito 50 cadenas reforzadas para honda",
                            "confidence": 0.8,
                            "data": {"query": "cadenas reforzadas"},
                        },
                        {
                            "type": "product_search",
                            "span": "tambien 30 pastillas delanteras titan",
                            "confidence": 0.78,
                            "data": {"query": "pastillas titan"},
                        },
                        {
                            "type": "cart_action",
                            "span": "arma el pedido completo",
                            "confidence": 0.76,
                            "data": {"action": "add"},
                        },
                    ]
                }
            )
        return f"LLM::{prompt.strip()}"

    return _llm


def _search_catalog(span: str, rows):
    text = span.lower()
    matches = []
    for row in rows:
        haystack = " ".join(
            filter(None, [row.get("descripcion", ""), row.get("sinonimos", ""), row.get("familia_nombre", "")])
        ).lower()
        if any(token in haystack for token in text.split()):
            matches.append(row["descripcion"])
        if len(matches) >= 5:
            break
    return matches or [rows[0]["descripcion"]]


def test_orchestrate_handles_wholesale_brief_with_catalog(complex_llm, catalog_sample):
    message = (
        "Buenas Fran, para stock mayorista necesito 50 cadenas reforzadas para Honda y 30 pastillas delanteras Titan; "
        "armá el pedido completo si tenés alternativas."
    )

    replies = orchestrate(
        complex_llm,
        message,
        run_allowed_products_search=lambda span: _search_catalog(span, catalog_sample),
        aplicar_accion_carrito=lambda text: f"cart::{text}",
    )

    assert replies.startswith("LLM::El usuario está en modo de charla social")
    assert "LLM::El usuario necesita aclarar algo" in replies

    product_prompts = [chunk for chunk in replies.split("LLM::") if "allowed_products" in chunk]
    assert len(product_prompts) == 2
    assert any(catalog_sample[0]["descripcion"] in prompt for prompt in product_prompts)

    assert replies.strip().endswith("cart::arma el pedido completo")


def test_multiple_product_spans_keep_order(complex_llm, catalog_sample, monkeypatch):
    calls = []

    def track_search(span: str):
        calls.append(span)
        return _search_catalog(span, catalog_sample)

    replies = orchestrate(
        complex_llm,
        "repito: 50 cadenas reforzadas y 30 pastillas delanteras titan, enviame lista mayorista y cerramos",
        run_allowed_products_search=track_search,
        aplicar_accion_carrito=lambda text: f"cart::{text}",
    )

    assert calls == [
        "necesito 50 cadenas reforzadas para honda",
        "tambien 30 pastillas delanteras titan",
    ]

    first_product_idx = replies.index("El usuario está consultando sobre productos")
    second_product_idx = replies.rindex("El usuario está consultando sobre productos")
    assert first_product_idx < second_product_idx
    assert "mayorista" in replies.lower()
