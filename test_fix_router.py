#!/usr/bin/env python3
"""
Test para verificar que el fix del router funciona correctamente
cuando el centroid es None (catálogo no cargado).
"""

from pipeline.router_dynamic import router_fase0_dynamic

def test_router_with_none_centroid():
    """Test que el router usa fallback con keywords cuando centroid=None"""
    print("="*60)
    print("🧪 TEST: Router con centroid=None (catálogo no cargado)")
    print("="*60)
    print()

    test_cases = [
        # (query, expected_route)
        ("hola", "social"),
        ("buenos dias", "social"),
        ("como estas", "social"),
        ("gracias", "social"),
        ("chau", "social"),

        ("filtro aceite", "technical"),
        ("bujia honda", "technical"),
        ("pastillas freno", "technical"),
        ("amortiguador yamaha", "technical"),
        ("kit transmision", "technical"),
        ("bateria", "technical"),
        ("honda wave", "technical"),
        ("yamaha fz", "technical"),
        ("filtro para honda cg 150", "technical"),
    ]

    passed = 0
    failed = 0

    for query, expected in test_cases:
        result = router_fase0_dynamic(query, None)  # centroid=None
        actual = result["route"]
        score = result["score"]
        fallback = result.get("fallback_mode", False)

        if actual == expected:
            status = "✅ PASS"
            passed += 1
        else:
            status = "❌ FAIL"
            failed += 1

        print(f'{status} | "{query}"')
        print(f'       → {actual} (score={score:.2f}, fallback={fallback})')
        if actual != expected:
            print(f'       → Esperado: {expected}')
        print()

    print("="*60)
    print(f"📊 RESULTADOS: {passed} passed, {failed} failed")
    print("="*60)

    if failed == 0:
        print("🎉 ¡Todos los tests pasaron!")
        return True
    else:
        print("⚠️  Algunos tests fallaron")
        return False


def test_router_comparison():
    """Comparar comportamiento con/sin centroid"""
    print()
    print("="*60)
    print("🔬 COMPARACIÓN: Router con y sin centroid")
    print("="*60)
    print()

    # Simular centroid con un catálogo mini
    from pipeline.router_dynamic import build_catalog_centroid

    mini_catalog = [
        "bujia ngk honda wave",
        "filtro aceite yamaha fz",
        "pastilla freno kawasaki",
        "kit transmision suzuki",
        "amortiguador delantero"
    ]

    centroid = build_catalog_centroid(mini_catalog)
    print(f"✅ Centroid generado: {centroid is not None}")
    print()

    queries = ["hola", "filtro aceite", "buenos dias", "bujia honda"]

    print("Query              | Con centroid      | Sin centroid (fallback)")
    print("-"*65)

    for query in queries:
        with_centroid = router_fase0_dynamic(query, centroid)
        without_centroid = router_fase0_dynamic(query, None)

        print(f"{query:18} | {with_centroid['route']:10} ({with_centroid['score']:.2f}) | "
              f"{without_centroid['route']:10} ({without_centroid['score']:.2f})")

    print()


if __name__ == "__main__":
    success1 = test_router_with_none_centroid()
    test_router_comparison()

    exit(0 if success1 else 1)
