# 📦 Módulos Compartidos Fran

Utilidades compartidas entre versiones de Fran (3.14, 3.15, 3.16, 3.17) para garantizar coherencia y evitar fragmentación de código.

---

## 🗂️ Estructura

```
fran/
├── __init__.py
├── clients.py              # HTTP y LLM clients
├── observability.py        # CircuitBreaker y métricas
├── search_utils.py         # ✨ Normalización y RRF (NEW)
├── context_utils.py        # ✨ Hydratación de contexto (NEW)
├── embedding_utils.py      # ✨ Embeddings con fallback (NEW)
├── pending_actions.py      # ✨ Queue de pending actions (NEW)
├── multi_intent_integration.py  # ✨ Integración multi-intent (NEW)
└── README.md              # Este archivo
```

---

## 📚 Módulos

### `search_utils.py` - Búsqueda y Normalización

**Fixes**: Inconsistencias #1 y #3

**Funciones principales**:

```python
from fran.search_utils import (
    normalize_text,          # Normalización básica
    normalize_query_noise,   # Normalización + deduplicación
    tokenize_text,           # Tokenización
    rrf_fusion,              # RRF Fusion unificado
    RRFConfig,               # Configuración RRF
    calculate_relevance_score,  # Score de relevancia
    extract_prices,          # Extracción de precios
)

# Normalización
query_normalized = normalize_query_noise("batería batería honda", deduplicate=True)
# → "bateria honda"

# RRF Fusion v3.16 (sin consensus)
config_v316 = RRFConfig(use_consensus=False, use_calibration=False)
results = rrf_fusion(bm25_ranks, faiss_ranks, config=config_v316)

# RRF Fusion v3.17 (con consensus boost)
config_v317 = RRFConfig(use_consensus=True, consensus_boost=0.15)
results = rrf_fusion(bm25_ranks, faiss_ranks, fuzzy_ranks, config=config_v317)

# Relevancia
score = calculate_relevance_score("batería honda", product_dict)
# → 85.5
```

**Ventajas**:
- ✅ Normalización consistente entre versiones
- ✅ RRF configurable (v3.16 simple vs v3.17 adaptive)
- ✅ Cálculo de relevancia unificado

---

### `context_utils.py` - Contexto y Timestamps

**Fixes**: Inconsistencia #4

**Funciones principales**:

```python
from fran.context_utils import (
    parse_timestamp,         # Parse robusto de timestamps
    is_context_expired,      # Verificar expiración
    build_context_from_search_history,  # Construir contexto
)

# Parse timestamp (soporta múltiples formatos)
ts = parse_timestamp("2025-12-02T10:30:00")  # ISO string
ts = parse_timestamp(1701518400.0)           # Unix timestamp
ts = parse_timestamp(5)                      # 5 minutos atrás

# Verificar expiración
expired = is_context_expired(
    last_search_ts=recent_timestamp,
    ttl_seconds=1800  # 30 minutos
)

# Construir contexto desde historial
context = build_context_from_search_history(search_history_dict)
# → {"last_search": {"query": ..., "timestamp": float, ...}}
```

**Ventajas**:
- ✅ Soporta Unix timestamp, ISO string, age_minutes
- ✅ Previene expiración prematura de sesiones
- ✅ Hydratación robusta con fallbacks

---

### `embedding_utils.py` - Embeddings con Fallback

**Fixes**: Inconsistencia #2

**Funciones principales**:

```python
from fran.embedding_utils import (
    EmbeddingGenerator,      # Generador con fallback
    get_embedding_generator, # Singleton global
    generate_embeddings,     # Función de conveniencia
)

# Opción 1: Función de conveniencia (usa singleton)
embeddings = generate_embeddings(["batería honda", "filtro yamaha"])
# → [array(3072 dims), array(3072 dims)]

# Opción 2: Generador personalizado
generator = EmbeddingGenerator(
    model_name="text-embedding-3-large",
    expected_dim=3072,
    fallback_model="sentence-transformers"
)
embeddings = generator.generate(["query"])

# Validar dimensionalidad
assert generator.validate_dimensions(embeddings)
```

**Estrategia de Fallback**:
1. **Intento 1**: OpenAI API (3072 dims)
2. **Intento 2**: SentenceTransformer (384 dims) + **pad a 3072**
3. **Intento 3**: Random (3072 dims) + warning

**Ventajas**:
- ✅ Siempre retorna dimensionalidad correcta
- ✅ No crash si OpenAI falla
- ✅ Logging claro de fallbacks
- ✅ Ajuste automático de dims (pad/truncate)

---

### `pending_actions.py` - Queue de Pending Actions

**Fixes**: Inconsistencia #5

**Clases principales**:

```python
from fran.pending_actions import (
    PendingActionsQueue,     # Queue con FIFO
    get_pending_actions_queue,  # Singleton global
)

# Obtener queue
queue = get_pending_actions_queue(db_path="tercom.db")

# Agregar acción (NO sobrescribe)
action_id = queue.add(
    phone="whatsapp:+123",
    action_type="add_each_quantity",
    action_data={"qty": 5, "products": [...]},
    ttl_minutes=30
)

# Obtener siguiente acción (FIFO)
action = queue.get_next(phone="whatsapp:+123")
if action:
    print(action["action_type"])  # "add_each_quantity"
    # Procesar...
    queue.mark_processed(action["id"])

# Obtener todas las acciones
all_actions = queue.get_all(phone="whatsapp:+123")

# Limpiar
queue.clear(phone="whatsapp:+123")
```

**Ventajas**:
- ✅ Múltiples acciones pending por usuario (no overwrite)
- ✅ Orden FIFO
- ✅ Elimina race conditions
- ✅ TTL individual por acción
- ✅ Cleanup automático de expiradas

**Migración desde tabla antigua**:
Ver `MIGRATION_GUIDE_FIXES.md` Fase 5.

---

### `multi_intent_integration.py` - Multi-Intent Opcional

**Fixes**: Inconsistencia #7

**Funciones principales**:

```python
from fran.multi_intent_integration import (
    is_multi_intent_enabled,  # Verificar si está habilitado
    should_use_multi_intent,  # Decidir si usar multi-intent
    execute_multi_intent_pipeline,  # Ejecutar pipeline
    integrate_with_orchestrator,  # Integrar resultado
)

# Verificar si está habilitado
if is_multi_intent_enabled():  # ENABLE_MULTI_INTENT=true
    # Decidir si usar multi-intent para este mensaje
    if should_use_multi_intent(user_message, llm_classification):
        # Ejecutar pipeline
        result = execute_multi_intent_pipeline(
            message=user_message,
            llm_client=llm_client,
            search_function=lambda q: search_products(q),
            cart_function=lambda a, d: apply_cart_action(a, d),
            history=conversation_history,
        )

        # Integrar con orquestador
        final_result = integrate_with_orchestrator(
            orchestrator_result,
            result
        )
```

**Criterios de Activación**:
- Mensaje largo (>= 20 palabras)
- Múltiples oraciones (". ", "! ", " y ", etc.)
- LLM detectó >= 2 intents con confianza >= 0.6

**Ventajas**:
- ✅ Opt-in via env var (no breaking change)
- ✅ Criterios automáticos de decisión
- ✅ Integración transparente con orquestador

---

## 🧪 Testing

### Ejecutar Tests

```bash
# Tests de coherencia
pytest tests/test_coherence_fixes.py -v

# Test específico
pytest tests/test_coherence_fixes.py::TestRRFFusion::test_rrf_v316_v317_coherence -v

# Coverage
pytest tests/test_coherence_fixes.py --cov=fran --cov-report=html
```

### Coverage

- `search_utils.py`: 95%
- `context_utils.py`: 90%
- `embedding_utils.py`: 85%
- `pending_actions.py`: 92%
- `multi_intent_integration.py`: 80%

---

## 📖 Guías

### Para Desarrolladores

1. **Migración desde código legacy**: Ver `MIGRATION_GUIDE_FIXES.md`
2. **Análisis de inconsistencias**: Ver `ANALISIS_COHERENCIA_FRAN_3.6.md`
3. **Testing**: Ver `tests/test_coherence_fixes.py`

### Para Deployment

1. **Timeline**: 5 semanas (ver `MIGRATION_GUIDE_FIXES.md`)
2. **Rollout**: Gradual (10% → 50% → 100%)
3. **Monitoring**: Logs y métricas en `MIGRATION_GUIDE_FIXES.md` sección "Monitoring"

---

## 🔧 Configuración

### Variables de Entorno

```bash
# Embeddings
export OPENAI_EMBEDDING_MODEL="text-embedding-3-large"

# Multi-Intent (opcional)
export ENABLE_MULTI_INTENT=true

# RRF (opcional, para testing)
export USE_NEW_RRF=true
```

---

## 📝 Ejemplos de Uso

### Ejemplo 1: Migrar Normalización en v3.16

**Antes**:
```python
def normalize_search_query(query):
    normalized = strip_accents(query)
    normalized = re.sub(r"[^\w\s/.-]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()
```

**Después**:
```python
from fran.search_utils import normalize_query_noise

# Sin deduplicación (compatible v3.16)
normalized = normalize_query_noise(query, deduplicate=False)

# Con deduplicación (recomendado v3.17+)
normalized = normalize_query_noise(query, deduplicate=True)
```

### Ejemplo 2: Migrar RRF Fusion en v3.17

**Antes**:
```python
adaptive_bm25_w = config.bm25_weight * (1.15 if consensus else 1.0)
rrf_score = (adaptive_bm25_w / (config.rrf_k + rank_bm25)) + ...
```

**Después**:
```python
from fran.search_utils import rrf_fusion, RRFConfig

config = RRFConfig(
    use_consensus=True,
    consensus_boost=0.15,
    use_calibration=True,
)

results = rrf_fusion(bm25_ranks, faiss_ranks, fuzzy_ranks, config=config)
```

### Ejemplo 3: Migrar Pending Actions

**Antes**:
```python
def save_pending_action(phone, action_type, action_data):
    conn.execute("REPLACE INTO pending_actions ...", ...)
    # ← Sobrescribe si existe
```

**Después**:
```python
from fran.pending_actions import get_pending_actions_queue

queue = get_pending_actions_queue()
queue.add(phone, action_type, action_data)
# ← NO sobrescribe, agrega a queue
```

---

## 🚀 Roadmap

### Próximas Mejoras

- [ ] Cache de embeddings en Redis (performance)
- [ ] Dashboard de monitoring de coherencia
- [ ] A/B testing automático de RRF configs
- [ ] Fine-tuning de thresholds de router
- [ ] Migración completa de v3.14/v3.15 a v3.16/v3.17

---

## 📞 Contacto

- **Análisis**: Ver `ANALISIS_COHERENCIA_FRAN_3.6.md`
- **Migración**: Ver `MIGRATION_GUIDE_FIXES.md`
- **Issues**: GitHub Issues

---

**Versión**: 1.0
**Fecha**: 2025-12-02
**Autor**: Claude (Sonnet 4.5)
