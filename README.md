┌─────────────────────────────────────┐
│ ARQUITECTURA                        │
├─────────────────────────────────────┤
│ • CSV → Catálogo                    │
│ • FAISS + RapidFuzz (híbrido)       │
│ • GPT-4o con FUNCTION CALLING       │
│ • Tools: 7 funciones                │
│ • Memoria: Hoy (20 msgs)            │
│ • SQLite local                      │
└─────────────────────────────────────┘

FLUJO:
1. Usuario → mensaje
2. GPT-4o decide qué tool usar
3. Ejecuta tools (búsqueda/carrito)
4. GPT-4o genera respuesta final
