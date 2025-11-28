# 🚀 Fran 3.16 - Arquitectura Híbrida Agentic

## 📋 Descripción

Fran 3.16 combina lo mejor de Fran 3.14 (razonamiento agentic) y Fran 3.15 (structured outputs) en una arquitectura híbrida que implementa:

- **Razonamiento explícito** antes de actuar
- **Reflexion pattern** para auto-crítica y retry inteligente
- **Structured outputs** con JSON schemas validados
- **Estrategias de búsqueda adaptativas**
- **Observabilidad completa** con logging estructurado

## 🏗️ Arquitectura de 8 Fases

```
┌─────────────────────────────────────────────────────────────┐
│ 1. QUERY UNDERSTANDING (Structured)                         │
│    → Normaliza query, detecta intent, extrae entidades      │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ 2. REASONING (Agentic - NUEVO)                              │
│    → Razona qué acción tomar y con qué estrategia          │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ 3. ACTION EXECUTION (Tool Calling)                          │
│    → Ejecuta búsqueda con estrategia seleccionada          │
│      * hybrid: FAISS + BM25 + RRF                           │
│      * semantic_only: Solo FAISS                            │
│      * keyword_only: Solo BM25                              │
│      * family_based: Por familia de producto                │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ 4. REFLECTION (Auto-Crítica - NUEVO)                        │
│    → Evalúa calidad de resultados                          │
│    → Decide si re-intentar con estrategia diferente        │
└─────────────────────────────────────────────────────────────┘
                          ↓ (retry si calidad < umbral)
┌─────────────────────────────────────────────────────────────┐
│ 5. PRODUCT SELECTION (Structured)                           │
│    → Selecciona mejores productos (máx 5)                  │
│    → Analiza tipo de cliente y nivel de interés            │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ 6. RESPONSE GENERATION (Structured)                         │
│    → Genera mensaje final considerando todo el contexto    │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ 7. VALIDATION (Anti-Alucinación)                            │
│    → Verifica códigos existen en catálogo                  │
│    → Regenera si detecta alucinaciones                     │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ 8. CHUNKS & DELIVERY                                        │
│    → Envía respuesta + chunks adicionales si necesario     │
└─────────────────────────────────────────────────────────────┘
```

## 🎯 Características Principales

### ✨ Novedades de 3.16

1. **Reasoning Explícito** (Fase 2)
   - El LLM razona paso por paso antes de actuar
   - Decide qué tipo de acción ejecutar
   - Selecciona estrategia de búsqueda óptima
   - Considera contexto histórico y carrito

2. **Reflexion Pattern** (Fase 4)
   - Auto-crítica de resultados de búsqueda
   - Evaluación de calidad (score 0-100)
   - Decisión inteligente de retry
   - Máximo 1 retry para evitar loops

3. **Estrategias de Búsqueda Adaptativas**
   - `hybrid`: FAISS + BM25 + RRF (default)
   - `semantic_only`: Solo búsqueda semántica
   - `keyword_only`: Solo búsqueda por keywords
   - `family_based`: Por familia de productos

4. **Observabilidad Completa**
   - Logging estructurado JSON de cada fase
   - Metadata detallada de performance
   - Tracking de LLM calls y duración
   - Debug mode con logs extendidos

### 📊 Metadata Capturada

```json
{
  "version": "3.16",
  "understanding": {
    "normalized_query": "...",
    "intent": "product_search",
    "confidence": 0.85,
    "entities": {...}
  },
  "reasoning": {
    "action_type": "search",
    "search_strategy": "hybrid",
    "confidence": 0.90,
    "steps_count": 5
  },
  "action": {
    "strategy_used": "hybrid",
    "total_found": 25,
    "avg_score": 72.5,
    "duration_ms": 450
  },
  "reflection": {
    "quality_score": 75,
    "should_retry": false,
    "issues_count": 0
  },
  "response": {
    "message_length": 280,
    "products_cited_count": 3,
    "tone": "friendly"
  },
  "validation": {
    "hallucinations_detected": false
  },
  "performance": {
    "total_duration_ms": 2800,
    "llm_calls_count": 5,
    "retry_executed": false
  }
}
```

## ⚙️ Configuración

### Variables de Entorno

#### Activación Global

```bash
# Activar Fran 3.16 para todos los usuarios
USE_FRAN_316=true
```

#### Beta Testing

```bash
# Lista de teléfonos para beta testing (separados por comas)
BETA_PHONES_316="+5491112345678,+5491198765432,whatsapp:+5491123456789"
```

#### Debug Mode

```bash
# Activar logs detallados (incluye metadata JSON completa)
FRAN_DEBUG=true
```

### Ejemplo .env

```env
# OpenAI
OPENAI_API_KEY=sk-...
MODEL_NAME=gpt-4o-mini

# Fran 3.16
USE_FRAN_316=true
BETA_PHONES_316=+5491112345678,+5491198765432
FRAN_DEBUG=false

# Twilio
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
```

## 🚀 Deployment

### Opción 1: Activación Global

```bash
# Activar para todos
export USE_FRAN_316=true

# Reiniciar servicio
gunicorn app:app
```

### Opción 2: Beta Testing

```bash
# Solo para teléfonos específicos
export BETA_PHONES_316="+5491112345678,+5491198765432"

# Reiniciar servicio
gunicorn app:app
```

### Opción 3: Railway/Heroku

```bash
# Railway
railway variables set USE_FRAN_316=true

# Heroku
heroku config:set USE_FRAN_316=true -a tu-app
```

## 📊 Monitoreo

### Logs Estructurados

Con `FRAN_DEBUG=true`, cada interacción genera un log JSON completo:

```bash
# Ver logs de Fran 3.16
tail -f logs/app.log | grep "Fran 3.16"

# Filtrar por fase específica
tail -f logs/app.log | grep "FASE 4"

# Ver metadata completa
tail -f logs/app.log | grep "Interaction Log"
```

### Métricas de Performance

El sistema registra:
- **Duración total** por interacción
- **LLM calls** ejecutados
- **Retries** efectuados
- **Quality scores** promedio
- **Hallucinations** detectadas

## 🔄 Rollback

Si necesitas volver a la versión anterior:

```bash
# Desactivar Fran 3.16
export USE_FRAN_316=false

# O eliminar la variable
unset USE_FRAN_316

# Reiniciar servicio
gunicorn app:app
```

## 🧪 Testing

### Test Manual

```bash
# Enviar mensaje de prueba
curl -X POST http://localhost:5000/whatsapp \
  -d "From=whatsapp:+5491112345678" \
  -d "Body=bateria honda cg 150"
```

### Test de Intents

```bash
# Intent social
curl -X POST http://localhost:5000/whatsapp \
  -d "From=whatsapp:+5491112345678" \
  -d "Body=Hola! Cómo andás?"

# Intent product_search
curl -X POST http://localhost:5000/whatsapp \
  -d "From=whatsapp:+5491112345678" \
  -d "Body=filtro de aceite yamaha fz"

# Intent cart_action
curl -X POST http://localhost:5000/whatsapp \
  -d "From=whatsapp:+5491112345678" \
  -d "Body=agregame 3 de cada uno"
```

## 📚 Comparación de Versiones

| Característica | Fran 3.14 | Fran 3.15 | **Fran 3.16** |
|----------------|-----------|-----------|---------------|
| Razonamiento interno | ✅ Sí | ❌ No | ✅✅ Mejorado |
| Structured outputs | ⚠️ Parcial | ✅ Sí | ✅ Sí |
| Auto-corrección | ✅ Re-búsqueda | ❌ No | ✅✅ Reflexion |
| Estrategias búsqueda | 🔧 Fixed | 🔧 Fixed | ✅ Adaptativas |
| Observabilidad | ⚠️ Media | ✅ Alta | ✅✅ Total |
| Retry inteligente | ❌ No | ❌ No | ✅ Sí (max 1) |
| Anti-alucinación | ✅ Básico | ✅ Básico | ✅✅ Mejorado |
| **Alineación industria** | ✅ Alta | ⚠️ Media | ✅✅ Estado del Arte |

## 🎓 Referencias

La arquitectura de Fran 3.16 está inspirada en:

- **ReAct** (Reasoning + Acting) - Google/OpenAI
- **Reflexion** (Self-Reflection) - Princeton/Meta
- **LangGraph** - Agentic workflows
- **OpenAI O1** - Reasoning models
- **Claude Extended Thinking** - Anthropic

## 📝 Notas Técnicas

### Performance

- **LLM Calls**: 4-6 por interacción (vs 2-3 en 3.15)
- **Duración**: ~2-4 segundos (vs 1-2 en 3.15)
- **Costo**: ~20% más tokens que 3.15
- **Calidad**: Significativamente mejor en edge cases

### Limitaciones

- Máximo 1 retry por interacción
- Requiere `gpt-4o-mini` o superior
- Mayor latencia que versiones anteriores
- Requiere más tokens (mayor costo)

### Próximas Mejoras

- [ ] Cache de razonamiento para queries similares
- [ ] Parallel tool calling para mayor velocidad
- [ ] A/B testing automático con métricas
- [ ] Dashboard de observabilidad
- [ ] Fine-tuning del modelo de reasoning

## 🆘 Troubleshooting

### Fran 3.16 no se activa

```bash
# Verificar variable
echo $USE_FRAN_316

# Verificar en logs
tail -f logs/app.log | grep "Usando Fran"
```

### Errores de LLM

```bash
# Verificar API key
echo $OPENAI_API_KEY

# Verificar modelo disponible
curl https://api.openai.com/v1/models \
  -H "Authorization: Bearer $OPENAI_API_KEY" | grep gpt-4o-mini
```

### Performance lento

```bash
# Activar FRAN_DEBUG y analizar duration_ms
export FRAN_DEBUG=true

# Ver qué fase tarda más
tail -f logs/app.log | grep "duration_ms"
```

---

**Versión**: 3.16
**Fecha**: 2025-11-28
**Autor**: Implementación basada en diseño de arquitectura híbrida
