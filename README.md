# ARQUITECTURA FRAN 3.1:

┌─────────────────────────────────────┐
│ 1. CATÁLOGO                         │
├─────────────────────────────────────┤
│ • CSV cargado manualmente           │
│ • Sin Assistants API                │
│ • Sin file_search de OpenAI         │
└─────────────────────────────────────┘
         ↓
┌─────────────────────────────────────┐
│ 2. BÚSQUEDA (RAG PROPIO)            │
├─────────────────────────────────────┤
│ • FAISS (vectores locales)          │
│ • RapidFuzz (fuzzy match)           │
│ • Híbrido: 60% semántico + 40% fuzzy│
│ • Embeddings: text-embedding-3-small│
└─────────────────────────────────────┘
         ↓
┌─────────────────────────────────────┐
│ 3. LÓGICA DETERMINISTA              │
├─────────────────────────────────────┤
│ • run_agent_rule_based()            │
│ • Detecta intenciones (keywords)    │
│ • Genera respuesta estructurada     │
└─────────────────────────────────────┘
         ↓
┌─────────────────────────────────────┐
│ 4. LLM (GPT-4o) - REESCRITURA       │
├─────────────────────────────────────┤
│ • Chat Completions API              │
│ • Reescribe respuesta determinista │
│ • Añade tono natural + contexto     │
│ • Memoria 3 días (30 msgs)          │
└─────────────────────────────────────┘

En resumen:
┌─────────────────────────────────────┐
│ ARQUITECTURA                        │
├─────────────────────────────────────┤
│ • CSV → Catálogo                    │
│ • FAISS + RapidFuzz (híbrido)       │
│ • LÓGICA DETERMINISTA primero       │
│ • GPT-4o REESCRIBE después          │
│ • Memoria: 3 días (30 msgs)         │
│ • SQLite local                      │
└─────────────────────────────────────┘

FLUJO:
1. Usuario → mensaje
2. run_agent_rule_based() decide
3. Genera respuesta estructurada
4. GPT-4o reescribe con tono natural
5. Agrega contexto de 3 días

Características:
	•	✅ Lógica determinista (keywords/patterns)
	•	✅ GPT-4o como “pulidor” de respuestas
	•	✅ Memoria 3 días (vs 1 día en 2.6)
	•	✅ Fallback: si GPT falla → respuesta determinista
	•	✅ Datos críticos manejados por código
