#!/usr/bin/env python3
"""
Test específico para los problemas encontrados en los logs de producción
"""

import sys
import os

# Agregar el directorio actual al path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Mock necesario para evitar errores de imports
os.environ["OPENAI_API_KEY"] = "test-key"
os.environ["CATALOG_URL"] = "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/refs/heads/Fran-3.13.2/catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv"

from app import detect_semantic_entities, _phase1_llm1_understanding

def test_copiones_no_flash():
    """
    Test que 'copiones' NO se detecta como 'flash' con nuevo threshold

    Problema original del log:
    - Input: "Mostrarme todas las copiones de baterías"
    - Bug: fuzzy matching detectaba "model": "flash" (copiones → flash con 88% threshold)
    - Fix: aumentar threshold a 92 y min token length a 4
    """

    print("=" * 60)
    print("TEST 1: Fuzzy matching - 'copiones' NO debe ser 'flash'")
    print("=" * 60)

    message = "Mostrarme todas las copiones de baterías"
    signals = detect_semantic_entities(message)
    understanding = _phase1_llm1_understanding(message)

    print(f"\nMensaje: '{message}'")
    print(f"Modelos detectados (fuzzy): {signals.get('models')}")
    print(f"Marcas detectadas (fuzzy): {signals.get('brands')}")
    print(f"Intent: {understanding.get('intent')}")
    print(f"Model extraído: {understanding.get('model')}")
    print(f"Product type: {understanding.get('product_type')}")

    # Assertions
    models = signals.get('models', [])
    extracted_model = understanding.get('model')

    # 'flash' NO debe estar en los modelos detectados por fuzzy matching
    assert 'flash' not in [m.lower() for m in models], \
        f"❌ 'flash' NO debe ser detectado en '{message}'. Modelos: {models}"

    # El LLM tampoco debe extraer 'flash'
    if extracted_model:
        assert extracted_model.lower() != 'flash', \
            f"❌ Model extraído no debe ser 'flash'. Got: {extracted_model}"

    print("\n✅ Test 1 PASADO: 'copiones' NO se confunde con 'flash'")


def test_price_query_detection():
    """
    Test que 'Tenes los precios?' se detecta como follow_up_precio

    Problema original del log:
    - Input: "Tenes los precios?"
    - Bug: extraía "product_type": "llave allen tipo t" (inventado)
    - Fix: detectar is_price_only_query y clasificar como follow_up_precio
    """

    print("\n" + "=" * 60)
    print("TEST 2: Follow-up precio - 'Tenes los precios?'")
    print("=" * 60)

    message = "Tenes los precios?"
    signals = detect_semantic_entities(message)
    understanding = _phase1_llm1_understanding(message)

    print(f"\nMensaje: '{message}'")
    print(f"Is price only query: {signals.get('is_price_only_query')}")
    print(f"Has technical: {signals.get('has_technical')}")
    print(f"Intent: {understanding.get('intent')}")
    print(f"Product type extraído: {understanding.get('product_type')}")
    print(f"Brand extraído: {understanding.get('brand')}")
    print(f"Model extraído: {understanding.get('model')}")

    # Assertions
    assert signals.get('is_price_only_query') == True, \
        "❌ 'Tenes los precios?' debe ser detectado como price-only query"

    assert understanding.get('intent') == 'follow_up_precio', \
        f"❌ Intent debe ser 'follow_up_precio', got: {understanding.get('intent')}"

    # NO debe extraer entidades inventadas
    assert understanding.get('product_type') is None, \
        f"❌ No debe extraer product_type para price query. Got: {understanding.get('product_type')}"

    assert understanding.get('brand') is None, \
        f"❌ No debe extraer brand para price query. Got: {understanding.get('brand')}"

    assert understanding.get('model') is None, \
        f"❌ No debe extraer model para price query. Got: {understanding.get('model')}"

    print("\n✅ Test 2 PASADO: 'Tenes los precios?' clasificado correctamente")


def test_other_price_queries():
    """Test otras variaciones de consultas de precio"""

    print("\n" + "=" * 60)
    print("TEST 3: Otras variaciones de precio")
    print("=" * 60)

    price_queries = [
        "cuanto cuesta?",
        "dame los precios",
        "cuanto me salen?",
        "pasame la cotizacion",
    ]

    for query in price_queries:
        signals = detect_semantic_entities(query)
        understanding = _phase1_llm1_understanding(query)

        print(f"\nQuery: '{query}'")
        print(f"  - Is price only: {signals.get('is_price_only_query')}")
        print(f"  - Intent: {understanding.get('intent')}")

        assert signals.get('is_price_only_query') == True, \
            f"❌ '{query}' debe ser price-only query"

        assert understanding.get('intent') == 'follow_up_precio', \
            f"❌ '{query}' debe ser follow_up_precio"

    print("\n✅ Test 3 PASADO: Todas las variaciones de precio funcionan")


def test_price_with_product_is_not_follow_up():
    """
    Test que consultas de precio CON producto específico
    NO se clasifican como follow_up_precio sino como busca_producto
    """

    print("\n" + "=" * 60)
    print("TEST 4: Precio + producto = busca_producto")
    print("=" * 60)

    test_cases = [
        "cuanto cuesta una bateria de gel?",
        "precio de pastillas de freno para honda wave",
        "dame cotizacion de amortiguadores",
    ]

    for message in test_cases:
        signals = detect_semantic_entities(message)
        understanding = _phase1_llm1_understanding(message)

        print(f"\nMensaje: '{message}'")
        print(f"  - Is price only: {signals.get('is_price_only_query')}")
        print(f"  - Has technical: {signals.get('has_technical')}")
        print(f"  - Intent: {understanding.get('intent')}")
        print(f"  - Product type: {understanding.get('product_type')}")

        # Cuando hay producto específico, NO debe ser price-only query
        assert signals.get('is_price_only_query') == False, \
            f"❌ '{message}' NO debe ser price-only (tiene producto específico)"

        # Debe ser busca_producto
        assert understanding.get('intent') in ['busca_producto', 'comparacion'], \
            f"❌ Intent debe ser busca_producto o comparacion, got: {understanding.get('intent')}"

        # Debe extraer alguna entidad técnica
        has_entities = (
            understanding.get('product_type') is not None
            or understanding.get('brand') is not None
            or len(signals.get('technical_tokens', [])) > 0
        )
        assert has_entities, \
            f"❌ Debe extraer entidades cuando hay producto específico: {message}"

    print("\n✅ Test 4 PASADO: Precio + producto se procesa correctamente")


if __name__ == "__main__":
    try:
        test_copiones_no_flash()
        test_price_query_detection()
        test_other_price_queries()
        test_price_with_product_is_not_follow_up()

        print("\n" + "=" * 60)
        print("✅ TODOS LOS TESTS DE PRODUCCIÓN PASARON")
        print("=" * 60)
        print("\nResumen de fixes:")
        print("1. ✅ Fuzzy matching threshold 88→92: 'copiones' ya NO matchea 'flash'")
        print("2. ✅ Min token length 3→4: reduce falsos positivos")
        print("3. ✅ is_price_only_query: detecta consultas de precio sin producto")
        print("4. ✅ Intent 'follow_up_precio': clasificación correcta de follow-ups")
        print("5. ✅ No extracción de entidades para price-only queries")
        print("6. ✅ Precio + producto = busca_producto (mantiene extracción)")

    except Exception as e:
        print(f"\n❌ TEST FALLIDO: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
