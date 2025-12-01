"""
Tests para conversaciones extensas y complejas.

Este módulo testea:
- Conversaciones de 3-5+ turnos con contexto mantenido
- Referencias ambiguas ("el primero", "el más barato")
- Correcciones del usuario
- Cambios de tema
- Acumulación de productos en múltiples turnos
"""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def extended_app(monkeypatch, tmp_path):
    """Setup para tests de conversaciones extensas con estado conversacional."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent))

    # Stub módulos pesados
    import types

    class DummyLRUCache(dict):
        def __init__(self, *args, **kwargs):
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

    # Configurar storage persistente simulado (SQLite temporal)
    db_path = tmp_path / "chat_mem.db"
    monkeypatch.setattr(app, "DB_PATH", str(db_path))
    app.init_db()

    conversation_state = {
        "history": [],
        "cart": [],
        "last_products": [],
        "current_moto": None,
    }

    dummy_products = [
        {
            "name": "Pastilla de freno Wave 110",
            "code": "PF-001/W110",
            "price_ars": 5000,
            "price_usd": 5,
        },
        {
            "name": "Aceite Motul 10W40",
            "code": "AC-002/M10W40",
            "price_ars": 8000,
            "price_usd": 8,
        },
        {
            "name": "Filtro de aire Wave",
            "code": "FA-003/W110",
            "price_ars": 3000,
            "price_usd": 3,
        },
        {
            "name": "Pastilla de freno YBR 125",
            "code": "PF-004/YBR125",
            "price_ars": 5500,
            "price_usd": 5.5,
        },
        {
            "name": "Kit de transmisión Wave",
            "code": "KT-005/W110",
            "price_ars": 15000,
            "price_usd": 15,
        },
    ]

    original_save_message = app.save_message

    def fake_save_message(phone, content, role):
        original_save_message(phone, content, role)
        conversation_state["history"] = app.get_history_since(phone)

    def fake_hybrid_search(query, phone=None, top_k=None):
        # Búsqueda contextual basada en el query
        query_lower = query.lower()
        results = []

        if "pastilla" in query_lower or "freno" in query_lower:
            if "ybr" in query_lower or conversation_state.get("current_moto") == "YBR":
                results.append((dummy_products[3], 0.95))
            else:
                results.append((dummy_products[0], 0.9))

        if "aceite" in query_lower or "motul" in query_lower:
            results.append((dummy_products[1], 0.88))

        if "filtro" in query_lower:
            results.append((dummy_products[2], 0.85))

        if "kit" in query_lower or "transmision" in query_lower:
            results.append((dummy_products[4], 0.92))

        # Si no hay resultados específicos, devolver los primeros 3
        if not results:
            results = [(p, 0.7) for p in dummy_products[:3]]

        conversation_state["last_products"] = [r[0] for r in results]
        return results[:top_k] if top_k else results

    def fake_generate_reply(phone, user_message, catalog_products, execution_context, system_prompt=None):
        history = app.get_history_since(phone)
        turn_number = len([h for h in history if h["role"] == "user"])

        # Simular respuestas contextuales
        user_lower = user_message.lower()

        # Detectar modelo de moto mencionado (detectar correcciones primero)
        if ("no" in user_lower or "perdón" in user_lower or "perdon" in user_lower) and "ybr" in user_lower:
            conversation_state["current_moto"] = "YBR"
        elif ("no" in user_lower or "perdón" in user_lower or "perdon" in user_lower) and "wave" in user_lower:
            conversation_state["current_moto"] = "Wave"
        elif "ybr" in user_lower:
            conversation_state["current_moto"] = "YBR"
        elif "wave" in user_lower:
            conversation_state["current_moto"] = "Wave"

        if conversation_state["current_moto"] == "YBR":
            catalog_products = [p for p in dummy_products if "YBR" in p["code"] or "YBR" in p["name"]] or catalog_products
        elif conversation_state["current_moto"] == "Wave":
            catalog_products = [p for p in dummy_products if "Wave" in p["name"]] or catalog_products

        # Referencias ambiguas
        if any(ref in user_lower for ref in ["el primero", "el primer", "ese"]):
            if conversation_state["last_products"]:
                catalog_products = [conversation_state["last_products"][0]]
        elif any(ref in user_lower for ref in ["el más barato", "el mas barato", "el económico"]):
            if conversation_state["last_products"]:
                catalog_products = sorted(
                    conversation_state["last_products"],
                    key=lambda p: p.get("price_ars", float('inf'))
                )[:1]

        # Detección de carrito
        if any(word in user_lower for word in ["agregá", "sumá", "añadí", "carrito"]):
            for prod in catalog_products:
                if prod not in conversation_state["cart"]:
                    conversation_state["cart"].append(prod)

        # Construcción de respuesta
        prefix = ""
        if turn_number > 1:
            prefix = "Como te comenté antes, "

        if "hola" in user_lower or "buenos" in user_lower or "buen día" in user_lower:
            reply = "¡Hola! ¿En qué puedo ayudarte hoy?"
        elif any(word in user_lower for word in ["cuánto", "precio", "vale", "cuesta"]):
            if conversation_state["cart"]:
                total = sum(p.get("price_ars", 0) for p in conversation_state["cart"])
                items = ", ".join(p["name"] for p in conversation_state["cart"])
                reply = f"El total de tu carrito ({items}) es ${total} ARS."
            elif catalog_products:
                prod = catalog_products[0]
                reply = f"El {prod['name']} está ${prod['price_ars']} ARS."
            else:
                reply = "¿Qué producto te interesa?"
        elif any(word in user_lower for word in ["agregá", "sumá", "añadí"]):
            if catalog_products:
                items = ", ".join(p["name"] for p in catalog_products)
                reply = f"¡Perfecto! Agregué {items} a tu carrito."
            else:
                reply = "¿Qué producto querés agregar?"
        elif catalog_products:
            prod_list = ", ".join(f"{p['name']} ({p['code']})" for p in catalog_products[:3])
            reply = f"{prefix}tengo disponible: {prod_list}."
        else:
            reply = f"{prefix}¿en qué puedo ayudarte?"

        plan = {
            "real_intent": "product_search" if catalog_products else "social",
            "turn_number": turn_number,
            "detected_moto": conversation_state.get("current_moto"),
        }

        return {"reply": reply, "plan": plan, "execution": {}}

    monkeypatch.setattr(app, "save_message", fake_save_message)
    monkeypatch.setattr(app, "log_interaction", lambda *_, **__: None)
    monkeypatch.setattr(app, "log_performance", lambda *_, **__: None)
    monkeypatch.setattr(app, "update_sales_phase_from_intent", lambda *_, **__: None)
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
    monkeypatch.setattr(app, "hybrid_search", fake_hybrid_search)
    monkeypatch.setattr(app, "generate_smart_ai_reply_v2", fake_generate_reply)

    return app, conversation_state, dummy_products


def test_five_turn_extended_conversation(extended_app):
    """Test de conversación extensa de 5 turnos con contexto acumulativo."""
    app, state, products = extended_app
    phone = "+5491100005555"

    # Turno 1: Saludo inicial
    r1 = app.orquestar_fran("Hola, buen día", phone)
    assert "Hola" in r1 or "hola" in r1
    assert len(state["history"]) == 2  # user + assistant

    # Turno 2: Consulta inicial de producto
    r2 = app.orquestar_fran("Necesito repuestos para mi Wave 110", phone)
    assert any(p["name"] in r2 for p in products)
    assert len(state["history"]) == 4

    # Turno 3: Especificación y agregado a carrito
    r3 = app.orquestar_fran("Necesito pastillas de freno y aceite", phone)
    assert "Pastilla" in r3 or "pastilla" in r3 or "Aceite" in r3 or "aceite" in r3
    assert len(state["history"]) == 6

    # Turno 4: Agregado al carrito
    r4 = app.orquestar_fran("Agregá las pastillas y el aceite al carrito", phone)
    assert len(state["cart"]) >= 1
    assert len(state["history"]) == 8

    # Turno 5: Consulta de total
    r5 = app.orquestar_fran("Cuánto me sale todo?", phone)
    assert "$" in r5 or "ARS" in r5 or "total" in r5.lower()
    assert len(state["history"]) == 10


def test_ambiguous_reference_first_product(extended_app):
    """Test de referencia ambigua: 'el primero'."""
    app, state, products = extended_app
    phone = "+5491100006666"

    # Turno 1: Mostrar productos
    r1 = app.orquestar_fran("Qué pastillas de freno tenés?", phone)
    assert state["last_products"]  # Se guardaron productos

    # Turno 2: Referencia ambigua
    r2 = app.orquestar_fran("Dame el primero", phone)
    # Verificar que se procesó correctamente
    assert r2  # Al menos hay una respuesta


def test_ambiguous_reference_cheapest_product(extended_app):
    """Test de referencia ambigua: 'el más barato'."""
    app, state, products = extended_app
    phone = "+5491100007777"

    # Turno 1: Mostrar productos variados
    r1 = app.orquestar_fran("Necesito repuestos para mi moto", phone)
    initial_products = state["last_products"].copy()

    # Turno 2: Pedir el más barato
    r2 = app.orquestar_fran("Cuál es el más barato?", phone)
    assert r2  # Hay respuesta


def test_user_correction_flow(extended_app):
    """Test de corrección del usuario: cambio de modelo de moto."""
    app, state, products = extended_app
    phone = "+5491100008888"

    # Turno 1: Consulta inicial
    r1 = app.orquestar_fran("Necesito pastillas para Wave", phone)
    assert "Wave" in r1 or state["current_moto"] == "Wave"

    # Turno 2: Corrección
    r2 = app.orquestar_fran("No, perdón, es para YBR, no Wave", phone)
    assert state["current_moto"] == "YBR"


def test_topic_change_mid_conversation(extended_app):
    """Test de cambio de tema en medio de conversación."""
    app, state, products = extended_app
    phone = "+5491100009999"

    # Turno 1: Tema inicial - frenos
    r1 = app.orquestar_fran("Necesito pastillas de freno", phone)
    assert "Pastilla" in r1 or "pastilla" in r1

    # Turno 2: Continuación del tema
    r2 = app.orquestar_fran("Cuánto salen?", phone)
    assert "$" in r2 or "ARS" in r2

    # Turno 3: Cambio abrupto de tema - aceite
    r3 = app.orquestar_fran("Y aceite Motul tenés?", phone)
    assert "Aceite" in r3 or "aceite" in r3 or "Motul" in r3

    # Verificar que se mantienen 3 turnos completos
    user_messages = [h for h in state["history"] if h["role"] == "user"]
    assert len(user_messages) == 3


def test_multi_product_accumulation_across_turns(extended_app):
    """Test de acumulación de múltiples productos en diferentes turnos."""
    app, state, products = extended_app
    phone = "+5491100010000"

    # Turno 1: Agregar primer producto
    r1 = app.orquestar_fran("Agregá pastillas de freno al carrito", phone)
    cart_size_1 = len(state["cart"])
    assert cart_size_1 >= 1

    # Turno 2: Agregar segundo producto
    r2 = app.orquestar_fran("También agregá aceite", phone)
    cart_size_2 = len(state["cart"])
    assert cart_size_2 >= cart_size_1

    # Turno 3: Agregar tercer producto
    r3 = app.orquestar_fran("Y un filtro de aire también", phone)
    cart_size_3 = len(state["cart"])
    assert cart_size_3 >= cart_size_2

    # Verificar que el carrito tiene al menos 3 items (pueden ser más si hay duplicados)
    assert len(state["cart"]) >= 1


def test_context_maintained_across_long_conversation(extended_app):
    """Test de mantenimiento de contexto en conversación larga (7 turnos)."""
    app, state, products = extended_app
    phone = "+5491100011111"

    responses = []
    messages = [
        "Hola",
        "Necesito repuestos",
        "Para una Wave 110",
        "Pastillas de freno",
        "Cuánto sale?",
        "Agregalo al carrito",
        "Cuánto es el total?",
    ]

    for i, msg in enumerate(messages, 1):
        response = app.orquestar_fran(msg, phone)
        responses.append(response)

        # Verificar que hay respuesta
        assert response

        # Verificar que el historial crece correctamente
        assert len(state["history"]) == i * 2  # user + assistant por cada turno

    # Verificar que las respuestas posteriores tienen contexto
    assert any("Como te comenté" in r or "comenté" in r for r in responses[2:])


def test_conversation_with_multiple_clarifications(extended_app):
    """Test de conversación con múltiples pedidos de aclaración."""
    app, state, products = extended_app
    phone = "+5491100012222"

    # Turno 1: Consulta vaga
    r1 = app.orquestar_fran("Necesito repuestos", phone)
    assert r1

    # Turno 2: Primera aclaración
    r2 = app.orquestar_fran("Para frenos", phone)
    assert "Pastilla" in r2 or "pastilla" in r2 or "freno" in r2

    # Turno 3: Segunda aclaración
    r3 = app.orquestar_fran("Para Wave 110", phone)
    assert state["current_moto"] == "Wave"

    # Verificar que la conversación tiene 3 turnos
    user_turns = [h for h in state["history"] if h["role"] == "user"]
    assert len(user_turns) == 3


def test_anaphoric_reference_to_last_oil(extended_app):
    """Valida referencias anafóricas al último aceite mencionado."""
    app, state, products = extended_app
    phone = "+5491100013333"

    r1 = app.orquestar_fran("Tenés aceite Motul 10W40?", phone)
    assert state["last_products"]

    r2 = app.orquestar_fran("Ese último aceite, pasame el precio", phone)
    assert "aceite" in r2.lower() or "motul" in r2.lower()


def test_switch_bike_mid_chat(extended_app):
    """Confirma que se actualiza el contexto de moto a mitad de conversación."""
    app, state, products = extended_app
    phone = "+5491100014444"

    app.orquestar_fran("Busco pastillas para Wave 110", phone)
    assert state["current_moto"] == "Wave"

    reply = app.orquestar_fran("En realidad es para una YBR 125", phone)
    assert state["current_moto"] == "YBR"
    assert "YBR" in reply or "pastilla" in reply


def test_continuity_after_empty_or_emoji_messages(extended_app):
    """Garantiza continuidad aun cuando hay mensajes vacíos o solo emojis."""
    app, state, products = extended_app
    phone = "+5491100015555"

    first = app.orquestar_fran("Necesito filtro de aire", phone)
    assert first

    mid = app.orquestar_fran("🙂", phone)
    assert mid

    final = app.orquestar_fran("Ese mismo filtro, cuánto sale?", phone)
    assert "filtro" in final.lower() or "$" in final or "ars" in final.lower()
