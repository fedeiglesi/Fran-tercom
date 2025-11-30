#!/usr/bin/env python3
"""
Test simple para verificar que el saludo 'hola' funciona correctamente
sin ejecutar búsqueda de productos ni extraer entidades inventadas.
"""

import sys
import os

# Agregar el directorio actual al path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Mock necesario para evitar errores de imports
os.environ["OPENAI_API_KEY"] = "test-key"
os.environ["CATALOG_URL"] = "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/refs/heads/Fran-3.13.2/catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv"

from app import detect_semantic_entities, _phase1_llm1_understanding

def test_greeting_detection():
    """Test que 'hola' se detecta como saludo simple sin fuzzy matching"""

    print("=" * 60)
    print("TEST 1: Detección de saludo simple 'hola'")
    print("=" * 60)

    message = "hola"
    signals = detect_semantic_entities(message)

    print(f"\nMensaje: '{message}'")
    print(f"Señales sociales: {signals.get('social_markers')}")
    print(f"Has social: {signals.get('has_social')}")
    print(f"Has technical: {signals.get('has_technical')}")
    print(f"Is simple greeting: {signals.get('is_simple_greeting')}")
    print(f"Marcas detectadas (fuzzy): {signals.get('brands')}")
    print(f"Modelos detectados (fuzzy): {signals.get('models')}")

    # Assertions
    assert signals.get('has_social') == True, "❌ 'hola' debe tener señal social"
    assert signals.get('is_simple_greeting') == True, "❌ 'hola' debe ser detectado como saludo simple"
    assert signals.get('has_technical') == False, "❌ 'hola' NO debe tener señales técnicas"
    assert len(signals.get('brands', [])) == 0, "❌ 'hola' NO debe detectar marcas"
    assert len(signals.get('models', [])) == 0, "❌ 'hola' NO debe detectar modelos"

    print("\n✅ Test 1 PASADO: Detección de saludo simple funciona correctamente")

def test_phase1_intent_classification():
    """Test que la Fase 1 clasifica 'hola' como intent social"""

    print("\n" + "=" * 60)
    print("TEST 2: Clasificación de intent en Fase 1")
    print("=" * 60)

    message = "hola"
    understanding = _phase1_llm1_understanding(message)

    print(f"\nMensaje: '{message}'")
    print(f"Intent detectado: {understanding.get('intent')}")
    print(f"Brand extraído: {understanding.get('brand')}")
    print(f"Model extraído: {understanding.get('model')}")
    print(f"Product type extraído: {understanding.get('product_type')}")
    print(f"Displacement extraído: {understanding.get('displacement_cc')}")

    # Assertions
    assert understanding.get('intent') == 'social', "❌ Intent debe ser 'social' para 'hola'"
    assert understanding.get('brand') is None, "❌ Brand debe ser None para intent social"
    assert understanding.get('model') is None, "❌ Model debe ser None para intent social"
    assert understanding.get('product_type') is None, "❌ Product type debe ser None para intent social"
    assert understanding.get('displacement_cc') is None, "❌ Displacement debe ser None para intent social"

    print("\n✅ Test 2 PASADO: Intent social no extrae entidades de producto")

def test_other_greetings():
    """Test otros saludos comunes"""

    print("\n" + "=" * 60)
    print("TEST 3: Otros saludos comunes")
    print("=" * 60)

    greetings = ["buenas", "buen dia", "que tal", "gracias"]

    for greeting in greetings:
        signals = detect_semantic_entities(greeting)
        understanding = _phase1_llm1_understanding(greeting)

        print(f"\nSaludo: '{greeting}'")
        print(f"  - Is simple greeting: {signals.get('is_simple_greeting')}")
        print(f"  - Intent: {understanding.get('intent')}")
        print(f"  - Entidades extraídas: brand={understanding.get('brand')}, model={understanding.get('model')}")

        assert signals.get('is_simple_greeting') == True, f"❌ '{greeting}' debe ser saludo simple"
        assert understanding.get('intent') == 'social', f"❌ '{greeting}' debe tener intent social"
        assert understanding.get('brand') is None, f"❌ '{greeting}' no debe extraer brand"

    print("\n✅ Test 3 PASADO: Todos los saludos funcionan correctamente")

def test_multi_intent_messages():
    """Test mensajes con múltiples intenciones (social + product_search)"""

    print("\n" + "=" * 60)
    print("TEST 4: Multi-intent (social + product_search)")
    print("=" * 60)

    test_cases = [
        {
            "message": "hola, quiero baterías de gel",
            "expected_intent": "busca_producto",
            "should_extract_entities": True,
            "should_have_technical": True,
            "should_have_social": True,
        },
        {
            "message": "hola Fra, gracias! Ahora necesito amortiguadores para honda wave",
            "expected_intent": "busca_producto",
            "should_extract_entities": True,
            "should_have_technical": True,
            "should_have_social": True,
        },
        {
            "message": "buenas! quiero precio de pastillas de freno",
            "expected_intent": "busca_producto",
            "should_extract_entities": True,
            "should_have_technical": True,
            "should_have_social": True,
        },
    ]

    for test_case in test_cases:
        message = test_case["message"]
        signals = detect_semantic_entities(message)
        understanding = _phase1_llm1_understanding(message)
        metadata = understanding.get("metadata", {})

        print(f"\nMensaje: '{message}'")
        print(f"  - Intent: {understanding.get('intent')}")
        print(f"  - Has social: {signals.get('has_social')}")
        print(f"  - Has technical: {signals.get('has_technical')}")
        print(f"  - Is simple greeting: {signals.get('is_simple_greeting')}")
        print(f"  - Product type: {understanding.get('product_type')}")
        print(f"  - Technical tokens: {signals.get('technical_tokens')}")

        # Assertions
        assert signals.get('has_social') == test_case["should_have_social"], \
            f"❌ Mensaje debe tener señal social"
        assert signals.get('has_technical') == test_case["should_have_technical"], \
            f"❌ Mensaje debe tener señal técnica"
        assert signals.get('is_simple_greeting') == False, \
            f"❌ Multi-intent NO debe ser simple greeting"
        assert understanding.get('intent') == test_case["expected_intent"], \
            f"❌ Intent debe ser '{test_case['expected_intent']}' para multi-intent"

        # Si tiene señales técnicas, debe extraer entidades (aunque también tenga componente social)
        if test_case["should_extract_entities"]:
            has_entities = (
                understanding.get('product_type') is not None
                or understanding.get('brand') is not None
                or len(signals.get('technical_tokens', [])) > 0
            )
            assert has_entities, \
                f"❌ Debe extraer entidades técnicas en multi-intent: {message}"

    print("\n✅ Test 4 PASADO: Multi-intent funciona correctamente")

if __name__ == "__main__":
    try:
        test_greeting_detection()
        test_phase1_intent_classification()
        test_other_greetings()
        test_multi_intent_messages()

        print("\n" + "=" * 60)
        print("✅ TODOS LOS TESTS PASARON EXITOSAMENTE")
        print("=" * 60)
        print("\nResumen de correcciones:")
        print("1. ✅ Detección de saludos simples sin fuzzy matching")
        print("2. ✅ Intent 'social' clasificado correctamente para saludos puros")
        print("3. ✅ No se extraen entidades para saludos puros")
        print("4. ✅ Bypass temprano solo para saludos puros (no multi-intent)")
        print("5. ✅ Multi-intent funciona: mensajes con saludo + búsqueda extraen entidades")
        print("6. ✅ Fuzzy matching solo se ejecuta cuando hay señales técnicas")

    except Exception as e:
        print(f"\n❌ TEST FALLIDO: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
