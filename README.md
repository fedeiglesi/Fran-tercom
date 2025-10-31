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
