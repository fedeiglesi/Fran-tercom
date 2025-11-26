import importlib
import sys
from pathlib import Path

import pytest


@pytest.fixture
def patched_app(monkeypatch):
    """Load the app module with a dummy API key and patch heavy dependencies."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent))

    # Stub módulos pesados para que los tests no dependan de downloads externos
    import types

    class DummyLRUCache(dict):
        def __init__(self, *args, **kwargs):  # noqa: D401
            super().__init__()

        def __getitem__(self, key):
            return super().get(key)

        def __setitem__(self, key, value):
            super().__setitem__(key, value)

    monkeypatch.setitem(sys.modules, "rank_bm25", types.SimpleNamespace(BM25Okapi=None))
    monkeypatch.setitem(sys.modules, "dotenv", types.SimpleNamespace(load_dotenv=lambda: None))
    monkeypatch.setitem(sys.modules, "dotenv.main", types.SimpleNamespace(load_dotenv=lambda: None))
    monkeypatch.setitem(sys.modules, "cachetools", types.SimpleNamespace(LRUCache=DummyLRUCache))

    if "app" in sys.modules:
        del sys.modules["app"]

    app = importlib.import_module("app")

    history = []
    dummy_products = [
        {
            "name": "Pastilla de freno Wave",
            "code": "1234/56789-000",
            "price_ars": 5000,
            "price_usd": 5,
        }
    ]

    def fake_save_message(phone, content, role):
        history.append({"phone": phone, "content": content, "role": role})

    def fake_get_history_since(phone, days=1, limit=2000):
        relevant = [h for h in history if h["phone"] == phone]
        return relevant[-limit:]

    monkeypatch.setattr(app, "save_message", fake_save_message)
    monkeypatch.setattr(app, "get_history_since", fake_get_history_since)
    monkeypatch.setattr(app, "log_interaction", lambda *_, **__: None)
    monkeypatch.setattr(app, "log_performance", lambda *_, **__: None)
    monkeypatch.setattr(app, "update_sales_phase_from_intent", lambda *_, **__: None)
    monkeypatch.setattr(app, "save_last_search", lambda *_, **__: None)
    monkeypatch.setattr(app, "validate_and_fix_response", lambda reply, *_: reply)

    monkeypatch.setattr(
        app,
        "assess_context_quality",
        lambda *_: {
            "sufficient": True,
            "action": "ok",
            "avg_score": 80,
            "max_score": 90,
            "relevant_count": len(dummy_products),
        },
    )

    monkeypatch.setattr(
        app,
        "filter_by_relevance",
        lambda _query, products, min_score=None: products,
    )

    def fake_hybrid_search(query, phone=None, top_k=None):
        return [(dummy_products[0], 0.9)]

    monkeypatch.setattr(app, "hybrid_search", fake_hybrid_search)

    def fake_generate_reply(phone, user_message, catalog_products, execution_context, system_prompt=None):
        has_previous_reply = any(
            h["role"] == "assistant" and h["phone"] == phone for h in history
        )
        prefix = "Como te comenté antes, " if has_previous_reply else ""
        reply = (
            f"{prefix}tengo {catalog_products[0]['name']} "
            f"({catalog_products[0]['code']}) disponible."
        )
        return {"reply": reply, "plan": {"real_intent": "product_search"}, "execution": {}}

    monkeypatch.setattr(app, "generate_smart_ai_reply_v2", fake_generate_reply)

    return app, history, dummy_products


def test_orquestador_generates_coherent_reply(patched_app):
    app, history, products = patched_app

    reply = app.orquestar_fran("Necesito pastillas de freno", "+5491100000000")

    assert products[0]["name"] in reply
    assert products[0]["code"] in reply
    assert any(h["role"] == "assistant" for h in history)


def test_conversation_maintains_context(patched_app):
    app, history, products = patched_app
    phone = "+5491100001234"

    first_reply = app.orquestar_fran("Hola, busco frenos", phone)
    assert products[0]["name"] in first_reply

    second_reply = app.orquestar_fran("¿Te queda algo para la Wave?", phone)

    assert "Como te comenté antes" in second_reply
    assert len([h for h in history if h["role"] == "assistant"]) == 2
