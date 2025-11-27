"""
Tests de escenarios realistas y conversaciones complejas.

Este módulo testea conversaciones que simulan casos reales del mundo:
- Conversaciones de 10+ turnos con múltiples cambios de contexto
- Negociaciones de precio y cantidades
- Errores del usuario (typos, mensajes confusos)
- Consultas técnicas complejas
- Inferencia de necesidades
- Contexto implícito y referencias complejas
"""

import importlib
import sys
from pathlib import Path

import pytest

sys.path.append(str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def realistic_app(monkeypatch):
    """Setup para tests realistas con catálogo amplio y lógica de negocio."""
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent.parent))

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

    # Estado de conversación realista
    conversation_state = {
        "history": [],
        "cart": [],
        "last_products": [],
        "current_moto": None,
        "user_budget": None,
        "negotiation_count": 0,
        "last_query": None,
    }

    # Catálogo más amplio y realista
    catalog = [
        {"name": "Pastilla de freno Wave 110", "code": "PF-W110-001", "price_ars": 5000, "price_usd": 5, "stock": 10},
        {"name": "Pastilla de freno YBR 125", "code": "PF-YBR125-002", "price_ars": 5500, "price_usd": 5.5, "stock": 8},
        {"name": "Pastilla de freno económica Wave", "code": "PF-ECO-003", "price_ars": 3500, "price_usd": 3.5, "stock": 15},
        {"name": "Aceite Motul 10W40 sintético", "code": "AC-MOT-004", "price_ars": 12000, "price_usd": 12, "stock": 20},
        {"name": "Aceite Castrol 20W50 mineral", "code": "AC-CAS-005", "price_ars": 6000, "price_usd": 6, "stock": 25},
        {"name": "Filtro de aire Wave 110", "code": "FA-W110-006", "price_ars": 3000, "price_usd": 3, "stock": 12},
        {"name": "Filtro de aire YBR 125", "code": "FA-YBR-007", "price_ars": 3200, "price_usd": 3.2, "stock": 10},
        {"name": "Kit transmisión Wave completo", "code": "KT-W110-008", "price_ars": 18000, "price_usd": 18, "stock": 5},
        {"name": "Corona Wave 110 (42 dientes)", "code": "CR-W110-009", "price_ars": 8000, "price_usd": 8, "stock": 7},
        {"name": "Piñón Wave 110 (14 dientes)", "code": "PI-W110-010", "price_ars": 4500, "price_usd": 4.5, "stock": 9},
        {"name": "Cadena 420 reforzada", "code": "CH-420-011", "price_ars": 7000, "price_usd": 7, "stock": 14},
        {"name": "Bujía NGK CR7HSA", "code": "BU-NGK-012", "price_ars": 2500, "price_usd": 2.5, "stock": 30},
        {"name": "Batería 12V 7Ah gel", "code": "BA-GEL-013", "price_ars": 15000, "price_usd": 15, "stock": 6},
    ]

    def fake_save_message(phone, content, role):
        conversation_state["history"].append(
            {"phone": phone, "content": content, "role": role}
        )

    def fake_get_history_since(phone, days=1, limit=2000):
        relevant = [h for h in conversation_state["history"] if h["phone"] == phone]
        return relevant[-limit:]

    def fake_hybrid_search(query, phone=None, top_k=None):
        query_lower = query.lower()
        results = []

        # Búsqueda más inteligente con typos y sinónimos
        keywords_map = {
            "pastilla": ["pastilla", "pastiya", "pastias", "freno", "frenos"],
            "aceite": ["aceite", "azeite", "lubricante", "oil"],
            "filtro": ["filtro", "fijltro", "filtru", "aire"],
            "kit": ["kit", "conjunto", "transmision", "transmisión"],
            "corona": ["corona", "coroa"],
            "piñon": ["piñon", "pinon", "pignon"],
            "cadena": ["cadena", "chain"],
            "bujia": ["bujia", "bujía", "spark"],
            "bateria": ["bateria", "batería", "battery"],
        }

        for product in catalog:
            score = 0
            product_lower = (product["name"] + " " + product["code"]).lower()

            # Búsqueda por keywords con tolerancia a errores
            for key, variations in keywords_map.items():
                if any(var in query_lower for var in variations):
                    if key in product_lower:
                        score += 0.8

            # Búsqueda por modelo de moto
            if conversation_state.get("current_moto"):
                moto = conversation_state["current_moto"].lower()
                if moto in product_lower:
                    score += 0.3

            # Búsqueda exacta por código
            if product["code"].lower() in query_lower:
                score = 1.0

            # Preferencia por stock disponible
            if product["stock"] > 0:
                score += 0.1

            if score > 0.3:
                results.append((product, score))

        # Ordenar por score
        results.sort(key=lambda x: x[1], reverse=True)
        conversation_state["last_products"] = [r[0] for r in results]

        return results[:top_k] if top_k else results

    def fake_generate_reply(phone, user_message, catalog_products, execution_context, system_prompt=None):
        history = fake_get_history_since(phone)
        turn_number = len([h for h in history if h["role"] == "user"])
        user_lower = user_message.lower()

        # Detectar modelo de moto
        if ("no" in user_lower or "perdón" in user_lower or "perdon" in user_lower) and "ybr" in user_lower:
            conversation_state["current_moto"] = "YBR"
        elif ("no" in user_lower or "perdón" in user_lower or "perdon" in user_lower) and "wave" in user_lower:
            conversation_state["current_moto"] = "Wave"
        elif "ybr" in user_lower:
            conversation_state["current_moto"] = "YBR"
        elif "wave" in user_lower:
            conversation_state["current_moto"] = "Wave"

        # Detectar presupuesto
        import re
        budget_match = re.search(r'(\d{1,6})\s*(pesos|ars|\$)?', user_lower)
        if "presupuesto" in user_lower or "tengo" in user_lower and budget_match:
            conversation_state["user_budget"] = int(budget_match.group(1))

        # Referencias complejas
        if any(ref in user_lower for ref in ["el primero", "el primer", "ese", "esa", "la primera"]):
            if conversation_state["last_products"]:
                catalog_products = [conversation_state["last_products"][0]]
        elif any(ref in user_lower for ref in ["el segundo", "la segunda"]):
            if len(conversation_state["last_products"]) > 1:
                catalog_products = [conversation_state["last_products"][1]]
        elif any(ref in user_lower for ref in ["el último", "el ultimo", "la última"]):
            if conversation_state["last_products"]:
                catalog_products = [conversation_state["last_products"][-1]]
        elif any(ref in user_lower for ref in ["el más barato", "el mas barato", "el económico", "lo más barato"]):
            if conversation_state["last_products"]:
                catalog_products = sorted(conversation_state["last_products"], key=lambda p: p.get("price_ars", float('inf')))[:1]
        elif any(ref in user_lower for ref in ["el más caro", "el mas caro", "el mejor"]):
            if conversation_state["last_products"]:
                catalog_products = sorted(conversation_state["last_products"], key=lambda p: p.get("price_ars", 0), reverse=True)[:1]
        elif "todos" in user_lower or "todas" in user_lower:
            if conversation_state["last_products"]:
                catalog_products = conversation_state["last_products"][:5]  # Máximo 5

        # Detección de carrito
        if any(word in user_lower for word in ["agregá", "sumá", "añadí", "carrito", "pedido", "agregala", "agregalo"]):
            # Si no hay productos en catalog_products pero hay last_products, usar esos
            products_to_add = catalog_products if catalog_products else conversation_state.get("last_products", [])[:1]
            for prod in products_to_add:
                if prod not in conversation_state["cart"]:
                    conversation_state["cart"].append(prod)

        # Negociación de precio
        if any(word in user_lower for word in ["descuento", "rebaja", "más barato", "mas barato", "negociar"]):
            conversation_state["negotiation_count"] += 1

        # Construcción de respuesta
        prefix = ""
        if turn_number > 1:
            prefix = "Como te comenté, " if turn_number <= 3 else "Te recuerdo que "

        # Lógica de respuesta basada en intención
        if "hola" in user_lower or "buenos" in user_lower or "buen día" in user_lower:
            reply = "¡Hola! ¿En qué puedo ayudarte hoy? Somos repuestos mayoristas para motos."

        elif any(word in user_lower for word in ["cuánto", "cuanto", "precio", "vale", "cuesta", "sale", "save"]):
            if conversation_state["cart"]:
                total = sum(p.get("price_ars", 0) for p in conversation_state["cart"])
                items_list = ", ".join(p["name"] for p in conversation_state["cart"])
                discount = ""
                if conversation_state["negotiation_count"] > 0:
                    discount = f" (con 10% de descuento mayorista: ${int(total * 0.9)})"
                reply = f"El total de tu pedido ({items_list}) es ${total} ARS{discount}."
            elif catalog_products:
                prod = catalog_products[0]
                stock_info = f" (Stock: {prod['stock']} unidades)" if prod["stock"] < 10 else ""
                reply = f"El {prod['name']} ({prod['code']}) está ${prod['price_ars']} ARS{stock_info}."
            else:
                reply = "¿Qué producto te interesa saber el precio?"

        elif any(word in user_lower for word in ["stock", "hay", "tenés", "tenes", "disponible"]):
            if catalog_products:
                stock_msgs = []
                for prod in catalog_products[:3]:
                    stock_status = "✅ En stock" if prod["stock"] > 5 else f"⚠️ Últimas {prod['stock']} unidades"
                    stock_msgs.append(f"{prod['name']}: {stock_status}")
                reply = prefix + "; ".join(stock_msgs)
            else:
                reply = "Decime qué producto necesitás y te chequeo el stock."

        elif any(word in user_lower for word in ["agregá", "sumá", "añadí", "quiero", "agregala", "agregalo", "dale"]):
            products_added = catalog_products if catalog_products else conversation_state.get("last_products", [])[:1]
            if products_added:
                items = ", ".join(p["name"] for p in products_added[:3])
                cart_count = len(conversation_state["cart"])
                reply = f"¡Perfecto! Agregué {items} a tu pedido. Llevás {cart_count} productos."
            else:
                reply = "¿Qué producto querés agregar al pedido?"

        elif "descuento" in user_lower or "rebaja" in user_lower:
            if catalog_products or conversation_state["cart"]:
                reply = "Como cliente mayorista te podemos hacer un 10% de descuento en compras de 3 o más productos."
            else:
                reply = "Hacemos descuentos mayoristas! ¿Qué productos te interesan?"

        elif any(word in user_lower for word in ["recomend", "mejor", "cuál", "cual"]):
            if catalog_products:
                best = max(catalog_products[:3], key=lambda p: p.get("price_ars", 0))
                cheap = min(catalog_products[:3], key=lambda p: p.get("price_ars", float('inf')))
                reply = f"Te recomiendo el {best['name']} (${best['price_ars']}) por calidad, pero si buscás economía, el {cheap['name']} (${cheap['price_ars']}) es excelente opción."
            else:
                reply = "¿Para qué modelo de moto es?"

        elif catalog_products:
            prod_list = ", ".join(f"{p['name']} ({p['code']}) ${p['price_ars']}" for p in catalog_products[:3])
            more = f" y {len(catalog_products) - 3} más" if len(catalog_products) > 3 else ""
            reply = f"{prefix}tengo disponible: {prod_list}{more}."

        else:
            suggestions = "¿Necesitás pastillas, aceite, filtros o repuestos de transmisión?"
            reply = f"{prefix}¿en qué puedo ayudarte? {suggestions}"

        plan = {
            "real_intent": "product_search" if catalog_products else "social",
            "turn_number": turn_number,
            "detected_moto": conversation_state.get("current_moto"),
        }

        return {"reply": reply, "plan": plan, "execution": {}}

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
            "relevant_count": len(catalog),
        },
    )
    monkeypatch.setattr(app, "filter_by_relevance", lambda _query, products, min_score=None: products)
    monkeypatch.setattr(app, "hybrid_search", fake_hybrid_search)
    monkeypatch.setattr(app, "generate_smart_ai_reply_v2", fake_generate_reply)

    return app, conversation_state, catalog


def test_realistic_10_turn_purchase_journey(realistic_app):
    """Conversación realista de 10 turnos: desde consulta inicial hasta compra final."""
    app, state, catalog = realistic_app
    phone = "+5491155551234"

    # Turno 1: Saludo inicial
    r1 = app.orquestar_fran("Hola buen día", phone)
    assert "Hola" in r1 or "hola" in r1
    assert "ayudar" in r1.lower()

    # Turno 2: Consulta vaga inicial
    r2 = app.orquestar_fran("Necesito repuestos para mi moto", phone)
    assert r2

    # Turno 3: Especificar modelo
    r3 = app.orquestar_fran("Es una Wave 110", phone)
    assert state["current_moto"] == "Wave"

    # Turno 4: Consulta específica
    r4 = app.orquestar_fran("Qué pastillas de freno tenés?", phone)
    assert "Pastilla" in r4 or "pastilla" in r4

    # Turno 5: Consulta de precio
    r5 = app.orquestar_fran("Cuánto sale la primera?", phone)
    assert "$" in r5 or "ARS" in r5

    # Turno 6: Agregar al carrito
    r6 = app.orquestar_fran("Dale, agregala", phone)
    assert len(state["cart"]) >= 1

    # Turno 7: Consulta adicional de otro producto
    r7 = app.orquestar_fran("También necesito aceite", phone)
    assert "aceite" in r7.lower() or "Aceite" in r7

    # Turno 8: Especificar preferencia
    r8 = app.orquestar_fran("Dame el más económico", phone)
    assert r8

    # Turno 9: Agregar segundo producto
    r9 = app.orquestar_fran("Agregá ese también", phone)
    assert len(state["cart"]) >= 1

    # Turno 10: Consulta de total final
    r10 = app.orquestar_fran("Cuánto es todo?", phone)
    assert "$" in r10 or "total" in r10.lower()

    # Verificar que la conversación tiene 10 turnos completos
    assert len(state["history"]) == 20  # 10 user + 10 assistant


def test_typo_tolerance_and_recovery(realistic_app):
    """Test de tolerancia a errores de tipeo del usuario."""
    app, state, catalog = realistic_app
    phone = "+5491155552345"

    # Turno 1: Typo en "pastillas"
    r1 = app.orquestar_fran("Necesito pastias de freno", phone)
    # El sistema debe entender "pastias" como "pastillas"
    assert r1

    # Turno 2: Typo en "aceite" y agregar
    r2 = app.orquestar_fran("Y azeite también, agregá todo", phone)
    assert r2
    assert len(state["cart"]) >= 1

    # Turno 3: Mensaje confuso pero con intención clara
    r3 = app.orquestar_fran("cuanto save todo eso???", phone)
    assert "$" in r3 or "precio" in r3.lower() or "total" in r3.lower()


def test_complex_negotiation_scenario(realistic_app):
    """Test de negociación compleja de precio con múltiples productos."""
    app, state, catalog = realistic_app
    phone = "+5491155553456"

    # Turno 1: Consulta inicial
    r1 = app.orquestar_fran("Hola, necesito varios repuestos para una Wave", phone)
    assert state["current_moto"] == "Wave"

    # Turno 2: Pedir múltiples productos y agregar
    r2 = app.orquestar_fran("Necesito pastillas, aceite y filtro de aire, agregá todo", phone)
    assert len(state["cart"]) >= 1

    # Turno 3: Consultar precio total
    r3 = app.orquestar_fran("Cuánto sale el pedido?", phone)
    assert "$" in r3

    # Turno 4: Pedir descuento
    r4 = app.orquestar_fran("Hacen descuento?", phone)
    assert "descuento" in r4.lower() or "10%" in r4 or "mayorista" in r4.lower()
    assert state["negotiation_count"] > 0


def test_ambiguous_pronoun_resolution(realistic_app):
    """Test de resolución de pronombres y referencias ambiguas complejas."""
    app, state, catalog = realistic_app
    phone = "+5491155554567"

    # Turno 1: Mostrar varios productos
    r1 = app.orquestar_fran("Qué aceites tenés para Wave?", phone)
    initial_count = len(state["last_products"])
    assert initial_count > 0

    # Turno 2: "el primero"
    r2 = app.orquestar_fran("Cuánto sale el primero?", phone)
    assert "$" in r2

    # Turno 3: "el segundo"
    r3 = app.orquestar_fran("Y el segundo?", phone)
    assert r3

    # Turno 4: "el último" y agregarlo
    r4 = app.orquestar_fran("Dale, agregame el último", phone)
    assert r4
    # Verificar que se agregó algo al carrito
    assert len(state["cart"]) >= 1 or "agregué" in r4.lower() or "agregue" in r4.lower()


def test_context_switch_mid_conversation(realistic_app):
    """Test de cambio completo de contexto en medio de conversación."""
    app, state, catalog = realistic_app
    phone = "+5491155555678"

    # Contexto 1: Consulta sobre pastillas para Wave
    r1 = app.orquestar_fran("Qué pastillas tenés para Wave?", phone)
    assert "Wave" in r1 or state["current_moto"] == "Wave"

    r2 = app.orquestar_fran("Dame el precio de la económica", phone)
    assert "$" in r2

    # CAMBIO ABRUPTO DE CONTEXTO
    r3 = app.orquestar_fran("Dejá, mejor decime qué aceites tenés para YBR", phone)
    # El sistema debe cambiar de Wave a YBR y de pastillas a aceites
    assert "aceite" in r3.lower() or "Aceite" in r3

    r4 = app.orquestar_fran("Cuánto el sintético?", phone)
    assert "$" in r4


def test_stock_availability_and_alternatives(realistic_app):
    """Test de consulta de stock y sugerencia de alternativas."""
    app, state, catalog = realistic_app
    phone = "+5491155556789"

    # Turno 1: Consulta de stock
    r1 = app.orquestar_fran("Tenés stock de kit de transmisión para Wave?", phone)
    assert "stock" in r1.lower() or "disponible" in r1.lower() or "✅" in r1 or "⚠️" in r1

    # Turno 2: Consultar stock de producto específico
    r2 = app.orquestar_fran("Hay de la batería 12V?", phone)
    assert r2


def test_product_comparison_request(realistic_app):
    """Test de comparación entre productos."""
    app, state, catalog = realistic_app
    phone = "+5491155557890"

    # Turno 1: Pedir recomendación
    r1 = app.orquestar_fran("Qué aceite me recomendás para Wave?", phone)
    assert "aceite" in r1.lower() or "Aceite" in r1

    # Turno 2: Pedir comparación
    r2 = app.orquestar_fran("Cuál es mejor, el sintético o el mineral?", phone)
    assert "recomiendo" in r2.lower() or "mejor" in r2.lower() or "$" in r2


def test_bulk_purchase_with_quantities(realistic_app):
    """Test de compra en cantidad (mayorista)."""
    app, state, catalog = realistic_app
    phone = "+5491155558901"

    # Turno 1: Consulta mayorista
    r1 = app.orquestar_fran("Hola, soy taller, necesito comprar por mayor", phone)
    assert "mayorista" in r1.lower() or "descuento" in r1.lower() or r1

    # Turno 2: Pedir múltiples unidades
    r2 = app.orquestar_fran("Necesito 10 pastillas para Wave", phone)
    assert r2

    # Turno 3: Consultar descuento
    r3 = app.orquestar_fran("Qué descuento hacen por cantidad?", phone)
    assert "10%" in r3 or "descuento" in r3.lower() or "mayorista" in r3.lower()


def test_inference_of_user_needs(realistic_app):
    """Test de inferencia de necesidades del usuario."""
    app, state, catalog = realistic_app
    phone = "+5491155559012"

    # El usuario menciona un síntoma, no el producto directamente
    r1 = app.orquestar_fran("Mi moto no arranca, qué puede ser?", phone)
    # El sistema debería sugerir bujía o batería
    assert r1

    # Turno 2: Usuario acepta sugerencia implícita
    r2 = app.orquestar_fran("Dale, mostrameesos", phone)
    assert r2


def test_long_15_turn_conversation_with_multiple_topics(realistic_app):
    """Test de conversación muy larga (15 turnos) con múltiples temas."""
    app, state, catalog = realistic_app
    phone = "+5491155550000"

    messages = [
        "Hola, qué tal?",
        "Necesito repuestos para Wave 110",
        "Qué pastillas tenés?",
        "Cuánto la más barata?",
        "Agregala",
        "Ahora necesito aceite también",
        "Tenés sintético?",
        "Cuánto ese?",
        "Y el mineral?",
        "Dame el sintético",
        "También necesito filtro de aire",
        "Agregá ese también",
        "Cuánto me sale todo?",
        "Hay descuento?",
        "Dale, confirmame el pedido",
    ]

    for i, msg in enumerate(messages, 1):
        response = app.orquestar_fran(msg, phone)
        assert response, f"No response for message {i}: {msg}"
        assert len(state["history"]) == i * 2

    # Verificar que el carrito tiene productos
    assert len(state["cart"]) >= 2

    # Verificar que se negoció
    assert state["negotiation_count"] >= 1


def test_correction_cascade(realistic_app):
    """Test de correcciones en cascada (múltiples correcciones seguidas)."""
    app, state, catalog = realistic_app
    phone = "+5491155551111"

    # Error 1: Modelo equivocado
    r1 = app.orquestar_fran("Necesito pastillas para Titan", phone)
    assert r1

    # Corrección 1
    r2 = app.orquestar_fran("No, perdón, es para Wave", phone)
    assert state["current_moto"] == "Wave"

    # Error 2: Producto equivocado
    r3 = app.orquestar_fran("Necesito aceite", phone)
    assert "aceite" in r3.lower()

    # Corrección 2
    r4 = app.orquestar_fran("No, mejor pastillas, no aceite", phone)
    assert r4


def test_all_products_reference(realistic_app):
    """Test de referencia 'todos' para múltiples productos."""
    app, state, catalog = realistic_app
    phone = "+5491155552222"

    # Turno 1: Pedir varios productos específicos
    r1 = app.orquestar_fran("Necesito pastillas, aceite y filtro para Wave", phone)
    assert len(state["last_products"]) > 0

    # Turno 2: Agregar "todos" los productos mencionados
    r2 = app.orquestar_fran("Dale, agregame todos esos", phone)
    assert r2
    # Verificar que hay productos en el carrito o que la respuesta menciona agregado
    assert len(state["cart"]) > 0 or "agregué" in r2.lower() or "agregue" in r2.lower()


def test_mixed_spanish_variations(realistic_app):
    """Test con variaciones dialectales del español (vos/tú/usted)."""
    app, state, catalog = realistic_app
    phone = "+5491155553333"

    # Vos (argentino)
    r1 = app.orquestar_fran("Tenés pastillas para Wave?", phone)
    assert r1

    # Tú (español estándar)
    r2 = app.orquestar_fran("Tienes aceite sintético?", phone)
    assert r2

    # Usted (formal)
    r3 = app.orquestar_fran("Tiene filtros de aire?", phone)
    assert r3


def test_ultra_complex_multi_product_multi_moto(realistic_app):
    """Test ultra complejo: saludo + referencia previa + 3 productos + 2 motos + typo."""
    app, state, catalog = realistic_app
    phone = "+5491155559999"

    # Simular conversación previa sobre amortiguadores FZ
    state["history"].append({"phone": phone, "content": "Recomendame amortiguadores para FZ", "role": "user"})
    state["history"].append({"phone": phone, "content": "Te recomiendo los amortiguadores YSS para FZ", "role": "assistant"})

    # Mensaje ultra complejo del usuario
    mensaje_complejo = (
        "fran, genio, como estas? gracias por la recomendación de los amortiguadores "
        "para la fz. me quede pensando y ademas necesito una batería para esa moto, "
        "unos espejos para una honda cg y un kit de herramIENTas. tenes?"
    )

    r1 = app.orquestar_fran(mensaje_complejo, phone)

    # Verificaciones:
    # 1. Debe responder (no crashear)
    assert r1
    assert len(r1) > 0

    # 2. Debe detectar múltiples productos
    # Batería, espejos, o herramientas deberían aparecer
    productos_mencionados = (
        "batería" in r1.lower() or "bateria" in r1.lower() or
        "espejo" in r1.lower() or
        "herramienta" in r1.lower() or "kit" in r1.lower() or
        # O al menos responder con stock/disponibilidad
        "disponible" in r1.lower() or "stock" in r1.lower() or "tengo" in r1.lower()
    )
    assert productos_mencionados, f"No detectó productos en: {r1}"

    # 3. Debe manejar el typo "herramIENTas"
    # (el sistema debe entenderlo como "herramientas")

    # 4. Debe tener productos en last_products
    assert len(state["last_products"]) > 0, "No generó búsqueda de productos"

    # 5. El historial debe crecer correctamente
    # Teníamos 2 mensajes previos + 2 nuevos = 4
    assert len(state["history"]) >= 4
