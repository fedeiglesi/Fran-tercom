import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

from pipeline.llm_classifier_dynamic import build_classifier_schema, fase1_llm_classifier_dynamic
from pipeline.orquestador_v317 import orquestar_v317
from pipeline.phases_v317 import (
    PHASE2_SCHEMA,
    fase2_hybrid_search,
    fase3_compatibility_filter,
    fase4_llm2_reasoning,
)
from pipeline.router_dynamic import build_catalog_centroid, router_fase0_dynamic


def fake_embeddings(texts):
    vectors = []
    for text in texts:
        norm = len(text)
        vectors.append(np.array([float(norm), float(text.count(" ")), float(sum(ord(c) for c in text) % 13)], dtype="float32"))
    return vectors


@pytest.fixture
def mini_catalog():
    return [
        {
            "codigo": "X1",
            "descripcion": "Bujía iridio NGK Honda Wave",
            "descripcion_normalizada": "bujia iridio ngk honda wave",
            "marca_moto": "Honda",
            "modelo_moto": "Wave",
            "cilindrada": "110",
            "categoria_final": "bujia",
        },
        {
            "codigo": "X2",
            "descripcion": "Pastillas freno delanteras",  # sin datos de compatibilidad
            "descripcion_normalizada": "pastillas freno delanteras genericas",
            "marca_moto": "",
            "modelo_moto": "",
            "cilindrada": "",
            "categoria_final": "frenos",
        },
    ]


def test_router_dynamic_centroid(mini_catalog):
    centroid = build_catalog_centroid([row["descripcion"] for row in mini_catalog])
    tech = router_fase0_dynamic("Necesito una bujia", centroid)
    social = router_fase0_dynamic("hola", centroid)

    assert tech["route"] == "technical"
    assert social["route"] == "social"


def test_classifier_schema_and_validation(monkeypatch, mini_catalog):
    schema = build_classifier_schema(mini_catalog)

    fake_response = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                "intent": "busca_producto",
                "product_type": "bujia",
                "brand": "Honda",
                "model": "Wave",
                "displacement_cc": 110,
                "confidence": 0.88,
            })))
        ]
    )

    monkeypatch.setattr(
        "pipeline.llm_classifier_dynamic.client.chat.completions.create",
        lambda *args, **kwargs: fake_response,
    )

    parsed = fase1_llm_classifier_dynamic("Tenes bujia para Honda Wave?", mini_catalog, schema)
    assert parsed["brand"] == "Honda"
    assert "product_type" in schema["properties"]


def test_compatibility_and_reasoning(mini_catalog):
    search_output = fase2_hybrid_search("bujia honda", mini_catalog, embedding_fn=fake_embeddings, top_k=2)
    assert search_output["phase"] == "search"

    classifier = {
        "intent": "busca_producto",
        "product_type": "bujia",
        "brand": "Honda",
        "model": "Wave",
        "displacement_cc": 110,
    }

    compat_output = fase3_compatibility_filter(search_output, classifier)
    assert any(c["status"] == "pending_reasoning" for c in compat_output["candidates"])

    reasoning_output = fase4_llm2_reasoning(search_output, compat_output, classifier)
    product_ids = {c["product_id"] for c in reasoning_output["candidates_evaluated"]}
    assert product_ids == {"X1", "X2"}
    assert reasoning_output["phase"] == "llm2_reasoning"


def test_requery_and_fallback_loop(monkeypatch, mini_catalog):
    schema = build_classifier_schema(mini_catalog)

    fake_response = SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=json.dumps({
                "intent": "busca_producto",
                "product_type": None,
                "brand": None,
                "model": None,
                "displacement_cc": None,
                "confidence": 0.4,
            })))
        ]
    )
    monkeypatch.setattr(
        "pipeline.llm_classifier_dynamic.client.chat.completions.create",
        lambda *args, **kwargs: fake_response,
    )

    centroid = build_catalog_centroid([row["descripcion"] for row in mini_catalog])
    output = orquestar_v317("algo generico", mini_catalog, centroid, schema, embedding_fn=fake_embeddings, max_requeries=1)

    phases = [step.get("phase") for step in output["trace"] if isinstance(step, dict)]
    assert "requery" in phases  # re-query disparado por low confidence
    assert output["final_response"]["phase"] == "whatsapp_response"
    assert len(output["final_response"]["message"]) < 1000

