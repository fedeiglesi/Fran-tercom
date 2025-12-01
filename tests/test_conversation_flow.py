import importlib
import sys
from datetime import datetime
from pathlib import Path

import pytest


@pytest.fixture
def patched_app(monkeypatch, tmp_path):
    """Load the app module with a dummy API key and patch heavy dependencies."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test_cart.db"))

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
    monkeypatch.setitem(
        sys.modules,
        "jsonschema",
        types.SimpleNamespace(
            Draft7Validator=lambda schema: types.SimpleNamespace(iter_errors=lambda data: [])
        ),
    )

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

    cart_products = [
        {"name": "Pastilla Wave 110", "code": "PF-W110", "price_ars": 5000, "price_usd": 5},
        {"name": "Pastilla YBR 125", "code": "PF-YBR125", "price_ars": 5200, "price_usd": 5.2},
        {"name": "Filtro de aire Wave", "code": "FA-W110", "price_ars": 3000, "price_usd": 3},
    ]

    test_catalog = dummy_products + cart_products

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
    monkeypatch.setattr(app, "validate_and_fix_response", lambda reply, *_: reply)

    last_search_store = {}

    def fake_save_last_search(phone, products, query):
        if not phone or not products:
            return
        last_search_store[phone] = {
            "products": products,
            "query": query,
            "metadata": {"timestamp": datetime.now().isoformat()},
            "age_minutes": 0,
        }

    def fake_get_last_search(phone):
        return last_search_store.get(phone)

    monkeypatch.setattr(app, "save_last_search", fake_save_last_search, raising=False)
    monkeypatch.setattr(app, "get_last_search", fake_get_last_search, raising=False)

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

    def fake_search_catalog_by_code(code):
        return next((p for p in test_catalog if p.get("code") == code), None)

    monkeypatch.setattr(app, "search_catalog_by_code", fake_search_catalog_by_code, raising=False)
    monkeypatch.setattr(app, "get_catalog_and_index", lambda: (test_catalog, None, None, None))

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


def test_cart_flow_updates_quantities_and_persists(patched_app):
    app, _history, _products = patched_app
    phone = "+5491100012345"

    catalog_products = [
        {"name": "Pastilla Wave 110", "code": "PF-W110", "price_ars": 5000},
        {"name": "Pastilla YBR 125", "code": "PF-YBR125", "price_ars": 5200},
    ]

    app.save_last_search(phone, catalog_products, "pastillas")
    last_search = app.get_last_search(phone)

    assert last_search and last_search.get("products")

    first_reply = app.handle_cart_action(phone, "Agregá 2 de la pastilla YBR 125")
    items_after_first = app.cart_get(phone)

    assert "2x" in first_reply
    assert items_after_first == [("PF-YBR125", 2, "Pastilla YBR 125", app.to_decimal_money(5200))]

    correction_reply = app.handle_cart_action(phone, "dejá la YBR en 1")
    items_after_correction = app.cart_get(phone)

    assert "1u" in correction_reply
    assert items_after_correction == [("PF-YBR125", 1, "Pastilla YBR 125", app.to_decimal_money(5200))]

    removal_reply = app.handle_cart_action(phone, "sacá la YBR")
    add_wave_reply = app.handle_cart_action(phone, "agregá 1 de la Wave 110")
    items_after_change = app.cart_get(phone)

    assert "saqué" in removal_reply.lower()
    assert "Wave" in add_wave_reply
    assert items_after_change == [("PF-W110", 1, "Pastilla Wave 110", app.to_decimal_money(5000))]


def test_add_is_idempotent_when_quantity_is_replaced(patched_app):
    app, _history, _products = patched_app
    phone = "+5491100098765"

    products = [
        {"name": "Filtro de aire Wave", "code": "FA-W110", "price_ars": 3000},
    ]

    app.save_last_search(phone, products, "filtro wave")
    assert app.get_last_search(phone)

    first_reply = app.handle_cart_action(phone, "agregá 1 filtro de aire wave")
    second_reply = app.handle_cart_action(phone, "agregá 3 filtro de aire wave")

    items = app.cart_get(phone)

    assert "1x" in first_reply
    assert "3u" in second_reply
    assert items == [("FA-W110", 3, "Filtro de aire Wave", app.to_decimal_money(3000))]
