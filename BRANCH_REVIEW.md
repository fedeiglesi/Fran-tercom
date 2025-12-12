# Revisión de rama y flujos

## Arquitectura actual
- El orquestador `orquestar_v317` encadena el enrutador dinámico, el clasificador LLM y un ciclo de búsqueda híbrida con filtros de compatibilidad, razonamiento, reconsultas y fallback antes de generar la respuesta de WhatsApp. La lógica corta temprano en rutas sociales o cuando el clasificador detecta intención social. 【F:pipeline/orquestador_v317.py†L6-L58】
- Las búsquedas combinan BM25 y FAISS con normalización de texto y un ranking fusionado. Los resultados incluyen metadatos estructurados para las fases siguientes y son validados con `jsonschema`. 【F:pipeline/phases_v317.py†L32-L128】【F:pipeline/phases_v317.py†L143-L196】
- La orquestación multi-intento prioriza social → aclaraciones → comparaciones → búsquedas → acciones de carrito → checkout, aplicando los prompts especializados por tipo para mantener coherencia mayorista. 【F:multi_intent.py†L62-L118】

## Observaciones e inconsistencias
- La fase de razonamiento marca `needs_requery` cuando la confianza cae < 0.35 incluso si ya catalogó el producto como incompatible por marca/modelo, activando reconsultas en escenarios sin camino de éxito y alargando la conversación. 【F:pipeline/phases_v317.py†L821-L915】

## Hallazgos recientes y brecha con el estado del arte
- El README promete una pila asíncrona con FastAPI, LangGraph y Qdrant, pero la rama activa sigue en Flask + FAISS en memoria y no expone endpoints async ni orquestación con grafo. Esto genera deuda de comunicación (la documentación induce a errores operativos) y dificulta la adopción de prácticas modernas como tracing estructurado o control de concurrencia. 【F:README.md†L1-L27】【F:app.py†L1-L80】
- La búsqueda híbrida reconstruye BM25 y FAISS en cada request (`_build_bm25_index`, `_build_faiss_index` dentro de `fase2_hybrid_search`). En catálogos de cientos/miles de productos esto multiplica la latencia y el consumo de CPU/ram por conversación, mientras que el estado del arte usa índices persistentes (vector DBs como Qdrant/Milvus) y precálculo de embeddings con caching caliente. 【F:pipeline/phases_v317.py†L339-L354】【F:pipeline/phases_v317.py†L492-L536】
- El parser de multi-intent carga el JSON del LLM sin try/except; cualquier respuesta malformada corta todo el flujo. Las stacks modernas encapsulan el LLM con validación/auto-retries o herramientas de parsing robusto (pydantic, JSON mode con reintentos) para evitar caídas. 【F:multi_intent.py†L17-L77】

## Ejercicios y pruebas con catálogo
- Se añadieron pruebas que cargan ejemplos reales del catálogo y simulan conversaciones complejas con múltiples intenciones (saludo, aclaración, dos búsquedas mayoristas y armado de carrito). Se verifica que las búsquedas por intent mantienen el orden y que los prompts incluyen los productos permitidos del catálogo. 【F:tests/test_catalog_multi_intent_simulations.py†L1-L97】
