# 🔍 Análisis de coherencia interna – Fran v3.17

**Fecha**: 2025-03-03
**Alcance**: Revisión rápida de documentación, arquitectura y nomenclatura de la línea 3.17 (según README principal), con foco en detectar brechas accionables.

## 📌 Resumen ejecutivo
- La documentación pública describe una arquitectura **Fran 4.0** (FastAPI + PostgreSQL + Qdrant + LangGraph), pero la aplicación activa sigue siendo **Fran 3.x sobre Flask** con catálogo en CSV y FAISS en memoria.
- Existen **nombres y etiquetas heredadas** (ej. `Fran 3.16` en cabecera, logger `fran313`) que contradicen el estado 3.17 y el README.
- Los **fixes de coherencia** documentados (normalización y RRF unificados en `fran/search_utils.py`) aún no se aplican en `app.py`, manteniendo divergencias entre versiones.

## 🧭 Observaciones detalladas

### 1) Documentación vs implementación
- README anuncia una pila Fran **4.0 asincrónica** con FastAPI, PostgreSQL, Redis y Qdrant.【F:README.md†L1-L26】
- El runtime real expone **Fran 3.16** sobre **Flask** y depende de CSV + FAISS locales, sin rastro de FastAPI/Qdrant en el entrypoint; la configuración dinámica de PostgreSQL está presente pero queda inactiva al no cumplir las condiciones de conexión.【F:app.py†L2-L140】【F:app.py†L101-L110】
- El catálogo sigue cargándose desde un CSV remoto/local (`catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv`) en lugar de PostgreSQL o un vector DB externo; la ruta de Postgres queda sin usarse en el flujo estándar.【F:app.py†L101-L110】【F:app.py†L86-L93】

### 2) Nomenclatura y versionado internos
- El header del entrypoint etiqueta el servicio como **Fran 3.16** mientras el README habla de 4.0 y el A/B lo etiqueta como 3.17; además el logger usa el nombre antiguo `fran313`.
  - Cabecera y variantes: líneas 2-24 muestran el bloque “Fran 3.16… / versiones disponibles 3.14–3.17”.【F:app.py†L2-L24】
  - Logger inicializado con nombre `fran313`, lo que dificulta trazabilidad de métricas/alerts para 3.17.【F:app.py†L111-L119】
- La lógica de split por hash para enrutar 40/30/15/15% a 3.17/3.16/3.15/3.14 existe y coincide con lo dicho en README, pero el mix de etiquetas 3.16/3.17/4.0 genera mensajes contradictorios hacia usuarios y operadores.【F:app.py†L851-L892】【F:README.md†L24-L26】

### 3) Fixes de coherencia pendientes
- El **MIGRATION_GUIDE_FIXES** indica adoptar `fran.search_utils.normalize_query_noise()` y RRF unificado para evitar divergencias entre 3.16 y 3.17.【F:MIGRATION_GUIDE_FIXES.md†L13-L57】
- `app.py` continúa usando la función local `normalize_search_query` con regex simple (sin deduplicación ni módulos compartidos) y el resto del pipeline sigue acoplado a esa versión, por lo que persisten discrepancias de resultados vs v3.17 y las librerías compartidas. El endpoint tampoco invoca el ensamblador de RRF descrito para 3.17.【F:app.py†L978-L985】【F:app.py†L851-L892】

### 4) Plan de acción priorizado (próximas 24–48 h)
1. **Documentar estado real de despliegue**: actualizar README y cabecera de `app.py` para dejar claro que el entrypoint productivo es Fran 3.x en Flask, y que Fran 4.0 vive en `fran_v4/` sin exposición pública todavía.【F:README.md†L1-L26】【F:app.py†L2-L24】
2. **Unificar nomenclatura operacional**: renombrar logger (`fran313` → `fran317` o identificador de rollout) y ajustar banners/metrics para evitar falsos dashboards entre 3.16/3.17.【F:app.py†L111-L119】
3. **Completar migración de normalización/RRF**: sustituir `normalize_search_query` y el scoring de FAISS/BM25 en `app.py` por las implementaciones compartidas de `fran/search_utils.py` según `MIGRATION_GUIDE_FIXES`, manteniendo feature flags para comparar contra 3.16.【F:app.py†L978-L985】【F:MIGRATION_GUIDE_FIXES.md†L13-L57】
4. **Activar ruta PostgreSQL o deslistar**: si el catálogo en Postgres está listo, habilitar `is_postgres_enabled()` con credenciales y health-check; si no, remover la referencia del README hasta que haya ambiente operativo para reducir confusión.【F:app.py†L86-L93】【F:README.md†L1-L26】

## ✅ Recomendaciones inmediatas
1) Alinear el README con la versión efectiva: documentar claramente que el entrypoint activo es la línea 3.x sobre Flask y que Fran 4.0 vive en `fran_v4/` pero no está desplegada.
2) Actualizar nombres/labels de servicio (cabecera, logger y métricas) a **3.17** o al identificador oficial del rollout para evitar confusiones operativas.
3) Completar la migración de normalización/RRF en `app.py` usando `fran/search_utils.py`, respetando las configuraciones v3.16 y v3.17 descritas en la guía, para que las búsquedas devuelvan resultados coherentes entre versiones.
