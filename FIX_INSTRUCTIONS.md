# 🔧 INSTRUCCIONES PARA ARREGLAR FRAN

## 🚨 Problema Actual

Fran responde "¿En qué te puedo ayudar?" cuando el catálogo no se carga y el centroide del router queda en `None`, por lo que todas las consultas se enrutan como sociales.

## ✅ Solución Rápida (5 minutos)

### Opción 1: Desactivar v3.17 temporalmente

```bash
export USE_FRAN_316=true
# o
export USE_FRAN_315=true
# o
export USE_FRAN_314=true

# Reiniciar aplicación
```

Esto fuerza a usar una versión que no depende del router dinámico.

### Opción 2: Forzar carga de catálogo en v3.17

1. Instala dependencias de catálogo (pandas): `pip install -r requirements.txt`.
2. Verifica que exista `catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv`.
3. Establece la variable `OPENAI_API_KEY` para que el clasificador LLM funcione.
4. Reinicia la app.

## 🛠️ Mitigación en Código

Se agregó un *fallback* de keywords en `pipeline/router_dynamic.py` para que, si el centroide es `None`, las consultas con términos técnicos sigan la ruta técnica en lugar de caer siempre en social.

## 🧪 Diagnóstico del Problema

- **Causa Raíz**: El catálogo no se estaba cargando en producción, lo que dejaba `centroid=None`.
- **Efecto**: El router dinámico clasificaba todas las queries como "social" (score ~0.03) y el orquestador v3.17 respondía "¿En qué te puedo ayudar?".
- **Prueba del Bug con `centroid=None`**:
  - "filtro aceite"  → social ❌
  - "bujia honda"    → social ❌
  - "pastillas freno" → social ❌

## 🛠️ Solución Implementada

1. **Router con fallback de keywords** (`pipeline/router_dynamic.py`)
   - Si `centroid=None`, se usan keywords técnicas y marcas de motos comunes.
   - Ejemplos con fallback:
     - "filtro aceite"  → technical ✅ (score≈0.60, fallback=True)
     - "bujia honda"    → technical ✅ (score≈0.60, fallback=True)
     - "hola"           → social ✅ (score≈0.00)

2. **Validación mejorada de recursos v3.17** (`app.py`)
   - Valida catálogo disponible y tamaño razonable.
   - Intenta generar centroid y loguea advertencias si falla (router usa fallback sin caerse).

3. **Tests automatizados** (`tests/test_pipeline_v317.py`)
   - Cobertura del router sin centroid (keywords + marcas).

4. **Diagnóstico rápido** (`debug_fran.py`)
   - Script que carga recursos v3.17 y prueba el router en CLI.
