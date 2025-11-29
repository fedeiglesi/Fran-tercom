# Revisión de rama y flujos

## Arquitectura actual
- El orquestador `orquestar_v317` encadena el enrutador dinámico, el clasificador LLM y un ciclo de búsqueda híbrida con filtros de compatibilidad, razonamiento, reconsultas y fallback antes de generar la respuesta de WhatsApp. La lógica corta temprano en rutas sociales o cuando el clasificador detecta intención social. 【F:pipeline/orquestador_v317.py†L6-L58】
- Las búsquedas combinan BM25 y FAISS con normalización de texto y un ranking fusionado. Los resultados incluyen metadatos estructurados para las fases siguientes y son validados con `jsonschema`. 【F:pipeline/phases_v317.py†L32-L128】【F:pipeline/phases_v317.py†L143-L196】
- La orquestación multi-intento prioriza social → aclaraciones → comparaciones → búsquedas → acciones de carrito → checkout, aplicando los prompts especializados por tipo para mantener coherencia mayorista. 【F:multi_intent.py†L62-L118】

## Observaciones e inconsistencias
- El enrutador dinámico depende del centroide del catálogo, pero no valida que el vector esté normalizado o definido; si el catálogo cargado está vacío, `compute_similarity` podría dividir por cero y forzar todas las rutas a "social". 【F:pipeline/router_dynamic.py†L15-L43】
- La fase de razonamiento marca `needs_requery` con cualquier confianza < 0.45, incluso cuando ya se decidió incompatibilidad por diferencia de marca/modelo; esto puede provocar reconsultas redundantes en casos claramente incompatibles. 【F:pipeline/phases_v317.py†L214-L275】
- La respuesta de WhatsApp trunca a 1000 caracteres, mientras que el límite configurado en README es 1600, generando riesgo de cortar resultados antes de lo permitido por el canal. 【F:pipeline/phases_v317.py†L316-L343】【F:README.md†L7-L10】

## Ejercicios y pruebas con catálogo
- Se añadieron pruebas que cargan ejemplos reales del catálogo y simulan conversaciones complejas con múltiples intenciones (saludo, aclaración, dos búsquedas mayoristas y armado de carrito). Se verifica que las búsquedas por intent mantienen el orden y que los prompts incluyen los productos permitidos del catálogo. 【F:tests/test_catalog_multi_intent_simulations.py†L1-L97】
