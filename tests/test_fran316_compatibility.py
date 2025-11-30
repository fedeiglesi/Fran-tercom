import pytest
import sys
import types
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

sys.modules.setdefault("rank_bm25", types.SimpleNamespace(BM25Okapi=None))
sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda: None))
sys.modules.setdefault("dotenv.main", types.SimpleNamespace(load_dotenv=lambda: None))
sys.modules.setdefault("cachetools", types.SimpleNamespace(LRUCache=lambda *args, **kwargs: {}))

jsonschema_module = types.ModuleType("jsonschema")
jsonschema_module.validate = lambda *args, **kwargs: None
class DummyValidationError(Exception):
    pass
jsonschema_module.ValidationError = DummyValidationError
sys.modules.setdefault("jsonschema", jsonschema_module)

import app


def test_needs_llm_compatibility_detects_missing_structure(monkeypatch):
    catalog = [
        {"family_name": "Bujia", "moto_brand": "", "moto_model": ""},
        {"family_name": "Pastilla de Freno", "moto_brand": "Honda", "moto_model": "Wave"},
        {"family_name": "Pastilla de Freno", "moto_brand": "Yamaha", "moto_model": "FZ"},
    ]
    profile = app.build_family_compatibility_profile(catalog)
    monkeypatch.setattr(app, "FAMILY_COMPATIBILITY_PROFILE", profile)

    assert app.needs_llm_compatibility("Bujia") is True
    assert app.needs_llm_compatibility("Pastilla de Freno") is True


def test_compatibility_filter_llm_policy(monkeypatch):
    monkeypatch.setattr(app, "FAMILY_COMPATIBILITY_PROFILE", {app.normalize_search_query("bujia"): {"family_name": "Bujia", "total": 1, "with_structured": 0}})
    understanding = {"brand": "Honda", "model": "Wave", "raw_query": "bujia wave"}
    search_payload = {
        "results": [
            {
                "product_id": "BU-1",
                "name": "Bujia NGK CR7HSA",
                "catalog_data": {
                    "marca_moto": "",
                    "modelo_moto": "",
                    "family": "Bujia",
                    "has_structured_compatibility": False,
                    "needs_llm_compatibility": True,
                },
            }
        ],
        "target_family": "Bujia",
        "needs_llm_compatibility": True,
    }

    payload = app._phase3_compatibility_filter(understanding, search_payload)
    assert payload["filter_policy"] == "llm_assisted"
    assert payload["candidates_after_filter"][0]["proceedes_to_llm2"] is True


def test_llm2_reasoning_decision_schema(monkeypatch):
    understanding = {"brand": "Honda", "model": "Wave", "displacement_cc": 110}
    search_payload = {
        "results": [
            {
                "product_id": "PF-W110-001",
                "name": "Pastilla de freno Wave 110",
                "catalog_data": {
                    "marca_moto": "Honda",
                    "modelo_moto": "Wave 110",
                    "cilindrada": "110",
                    "family": "Pastilla de Freno",
                    "has_structured_compatibility": True,
                    "needs_llm_compatibility": False,
                },
            }
        ]
    }
    filter_payload = {
        "candidates_after_filter": [
            {"product_id": "PF-W110-001", "status": "hard_compatible", "proceedes_to_llm2": True}
        ]
    }

    result = app._phase4_llm2_reasoning(understanding, search_payload, filter_payload)
    assert result["candidates_evaluated"][0]["compatibility_decision"] == "compatible"
    assert result["candidates_evaluated"][0]["confidence_score"] >= 0.85


def test_phase5_requery_strategies_cover_all_attempts(monkeypatch):
    understanding = {"brand": "Honda", "model": "Wave 110S", "raw_query": "bujia wave"}
    strategy1, query1 = app._phase5_requery(understanding, 1)
    strategy2, query2 = app._phase5_requery(understanding, 2)
    strategy3, query3 = app._phase5_requery(understanding, 3)

    assert strategy1 == "expand_moto_variants"
    assert strategy2 == "minimal_core_query"
    assert strategy3 == "semantic_expansion"
    assert all(q for q in [query1, query2, query3])


def test_response_includes_follow_up(monkeypatch):
    understanding = {"brand": "Honda", "model": "Wave", "raw_query": "bujia"}
    reasoning_payload = {"candidates_evaluated": [], "llm2_confidence_overall": 0.4}
    fallback_payload = {"fallback_message": "Necesito más datos"}

    response = app._phase7_llm3_response(understanding, reasoning_payload, fallback_payload)
    assert "¿Querés precio o ver más opciones?" in response["whatsapp_response"]


def test_social_intent_skips_product_mode():
    semantic = app.detect_semantic_entities("Hola")
    assert semantic["has_social"] is True
    assert semantic["has_technical"] is False

    understanding = app._phase1_llm1_understanding("Hola")
    assert understanding["intent"] == "social"


class DummyChoice:
    def __init__(self, content: str):
        self.message = types.SimpleNamespace(content=content)


def test_social_reply_prefers_llm(monkeypatch):
    captured = {}

    def _fake_completion(**kwargs):
        captured["called"] = True
        return types.SimpleNamespace(choices=[DummyChoice("¡Hola! Te ayudo con repuestos cuando quieras.")])

    monkeypatch.setattr(app, "llm_client", types.SimpleNamespace(completion=_fake_completion))

    reply = app.build_social_reply("54911", "Hola Fran")

    assert captured.get("called") is True
    assert "hola" in reply.lower()
    assert "repuesto" in reply.lower()


def test_social_reply_has_fallback(monkeypatch):
    monkeypatch.setattr(app, "llm_client", types.SimpleNamespace(completion=lambda **kwargs: (_ for _ in ()).throw(Exception("boom"))))

    reply = app.build_social_reply("54911", "Hola, gracias!", {"follow_up_markers": ["gracias"]})

    assert "hola" in reply.lower()
    assert "repuesto" in reply.lower()
    assert "de nuevo" in reply.lower()


def test_orchestrator_skips_search_for_social(monkeypatch):
    monkeypatch.setattr(app, "rate_limit_check", lambda phone: True)
    monkeypatch.setattr(app, "save_message", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "log_interaction", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "log_performance", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "update_sales_phase_from_intent", lambda *args, **kwargs: None)
    monkeypatch.setattr(app, "sanitize_input", lambda text, max_length=1500: text)

    def _fail_search(*args, **kwargs):
        raise AssertionError("Hybrid search should not run for social intent")

    monkeypatch.setattr(app, "_phase2_hybrid_search", _fail_search)

    reply = app.orquestar_fran_v316("Hola", phone="54911")

    assert "hola" in reply.lower()
    assert "repuesto" in reply.lower()
