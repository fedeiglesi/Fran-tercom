# 🚀 PROMPT: Implementar Fran 3.16 - Arquitectura Híbrida Agentic

## 📋 CONTEXTO DEL PROYECTO

Fran es un bot mayorista inteligente para WhatsApp que vende repuestos de motos (Tercom). Actualmente conviven dos arquitecturas:

- **Fran 3.14**: Orquestador unificado con razonamiento interno (pensar_con_llm) y capacidad de re-búsqueda dinámica
- **Fran 3.15**: Pipeline estructurado de 4 steps con JSON schemas rígidos

**Tu misión**: Crear Fran 3.16 que combine lo mejor de ambas arquitecturas siguiendo las mejores prácticas de sistemas agentic modernos (ReAct, Reflexion, Structured Outputs).

---

## 🎯 OBJETIVOS DE FRAN 3.16

### ✅ Heredar de Fran 3.14:
- Razonamiento interno explícito antes de actuar
- Capacidad de re-búsqueda cuando los resultados son insuficientes
- Flexibilidad para manejar edge cases complejos
- Auto-corrección basada en reflection

### ✅ Heredar de Fran 3.15:
- Structured outputs con JSON schemas para observabilidad
- Separación clara de fases (query understanding, search, selection, response)
- Validación estricta anti-alucinación
- Manejo especial de intents sociales

### ✅ Agregar Mejoras Modernas:
- **Reflexion pattern**: Auto-crítica de resultados antes de responder
- **Tool calling explícito**: Las acciones (búsqueda, carrito) son "herramientas" que el LLM decide usar
- **Reasoning transparency**: Logs detallados del razonamiento para debugging
- **Adaptive search**: Estrategias de búsqueda dinámicas según contexto
- **Quality gates**: Checkpoints de calidad en cada fase

---

## 🏗️ ARQUITECTURA FRAN 3.16

```
┌──────────────────────────────────────────────────────────────┐
│ ENTRADA: mensaje WhatsApp + phone                            │
└──────────────────────────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────┐
│ FASE 1: QUERY UNDERSTANDING (Structured Output)              │
│                                                               │
│ Template: query_understanding_schema (heredado de 3.15)      │
│                                                               │
│ Output:                                                       │
│   {                                                           │
│     "normalized_query": str,                                  │
│     "entities": {brand, model, category},                    │
│     "intent": "product_search|cart_action|social|...",       │
│     "confidence": float,                                      │
│     "needs_clarification": bool                              │
│   }                                                           │
│                                                               │
│ ✅ Quality Gate: Si confidence < 0.5 → pedir aclaración      │
└──────────────────────────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────┐
│ FASE 2: REASONING (Agentic - inspirado en 3.14)              │
│                                                               │
│ LLM con structured output:                                    │
│                                                               │
│ Prompt:                                                       │
│   "Analiza la query normalizada: {normalized_query}          │
│    Intent detectado: {intent}                                │
│    Historial reciente: {last_3_messages}                     │
│    Última búsqueda: {last_search_summary}                    │
│                                                               │
│    Razona paso por paso:                                     │
│    1. ¿Qué tipo de interacción es esta?                      │
│    2. ¿Necesito buscar productos o solo conversar?           │
│    3. ¿Tengo suficiente contexto de la conversación?         │
│    4. ¿Qué estrategia de búsqueda debo usar?                 │
│    5. ¿Debería usar información del carrito?                 │
│                                                               │
│    Genera un plan estructurado."                             │
│                                                               │
│ Output Schema:                                                │
│   {                                                           │
│     "reasoning_steps": [str],  // Pasos del razonamiento     │
│     "decision": {                                             │
│       "action_type": "search|cart_op|social|clarify",        │
│       "search_strategy": "hybrid|semantic|keyword|family",   │
│       "use_context": bool,  // Usar last_search              │
│       "expected_outcome": str                                 │
│     },                                                        │
│     "confidence": float,                                      │
│     "fallback_plan": str  // Si falla el plan principal      │
│   }                                                           │
│                                                               │
│ ✅ Quality Gate: Log reasoning_steps para observabilidad     │
└──────────────────────────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────┐
│ FASE 3: ACTION EXECUTION (Tool Calling)                      │
│                                                               │
│ Según decision.action_type:                                   │
│                                                               │
│ ┌─────────────────────────────────────┐                      │
│ │ "search":                            │                      │
│ │   → run_search_with_strategy()       │                      │
│ │      - hybrid: FAISS + BM25 + RRF    │                      │
│ │      - semantic: Solo FAISS          │                      │
│ │      - keyword: Solo BM25            │                      │
│ │      - family: Búsqueda por familia  │                      │
│ │   → filter_by_relevance()            │                      │
│ │   → assess_context_quality()         │                      │
│ └─────────────────────────────────────┘                      │
│                                                               │
│ ┌─────────────────────────────────────┐                      │
│ │ "cart_op":                           │                      │
│ │   → detect_cart_action()             │                      │
│ │   → execute_cart_operation()         │                      │
│ └─────────────────────────────────────┘                      │
│                                                               │
│ ┌─────────────────────────────────────┐                      │
│ │ "social":                            │                      │
│ │   → allowed_products = []            │                      │
│ │   → Saltar a response generation     │                      │
│ └─────────────────────────────────────┘                      │
│                                                               │
│ Output: action_results con productos/carrito/etc             │
└──────────────────────────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────┐
│ FASE 4: REFLECTION (Auto-Crítica - NUEVO)                    │
│                                                               │
│ LLM analiza resultados de la acción:                         │
│                                                               │
│ Prompt:                                                       │
│   "Ejecuté la acción: {action_type}                          │
│    Estrategia usada: {search_strategy}                       │
│    Resultados obtenidos:                                     │
│      - Cantidad de productos: {len(products)}                │
│      - Score promedio: {avg_score}                           │
│      - Score máximo: {max_score}                             │
│      - Productos con score > 70: {high_quality_count}        │
│                                                               │
│    Query original: {normalized_query}                        │
│    Entidades detectadas: {entities}                          │
│                                                               │
│    Evalúa críticamente:                                      │
│    1. ¿Los resultados responden a la query del usuario?      │
│    2. ¿La cantidad de resultados es apropiada?               │
│    3. ¿Los scores de relevancia son suficientemente altos?   │
│    4. ¿Hay incoherencias (ej: pidió Honda, encontré Yamaha)? │
│    5. ¿Debería intentar una estrategia diferente?            │
│                                                               │
│    Decide si necesitas re-intentar o continuar."             │
│                                                               │
│ Output Schema:                                                │
│   {                                                           │
│     "evaluation": {                                           │
│       "quality_score": float,  // 0-100                      │
│       "coherence_check": bool,  // Resultados coherentes     │
│       "quantity_appropriate": bool,                          │
│       "issues_found": [str]  // Problemas detectados         │
│     },                                                        │
│     "decision": {                                             │
│       "should_retry": bool,                                   │
│       "retry_strategy": str,  // Si should_retry=true        │
│       "retry_reason": str,                                    │
│       "proceed_with_results": bool                           │
│     },                                                        │
│     "reflection_notes": str  // Notas para logging           │
│   }                                                           │
│                                                               │
│ ✅ Quality Gate: Si should_retry → volver a FASE 3           │
│    (máximo 1 retry para evitar loops infinitos)              │
└──────────────────────────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────┐
│ FASE 5: PRODUCT SELECTION (Structured - de 3.15)             │
│                                                               │
│ SKIP SI: intent = "social" o action_type != "search"         │
│                                                               │
│ Template: product_selection_schema (heredado de 3.15)        │
│                                                               │
│ Context enriquecido:                                          │
│   - allowed_products (máx 15)                                │
│   - reasoning_steps (de FASE 2)                              │
│   - reflection_notes (de FASE 4)                             │
│   - customer_history                                          │
│                                                               │
│ Output:                                                       │
│   {                                                           │
│     "selected_products": [                                    │
│       {code, reason, rank, compatibility}                    │
│     ],  // Máx 5 (o 3 si primer mensaje)                     │
│     "analysis": {                                             │
│       "customer_type": "nuevo|recurrente|comparador|urgente",│
│       "interest_level": "bajo|medio|alto",                   │
│       "key_arguments": [str]                                  │
│     }                                                         │
│   }                                                           │
└──────────────────────────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────┐
│ FASE 6: RESPONSE GENERATION (Structured - de 3.15)           │
│                                                               │
│ Template: response_generation_schema                          │
│                                                               │
│ Context completo:                                             │
│   - understanding (FASE 1)                                    │
│   - reasoning (FASE 2)                                        │
│   - action_results (FASE 3)                                   │
│   - reflection (FASE 4)                                       │
│   - selection (FASE 5)                                        │
│                                                               │
│ REGLAS ESPECIALES:                                            │
│   SI intent = "social":                                       │
│     → Respuesta breve 1-3 líneas, tono humano                │
│     → products_cited = []                                     │
│     → NO mencionar productos                                  │
│                                                               │
│   SI intent = "product_search":                               │
│     → Listar productos con código + precio                   │
│     → Máx 350 chars por mensaje                              │
│     → CTA al final                                            │
│                                                               │
│ Output:                                                       │
│   {                                                           │
│     "message": str,                                           │
│     "products_cited": [str],                                  │
│     "tone": "friendly|professional|urgent",                  │
│     "metadata": {                                             │
│       "reasoning_visible": bool,  // Para debugging          │
│       "retry_count": int                                      │
│     }                                                         │
│   }                                                           │
└──────────────────────────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────┐
│ FASE 7: VALIDATION (Anti-Alucinación - de 3.14 mejorado)     │
│                                                               │
│ Validaciones estrictas:                                       │
│   1. Cada código en products_cited existe en allowed_products│
│   2. Nombres de productos no están inventados                │
│   3. Precios coinciden con catálogo (±5% tolerancia)         │
│   4. No hay productos duplicados                             │
│   5. Coherencia con intent detectado                         │
│                                                               │
│ SI detecta alucinaciones:                                     │
│   → Regenerar mensaje con template básico seguro            │
│   → Log warning para análisis posterior                      │
│   → Agregar nota: "_(Productos verificados del catálogo)_"   │
│                                                               │
│ ✅ Output Final: mensaje validado + metadata                 │
└──────────────────────────────────────────────────────────────┘
                           ↓
┌──────────────────────────────────────────────────────────────┐
│ FASE 8: CHUNKS & DELIVERY                                     │
│                                                               │
│ Si hay productos adicionales (> MAX_PRODUCTS_FOR_LLM):        │
│   → Dividir en chunks de PRODUCTS_PER_CHUNK                  │
│   → Enviar con delay de 0.5s entre chunks                    │
│                                                               │
│ Guardar estado:                                               │
│   → save_message(phone, role="assistant", content=message)   │
│   → update_last_search(phone, products)                      │
│   → update_conversation_phase(phone, detected_phase)         │
│   → log_interaction_metrics(phone, metadata)                 │
└──────────────────────────────────────────────────────────────┘
```

---

## 📐 ESPECIFICACIONES TÉCNICAS

### 1. Función Principal: `orquestar_fran_v316()`

```python
def orquestar_fran_v316(message: str, phone: str) -> str:
    """
    Orquestador principal de Fran 3.16 - Arquitectura Híbrida Agentic

    Combina:
    - Razonamiento interno (de 3.14)
    - Structured outputs (de 3.15)
    - Reflexion pattern (nuevo)
    - Tool calling explícito (nuevo)

    Args:
        message: Mensaje del usuario (sanitizado)
        phone: Número de teléfono en formato WhatsApp

    Returns:
        str: Mensaje de respuesta para enviar al usuario

    Raises:
        RateLimitError: Si excede límite de mensajes
        ValidationError: Si la respuesta final no pasa validación
    """
    # Implementar según arquitectura descrita arriba
    pass
```

### 2. Nuevo Schema: `REASONING_SCHEMA`

```python
REASONING_SCHEMA = {
    "task": "reason_about_query",
    "description": """
    Sos un experto en ventas de repuestos de motos. Analiza la situación
    y razona qué acción tomar.

    Piensa paso por paso sobre:
    1. ¿Qué está pidiendo realmente el usuario?
    2. ¿Tengo suficiente información del contexto?
    3. ¿Qué estrategia de búsqueda es más apropiada?
    4. ¿Debería usar información previa (carrito, última búsqueda)?
    5. ¿Cuál es el mejor resultado esperado?

    Genera un plan claro de acción.
    """,
    "output_schema": {
        "type": "object",
        "required": ["reasoning_steps", "decision", "confidence"],
        "properties": {
            "reasoning_steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Pasos del razonamiento (para logging)"
            },
            "decision": {
                "type": "object",
                "required": ["action_type", "search_strategy"],
                "properties": {
                    "action_type": {
                        "type": "string",
                        "enum": ["search", "cart_operation", "social", "clarification"],
                        "description": "Tipo de acción a ejecutar"
                    },
                    "search_strategy": {
                        "type": "string",
                        "enum": ["hybrid", "semantic_only", "keyword_only", "family_based", "none"],
                        "description": "Estrategia de búsqueda a usar"
                    },
                    "use_last_search": {
                        "type": "boolean",
                        "description": "Si debería usar productos de última búsqueda"
                    },
                    "use_cart_context": {
                        "type": "boolean",
                        "description": "Si debería considerar el carrito actual"
                    },
                    "expected_outcome": {
                        "type": "string",
                        "description": "Qué espera lograr con esta acción"
                    }
                }
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": "Confianza en el plan (0.0-1.0)"
            },
            "fallback_plan": {
                "type": "string",
                "description": "Qué hacer si el plan principal falla"
            }
        }
    }
}
```

### 3. Nuevo Schema: `REFLECTION_SCHEMA`

```python
REFLECTION_SCHEMA = {
    "task": "reflect_on_results",
    "description": """
    Analiza críticamente los resultados de la búsqueda/acción ejecutada.

    Evalúa:
    1. ¿Los resultados son coherentes con la query?
    2. ¿La calidad es suficiente?
    3. ¿Hay problemas que ameriten re-intentar?
    4. ¿Debería usar una estrategia diferente?

    Sé crítico y honesto. Es mejor re-intentar que dar resultados pobres.
    """,
    "output_schema": {
        "type": "object",
        "required": ["evaluation", "decision"],
        "properties": {
            "evaluation": {
                "type": "object",
                "properties": {
                    "quality_score": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "Score de calidad general (0-100)"
                    },
                    "coherence_check": {
                        "type": "boolean",
                        "description": "¿Resultados coherentes con query?"
                    },
                    "quantity_appropriate": {
                        "type": "boolean",
                        "description": "¿Cantidad de resultados apropiada?"
                    },
                    "issues_found": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Problemas específicos detectados"
                    }
                }
            },
            "decision": {
                "type": "object",
                "required": ["should_retry", "proceed_with_results"],
                "properties": {
                    "should_retry": {
                        "type": "boolean",
                        "description": "Si debería re-intentar la búsqueda"
                    },
                    "retry_strategy": {
                        "type": "string",
                        "enum": ["broader_search", "narrower_search", "different_keywords", "family_fallback"],
                        "description": "Estrategia para el retry (si should_retry=true)"
                    },
                    "retry_reason": {
                        "type": "string",
                        "description": "Por qué se necesita retry"
                    },
                    "proceed_with_results": {
                        "type": "boolean",
                        "description": "Si puede continuar con resultados actuales"
                    }
                }
            },
            "reflection_notes": {
                "type": "string",
                "description": "Notas adicionales para logging"
            }
        }
    }
}
```

### 4. Nueva Función: `run_search_with_strategy()`

```python
def run_search_with_strategy(
    query: str,
    strategy: str,
    phone: str,
    use_last_search: bool = False,
    top_k: int = 60
) -> tuple[list[dict], dict]:
    """
    Ejecuta búsqueda con estrategia específica.

    Args:
        query: Query normalizada
        strategy: "hybrid"|"semantic_only"|"keyword_only"|"family_based"
        phone: Teléfono del usuario
        use_last_search: Si debe combinar con última búsqueda
        top_k: Máximo de resultados

    Returns:
        (productos, metadata) donde metadata incluye scores, timing, etc.
    """
    start_time = time.time()

    if strategy == "hybrid":
        # FAISS + BM25 + RRF (actual)
        products = hybrid_search(query, phone=phone, top_k=top_k)

    elif strategy == "semantic_only":
        # Solo FAISS
        products = faiss_search_only(query, top_k=top_k)

    elif strategy == "keyword_only":
        # Solo BM25
        products = bm25_search_only(query, top_k=top_k)

    elif strategy == "family_based":
        # Búsqueda por familia de productos
        parsed = parse_query_v2(query, phone=phone)
        products = search_by_family(parsed.get("families", []), top_k=top_k)

    else:
        products = []

    # Si use_last_search, combinar con última búsqueda
    if use_last_search:
        last_search = get_last_search(phone)
        if last_search:
            products = merge_search_results(products, last_search["products"])

    # Post-procesamiento
    products = filter_by_relevance(products, query)
    quality = assess_context_quality(products, query)

    metadata = {
        "strategy_used": strategy,
        "total_found": len(products),
        "avg_score": quality.get("avg_score", 0),
        "max_score": quality.get("max_score", 0),
        "duration_ms": (time.time() - start_time) * 1000,
        "quality_assessment": quality
    }

    return products, metadata
```

### 5. Nueva Función: `complete_template_with_retry()`

```python
def complete_template_with_retry(
    template_name: str,
    context: dict,
    max_retries: int = 2,
    timeout: int = 30
) -> dict:
    """
    Completa template con retry y timeout.

    Mejora sobre complete_template() original:
    - Retry con exponential backoff
    - Timeout configurable
    - Logging detallado
    - Fallback a respuesta segura

    Args:
        template_name: Nombre del template
        context: Contexto para el LLM
        max_retries: Máximo de reintentos
        timeout: Timeout en segundos

    Returns:
        dict: Resultado del template o fallback
    """
    for attempt in range(max_retries + 1):
        try:
            with openai_sem:  # Rate limiting
                result = complete_template(
                    template_name,
                    context,
                    timeout=timeout
                )

                # Validar resultado
                if validate_template_output(result, template_name):
                    return result
                else:
                    logger.warning(f"Template {template_name} output inválido, retry {attempt+1}")

        except RateLimitError as e:
            wait_time = 2 ** attempt  # Exponential backoff
            logger.warning(f"Rate limit, esperando {wait_time}s...")
            time.sleep(wait_time)

        except Exception as e:
            logger.error(f"Error en template {template_name}: {e}")
            if attempt == max_retries:
                # Último intento, usar fallback
                return TEMPLATE_FALLBACKS.get(template_name, {})

    # Si llegó aquí, usar fallback
    return TEMPLATE_FALLBACKS.get(template_name, {})
```

---

## 🧪 TESTS REQUERIDOS

Crea archivo `tests/test_fran_316.py` con estos tests:

### Test 1: Búsqueda Simple con Reasoning

```python
def test_product_search_with_reasoning():
    """
    Test que el sistema razona antes de buscar y genera respuesta coherente.
    """
    message = "bateria honda cg 150"
    phone = "+5491112345678"

    response = orquestar_fran_v316(message, phone)

    # Verificar que se ejecutó reasoning
    # (puede checkear logs o metadata de respuesta)
    assert "reasoning_executed" in get_last_metadata(phone)

    # Verificar que hay productos en la respuesta
    assert any(keyword in response.lower() for keyword in ["batería", "ytx", "gel"])

    # Verificar que no hay alucinaciones
    products_cited = extract_codes_from_response(response)
    for code in products_cited:
        assert product_exists_in_catalog(code)
```

### Test 2: Intent Social (Sin Búsqueda)

```python
def test_social_intent_no_search():
    """
    Test que los saludos NO ejecutan búsqueda y responden humanamente.
    """
    message = "Hola! Cómo andás?"
    phone = "+5491112345678"

    response = orquestar_fran_v316(message, phone)

    # No debe mencionar productos
    assert not any(keyword in response.lower() for keyword in ["código", "tercom", "marca", "modelo"])

    # Debe ser breve
    assert len(response) < 200

    # Debe tener tono humano
    assert any(keyword in response.lower() for keyword in ["hola", "todo bien", "qué necesitás"])
```

### Test 3: Re-búsqueda por Reflection

```python
def test_reflection_triggers_retry():
    """
    Test que si la primera búsqueda es mala, el sistema re-intenta.
    """
    # Usar query ambigua que probablemente dé malos resultados
    message = "repuesto moto"  # Muy vago
    phone = "+5491112345678"

    response = orquestar_fran_v316(message, phone)

    # Debería pedir aclaración (no dar resultados malos)
    assert any(keyword in response.lower() for keyword in ["marca", "modelo", "qué moto", "qué repuesto"])

    # O, si hay reflection, debería haber intentado retry
    metadata = get_last_metadata(phone)
    if "reflection_executed" in metadata:
        # Verificar que detectó baja calidad
        assert metadata["reflection"]["quality_score"] < 50
```

### Test 4: Coherencia Multi-Fase

```python
def test_multiphase_coherence():
    """
    Test que todas las fases mantienen coherencia.
    """
    message = "filtro de aceite yamaha fz 150"
    phone = "+5491112345678"

    response = orquestar_fran_v316(message, phone)

    metadata = get_last_metadata(phone)

    # Verificar que understanding detectó entities correctas
    assert metadata["understanding"]["entities"]["brand"].lower() == "yamaha"
    assert metadata["understanding"]["entities"]["category"].lower() == "filtro"

    # Verificar que reasoning decidió "search"
    assert metadata["reasoning"]["decision"]["action_type"] == "search"

    # Verificar que productos citados son coherentes
    products_cited = metadata["response"]["products_cited"]
    for code in products_cited:
        product = get_product_by_code(code)
        assert "yamaha" in product["name"].lower() or "fz" in product["name"].lower()
        assert "filtro" in product["name"].lower()
```

### Test 5: Validación Anti-Alucinación

```python
def test_anti_hallucination_validation():
    """
    Test que el sistema NO inventa códigos ni productos.
    """
    message = "amortiguador honda wave"
    phone = "+5491112345678"

    response = orquestar_fran_v316(message, phone)

    # Extraer todos los códigos mencionados
    codes = extract_codes_from_response(response)

    # TODOS deben existir en el catálogo
    catalog, _, _, _ = get_catalog_and_index()
    catalog_codes = {p["code"] for p in catalog}

    for code in codes:
        assert code in catalog_codes, f"Código alucinado: {code}"
```

### Test 6: Performance (Timeout)

```python
def test_response_time_acceptable():
    """
    Test que el sistema responde en tiempo razonable.
    """
    message = "bujia motomel 150"
    phone = "+5491112345678"

    start = time.time()
    response = orquestar_fran_v316(message, phone)
    duration = time.time() - start

    # No debería tardar más de 10 segundos
    # (ajustar según infraestructura)
    assert duration < 10, f"Respuesta tardó {duration}s"
```

---

## 📊 LOGGING Y OBSERVABILIDAD

### Estructura de Log por Interacción

```python
def log_interaction_v316(phone: str, metadata: dict):
    """
    Log estructurado de cada interacción para análisis.
    """
    log_entry = {
        "timestamp": datetime.now().isoformat(),
        "phone": phone,
        "version": "3.16",

        # FASE 1: Understanding
        "understanding": {
            "normalized_query": metadata.get("understanding", {}).get("normalized_query"),
            "intent": metadata.get("understanding", {}).get("intent"),
            "confidence": metadata.get("understanding", {}).get("confidence"),
            "entities": metadata.get("understanding", {}).get("entities")
        },

        # FASE 2: Reasoning
        "reasoning": {
            "action_type": metadata.get("reasoning", {}).get("decision", {}).get("action_type"),
            "search_strategy": metadata.get("reasoning", {}).get("decision", {}).get("search_strategy"),
            "confidence": metadata.get("reasoning", {}).get("confidence"),
            "steps_count": len(metadata.get("reasoning", {}).get("reasoning_steps", []))
        },

        # FASE 3: Action Execution
        "action": {
            "strategy_used": metadata.get("action", {}).get("strategy_used"),
            "products_found": metadata.get("action", {}).get("total_found"),
            "avg_score": metadata.get("action", {}).get("avg_score"),
            "duration_ms": metadata.get("action", {}).get("duration_ms")
        },

        # FASE 4: Reflection
        "reflection": {
            "quality_score": metadata.get("reflection", {}).get("evaluation", {}).get("quality_score"),
            "should_retry": metadata.get("reflection", {}).get("decision", {}).get("should_retry"),
            "retry_executed": metadata.get("retry_executed", False),
            "issues_found": metadata.get("reflection", {}).get("evaluation", {}).get("issues_found", [])
        },

        # FASE 6: Response
        "response": {
            "message_length": len(metadata.get("response", {}).get("message", "")),
            "products_cited_count": len(metadata.get("response", {}).get("products_cited", [])),
            "tone": metadata.get("response", {}).get("tone")
        },

        # FASE 7: Validation
        "validation": {
            "hallucinations_detected": metadata.get("validation", {}).get("hallucinations_detected", False),
            "regeneration_needed": metadata.get("validation", {}).get("regeneration_needed", False)
        },

        # Performance
        "performance": {
            "total_duration_ms": metadata.get("total_duration_ms"),
            "llm_calls_count": metadata.get("llm_calls_count"),
            "total_tokens": metadata.get("total_tokens")
        }
    }

    # Guardar en DB o archivo
    save_interaction_log(log_entry)

    # También log a consola en desarrollo
    if os.getenv("ENV") == "development":
        logger.info(f"📊 Interaction Log:\n{json.dumps(log_entry, indent=2)}")
```

---

## 🎯 CRITERIOS DE ACEPTACIÓN

### ✅ Funcionalidad

- [ ] Implementada función `orquestar_fran_v316()` completa
- [ ] Agregados schemas `REASONING_SCHEMA` y `REFLECTION_SCHEMA`
- [ ] Implementada función `run_search_with_strategy()`
- [ ] Implementada función `complete_template_with_retry()`
- [ ] Todos los tests pasan (6/6)

### ✅ Calidad

- [ ] No hay alucinaciones en 100 queries de test
- [ ] Tiempo de respuesta < 10 segundos en 95% de casos
- [ ] Logs estructurados para observabilidad
- [ ] Manejo de errores robusto (try/except específicos)
- [ ] Documentación en docstrings

### ✅ Compatibilidad

- [ ] Mantiene compatibilidad con DB existente
- [ ] No rompe endpoints actuales (`/whatsapp`, `/health`)
- [ ] Puede coexistir con 3.14 y 3.15 (rollout gradual)
- [ ] Variables de entorno: `USE_FRAN_316=true` para activar

### ✅ Observabilidad

- [ ] Métricas de cada fase logueadas
- [ ] Reasoning visible en logs de debug
- [ ] Reflection decisions registradas
- [ ] Performance tracking (duration, tokens, retries)

---

## 🚀 PLAN DE IMPLEMENTACIÓN

### Fase 1: Schemas y Estructura Base (1-2 horas)

1. Agregar `REASONING_SCHEMA` y `REFLECTION_SCHEMA` a app.py
2. Agregar fallbacks a `TEMPLATE_FALLBACKS`
3. Crear función `orquestar_fran_v316()` con estructura de fases (sin implementar)

### Fase 2: Implementar Fases Core (2-3 horas)

4. Implementar FASE 1: Query Understanding (reusar de 3.15)
5. Implementar FASE 2: Reasoning (nuevo)
6. Implementar FASE 3: Action Execution con `run_search_with_strategy()`
7. Implementar FASE 4: Reflection (nuevo, crítico)

### Fase 3: Implementar Fases Finales (1-2 horas)

8. Implementar FASE 5: Product Selection (reusar de 3.15, con skip logic)
9. Implementar FASE 6: Response Generation (reusar de 3.15)
10. Implementar FASE 7: Validation (mejorar de 3.14)
11. Implementar FASE 8: Chunks & Delivery (reusar existente)

### Fase 4: Utilidades y Retry Logic (1 hora)

12. Implementar `complete_template_with_retry()`
13. Implementar `merge_search_results()` para combinar búsquedas
14. Implementar `validate_template_output()`

### Fase 5: Logging y Observabilidad (1 hora)

15. Implementar `log_interaction_v316()`
16. Agregar metadata tracking en cada fase
17. Configurar logs estructurados

### Fase 6: Tests (1-2 horas)

18. Crear `tests/test_fran_316.py`
19. Implementar los 6 tests descritos
20. Ejecutar y ajustar hasta que pasen todos

### Fase 7: Integración (30 min)

21. Agregar `should_use_v316()` similar a `should_use_v315()`
22. Modificar `/whatsapp` endpoint para soportar 3.16
23. Agregar variable de entorno `USE_FRAN_316`
24. Agregar `BETA_PHONES_316` para rollout gradual

### Fase 8: Documentación y Commit (30 min)

25. Documentar cambios en docstrings
26. Crear commit descriptivo
27. Actualizar README si es necesario

---

## 📝 NOTAS IMPORTANTES

### Mantener Compatibilidad

- NO modificar funciones existentes de 3.14 o 3.15
- Crear nuevas funciones con sufijo `_v316` si es necesario
- La arquitectura debe poder coexistir con versiones anteriores

### Performance

- Usar `openai_sem` (Semaphore) para rate limiting
- Cachear resultados donde sea posible
- Limitar reflection a máximo 1 retry para evitar loops

### Seguridad

- Sanitizar inputs en cada fase
- Validar outputs de LLM antes de usar
- No exponer reasoning en respuestas a usuario (solo en logs)

### Edge Cases

- Manejar timeout de OpenAI con fallback
- Manejar queries vacías o muy cortas
- Manejar catálogo vacío o no disponible
- Manejar múltiples intents en un mensaje

---

## 🎓 RECURSOS DE REFERENCIA

### Código Existente para Reusar

- `hybrid_search()` - app.py:2408
- `filter_by_relevance()` - app.py:678
- `assess_context_quality()` - app.py:692
- `complete_template()` - app.py:486
- `validate_and_fix_response()` - app.py:843
- `build_enriched_context()` - app.py:3675

### Schemas Existentes para Extender

- `QUERY_UNDERSTANDING_SCHEMA` - app.py:116
- `PRODUCT_SELECTION_SCHEMA` - app.py:192
- `RESPONSE_GENERATION_SCHEMA` - app.py:267

### Constantes Relevantes

- `MAX_PRODUCTS_FOR_LLM = 15`
- `RELEVANCE_MIN_SCORE = 65.0`
- `MODEL_REASONING = "gpt-4o-mini"`
- `WHATSAPP_MSG_LIMIT = 1600`

---

## ✅ CHECKLIST FINAL

Antes de marcar como completo, verificar:

- [ ] Código implementado y funcionando
- [ ] Todos los tests pasan
- [ ] No hay warnings ni errores en logs
- [ ] Performance aceptable (< 10s por interacción)
- [ ] Documentación completa en docstrings
- [ ] Logging estructurado configurado
- [ ] Compatible con versiones anteriores
- [ ] Rollout gradual configurado (variable de entorno)
- [ ] Commit creado con mensaje descriptivo
- [ ] Listo para push a rama `claude/fran-3.16-implementation-{SESSION_ID}`

---

## 🎯 OBJETIVO FINAL

Al completar esta tarea, Fran 3.16 debe ser:

1. **Más inteligente** que 3.14 y 3.15 (gracias a reasoning + reflection)
2. **Más observable** (logs estructurados de cada fase)
3. **Más robusto** (retry logic, validación estricta)
4. **Más adaptativo** (estrategias de búsqueda dinámicas)
5. **Listo para producción** (tests, manejo de errores, performance)

---

**¿Listo para comenzar?** Implementa Fran 3.16 siguiendo esta guía paso por paso. ¡Buena suerte! 🚀
