┌──────────────────────────────────────────────────────────────────┐
│                 1. MENSAJE DEL CLIENTE (WhatsApp)                │
└──────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│     2. NORMALIZACIÓN Y CONTEXTO                                  │
│     - Se arma short_history                                       │
│     - Se cargan memory + perfil + moto habitual                   │
│     - Se ejecuta búsqueda híbrida (FAISS + BM25)                  │
│     - Se obtienen allowed_products + context_quality              │
└──────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│     3. SALES ANALYSIS LLM (NUEVO BLOQUE PREVIO)                  │
│     - Analiza intención comercial                                 │
│     - Detecta señales de cierre                                   │
│     - Clasifica tipo de cliente                                   │
│     - Sugiere tono de venta                                       │
│     - Devuelve JSON {sales_analysis, alertas}                     │
└──────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
     (sales_analysis alimenta al planning unificado)
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│     4. PLANNING UNIFICADO (LLM RAZONADOR)                        │
│     Recibe JSON con:                                             │
│       • user_message                                              │
│       • short_history                                             │
│       • allowed_products                                          │
│       • context_quality                                           │
│       • sales_analysis (del paso previo)                          │
│       • carrito, pending_actions                                  │
│                                                                   │
│     Y genera:                                                     │
│       → status (OK | NEED_REQUERY | NEED_CLARIFICATION)           │
│       → real_intent                                               │
│       → productos_elegidos                                        │
│       → actions_to_execute                                        │
│       → products_strategy                                         │
│       → response_tone                                             │
│       → meta_razonamiento                                         │
│       → requery (si corresponde)                                  │
└──────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│ 5. LÓGICA DE REQUERY AUTOMÁTICO (MÁX 1–2 intentos)               │
│   ¿status = NEED_REQUERY?                                         │
│      ├── NO → seguir                                              │
│      └── SÍ → nueva búsqueda → new_allowed_products               │
│                     │                                             │
│                     └── run_planning_unificado NUEVAMENTE        │
└──────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│     6. VALIDACIÓN DEL JSON                                        │
│       - validate_reasoning_json()                                 │
│       - Asegura que:                                              │
│           productos_elegidos ∈ allowed_products                   │
│           requery completo                                        │
│           sales_analysis normalizado                              │
│           meta_razonamiento consistente                           │
└──────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│     7. EJECUCIÓN DEL PLAN INTERNO (SIDE EFFECTS)                 │
│       ejecutar_plan_interno():                                    │
│         • agrega al carrito si hace falta                         │
│         • guarda moto en memoria                                  │
│         • guarda búsqueda/última familia                          │
│         • resuelve referencias (“el primero”, “el más barato”)    │
│         • valida cantidades                                       │
└──────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│     8. CUSTOMER OUTPUT LLM                                        │
│       Usa:                                                        │
│         • productos_finales                                        │
│         • sales_analysis (tono, interés, señales)                  │
│         • meta_razonamiento (cautela, riesgos)                     │
│         • customer_state                                           │
│       Produce el MENSAJE FINAL para WhatsApp                       │
└──────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│     9. RESPUESTA AL CLIENTE (WhatsApp)                           │
│       - 100% humano, vendedor                                     │
│       - 0 errores                                                 │
│       - tono perfecto según análisis                              │
│       - máximo 5 productos                                        │
└──────────────────────────────────────────────────────────────────┘
