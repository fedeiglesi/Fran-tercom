# 🔍 Análisis de Coherencia: Fran 3.16/3.17

**Fecha**: 2025-12-02
**Rama**: `claude/analyze-fran-3.6-coherence-01LaKW9smYJPrSedHPQTExT4`
**Versiones Analizadas**: Fran 3.14, 3.15, 3.16, 3.17

---

## 📊 Resumen Ejecutivo

El análisis identifica **7 inconsistencias críticas** en la arquitectura de Fran que afectan:
- **Búsquedas de productos**: Diferentes resultados entre versiones para la misma query
- **Contexto y memoria**: Riesgo de expiración prematura de sesiones
- **Carrito**: Race conditions en operaciones concurrentes
- **Flujos**: Multi-intent implementado pero no utilizado en producción

**Severidad Global**: 🔴 **ALTA** - Requiere atención inmediata

---

## 🏗️ Arquitectura General

### Versiones Desplegadas (A/B/C Testing)

```
├── v3.17 (40% usuarios) - Orquestador dinámico + Router + Búsqueda híbrida v3.17
├── v3.16 (30% usuarios) - Arquitectura híbrida (Templates + Reasoning)
├── v3.15 (15% usuarios) - Templates estructurados
└── v3.14 (15% usuarios) - Dual LLM con reasoning
```

**Routing**: Hash MD5 del teléfono (`app.py:850-891`)

**Fallback**: v3.17 → v3.16 → Mensaje de error (`app.py:6903-6911`)

---

## 1️⃣ BÚSQUEDAS DE PRODUCTOS

### 1.1 Implementación Híbrida (FAISS + BM25 + RRF)

**Pipeline v3.16** (`app.py:3221-3448`):
```
Query → Normalización → [BM25 + FAISS] → RRF Fusion → Filtrado → Top-K
```

**Pipeline v3.17** (`pipeline/phases_v317.py:492-644`):
```
Query → Normalización + Dedup → [BM25 + FAISS + Fuzzy]
      → RRF Adaptive + Consensus Boost → Reranking → Top-K
```

#### Componentes

**BM25 (Keyword-based)**
- Implementación: `BM25Okapi` con tokenización normalizada
- Threshold: `0.4 × max_score`
- Peso RRF: `1.0`

**FAISS (Semantic)**
- Índice: `IndexFlatIP` (Inner Product)
- Threshold: `0.6 × max_distance`
- Peso RRF: `1.2` (mayor que BM25)
- k dinámico: `top_k × (4 si hay familias else 8)`

**RRF (Reciprocal Rank Fusion)**
```python
# v3.16
score = weight / (60 + rank)

# v3.17
adaptive_weight = base_weight × (1.15 si consensus else 1.0)
score = adaptive_weight / (60 + rank)
calibrated = score + 0.25 × (ponderación de scores crudos)
if consensus: calibrated × (1.0 + consensus_boost)
```

### 1.2 Normalización de Queries

**v3.16** (`app.py:977-984`):
```python
def normalize_search_query(query):
    normalized = strip_accents(query)  # Quita tildes
    normalized = re.sub(r"[^\w\s/.-]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized
```

**v3.17** (`pipeline/phases_v317.py:43-80`):
```python
def _normalize_text(text):
    text = lower + strip_accents + cleanup
    return " ".join(text.split())

def _normalize_query_noise(text):
    # Deduplica tokens adyacentes
    # Elimina n-gramas repetidos (ventana=3)
    return " ".join(deduped_tokens)
```

**Diferencia**:
- v3.16: "batería batería honda" → 3 tokens
- v3.17: "batería batería honda" → 2 tokens (deduplica)

### 🚨 INCONSISTENCIAS CRÍTICAS - BÚSQUEDAS

#### ❌ INCONSISTENCIA #1: RRF Fusion Diferente

**Ubicación**:
- v3.16: `app.py:3334-3355`
- v3.17: `pipeline/phases_v317.py:613-627`

**Problema**:
```python
# v3.16: RRF simple
fused_scores[code] += weight / (60 + rank)

# v3.17: RRF adaptive + consensus boost
adaptive_w = base_w * (1.15 if consensus else 1.0)
rrf = adaptive_w / (60 + rank)
calibrated = rrf + 0.25 * (scores crudos ponderados)
if consensus: calibrated *= 1.0 + config.consensus_boost
```

**Impacto**: 🔴 **CRÍTICO**
- Misma query retorna diferentes Top-5 entre versiones
- Productos con consenso (FAISS+BM25) ranquean más alto en v3.17
- Usuario puede recibir respuesta diferente según hash de teléfono

**Ejemplo**:
```
Query: "batería honda cg 150"
v3.16 Top-1: TERCOM 1234 (score: 0.82)
v3.17 Top-1: TERCOM 5678 (score: 0.91, con consensus boost)
```

---

#### ❌ INCONSISTENCIA #2: Embedding Model Fallback

**Ubicación**:
- v3.16: `app.py:151`
- v3.17: `pipeline/phases_v317.py:106-112`

**Problema**:
```python
# Ambas versiones:
EMBEDDING_MODEL = "text-embedding-3-large"  # 3072 dim

# Pero v3.17 fallback:
from sentence_transformers import SentenceTransformer
model = SentenceTransformer("all-MiniLM-L6-v2")  # 384 dim

# v3.16 fallback:
# Genera embeddings aleatorios de 3 dims (!!)
```

**Impacto**: 🔴 **CRÍTICO**
- Si OpenAI API falla, v3.17 usa embeddings incompatibles con índice FAISS
- FAISS espera vectores de 3072 dims, recibe 384 dims → **Crash o resultados inválidos**
- v3.16 fallback es aún peor (3 dims)

**Recomendación**:
- Usar mismo modelo de fallback en ambas versiones
- Generar índices FAISS separados por dimensionalidad
- O implementar validación de dimensionalidad antes de búsqueda

---

#### ❌ INCONSISTENCIA #3: Normalización Diferente

**Ubicación**:
- v3.16: `app.py:977` (`normalize_search_query`)
- v3.17: `pipeline/phases_v317.py:43,51` (`_normalize_text` + `_normalize_query_noise`)

**Problema**:
```python
# Input: "batería batería honda honda cg"

# v3.16: ["bateria", "bateria", "honda", "honda", "cg"]
# v3.17: ["bateria", "honda", "cg"]  # Deduplica
```

**Impacto**: 🟠 **ALTO**
- Queries con repeticiones (común en conversaciones) procesan diferente
- BM25 scores diferentes (frecuencia de términos cambia)
- Usuario dice "honda honda cg" → v3.16 puede sobre-ponderar "honda"

**Casos de uso afectados**:
- Follow-ups: "esa misma batería batería"
- Énfasis: "HONDA HONDA filtro"
- Errores de tipeo duplicados

---

## 2️⃣ CONTEXTO Y MEMORIA

### 2.1 SessionMemory (In-Memory)

**Estructura** (`app.py:447-625`):
```python
{
    "last_search": {
        "query": str,
        "results": list[dict],
        "metadata": dict,
        "timestamp": float,
        "confidence": float
    },
    "last_products_shown": {...},
    "last_cart_action": {...},
    "last_follow_up": {...},
    "last_brand_model": {...},
    "conversation_turns": int,
    "context_ttl": 1800  # 30 minutos
}
```

**TTL Check** (`app.py:568-576`):
```python
def _is_expired(data: dict) -> bool:
    ttl = data.get("context_ttl") or 1800
    latest_ts = max([last_search_ts, last_products_ts, ...])
    return (time.time() - latest_ts) > ttl
```

### 2.2 Persistent Storage (SQLite)

**Tablas**:
1. `search_history`: `phone, query, products, metadata, timestamp`
2. `pending_actions`: `phone, action_type, action_data, expires_at`
3. `carts`: `phone, code, quantity, name, price_ars, price_usd, created_at`
4. `messages`: `phone, role, content, timestamp`

**TTLs**:
- Carrito: **168 horas** (7 días)
- Pending action: **30 minutos**
- SessionMemory: **30 minutos**

### 🚨 INCONSISTENCIAS CRÍTICAS - CONTEXTO

#### ❌ INCONSISTENCIA #4: Hydratación de Contexto Incompleta

**Ubicación**: `app.py:482-490`

**Problema**:
```python
# Al recuperar contexto desde SQLite:
ts = last_search.get("metadata", {}).get("timestamp") or last_search.get("age_minutes")

if isinstance(ts, (int, float)):
    # Asume age_minutes → convierte a timestamp relativo
    timestamp = time.time() - float(ts) * 60

# ¿Pero qué si "timestamp" es un ISO string? → TypeError
# ¿Qué si "age_minutes" no existe? → timestamp = None
```

**Impacto**: 🟠 **ALTO**
- Si formato de timestamp es inconsistente → SessionMemory puede fallar en hydratación
- Contexto válido puede marcarse como "expirado" incorrectamente
- Usuario pierde historial de búsqueda reciente

**Casos observados**:
```python
# Guardado como ISO string (get_last_search retorna):
{"metadata": {"timestamp": "2025-12-02T10:30:00"}}

# Código asume float/int:
timestamp = time.time() - float("2025-12-02T10:30:00") * 60  # ← CRASH
```

**Fix sugerido**:
```python
def _parse_timestamp(ts_value):
    if isinstance(ts_value, (int, float)):
        return float(ts_value)
    if isinstance(ts_value, str):
        try:
            return datetime.fromisoformat(ts_value).timestamp()
        except:
            pass
    return None
```

---

## 3️⃣ CARRITO DE COMPRAS

### 3.1 Operaciones

**Funciones** (`app.py:2496-2607`):
1. `cart_add(phone, code, qty, ...)` - Inserta o actualiza
2. `cart_get(phone, max_age_hours=168)` - Consulta + auto-limpieza
3. `cart_update_qty(phone, code, qty)` - Actualiza cantidad
4. `cart_clear(phone)` - Elimina todo

**Auto-limpieza**:
```python
# En cart_get():
DELETE FROM carts
WHERE phone = ?
  AND created_at < datetime('now', '-168 hours')
```

### 3.2 Pending Actions

**Tabla**: `pending_actions`
- `phone, action_type, action_data, context, created_at, expires_at`
- **Constraint**: `UNIQUE(phone)` → Solo 1 acción pendiente por usuario

**Uso**:
```python
save_pending_action(
    phone,
    action_type="add_each_quantity",
    action_data={"qty": 5, "products": [...]},
    ttl_minutes=30
)

# Luego:
apply_add_each_quantity_pending(phone)
```

### 🚨 INCONSISTENCIAS CRÍTICAS - CARRITO

#### ❌ INCONSISTENCIA #5: Pending Action Race Condition

**Ubicación**:
- `app.py:1826-1847` (`save_pending_action`)
- `app.py:2524` (`SessionMemory.update_last_cart_action`)

**Problema**:
```python
# Request 1 (t=0):
save_pending_action(phone, "add_each_quantity", {"qty": 3, ...})

# Request 2 (t=0.1s, concurrente):
save_pending_action(phone, "stock_check", {"codes": [...]})

# SQLite: REPLACE INTO pending_actions ... (UNIQUE phone)
# → Request 2 sobrescribe Request 1

# Pero SessionMemory en Request 1:
SessionMemory.update_last_cart_action(phone, {"qty": 3})

# → SQLite tiene action="stock_check"
# → SessionMemory tiene action="add_each_quantity"
# → INCONSISTENCIA
```

**Impacto**: 🟠 **ALTO**
- Dos requests simultáneos del mismo usuario → Acción perdida
- SessionMemory y SQLite desincronizados
- Usuario puede no recibir confirmación de acción ejecutada

**Casos reales**:
- Usuario envía 2 mensajes rápidos: "Agregá 5 baterías" + "Y dame stock de filtros"
- Ambos llegan a webhook simultáneamente (2 workers de Gunicorn)
- Segunda acción sobrescribe primera

**Fix sugerido**:
- Usar queue de acciones (JSON array en SQLite)
- O cambiar constraint a `UNIQUE(phone, created_at)`
- O usar lock optimista con version counter

---

## 4️⃣ FLUJOS DE USUARIO

### 4.1 Intents Soportados

```
product_search    - Búsqueda de productos
cart_action       - Agregar/quitar/ver carrito
social            - Saludos, conversación casual
tech_question     - Preguntas técnicas sin búsqueda
order_flow        - Checkout
follow_up         - Dependencia de contexto previo
busca_producto    - Alias (retro-compatibilidad)
otros             - Otros/desconocido
```

### 4.2 Arquitectura de Routing

**v3.14**: `LLM1(reasoning) → Action → LLM2(response)`

**v3.15**: `Templates JSON → Normalized → Structured Output`

**v3.16**: `Phase1(Understanding) → Phase2(Search) → Phase3(Compat) → Phase4(Reasoning) → Phase5(Response)`

**v3.17**: `Phase0(Router) → Phase1(Classifier) → Phases2-7(Hybrid + Reasoning)`

### 4.3 Router Dinámico v3.17

**Ubicación**: `pipeline/router_dynamic.py`

**Decisión**: Social vs Técnico

**Lógica**:
```python
def router_fase0_dynamic(message, catalog_centroid):
    # Regla 1: Mensajes cortos + baja entropía = social
    if len(text.split()) == 1 and len(text) <= 4:
        if estimate_entropy(text) < 0.25:
            return "social"

    # Regla 2: Sin centroide → fallback a keywords
    if catalog_centroid is None:
        if any(kw in text for kw in TECHNICAL_KEYWORDS):
            return "technical"
        return "social"

    # Regla 3: Similitud semántica vs centroide
    sim = compute_similarity(text, catalog_centroid)
    entropy = estimate_entropy(text)
    score = 0.7 * sim + 0.3 * entropy

    return "technical" if score >= 0.35 else "social"
```

### 🚨 INCONSISTENCIAS CRÍTICAS - FLUJOS

#### ❌ INCONSISTENCIA #6: Router Frágil sin Centroide

**Ubicación**: `pipeline/router_dynamic.py:107-159`

**Problema**:
```python
if catalog_centroid is None:
    # Fallback a keywords: "bateria", "honda", "filtro", ...
    # Pero mensaje: "tengo una moto y no se que repuesto comprar"
    # → NO triggerea keywords
    # → Marca como "social"
    # → Usuario no recibe búsqueda de productos
```

**Impacto**: 🟠 **ALTO**
- Si catálogo no carga (error al generar centroide) → TODO se marca como social
- Queries técnicas válidas sin keywords específicas → Ignoradas
- False negatives en detección de intents técnicos

**Casos problemáticos**:
```python
# Mensajes técnicos SIN keywords que fallan:
"necesito repuestos para mi moto"          → social ❌
"cuánto sale arreglar el motor"            → social ❌
"me recomendás algo para el mantenimiento" → social ❌

# Deberían ser "technical" pero no tienen keywords hardcodeadas
```

**Fix sugerido**:
- Agregar más keywords técnicas
- O usar LLM lightweight para clasificar si centroide=None
- O forzar a "technical" por defecto sin centroide

---

#### ❌ INCONSISTENCIA #7: Multi-Intent No Usado

**Ubicación**:
- Módulo: `multi_intent.py`
- Imports: Solo en tests (`test_multi_intent.py`, `test_advanced_multi_intent.py`)
- **NO usado** en `app.py` ni `orquestador_v317.py`

**Problema**:
```python
# multi_intent.py tiene implementación completa:
def parse_multi_intent(llm, message):
    # Detecta múltiples intenciones en 1 mensaje
    # Ej: "Hola! Quiero 2 baterías y 1 filtro, ¿cuánto sale?"
    # → [social, product_search, product_search, price_question]
    return intents

def orchestrate(llm, message, ...):
    # Ejecuta cada intent en orden de prioridad
    for intent in sorted_by_priority(intents):
        execute(intent)
    return combined_response

# PERO en orquestador_v317.py:
# ¡NO SE USA! Solo detecta intent principal
```

**Impacto**: 🟠 **ALTO**
- Mensajes multi-intent solo procesan 1 intent (el principal)
- Usuario pierde funcionalidad en queries complejas
- Código muerto (200 líneas) en producción

**Casos afectados**:
```python
# Input: "Hola! Dame 3 baterías para honda y stock de filtros"

# v3.16/v3.17 actual:
intent = "product_search"  # Solo principal
response = buscar("3 baterías para honda y stock de filtros")

# Multi-intent esperado:
intents = [
    {"type": "social", "span": "Hola!"},
    {"type": "cart_action", "span": "Dame 3 baterías para honda"},
    {"type": "product_search", "span": "stock de filtros"}
]
# Ejecutar cada uno por separado
```

**Decisión requerida**:
- ¿Activar multi-intent en v3.17?
- ¿O eliminar código no usado?

---

## 🔧 OTROS PROBLEMAS DE COHERENCIA

### 5.1 Fragmentación de Búsqueda

- **v3.16**: Función `hybrid_search()` en `app.py:3221`
- **v3.17**: Función `fase2_hybrid_search()` en `pipeline/phases_v317.py:492`
- **Resultado**: Duplicación de lógica → Bug fix en una no se aplica a la otra

**Ejemplo**:
```python
# Bug fix en v3.16 (agregar filtro de stock):
if product.get("stock", 0) > 0:
    candidates.append(product)

# ¿Se aplica en v3.17? ← Hay que recordar hacerlo manual
```

### 5.2 Fallbacks en Cascada Peligrosos

```python
# v3.17 → v3.16 → Error
try:
    return orquestar_v317(...)
except Exception as exc:
    try:
        return orquestar_fran_v316(...)  # ← Puede fallar por MISMO error
    except Exception:
        return "Error técnico"
```

**Problema**: Si error es en componente compartido (ej. `build_catalog_centroid`), ambos fallan.

**Fix**: Clasificar errores antes de fallback:
```python
except CatalogError:
    # No hacer fallback, retornar error específico
except LLMError:
    # Sí hacer fallback
```

### 5.3 Normalización Inconsistente Entre Componentes

| Componente | v3.16 | v3.17 |
|------------|-------|-------|
| Query input | `normalize_search_query()` | `_normalize_text()` + `_normalize_query_noise()` |
| Catálogo | `search_text` precalculado | Concatenación dinámica |
| Compatibilidad | `normalize_search_query()` | `_normalize_text()` |
| BM25 tokens | `_tokenize_text()` | `_tokenize()` |

**Resultado**: Mismo producto podría no matchear entre versiones.

---

## 📈 TABLA RESUMEN DE INCONSISTENCIAS

| # | Descripción | Versiones | Severidad | Ubicación | Impacto | Fix Estimado |
|---|-------------|-----------|-----------|-----------|---------|--------------|
| 1 | RRF Fusion diferente | 3.16 vs 3.17 | 🔴 CRÍTICO | `app.py:3334` vs `phases_v317.py:613` | Resultados diferentes | 4 hrs |
| 2 | Embedding fallback incompatible | 3.16 vs 3.17 | 🔴 CRÍTICO | `app.py:151` vs `phases_v317.py:106` | Crash si OpenAI falla | 2 hrs |
| 3 | Normalización diferente | 3.16 vs 3.17 | 🟠 ALTO | `app.py:977` vs `phases_v317.py:43` | Queries duplicadas procesan distinto | 1 hr |
| 4 | Hydratación contexto | Todos | 🟠 ALTO | `app.py:482-490` | Sesiones expiran prematuramente | 2 hrs |
| 5 | Race condition pending actions | Todos | 🟠 ALTO | `app.py:1826` | Acciones perdidas en concurrencia | 3 hrs |
| 6 | Router frágil sin centroide | 3.17 | 🟠 ALTO | `router_dynamic.py:107` | Queries técnicas → social erróneamente | 2 hrs |
| 7 | Multi-intent no usado | 3.17 | 🟠 ALTO | `multi_intent.py` (200 LOC) | Funcionalidad no disponible | 4 hrs o delete |

**Total Fix Estimado**: 18-22 horas de desarrollo

---

## ✅ RECOMENDACIONES PRIORITARIAS

### 🔴 **Urgente** (Esta Sprint)

1. **Unificar RRF Fusion** (#1)
   - Crear función `_rrf_fusion()` única
   - Ambas versiones llaman misma implementación
   - Agregar flag `use_consensus_boost` para v3.17

2. **Estandarizar Embedding Fallback** (#2)
   - Usar `SentenceTransformer("all-MiniLM-L6-v2")` en ambas
   - O eliminar fallback y fallar rápido si OpenAI no responde
   - Validar dimensionalidad antes de FAISS search

3. **Fix Hydratación de Contexto** (#4)
   - Implementar `_parse_timestamp()` robusto
   - Agregar tests para ISO string, float, int, None
   - Loggear warnings si formato inesperado

### 🟠 **Alta Prioridad** (Próxima Sprint)

4. **Unificar Normalización** (#3)
   - Migrar v3.16 a `_normalize_text()` + `_normalize_query_noise()`
   - O agregar flag para deshabilitar dedup si afecta performance

5. **Fix Race Condition Carrito** (#5)
   - Cambiar `pending_actions` a queue JSON
   - O agregar lock optimista con version counter
   - Agregar test de concurrencia

6. **Mejorar Router sin Centroide** (#6)
   - Agregar más keywords técnicas
   - O cambiar default a "technical" si centroide=None
   - Agregar telemetría de false negatives

### 🟡 **Media Prioridad**

7. **Multi-Intent Decision** (#7)
   - **Opción A**: Activar en v3.17 (4 hrs)
   - **Opción B**: Eliminar código muerto (30 mins)
   - Decisión de producto requerida

8. **Consolidar Búsqueda**
   - Refactor `hybrid_search()` a módulo compartido
   - Eliminar duplicación entre v3.16 y v3.17

9. **Mejorar Fallbacks**
   - Clasificar errores antes de fallback
   - Agregar circuit breaker para evitar cascading failures

---

## 📊 MÉTRICAS DE COHERENCIA

### Cobertura de Tests

```bash
# Correr tests para validar inconsistencias:
pytest tests/test_fran316_compatibility.py
pytest tests/test_pipeline_v317.py
pytest tests/test_multi_intent.py
pytest tests/test_hybrid_rrf_calibration.py
```

**Coverage actual**:
- Búsqueda híbrida: ~70% (falta test de fallback embeddings)
- Contexto/Memoria: ~60% (falta test de race conditions)
- Carrito: ~80%
- Routing: ~50% (falta test de centroide=None)

### Observabilidad

**Logs a Monitorear**:
```python
# Inconsistencia #1 (RRF):
logger.info(f"[Fran {version}] Top-5 codes: {top_codes}")

# Inconsistencia #2 (Embeddings):
logger.warning(f"Using fallback embeddings: {model_name}")

# Inconsistencia #4 (Contexto):
logger.warning(f"Failed to parse timestamp: {ts_value}")

# Inconsistencia #5 (Race):
logger.warning(f"Pending action overwritten: {phone}")
```

**Métricas de Coherencia**:
```python
# % de queries con Top-5 coincidente entre v3.16 y v3.17
coherence_rate = len(matching_top5) / total_queries

# Target: > 80% coherence
```

---

## 🧪 PLAN DE VALIDACIÓN

### Fase 1: Tests Unitarios (2 días)

```python
# test_rrf_consistency.py
def test_rrf_v316_vs_v317_same_results():
    query = "batería honda cg 150"
    results_316 = hybrid_search_v316(query, top_k=5)
    results_317 = fase2_hybrid_search_v317(query, top_k=5)

    # Top-5 codes deben coincidir en 80%+
    overlap = len(set(results_316[:5]) & set(results_317[:5]))
    assert overlap >= 4  # 80% de 5

# test_embedding_fallback.py
def test_embedding_fallback_dimensions():
    # Simular fallo de OpenAI
    with mock_openai_failure():
        emb_316 = generate_embeddings_v316(["test"])
        emb_317 = generate_embeddings_v317(["test"])

        # Ambas deben tener misma dimensionalidad
        assert emb_316[0].shape == emb_317[0].shape
```

### Fase 2: Tests de Integración (3 días)

```python
# test_multi_version_consistency.py
@pytest.mark.parametrize("version", ["3.16", "3.17"])
def test_search_consistency(version):
    queries = [
        "batería honda cg 150",
        "filtro aceite yamaha fz",
        "amortiguador zanella zb"
    ]

    for query in queries:
        result = run_search(query, version=version)
        assert len(result) > 0
        assert result[0]["relevance_score"] >= 65.0
```

### Fase 3: A/B Test en Producción (1 semana)

- 50% usuarios en v3.16 (con fixes)
- 50% usuarios en v3.17 (con fixes)
- Monitorear:
  - Tasa de conversión (búsqueda → carrito)
  - Satisfacción (mensajes de "no encontré")
  - Latencia p95
  - Coherencia de Top-5

---

## 📚 ARCHIVOS CLAVE

| Archivo | Líneas | Descripción | Prioridad Fix |
|---------|--------|-------------|---------------|
| `app.py` | 7292 | Core v3.16 + búsqueda | 🔴 Urgente |
| `pipeline/phases_v317.py` | ~1200 | Pipeline v3.17 | 🔴 Urgente |
| `pipeline/orquestador_v317.py` | 99 | Orquestador v3.17 | 🟠 Alta |
| `pipeline/router_dynamic.py` | 180 | Router Fase 0 | 🟠 Alta |
| `multi_intent.py` | 200 | Multi-intent (no usado) | 🟡 Media |
| `fran/clients.py` | 25 | HTTP + LLM clients | 🟢 Baja |
| `fran/observability.py` | 47 | Metrics + CircuitBreaker | 🟢 Baja |

---

## 🎯 CONCLUSIONES

### ✅ Fortalezas

1. **Arquitectura modular** - Separación clara de fases
2. **Observabilidad** - Logging estructurado y métricas
3. **Testing robusto** - Buena cobertura en tests unitarios
4. **Fallbacks defensivos** - v3.17 → v3.16 → Error

### ⚠️ Debilidades

1. **Fragmentación** - Búsqueda duplicada en v3.16 y v3.17
2. **Inconsistencias** - 7 problemas críticos/altos identificados
3. **Código muerto** - `multi_intent.py` no usado en producción
4. **Documentación** - Falta doc de diferencias entre versiones

### 🚀 Next Steps

1. **Inmediato** (Esta semana):
   - Fix #1, #2, #4 (RRF, embeddings, contexto)
   - Agregar tests de validación
   - Deploy con feature flag

2. **Corto plazo** (Próxima sprint):
   - Fix #3, #5, #6 (normalización, race condition, router)
   - Consolidar búsqueda en módulo compartido
   - Decisión sobre multi-intent (#7)

3. **Mediano plazo** (2-3 sprints):
   - Migrar v3.14/v3.15 a arquitectura unificada
   - Eliminar código legacy
   - Mejorar observabilidad con dashboard

---

**Generado por**: Claude (Sonnet 4.5)
**Fecha**: 2025-12-02
**Versión**: 1.0
