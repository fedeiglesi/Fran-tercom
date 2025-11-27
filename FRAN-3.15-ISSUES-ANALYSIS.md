# Análisis de Errores en Fran-3.15

## Problemas Identificados

### 1. ❌ Filtro de Motos Rechaza Productos Universales

**Ubicación:** `app.py:1274-1286`

**Problema:**
El filtro rechaza TODOS los productos cuando se detecta una moto, porque:

```python
if motos_detectadas:
    p_moto_brand = normalize_search_query(p.get("moto_brand", "") or p.get("brand", ""))
    p_moto_model = normalize_search_query(p.get("moto_model", "") or p.get("model", ""))
    if not p_moto_brand or not p_moto_model:  # ← PROBLEMA AQUÍ
        rejection_reasons["moto_detection_missing"] += 1
        return False
```

**Causa Raíz:**
- Las bujías NGK (y otros productos universales) NO tienen `moto_brand` ni `moto_model` válidos
- El catálogo CSV tiene datos incorrectos en las columnas `marca_moto` y `modelo_moto` para bujías:
  - Ejemplo: `1131/00205-002` (Bujía NGK C6HSA) tiene:
    - `marca_moto: "C6HSA"` ← ¡Debería ser vacío o una marca de moto!
    - `modelo_moto: "R/CORTA 10MM INTERMEDIA BRASIL"` ← ¡Debería ser vacío o un modelo de moto!

**Impacto:**
```
[MOTO][Filtro] Compatibles antes del filtro: 120 productos
[MOTO][Filtro] Después del filtro: 0 productos  ← ❌ Todos rechazados
```

**Solución:**
```python
if motos_detectadas:
    p_moto_brand = normalize_search_query(p.get("moto_brand", "") or p.get("brand", ""))
    p_moto_model = normalize_search_query(p.get("moto_model", "") or p.get("model", ""))

    # Si el producto NO tiene info de moto, considerarlo universal y PERMITIRLO
    if not p_moto_brand or not p_moto_model:
        # Producto universal - pasa el filtro
        return True  # ← Cambio principal

    # Si tiene info de moto, verificar compatibilidad
    if not any(
        normalize_search_query(m.get("brand", "")) in p_moto_brand and
        normalize_search_query(m.get("model", "")) in p_moto_model
        for m in motos_detectadas
    ):
        rejection_reasons["moto_detection_mismatch"] += 1
        return False
```

**Alternativa:** En lugar de cambiar el código, corregir el CSV para que bujías y productos universales tengan las columnas `marca_moto` y `modelo_moto` vacías.

---

### 2. ❌ Schema Validation Falla: `'intents' is a required property`

**Ubicación:** Template completion para query understanding

**Problema:**
```
2025-11-27 18:21:31,954 - WARNING - Template completion attempt 1 failed:
Schema validation failed: : 'intents' is a required property

2025-11-27 18:21:35,990 - WARNING - Template completion attempt 2 failed:
Schema validation failed: : 'intents' is a required property
```

**Causa:**
El LLM está devolviendo un JSON que NO incluye el campo `intents` requerido por el schema `QUERY_UNDERSTANDING_SCHEMA`.

El schema en Fran-3.15 requiere:
```python
{
    "type": "object",
    "required": ["intents"],  # ← Campo obligatorio
    "properties": {
        "intents": {
            "type": "array",
            "minItems": 1,
            ...
        }
    }
}
```

**Posibles causas:**
1. El prompt no es lo suficientemente claro para el LLM
2. El LLM está devolviendo un formato diferente
3. El mensaje "Qué diferencia hay" es ambiguo y confunde al LLM

**Solución:**
Agregar ejemplo en el prompt y hacer más explícito el formato esperado:

```python
"description": """
Sos Fran 3.16, asistente mayorista argentino 100% LLM-first.

Detectá TODAS las intenciones presentes en el mensaje...

**FORMATO DE SALIDA OBLIGATORIO:**
{
  "intents": [
    {
      "type": "product_search",  // SOLO: product_search, cart_action, social, clarification, tech_question, order_flow
      "span": "texto exacto del mensaje",
      "confidence": 0.85,
      "data": { "query": "...", "brand": "...", ... }
    }
  ]
}

**IMPORTANTE:** SIEMPRE devolvé el objeto con el array "intents", incluso si está vacío.
"""
```

---

## Logs Relevantes

### Caso 1: Bujía para Honda Wave
```
2025-11-27 18:08:57 - INFO - Body: Tenés bujía NGK para Honda Wave?
2025-11-27 18:09:00 - INFO - [MOTO] Detectada raw='wave' -> normalizada='WAVE 110'
2025-11-27 18:09:00 - INFO - [MOTO] Para filtrar: brand=HONDA model=WAVE 110
2025-11-27 18:09:01 - INFO - [MOTO][Filtro] Compatibles antes del filtro: 120
2025-11-27 18:09:01 - INFO - [MOTO][Filtro] Después del filtro: 0  ← ❌
2025-11-27 18:09:01 - INFO - [SEARCH] filtered_by_moto=30
```

### Caso 2: Cadena + Piñón + Corona para YBR 125
```
2025-11-27 22:24:06 - INFO - Body: busco cadena, piñón y corona para YBR 125.
2025-11-27 22:24:09 - INFO - [MOTO] Detectada raw='ybr' -> normalizada='YBR 125'
2025-11-27 22:24:11 - INFO - [MOTO][Filtro] Compatibles antes del filtro: 120
2025-11-27 22:24:11 - INFO - [MOTO][Filtro] Después del filtro: 0  ← ❌
```

### Caso 3: Schema Validation Error
```
2025-11-27 18:21:27 - INFO - Body: Qué diferencia hay?
2025-11-27 18:21:31 - WARNING - Template completion attempt 1 failed:
  Schema validation failed: : 'intents' is a required property
2025-11-27 18:21:35 - WARNING - Template completion attempt 2 failed:
  Schema validation failed: : 'intents' is a required property
2025-11-27 18:21:37 - INFO - [STEP 1] Normalized: 'Qué diferencia hay' | Intent: clarification
```

---

## Recomendaciones

### Prioridad Alta
1. **Corregir filtro de motos** para permitir productos universales
2. **Mejorar prompt de query understanding** para evitar errores de schema

### Prioridad Media
3. **Limpiar catálogo CSV**: Revisar y corregir columnas `marca_moto`/`modelo_moto` para productos universales
4. **Agregar validación** al cargar el CSV para detectar datos incorrectos

### Prioridad Baja
5. **Agregar tests** para validar el filtro de motos con productos universales
6. **Mejorar logs** para mostrar por qué se rechazan productos (incluir `rejection_reasons`)

---

## Datos del CSV

**Header:**
```
codigo,descripcion,precio_dolares,precio_pesos,familia,familia_nombre,proveedor_nombre,marca_moto,modelo_moto,descripcion_normalizada,sinonimos,cilindrada,categoria_final
```

**Ejemplo de bujía (INCORRECTO):**
```
1131/00205-002,BUJIA NGK C6HSA R/CORTA 10MM INTERMEDIA BRASIL NGK,2.66,,1131,BUJIA NGK,NGK,C6HSA,R/CORTA 10MM INTERMEDIA BRASIL,...
                                                                                              ^^^^^^ ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
                                                                                              marca_moto  modelo_moto
                                                                                              (DEBERÍA ESTAR VACÍO)
```

**Ejemplo correcto (amortiguador específico):**
```
1021/00067-532,AMORTIGUADOR TRASERO HIDRAULICO HONDA NEW WAVE 110 FAR,...,FAR,HONDA,NEW WAVE 110,...
                                                                              ^^^^^ ^^^^^^^^^^^^
                                                                              marca_moto modelo_moto
                                                                              (CORRECTO)
```
