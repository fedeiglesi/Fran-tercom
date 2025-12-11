# Guía Completa: Railway + PostgreSQL + pgvector - Solución de Problemas

**Problema**: Conexión y configuración de PostgreSQL con pgvector en Railway

**Última actualización**: 11 de Diciembre, 2025

---

## 🚨 Diagnóstico Rápido: ¿Cuál es tu problema?

### Problema 1: Error de Conexión
```
sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) connection to server failed
ConnectionRefusedError: [Errno 111] Connection refused
```

### Problema 2: pgvector no funciona
```
ERROR: extension "vector" is not available
UndefinedObject: type "vector" does not exist
```

### Problema 3: La búsqueda no da resultados
```
hybrid_search() returns empty array []
count_products() returns 0
```

### Problema 4: El proceso 'release' falla
```
python -m fran_v4.catalog_to_postgres failed
Max retries exceeded
```

---

## ✅ SOLUCIÓN COMPLETA - Paso a Paso

### PASO 1: Configurar PostgreSQL en Railway (CON pgvector)

#### Opción A: Crear nueva base de datos con pgvector

**1.1. En el Dashboard de Railway:**
```
1. Click "New Project"
2. Click "Add Service" → "Database" → "Add PostgreSQL"
3. Espera a que se provisione (2-3 minutos)
```

**1.2. Habilitar la extensión pgvector:**

Railway **NO incluye pgvector por defecto**. Tienes 2 opciones:

#### ✅ **Opción Recomendada: Usar Template con pgvector**

```bash
# En Railway, usa el template oficial de pgvector:
https://railway.app/template/pgvector

# O manualmente, conecta y ejecuta:
railway run psql $DATABASE_URL

# Dentro de psql:
CREATE EXTENSION IF NOT EXISTS vector;
\dx  # Verifica que aparezca "vector"
```

#### ⚠️ **Opción B: Migrar a Supabase (recomendado si Railway falla)**

Supabase incluye pgvector por defecto y es gratis hasta 500MB:

```bash
# 1. Crea proyecto en https://supabase.com/dashboard
# 2. Ve a Settings → Database
# 3. Copia el "Connection string" (URI mode)
# 4. Usa esa URL en Railway como DATABASE_URL
```

---

### PASO 2: Configurar Variables de Entorno en Railway

**2.1. En tu servicio web de Railway, agrega estas variables:**

```bash
# CRÍTICO: PostgreSQL
DATABASE_URL=postgresql://postgres:PASSWORD@HOST:PORT/DATABASE
# Railway provee esto automáticamente si vinculaste la DB

# CRÍTICO: OpenAI
OPENAI_API_KEY=sk-proj-...tu_clave_real_aqui

# OPCIONAL pero recomendado:
MODEL_NAME=gpt-4o-mini
OPENAI_EMBEDDING_MODEL=text-embedding-3-large

# Catálogo (URL pública de tu CSV):
CATALOGO_CSV_URL=https://raw.githubusercontent.com/TU_USUARIO/TU_REPO/main/catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv

# Configuración de reintentos:
DB_INIT_MAX_RETRIES=20
DB_INIT_BASE_DELAY=2.0
DB_INIT_MAX_DELAY=30.0

# Rate limiting:
RATE_LIMIT_PER_MINUTE=30

# Puerto (Railway lo setea automáticamente):
PORT=8000
```

**2.2. IMPORTANTE: Vincular base de datos al servicio web**

```
1. En Railway Dashboard, ve a tu servicio web
2. Click en "Variables" tab
3. Click "+ New Variable"
4. Click "Add Reference" → Selecciona tu PostgreSQL database
5. Esto auto-genera DATABASE_URL con el formato correcto
```

---

### PASO 3: Verificar que pgvector esté instalado

**3.1. Conecta a PostgreSQL desde Railway:**

```bash
# Desde tu terminal local (con Railway CLI):
railway login
railway link  # Selecciona tu proyecto
railway run psql $DATABASE_URL

# O usando la URL directamente:
psql "postgresql://postgres:PASSWORD@HOST:PORT/DATABASE"
```

**3.2. Dentro de psql, ejecuta:**

```sql
-- Verificar si pgvector está disponible:
SELECT * FROM pg_available_extensions WHERE name = 'vector';

-- Si aparece, actívala:
CREATE EXTENSION IF NOT EXISTS vector;

-- Verifica que esté activa:
\dx

-- Deberías ver:
--  vector | 0.5.1 | public | vector data type and ivfflat access method
```

**3.3. Si NO aparece pgvector:**

```
SOLUCIÓN 1: Usa el template de Railway con pgvector:
https://railway.app/template/pgvector

SOLUCIÓN 2: Migra a Supabase (pgvector incluido):
https://supabase.com/

SOLUCIÓN 3 (temporal): Desactiva pgvector y usa solo full-text
(ver sección "Plan B" al final)
```

---

### PASO 4: Actualizar el Procfile

**4.1. Verifica que tu `Procfile` tenga esto:**

```procfile
web: gunicorn fran_v4.api:app --worker-class uvicorn.workers.UvicornWorker --bind=0.0.0.0:$PORT --workers=1 --timeout=300 --graceful-timeout=120 --preload
release: python -m fran_v4.catalog_to_postgres "$CATALOGO_CSV_URL" --table-name products --drop-existing
```

**Cambios clave:**
- `--table-name products` (no `catalogo3`) para que coincida con `search_engine.py`
- `$CATALOGO_CSV_URL` debe apuntar a una URL pública (GitHub raw)

---

### PASO 5: Subir el CSV del catálogo a GitHub

**5.1. Sube tu catálogo a un repositorio público:**

```bash
# En tu repo, agrega el CSV:
git add catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv
git commit -m "Add catalog CSV"
git push
```

**5.2. Obtén la URL raw:**

```
1. Ve a GitHub → tu repo → archivo CSV
2. Click "Raw"
3. Copia la URL (ejemplo):
   https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/main/catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv
```

**5.3. En Railway, setea:**

```bash
CATALOGO_CSV_URL=https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/main/catalogo_tercom_ultra_normalizado_faiss_v2_FINAL.csv
```

---

### PASO 6: Deploy y Testing

**6.1. Deploy manual desde Railway CLI:**

```bash
# Desde tu repo local:
railway login
railway link
railway up
```

**6.2. Verificar logs:**

```bash
railway logs

# Busca estos mensajes de éxito:
# ✅ "Base de datos inicializada"
# ✅ "Se cargaron 2500 productos desde..."
# ✅ "Catálogo ya inicializado con 2500 productos"
```

**6.3. Test del endpoint de health:**

```bash
# Reemplaza con tu URL de Railway:
curl https://tu-app.up.railway.app/health

# Respuesta esperada:
{
  "status": "ok",
  "version": "4.0",
  "components": {
    "fastapi_async": true,
    "postgresql": true,
    "langgraph": true
  }
}
```

**6.4. Test de búsqueda:**

```bash
curl -X POST https://tu-app.up.railway.app/agent \
  -H "Content-Type: application/json" \
  -d '{
    "session_id": "test-123",
    "message": "batería honda cg 150"
  }'

# Respuesta esperada:
{
  "session_id": "test-123",
  "reply": "Te ofrezco estas baterías...",
  "context": [{"codigo": "...", "nombre": "...", "precio": ...}, ...]
}
```

---

## 🔧 TROUBLESHOOTING - Errores Comunes

### Error 1: "extension vector is not available"

**Causa**: PostgreSQL de Railway no tiene pgvector instalado

**Solución A (Recomendada)**: Migrar a Supabase

```bash
# 1. Crea cuenta en Supabase: https://supabase.com/dashboard
# 2. Nuevo proyecto → espera 2 min
# 3. Settings → Database → Connection string → URI
# 4. Copia la URI y úsala como DATABASE_URL en Railway
```

**Solución B**: Usar template de Railway con pgvector

```bash
# 1. Ve a: https://railway.app/template/pgvector
# 2. Deploy template
# 3. Migra tus datos a esta nueva DB
```

**Solución C (temporal)**: Desactivar búsqueda vectorial

Ver sección "Plan B: Sin pgvector" al final.

---

### Error 2: "Connection refused" o "timeout"

**Causa**: DATABASE_URL incorrecta o red no configurada

**Solución**:

```bash
# 1. Verifica DATABASE_URL en Railway:
railway variables

# 2. Debe tener formato:
# postgresql://usuario:password@host.railway.app:5432/railway

# 3. Si falta, vincular manualmente:
# Railway Dashboard → tu servicio → Variables → + Add Reference → PostgreSQL

# 4. Test de conectividad:
railway run psql $DATABASE_URL

# Si se conecta, el problema está en el código
# Si NO se conecta, el problema está en la configuración de Railway
```

---

### Error 3: "La búsqueda no retorna resultados"

**Causa**: Tabla `products` vacía o nombre incorrecto

**Diagnóstico**:

```bash
# Conecta a la DB:
railway run psql $DATABASE_URL

# Verifica tablas:
\dt

# Deberías ver:
# - products (con los productos)
# - conversation_events
# - carts
# - rate_limits
# - session_messages
# - session_pending_actions
# - session_search_snapshots

# Cuenta productos:
SELECT COUNT(*) FROM products;

# Si retorna 0:
# → El proceso 'release' falló o no se ejecutó
```

**Solución**:

```bash
# Ejecuta manualmente el cargador:
railway run python -m fran_v4.catalog_to_postgres "$CATALOGO_CSV_URL" --table-name products --drop-existing

# Verifica nuevamente:
railway run psql $DATABASE_URL -c "SELECT COUNT(*) FROM products;"
```

---

### Error 4: "Max retries exceeded" en startup

**Causa**: PostgreSQL tarda en iniciar o credenciales incorrectas

**Solución**:

```bash
# 1. Aumenta reintentos en Railway variables:
DB_INIT_MAX_RETRIES=30
DB_INIT_BASE_DELAY=3.0
DB_INIT_MAX_DELAY=60.0

# 2. Verifica que DATABASE_URL sea correcta:
railway variables | grep DATABASE_URL

# 3. Si sigue fallando, ejecuta startup manualmente:
railway run python -c "
import asyncio
from fran_v4.database import Database

async def test():
    db = Database()
    await db.init_models()
    print('✅ Conectado exitosamente')

asyncio.run(test())
"
```

---

### Error 5: "CATALOGO_CSV_URL not found"

**Causa**: URL inválida o archivo no público

**Solución**:

```bash
# 1. Verifica que la URL sea accesible:
curl -I "$CATALOGO_CSV_URL"

# Debe retornar:
# HTTP/2 200 OK

# 2. Si retorna 404:
# → El archivo no existe o el repo es privado
# → Asegúrate de usar la URL "raw" de GitHub

# 3. Si retorna 403:
# → El repo es privado
# → Haz el repo público o usa un token:
https://raw.githubusercontent.com/USER/REPO/BRANCH/file.csv?token=YOUR_TOKEN

# 4. Alternativa local (solo para testing):
# En Railway, sube el CSV como un volumen:
# Settings → Volumes → + Add Volume
# Luego usa ruta absoluta: /app/catalogo.csv
```

---

## 🎯 PLAN B: Sin pgvector (solo full-text)

Si pgvector te da demasiados problemas, puedes desactivarlo temporalmente y usar **solo búsqueda full-text** (que es nativa de PostgreSQL).

### Cambios necesarios:

**1. Modifica `fran_v4/search_engine.py`:**

Encuentra el método `ensure_schema()` (línea 92) y comenta la creación de embeddings:

```python
async def ensure_schema(self) -> None:
    pool = await self._get_pool()
    async with pool.acquire() as conn:
        # await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")  # ❌ COMENTAR
        await conn.execute("CREATE EXTENSION IF NOT EXISTS unaccent")

        await conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS products (
                codigo TEXT PRIMARY KEY,
                nombre TEXT NOT NULL,
                descripcion TEXT,
                marca TEXT,
                categoria TEXT,
                precio NUMERIC(12,2),
                stock INTEGER,
                metadata JSONB,
                -- embedding VECTOR({EMBEDDING_DIM}),  ❌ COMENTAR
                search_tsv tsvector GENERATED ALWAYS AS (
                    setweight(to_tsvector('spanish', coalesce(unaccent(nombre), '')), 'A') ||
                    setweight(to_tsvector('spanish', coalesce(unaccent(descripcion), '')), 'B') ||
                    setweight(to_tsvector('spanish', coalesce(unaccent(marca), '')), 'C') ||
                    setweight(to_tsvector('spanish', coalesce(unaccent(categoria), '')), 'C')
                ) STORED
            )
            """
        )

        # ❌ COMENTAR índice vectorial:
        # await conn.execute(
        #     """
        #     CREATE INDEX IF NOT EXISTS products_embedding_idx
        #     ON products USING ivfflat (embedding vector_cosine_ops)
        #     WITH (lists = 100)
        #     """
        # )

        # ✅ MANTENER índice full-text:
        await conn.execute(
            """
            CREATE INDEX IF NOT EXISTS products_search_idx
            ON products USING GIN (search_tsv)
            """
        )
```

**2. Modifica el método `hybrid_search()` para usar solo full-text:**

```python
async def hybrid_search(
    self, query_text: str, limit: int = 10, filters: Optional[Dict[str, Any]] = None
) -> List[Dict[str, Any]]:
    # ❌ COMENTAR búsqueda vectorial:
    # vector = await self._embed(query_text)
    # dense_results, text_results = await asyncio.gather(
    #     self._dense_search(vector, limit * 2, filters),
    #     self._text_search(query_text, limit * 2, filters),
    # )

    # ✅ USAR solo full-text:
    text_results = await self._text_search(query_text, limit, filters)

    # ❌ COMENTAR fusión:
    # merged = self._merge_results(dense_results, text_results)

    # ✅ USAR directamente resultados de texto:
    formatted: List[Dict[str, Any]] = []
    for _, score, payload in text_results[:limit]:
        enriched = dict(payload)
        enriched["score"] = float(score * 100)
        formatted.append(enriched)
    return formatted
```

**3. Modifica `prepare_catalog_payloads()` para NO generar embeddings:**

```python
async def prepare_catalog_payloads(self, rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    payloads: List[Dict[str, Any]] = []
    for row in rows:
        # ... (mismo código de antes)

        # ❌ COMENTAR generación de embeddings:
        # vector = await self._embed(base_text)

        payloads.append(
            {
                "codigo": row.get("codigo") or row.get("code") or row.get("id"),
                "nombre": nombre,
                "descripcion": descripcion or nombre,
                "marca": marca,
                "categoria": categoria,
                "precio": precio,
                "stock": row.get("stock"),
                # "vector": vector,  ❌ COMENTAR
                "metadata": {"sinonimos": synonyms} if synonyms else None,
            }
        )

    return payloads
```

**4. Modifica `upsert_documents()` para NO insertar embeddings:**

```python
async def upsert_documents(self, payloads: List[Dict[str, Any]]) -> None:
    if not payloads:
        return

    pool = await self._get_pool()
    records = []
    for item in payloads:
        # ❌ COMENTAR:
        # embedding = item.get("vector")
        # if embedding is None:
        #     continue

        codigo = item.get("codigo") or item.get("code") or item.get("id")
        if codigo is None:
            continue

        records.append(
            (
                str(codigo),
                item.get("nombre") or item.get("name") or "",
                item.get("descripcion") or item.get("description"),
                item.get("marca") or item.get("brand"),
                item.get("categoria") or item.get("family"),
                item.get("precio") or item.get("price") or item.get("price_ars"),
                item.get("stock"),
                item.get("metadata"),
                # embedding,  ❌ COMENTAR
            )
        )

    if not records:
        return

    async with pool.acquire() as conn:
        await conn.executemany(
            """
            INSERT INTO products (codigo, nombre, descripcion, marca, categoria, precio, stock, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            ON CONFLICT (codigo) DO UPDATE SET
                nombre = EXCLUDED.nombre,
                descripcion = EXCLUDED.descripcion,
                marca = EXCLUDED.marca,
                categoria = EXCLUDED.categoria,
                precio = EXCLUDED.precio,
                stock = EXCLUDED.stock,
                metadata = EXCLUDED.metadata
            """,
            records,
        )
```

### Ventajas del Plan B (solo full-text):

✅ **NO requiere pgvector** (PostgreSQL estándar)
✅ **Más rápido de configurar** (sin embeddings)
✅ **Menor latencia** (no hay llamadas a OpenAI para embeddings)
✅ **Menor costo** (no se usan tokens de embeddings)

### Desventajas:

⚠️ **Menor precisión semántica** (no entiende sinónimos complejos)
⚠️ **Búsqueda más literal** (requiere coincidencias de palabras)

---

## 📋 Checklist Final

Antes de hacer un deploy, verifica:

- [ ] PostgreSQL creado en Railway (o Supabase)
- [ ] pgvector instalado (o Plan B activado)
- [ ] DATABASE_URL configurada en variables de Railway
- [ ] OPENAI_API_KEY configurada
- [ ] CATALOGO_CSV_URL apunta a GitHub raw (URL pública)
- [ ] Procfile correcto con `release` y `web`
- [ ] Repo sincronizado con Railway
- [ ] Test de health endpoint retorna 200
- [ ] Test de búsqueda retorna productos

---

## 🚀 Comandos de Emergencia

### Resetear todo y empezar de cero:

```bash
# 1. Borrar todas las tablas:
railway run psql $DATABASE_URL -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"

# 2. Re-crear esquema:
railway run python -c "
import asyncio
from fran_v4.database import Database
from fran_v4.search_engine import HybridSearchEngine

async def reset():
    db = Database()
    await db.init_models()
    search = HybridSearchEngine()
    await search.ensure_schema()
    print('✅ Esquema recreado')

asyncio.run(reset())
"

# 3. Cargar catálogo:
railway run python -m fran_v4.catalog_to_postgres "$CATALOGO_CSV_URL" --table-name products --drop-existing

# 4. Verificar:
railway run psql $DATABASE_URL -c "SELECT COUNT(*) FROM products;"
```

---

## 📞 Soporte Adicional

**Si nada funciona:**

1. **Verifica logs completos**:
   ```bash
   railway logs --tail 100
   ```

2. **Prueba localmente primero**:
   ```bash
   # Usa PostgreSQL local con Docker:
   docker run --name postgres-pgvector \
     -e POSTGRES_PASSWORD=postgres \
     -p 5432:5432 \
     -d ankane/pgvector

   # Conecta localmente:
   export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/postgres"
   export OPENAI_API_KEY="sk-..."
   python main.py
   ```

3. **Comparte logs específicos**: Busca líneas con `ERROR`, `CRITICAL`, o `Exception`

---

## 🎯 Resumen de Soluciones

| Problema | Solución Rápida |
|----------|----------------|
| pgvector no disponible | Migrar a Supabase o usar Plan B (solo full-text) |
| Connection refused | Verificar DATABASE_URL y vincular servicio en Railway |
| Búsqueda sin resultados | Ejecutar `release` manualmente, verificar tabla `products` |
| Max retries | Aumentar `DB_INIT_MAX_RETRIES=30`, verificar credenciales |
| CSV no carga | Usar GitHub raw URL pública, verificar con `curl` |

---

**Fin de la Guía**

*Última actualización: 11 de Diciembre, 2025*
