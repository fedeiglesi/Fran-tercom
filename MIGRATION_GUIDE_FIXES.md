# 🔧 Guía de Migración: Fixes de Coherencia Fran 3.16/3.17

**Fecha**: 2025-12-02
**Versión**: 1.0
**Relacionado**: `ANALISIS_COHERENCIA_FRAN_3.6.md`

---

## 📋 Resumen de Cambios

Esta migración introduce **módulos compartidos** para garantizar coherencia entre versiones de Fran:

### Nuevos Módulos

1. **`fran/search_utils.py`** - Normalización y RRF unificados
2. **`fran/context_utils.py`** - Hydratación de contexto robusta
3. **`fran/embedding_utils.py`** - Embeddings con fallback compatible
4. **`fran/pending_actions.py`** - Queue de pending actions (sin race conditions)
5. **`fran/multi_intent_integration.py`** - Integración opcional de multi-intent

### Archivos Modificados

- **`pipeline/router_dynamic.py`** - Router mejorado con más keywords técnicas

---

## 🚀 Migración Paso a Paso

### Fase 1: Adopción de Normalización Unificada (Fix #3)

**Problema**: v3.16 y v3.17 normalizan queries de forma diferente.

**Solución**: Usar `fran.search_utils.normalize_query_noise()`.

#### En `app.py` (v3.16)

**Antes**:
```python
def normalize_search_query(query):
    normalized = strip_accents(query)
    normalized = re.sub(r"[^\w\s/.-]", " ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()
```

**Después**:
```python
from fran.search_utils import normalize_query_noise, tokenize_text

# Para normalización con deduplicación (recomendado v3.17+)
normalized = normalize_query_noise(query, deduplicate=True)

# Para compatibilidad v3.16 (sin dedup)
normalized = normalize_query_noise(query, deduplicate=False)

# Para tokenización
tokens = tokenize_text(query, deduplicate=True)
```

**Impacto**: 🟢 **Bajo** - Cambio compatible hacia atrás

**Rollout**: Gradual (habilitar dedup solo en v3.17, luego migrar v3.16)

---

### Fase 2: RRF Fusion Unificado (Fix #1) 🔴 CRÍTICO

**Problema**: v3.16 y v3.17 usan fórmulas RRF diferentes → Resultados inconsistentes.

**Solución**: Usar `fran.search_utils.rrf_fusion()` con configuración por versión.

#### En `app.py:3334-3355` (v3.16)

**Antes**:
```python
k_rrf = 60
RRF_BM25_WEIGHT = 1.0
RRF_FAISS_WEIGHT = 1.2

fused_scores = defaultdict(float)
for idx, rank in bm25_candidates.items():
    fused_scores[idx] += RRF_BM25_WEIGHT / (k_rrf + rank)
for idx, rank in faiss_candidates.items():
    fused_scores[idx] += RRF_FAISS_WEIGHT / (k_rrf + rank)

# Ordenar por score
sorted_results = sorted(fused_scores.items(), key=lambda x: x[1], reverse=True)
```

**Después**:
```python
from fran.search_utils import rrf_fusion, RRFConfig

# Configuración v3.16 (sin consensus boost)
config_v316 = RRFConfig(
    k_rrf=60,
    bm25_weight=1.0,
    faiss_weight=1.2,
    use_consensus=False,  # Desactivar consensus boost
    use_calibration=False,  # Desactivar calibración
)

# Preparar ranks
bm25_ranks = {idx: (rank, score) for idx, (rank, score) in bm25_candidates.items()}
faiss_ranks = {idx: (rank, score) for idx, (rank, score) in faiss_candidates.items()}

# Ejecutar RRF
results = rrf_fusion(bm25_ranks, faiss_ranks, config=config_v316)
# results = [(idx, combined_score, breakdown), ...]
```

#### En `pipeline/phases_v317.py:613-627` (v3.17)

**Antes**:
```python
adaptive_bm25_w = config.bm25_weight * (1.15 if consensus else 1.0)
rrf_score = (adaptive_bm25_w / (config.rrf_k + rank_bm25)) + ...
calibrated = rrf_score + 0.25 * (...)
if consensus:
    calibrated *= 1.0 + config.consensus_boost
```

**Después**:
```python
from fran.search_utils import rrf_fusion, RRFConfig

# Configuración v3.17 (con consensus boost)
config_v317 = RRFConfig(
    k_rrf=60,
    bm25_weight=1.0,
    faiss_weight=1.2,
    fuzzy_weight=0.8,
    consensus_boost=0.15,
    partial_consensus_boost=0.05,
    use_consensus=True,  # Activar consensus boost
    use_calibration=True,  # Activar calibración
)

# Preparar ranks (incluir fuzzy si existe)
bm25_ranks = {idx: (rank, norm_score) for ...}
faiss_ranks = {idx: (rank, norm_score) for ...}
fuzzy_ranks = {idx: (rank, norm_score) for ...}

# Ejecutar RRF
results = rrf_fusion(bm25_ranks, faiss_ranks, fuzzy_ranks, config=config_v317)
```

**Validación**:
```python
# Test de coherencia entre versiones
results_v316 = rrf_fusion(bm25, faiss, config=RRFConfig(use_consensus=False))
results_v317 = rrf_fusion(bm25, faiss, config=RRFConfig(use_consensus=True))

# Top-5 debe coincidir en >= 80%
top5_v316 = {r[0] for r in results_v316[:5]}
top5_v317 = {r[0] for r in results_v317[:5]}
overlap = len(top5_v316 & top5_v317)
assert overlap >= 4, f"Overlap {overlap}/5 < 80%"
```

**Impacto**: 🔴 **ALTO** - Puede cambiar rankings, requiere A/B testing

**Rollout**: A/B test con 10% tráfico → 50% → 100%

---

### Fase 3: Embeddings con Fallback Robusto (Fix #2) 🔴 CRÍTICO

**Problema**: Si OpenAI falla, v3.17 usa SentenceTransformer (384 dims) incompatible con FAISS (3072 dims).

**Solución**: Usar `fran.embedding_utils.EmbeddingGenerator` con ajuste de dimensionalidad.

#### En `app.py` y `pipeline/phases_v317.py`

**Antes**:
```python
# app.py
EMBEDDING_MODEL = "text-embedding-3-large"
client = OpenAI(api_key=OPENAI_API_KEY)

def generate_embeddings(texts):
    response = client.embeddings.create(input=texts, model=EMBEDDING_MODEL)
    return [np.array(item.embedding) for item in response.data]
```

**Después**:
```python
from fran.embedding_utils import get_embedding_generator, generate_embeddings

# Opción 1: Usar generador global (recomendado)
embeddings = generate_embeddings(texts)

# Opción 2: Configurar generador personalizado
generator = get_embedding_generator(
    model_name="text-embedding-3-large",
    expected_dim=3072,
    fallback_model="sentence-transformers"  # o "random" para tests
)
embeddings = generator.generate(texts)

# Validar dimensionalidad
assert generator.validate_dimensions(embeddings), "Dimension mismatch!"
```

**Comportamiento**:
1. Intenta OpenAI (3072 dims)
2. Si falla → SentenceTransformer (384 dims) + **pad a 3072**
3. Si todo falla → Random (3072 dims) + warning

**Ventajas**:
- ✅ Siempre retorna dimensionalidad correcta
- ✅ No crash si OpenAI falla
- ✅ Logging claro de fallbacks

**Impacto**: 🔴 **CRÍTICO** - Previene crashes en producción

**Rollout**: Inmediato (defensive fix)

---

### Fase 4: Hydratación de Contexto Robusta (Fix #4)

**Problema**: `parse_timestamp()` falla con ISO strings o age_minutes inconsistente.

**Solución**: Usar `fran.context_utils.parse_timestamp()`.

#### En `app.py:482-490` (SessionMemory)

**Antes**:
```python
ts = last_search.get("metadata", {}).get("timestamp") or last_search.get("age_minutes")
if isinstance(ts, (int, float)):
    timestamp = time.time() - float(ts) * 60  # Asume age_minutes
```

**Después**:
```python
from fran.context_utils import parse_timestamp, build_context_from_search_history

# Parse timestamp robusto
ts_raw = last_search.get("metadata", {}).get("timestamp") or last_search.get("age_minutes")
timestamp = parse_timestamp(ts_raw)

# O usar helper completo
context = build_context_from_search_history(last_search_data)
# context = {"last_search": {"query": ..., "timestamp": float, ...}}
```

**Soporta**:
- `float`: `1701518400.0` → timestamp Unix
- `str ISO`: `"2025-12-02T10:30:00"` → timestamp Unix
- `int small`: `30` → 30 minutos atrás
- `None` → `None`

**Impacto**: 🟠 **MEDIO** - Mejora estabilidad de contexto

**Rollout**: Inmediato (defensive fix)

---

### Fase 5: Pending Actions Queue (Fix #5)

**Problema**: Race condition con `UNIQUE(phone)` → Acciones sobrescritas.

**Solución**: Migrar a `fran.pending_actions.PendingActionsQueue`.

#### Migración de Schema

**Tabla antigua** (`pending_actions`):
```sql
CREATE TABLE pending_actions (
    phone TEXT PRIMARY KEY,  -- ← UNIQUE → Overwrite
    action_type TEXT,
    action_data TEXT,
    expires_at TEXT
)
```

**Tabla nueva** (`pending_actions_queue`):
```sql
CREATE TABLE pending_actions_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- ← ID único
    phone TEXT NOT NULL,  -- ← Múltiples rows por phone
    action_type TEXT NOT NULL,
    action_data TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    processed BOOLEAN DEFAULT 0,
    processed_at TEXT
)
-- Índices para performance
CREATE INDEX idx_pending_phone_processed ON pending_actions_queue(phone, processed);
```

#### Uso en Código

**Antes**:
```python
# app.py:1826
def save_pending_action(phone, action_type, action_data, ttl_minutes=30):
    conn.execute(
        "REPLACE INTO pending_actions (phone, action_type, action_data, expires_at) VALUES (?, ?, ?, ?)",
        (phone, action_type, json.dumps(action_data), expires_at)
    )

# app.py:1850
def get_pending_action(phone):
    row = conn.execute("SELECT * FROM pending_actions WHERE phone = ?", (phone,)).fetchone()
    return json.loads(row["action_data"]) if row else None
```

**Después**:
```python
from fran.pending_actions import get_pending_actions_queue

queue = get_pending_actions_queue()

# Agregar acción (no sobrescribe)
action_id = queue.add(
    phone="whatsapp:+123",
    action_type="add_each_quantity",
    action_data={"qty": 5, "products": [...]},
    ttl_minutes=30
)

# Obtener siguiente acción (FIFO)
action = queue.get_next(phone="whatsapp:+123")
if action:
    print(action["action_type"], action["action_data"])
    # Procesar...
    queue.mark_processed(action["id"])

# Obtener todas las acciones pending
all_actions = queue.get_all(phone="whatsapp:+123")

# Limpiar
queue.clear(phone="whatsapp:+123")
```

**Ventajas**:
- ✅ Múltiples acciones pending por usuario
- ✅ Orden FIFO
- ✅ No race conditions
- ✅ Cleanup automático de expiradas

**Migración de datos**:
```python
# Script de migración (ejecutar UNA VEZ)
import sqlite3
from fran.pending_actions import get_pending_actions_queue

queue = get_pending_actions_queue()

conn = sqlite3.connect("tercom.db")
old_actions = conn.execute("SELECT * FROM pending_actions").fetchall()

for row in old_actions:
    queue.add(
        phone=row["phone"],
        action_type=row["action_type"],
        action_data=json.loads(row["action_data"]),
        ttl_minutes=30
    )

# Opcional: Renombrar tabla antigua
conn.execute("ALTER TABLE pending_actions RENAME TO pending_actions_old")
```

**Impacto**: 🟠 **MEDIO** - Requiere migración de DB

**Rollout**: Staged
1. Deploy nuevo código (crea tabla nueva)
2. Migrar datos de tabla antigua
3. Monitorear 24h
4. Drop tabla antigua

---

### Fase 6: Router Mejorado (Fix #6)

**Cambio**: `pipeline/router_dynamic.py` ya actualizado en este PR.

**Mejoras**:
- ✅ +60 keywords técnicas
- ✅ +20 marcas de motos
- ✅ +20 modelos comunes
- ✅ Patrones de intención técnica
- ✅ Default a "technical" (en lugar de "no_centroid")

**Sin cambios de código** necesarios en orquestador.

**Impacto**: 🟢 **BAJO** - Mejora automática

---

### Fase 7: Multi-Intent Opcional (Fix #7)

**Problema**: Módulo `multi_intent.py` no usado en producción.

**Solución**: Integración opcional vía `fran.multi_intent_integration`.

#### Activación

**Variable de entorno**:
```bash
export ENABLE_MULTI_INTENT=true
```

#### Uso en Orquestador v3.17

**Agregar en `pipeline/orquestador_v317.py`**:
```python
from fran.multi_intent_integration import (
    should_use_multi_intent,
    execute_multi_intent_pipeline,
    integrate_with_orchestrator,
)

def orquestar_v317(user_message, phone):
    # ... código existente ...

    # Después de clasificación LLM (Fase 1)
    classification = fase1_classify(user_message, ...)

    # Decidir si usar multi-intent
    if should_use_multi_intent(user_message, classification):
        logger.info(f"[{phone}] Using multi-intent pipeline")

        multi_result = execute_multi_intent_pipeline(
            message=user_message,
            llm_client=llm_client,
            search_function=lambda q: fase2_hybrid_search(q, ...),
            cart_function=lambda action, data: apply_cart_action(phone, action, data),
            history=get_conversation_history(phone),
        )

        # Integrar resultado
        final_result = integrate_with_orchestrator(orchestrator_result, multi_result)
        return final_result["response"]

    # Si no multi-intent → flujo normal
    return orquestar_normal(...)
```

**Criterios de activación**:
- Mensaje largo (>= 20 palabras)
- Múltiples oraciones (". ", "! ", " y ", etc.)
- LLM detectó >= 2 intents con confianza >= 0.6

**Impacto**: 🟡 **MEDIO** - Opt-in, requiere testing

**Rollout**: Beta testing
1. Activar para beta phones (10 usuarios)
2. Monitorear logs de multi-intent
3. Validar respuestas combinadas
4. Expandir a 50% → 100%

---

## 🧪 Testing y Validación

### Test Suite Nuevo

Crear `tests/test_coherence_fixes.py`:

```python
import pytest
from fran.search_utils import normalize_query_noise, rrf_fusion, RRFConfig
from fran.context_utils import parse_timestamp
from fran.embedding_utils import get_embedding_generator
from fran.pending_actions import get_pending_actions_queue


class TestNormalization:
    def test_deduplication(self):
        # Con dedup
        assert normalize_query_noise("bateria bateria honda", deduplicate=True) == "bateria honda"
        # Sin dedup
        assert normalize_query_noise("bateria bateria honda", deduplicate=False) == "bateria bateria honda"

    def test_accents(self):
        assert normalize_query_noise("batería ñandú") == "bateria nandu"


class TestRRFFusion:
    def test_v316_simple(self):
        bm25 = {0: (1, 0.8), 1: (2, 0.6)}
        faiss = {0: (1, 0.9), 2: (2, 0.7)}

        config = RRFConfig(use_consensus=False, use_calibration=False)
        results = rrf_fusion(bm25, faiss, config=config)

        # idx=0 debe estar primero (consenso BM25+FAISS)
        assert results[0][0] == 0

    def test_v317_consensus_boost(self):
        bm25 = {0: (1, 0.8), 1: (2, 0.6)}
        faiss = {0: (1, 0.9), 2: (2, 0.7)}

        config = RRFConfig(use_consensus=True, consensus_boost=0.15)
        results = rrf_fusion(bm25, faiss, config=config)

        # idx=0 con consensus boost debe tener score mayor
        assert results[0][2]["consensus"] == True


class TestTimestampParsing:
    def test_unix_timestamp(self):
        ts = parse_timestamp(1701518400.0)
        assert ts == 1701518400.0

    def test_iso_string(self):
        ts = parse_timestamp("2025-12-02T10:30:00")
        assert ts is not None
        assert isinstance(ts, float)

    def test_age_minutes(self):
        import time
        ts = parse_timestamp(5)  # 5 minutos atrás
        assert abs(ts - (time.time() - 300)) < 10  # margen de 10 seg


class TestEmbeddings:
    def test_openai_embeddings(self):
        gen = get_embedding_generator(expected_dim=3072)
        embs = gen.generate(["test"])

        assert len(embs) == 1
        assert embs[0].shape[0] == 3072
        assert gen.validate_dimensions(embs)

    def test_fallback_padding(self):
        # Simular fallo OpenAI → fallback a SentenceTransformer (384 dims)
        # Debe pad a 3072
        gen = get_embedding_generator(expected_dim=3072, fallback_model="sentence-transformers")
        # ... test con mock de OpenAI failure


class TestPendingActionsQueue:
    def test_fifo_order(self):
        queue = get_pending_actions_queue(db_path=":memory:")

        queue.add("phone1", "action1", {"data": 1})
        queue.add("phone1", "action2", {"data": 2})

        next_action = queue.get_next("phone1")
        assert next_action["action_type"] == "action1"

        queue.mark_processed(next_action["id"])

        next_action = queue.get_next("phone1")
        assert next_action["action_type"] == "action2"

    def test_no_overwrite(self):
        queue = get_pending_actions_queue(db_path=":memory:")

        queue.add("phone1", "action1", {"data": 1})
        queue.add("phone1", "action2", {"data": 2})

        all_actions = queue.get_all("phone1")
        assert len(all_actions) == 2  # No sobrescribió
```

### Ejecutar Tests

```bash
# Tests unitarios
pytest tests/test_coherence_fixes.py -v

# Tests de integración
pytest tests/test_fran316_compatibility.py -v
pytest tests/test_pipeline_v317.py -v

# Coverage
pytest --cov=fran --cov-report=html
```

---

## 📊 Monitoring

### Logs a Buscar

```bash
# Fix #1: RRF
tail -f logs/app.log | grep "rrf_fusion"

# Fix #2: Embeddings fallback
tail -f logs/app.log | grep "fallback embeddings"

# Fix #4: Timestamp parsing
tail -f logs/app.log | grep "parse_timestamp"

# Fix #5: Pending actions
tail -f logs/app.log | grep "pending action"

# Fix #6: Router fallback
tail -f logs/app.log | grep "No centroid available"

# Fix #7: Multi-intent
tail -f logs/app.log | grep "multi-intent"
```

### Métricas Clave

```python
# Coherencia RRF (v3.16 vs v3.17)
coherence_rate = matching_top5 / total_queries  # Target: > 80%

# Embedding fallback rate
fallback_rate = fallback_count / total_embeddings  # Target: < 5%

# Pending actions race conditions
race_condition_rate = overwrites / total_actions  # Target: 0%

# Router accuracy (sin centroide)
false_negatives = technical_marked_social / total_technical  # Target: < 10%

# Multi-intent usage
multi_intent_rate = multi_intent_messages / total_messages  # Target: 5-10%
```

---

## 🚨 Rollback Plan

Si algo falla:

### Rollback Rápido (< 5 min)

```bash
# Revertir a commit anterior
git revert <commit_hash>
git push -f

# O revertir via env vars
export USE_NEW_RRF=false
export USE_NEW_EMBEDDINGS=false
export ENABLE_MULTI_INTENT=false
```

### Rollback por Componente

```python
# app.py
USE_UNIFIED_RRF = os.environ.get("USE_NEW_RRF", "false") == "true"

if USE_UNIFIED_RRF:
    from fran.search_utils import rrf_fusion
    results = rrf_fusion(...)
else:
    # Old implementation
    results = legacy_rrf_fusion(...)
```

---

## 📅 Timeline Recomendado

| Fase | Duración | Actividades |
|------|----------|-------------|
| **Semana 1** | 5 días | Deploy fixes #2, #4, #6 (defensive) |
| **Semana 2** | 5 días | A/B test fix #1 (RRF) con 10% tráfico |
| **Semana 3** | 5 días | Migración DB fix #5 (pending actions) |
| **Semana 4** | 5 días | Beta test fix #7 (multi-intent) |
| **Semana 5** | 5 días | Rollout completo + monitoring |

---

## ✅ Checklist de Deployment

- [ ] Tests unitarios pasan (pytest)
- [ ] Tests de integración pasan
- [ ] Logs configurados
- [ ] Métricas en dashboard
- [ ] Rollback plan documentado
- [ ] Beta phones configurados
- [ ] A/B test setup (10% → 50% → 100%)
- [ ] Monitoring 24/7 primera semana
- [ ] Postmortem si > 1% error rate

---

**Contacto**: Ver `ANALISIS_COHERENCIA_FRAN_3.6.md` para detalles técnicos.
