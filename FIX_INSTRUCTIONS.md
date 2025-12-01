# 🔧 INSTRUCCIONES PARA ARREGLAR FRAN

## 🚨 Problema Actual
Fran solo responde "En qué te puedo ayudar" porque el catálogo no se carga correctamente y el centroid es None.

## ✅ Solución Rápida (5 minutos)

### Opción 1: Desactivar v3.17 temporalmente
```bash
# En Railway/Heroku/tu servidor
export USE_FRAN_316=true
# O
export USE_FRAN_315=true
# O
export USE_FRAN_314=true

# Reiniciar aplicación
```

Esto hace que Fran use una versión anterior que NO depende del router dinámico.

---

### Opción 2: Configurar variables faltantes

```bash
# Configurar API Key de OpenAI (CRÍTICO)
export OPENAI_API_KEY="sk-tu-api-key-aqui"

# Configurar modelo
export MODEL_NAME="gpt-4o-mini"

# Configurar catálogo
export CATALOG_URL="https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/refs/heads/Fran-3.13.2/catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv"

# Reiniciar aplicación
gunicorn app:app --workers=4 --timeout=120
```

---

## 🔧 Solución Robusta (20 minutos)

### Fix 1: Validación del Centroid

Editar `app.py` línea 2939-2950 (función `get_v317_resources`):

```python
def get_v317_resources():
    with _v317_lock:
        catalog, _, _, _ = get_catalog_and_index()

        if catalog and _v317_cache["catalog"] is None:
            _v317_cache["catalog"] = catalog

        if _v317_cache["catalog"] and _v317_cache["centroid"] is None:
            descriptions = [
                row.get("descripcion_normalizada") or row.get("descripcion") or ""
                for row in _v317_cache["catalog"]
                if (row.get("descripcion_normalizada") or row.get("descripcion"))
            ]

            # 🆕 VALIDACIÓN: Asegurar que hay descripciones
            if not descriptions or len(descriptions) < 10:
                logger.error(f"❌ Catálogo insuficiente para v3.17: {len(descriptions)} productos")
                return None, None, None

            _v317_cache["centroid"] = build_catalog_centroid(descriptions)

            # 🆕 VALIDACIÓN: Verificar que centroid se generó
            if _v317_cache["centroid"] is None:
                logger.error("❌ No se pudo generar centroid del catálogo")
                return None, None, None

        if _v317_cache["catalog"] and _v317_cache["schema"] is None:
            _v317_cache["schema"] = build_classifier_schema(_v317_cache["catalog"])

        return _v317_cache["catalog"], _v317_cache["centroid"], _v317_cache["schema"]
```

### Fix 2: Fallback Automático

Editar `app.py` línea 6199-6218 (función `orquestar_fran_v317`):

```python
def orquestar_fran_v317(mensaje_usuario: str, phone: str) -> str:
    start_time = time.time()
    user_message = sanitize_input(mensaje_usuario or "", max_length=1500)

    if not rate_limit_check(phone):
        reply = "Demasiados mensajes, esperá un minuto."
        save_message(phone, reply, "assistant")
        return reply

    save_message(phone, user_message, "user")

    catalog, centroid, schema = get_v317_resources()

    # 🆕 FALLBACK: Si no hay recursos, usar v3.16
    if not catalog or schema is None or centroid is None:
        logger.warning(f"⚠️  v3.17 no disponible (catalog={bool(catalog)}, centroid={bool(centroid)}, schema={bool(schema)}), usando fallback")
        # Fallback a v3.16
        return orquestar_fran_v316(mensaje_usuario, phone)

    output = orquestar_v317(user_message, catalog, centroid, schema)
    # ... resto del código
```

### Fix 3: Mejora del Router Dinámico

Editar `pipeline/router_dynamic.py` línea 62-82:

```python
def router_fase0_dynamic(message, catalog_centroid, threshold=0.35):
    """
    Decide si el mensaje pertenece al dominio técnico sin hardcodear palabras.
    """
    text = message.lower().strip()

    if len(text.split()) <= 1:
        return {"route": "social", "score": 0.0}

    # 🆕 VALIDACIÓN: Si no hay centroid, clasificar por keywords básicas
    if catalog_centroid is None:
        # Fallback básico con keywords
        technical_keywords = ['filtro', 'bujia', 'pastilla', 'amortiguador', 'aceite',
                            'kit', 'cadena', 'freno', 'motor', 'repuesto', 'pieza']
        has_technical = any(kw in text for kw in technical_keywords)
        return {
            "route": "technical" if has_technical else "social",
            "score": 0.5 if has_technical else 0.0,
            "fallback": True  # Marca que usó fallback
        }

    sim = compute_similarity(text, catalog_centroid)
    entropy = estimate_entropy(text)

    score = 0.7 * sim + 0.3 * entropy

    if len(text.split()) <= 2 and len(text) <= 5 and score < (threshold * 1.2):
        return {"route": "social", "score": score}

    if score >= threshold:
        return {"route": "technical", "score": score}
    return {"route": "social", "score": score}
```

---

## 🧪 Verificación

Después de aplicar los fixes:

```bash
# 1. Verificar que el servidor inicia sin errores
tail -f logs/app.log | grep "ERROR\|WARNING"

# 2. Test manual
curl -X POST http://localhost:5000/whatsapp \
  -d "From=whatsapp:+5491112345678" \
  -d "Body=filtro aceite yamaha"

# Debe responder con productos, NO "en que te puedo ayudar"

# 3. Test de saludos (debe responder apropiadamente)
curl -X POST http://localhost:5000/whatsapp \
  -d "From=whatsapp:+5491112345678" \
  -d "Body=hola"
```

---

## 📊 Monitoreo Post-Fix

```bash
# Ver versión activa
tail -f logs/app.log | grep "Usando Fran"

# Ver clasificaciones del router
tail -f logs/app.log | grep "router_fase0"

# Ver cargas del catálogo
tail -f logs/app.log | grep "Catálogo"
```

---

## 🆘 Si Nada Funciona

**Rollback total a v3.14:**
```bash
export USE_FRAN_314=true
unset USE_FRAN_317
unset USE_FRAN_316
unset USE_FRAN_315

# Reiniciar
gunicorn app:app
```

v3.14 es la versión más estable y NO depende del router dinámico ni centroid.
