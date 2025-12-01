#!/usr/bin/env python3
"""
Script de debugging para diagnosticar por qué Fran solo responde "en que te puedo ayudar"
"""
import os
import sys

print("="*60)
print("🔍 DIAGNÓSTICO DE FRAN")
print("="*60)

# 1. Verificar variables de entorno
print("\n1️⃣ VARIABLES DE ENTORNO:")
env_vars = [
    "USE_FRAN_317",
    "USE_FRAN_316",
    "USE_FRAN_315",
    "BETA_PHONES",
    "OPENAI_API_KEY",
    "MODEL_NAME",
    "CATALOG_URL"
]

for var in env_vars:
    val = os.environ.get(var)
    if val:
        if "API_KEY" in var:
            print(f"  ✅ {var}={val[:10]}...")
        else:
            print(f"  ✅ {var}={val}")
    else:
        print(f"  ⚠️  {var}=<no configurada>")

# 2. Verificar archivos de catálogo
print("\n2️⃣ ARCHIVOS DE CATÁLOGO:")
import glob
catalogs = glob.glob("catalogo*.csv")
for cat in catalogs:
    size = os.path.getsize(cat)
    print(f"  ✅ {cat} ({size/1024/1024:.2f} MB)")

if not catalogs:
    print("  ❌ No se encontraron archivos de catálogo")

# 3. Test de carga del catálogo
print("\n3️⃣ TEST DE CARGA DEL CATÁLOGO:")
try:
    import pandas as pd

    # Intentar cargar el catálogo
    csv_file = "catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv"
    if os.path.exists(csv_file):
        df = pd.read_csv(csv_file, nrows=5)  # Solo 5 filas de prueba
        print(f"  ✅ Catálogo cargado: {csv_file}")
        print(f"  ✅ Columnas: {list(df.columns)[:5]}...")
        print(f"  ✅ Primeras filas:")
        for idx, row in df.iterrows():
            desc = row.get('descripcion') or row.get('descripcion_normalizada', 'N/A')
            print(f"     - {desc[:50]}")
    else:
        print(f"  ❌ No existe: {csv_file}")

except Exception as e:
    print(f"  ❌ Error cargando catálogo: {e}")

# 4. Test del router dinámico
print("\n4️⃣ TEST DEL ROUTER DINÁMICO:")
try:
    from pipeline.router_dynamic import build_catalog_centroid, router_fase0_dynamic

    # Catálogo de prueba
    test_descriptions = [
        "bujia ngk honda wave",
        "filtro aceite yamaha fz",
        "pastilla freno kawasaki",
        "kit transmision",
        "amortiguador delantero"
    ]

    print(f"  📝 Creando centroid de {len(test_descriptions)} descripciones...")
    centroid = build_catalog_centroid(test_descriptions)

    if centroid is None:
        print("  ❌ ERROR: Centroid es None")
        print("  → Causa probable: sentence-transformers no instalado o falla en carga")
    else:
        print(f"  ✅ Centroid generado correctamente")

        # Test de clasificación
        test_queries = [
            ("hola", "social"),
            ("filtro aceite", "technical"),
            ("buenos dias", "social"),
            ("bujia para honda", "technical"),
            ("como estas", "social")
        ]

        print("\n  🧪 Probando clasificación:")
        all_ok = True
        for query, expected in test_queries:
            result = router_fase0_dynamic(query, centroid)
            route = result["route"]
            score = result["score"]
            status = "✅" if route == expected else "❌"
            print(f"    {status} '{query}' → {route} (score={score:.3f}) [esperado: {expected}]")
            if route != expected:
                all_ok = False

        if all_ok:
            print("\n  ✅ Router funcionando correctamente")
        else:
            print("\n  ⚠️  Router no clasifica correctamente")
            print("  → Causa probable: threshold muy alto o centroid incorrecto")

except Exception as e:
    print(f"  ❌ Error en router: {e}")
    import traceback
    traceback.print_exc()

# 5. Test del clasificador LLM
print("\n5️⃣ TEST DEL CLASIFICADOR LLM:")
try:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("  ⚠️  OPENAI_API_KEY no configurada")
        print("  → Sin API key, el clasificador LLM fallará")
    else:
        print(f"  ✅ OPENAI_API_KEY configurada: {api_key[:10]}...")

        # Test simple
        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        try:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "Di 'OK'"}],
                max_tokens=10
            )
            result = response.choices[0].message.content
            print(f"  ✅ OpenAI API funciona: '{result}'")
        except Exception as e:
            print(f"  ❌ Error al llamar OpenAI: {e}")

except Exception as e:
    print(f"  ❌ Error: {e}")

# 6. Verificar función get_v317_resources
print("\n6️⃣ TEST DE get_v317_resources:")
try:
    # Necesitamos importar app completo
    # Esto puede fallar si hay dependencias no satisfechas
    print("  ⚠️  Requiere imports completos de app.py")
    print("  → Saltando test para evitar side effects")

except Exception as e:
    print(f"  ❌ Error: {e}")

# RESUMEN
print("\n" + "="*60)
print("📊 RESUMEN DEL DIAGNÓSTICO")
print("="*60)

print("\n✅ Pasos completados del diagnóstico")
print("\n⚠️  Posibles causas del problema:")
print("   1. Catálogo no se carga → centroid = None → todo es 'social'")
print("   2. Router threshold muy alto → clasifica mal")
print("   3. OPENAI_API_KEY faltante → clasificador falla")
print("   4. Versión incorrecta activada")

print("\n💡 Sugerencias:")
print("   • Verificar que el catálogo se cargue correctamente")
print("   • Verificar que build_catalog_centroid() retorne un vector válido")
print("   • Verificar logs de la aplicación para ver errores")
print("   • Probar con USE_FRAN_314=true como fallback")

print("\n" + "="*60)
