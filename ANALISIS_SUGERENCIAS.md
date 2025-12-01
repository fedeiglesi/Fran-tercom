# Análisis de viabilidad de sugerencias

Este documento evalúa cada sugerencia propuesta, considerando la arquitectura actual (Fran 3.16 con fases estructuradas y razonamiento/reflexión) y el módulo experimental Fran 4.0 sobre Assistants API.

## 1. Structured Output (JSON Mode) en todas las fases
- **Estado actual**: Fran 3.16 ya usa outputs estructurados en las fases de understanding, reasoning, product selection y response, con schemas y logging JSON. 【F:FRAN_3.16_README.md†L5-L138】
- **Viabilidad**: Alta para Fase 1/5/6; media para reasoning interno (Fase 3/4) porque hoy la reflexión se apoya en prompts libres y postprocesamiento Python.
- **Beneficio**: Reduciríamos KeyError/parseos y simplificaríamos validaciones; alinea con metadatos ya capturados. Riesgo bajo.
- **Consideración**: Requiere revisar prompts actuales para que sigan siendo expresivos; cuidado con el tamaño de schemas en contextos largos.

## 2. Tools (function calling) para acciones internas
- **Estado actual**: Fran 3.16 ejecuta búsquedas y validaciones desde Python (fase 3). Herramientas no expuestas al LLM salvo los pasos predefinidos. 【F:FRAN_3.16_README.md†L27-L85】
- **Viabilidad**: Media-Alta. Añadir tools para búsqueda/validación permitiría que el modelo decida estrategias sin tanto glue code, pero exige gobernanza para evitar costos y proteger endpoints internos.
- **Beneficio**: Simplificación del pipeline y reintentos más naturales; compatibilidad con reflexión. Riesgo moderado de llamadas redundantes si no se fijan límites.

## 3. Re-ranking LLM con `response_format`
- **Estado actual**: El re-ranking se hace con heurísticas + reflexión, no con schemas de consolidación. 【F:FRAN_3.16_README.md†L68-L90】
- **Viabilidad**: Alta. Podemos encapsular criterios de compatibilidad y anti-fruta en un schema y dejar que el modelo devuelva lista ordenada + flags de descarte.
- **Beneficio**: Menos código de filtrado y mejor trazabilidad; riesgo bajo, costo LLM similar.

## 4. Contextos largos con streaming parcial
- **Estado actual**: El diseño considera chunks y delivery limitado (WhatsApp 1600 caracteres), pero no usa contextos > 8k ni streaming. 【F:README.md†L1-L12】【F:FRAN_3.16_README.md†L59-L61】
- **Viabilidad**: Técnica alta (modelos 200k tokens). Operativa media: WhatsApp y latencia pueden limitar valor de contextos gigantes; habría que paginar catálogos y controlar costos.
- **Beneficio**: Mejor manejo de histórico y catálogos grandes; streaming mejora UX. Riesgo: sobrecosto y prompts más frágiles si crecen sin límites.

## 5. Embeddings `text-embedding-3-large`
- **Estado actual**: No se documenta el modelo actual; se menciona FAISS híbrido + BM25. 【F:FRAN_3.16_README.md†L27-L34】【F:FRAN_3.16_README.md†L80-L85】
- **Viabilidad**: Alta. Catálogo de ~7k SKU es pequeño, migrar embeddings es barato. Necesita reprocesar vector store y ajustar normalización.
- **Beneficio**: Mejor recall/precision y menos alucinación de productos. Riesgo bajo (costo puntual de regeneración).

## 6. Prompt auto-adaptativo por fase
- **Estado actual**: Temperaturas fijas (ej. Fran 4.0 usa 0.7) y modos definidos en prompts, sin tuning dinámico por confianza. 【F:Fran_4.0.py†L83-L121】
- **Viabilidad**: Media. Requiere señales de confianza en tiempo real y rutas de código para ajustar `temperature/top_p` por mensaje.
- **Beneficio**: Respuestas más estables en follow-up y exploración controlada. Riesgo: mayor complejidad de orquestación y tuning.

## 7. Caching de respuestas (ETag-style)
- **Estado actual**: No hay caché documentada; solo observabilidad de métricas y circuit breaker. 【F:fran/observability.py†L1-L47】
- **Viabilidad**: Media-Alta. Fácil para mensajes sociales y razonamientos repetidos; se debe definir clave de caché y expiración por fase.
- **Beneficio**: Ahorro 20–30% de costo y latencia. Riesgo: respuestas desactualizadas si cambia catálogo o contexto; requiere invalidación ligada a catálogo.

## 8. Parallel Function Calls
- **Estado actual**: Búsquedas y validaciones son secuenciales. No hay soporte de paralelismo en la orquestación. 【F:fran/clients.py†L1-L25】
- **Viabilidad**: Media. Necesita redesign del pipeline para coordinar múltiples tools y consolidar resultados; cuidado con rate limits.
- **Beneficio**: Menor latencia (sub-1s) al consultar precio/stock en paralelo. Riesgo: complejidad de merges y control de errores simultáneos.

## 9. Assistant Orchestrator (`reasoning: enabled`)
- **Estado actual**: Fran 3.16 ya implementa razonamiento y reflexión custom; Fran 4.0 usa Assistants con file_search sin auto-planning. 【F:FRAN_3.16_README.md†L68-L90】【F:Fran_4.0.py†L83-L180】
- **Viabilidad**: Media. Podría simplificar la lógica Python, pero habría que mapear métricas/observabilidad actuales al nuevo modo y validar compliance (no inventar precios).
- **Beneficio**: Menos código y flujos más adaptativos. Riesgo: pérdida de control fino en entornos productivos; exigiría guardrails y tests exhaustivos.

## 10. Conversaciones multi-intent
- **Estado actual**: La fase de query understanding está pensada para una intención principal; no se documenta soporte multi-intent nativo. 【F:FRAN_3.16_README.md†L17-L45】
- **Viabilidad**: Media-Alta. Necesita ajustar schemas y rutas de búsqueda para manejar listas de intents y resultados combinados.
- **Beneficio**: Mejor cobertura de mensajes reales con múltiples pedidos; riesgo moderado en complejidad de ranking/UX de respuesta.

## Resumen general
- **Sugerencias de adopción inmediata**: (1) completar JSON Mode en todas las fases, (3) re-ranking con `response_format`, (5) embeddings 3-large. Son cambios acotados con alto beneficio y bajo riesgo.
- **Siguientes priorizables**: (2) tools internos, (7) caching, (10) multi-intent. Requieren diseño pero encajan con la arquitectura agentic.
- **Cambios estructurales**: (4) contextos largos/streaming, (6) prompts auto-adaptativos, (8) parallel tools, (9) orchestrator. Aportan valor pero demandan rediseño y controles para mantener observabilidad y anti-alucinación.
