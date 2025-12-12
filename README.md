## Estado actual de Fran
- **Fran 4.0** (FastAPI + PostgreSQL/pgvector + Redis + LangGraph) es ahora el entrypoint por defecto. El servidor se
  expone desde `fran_v4.api:app` y puede correrse localmente con `python run_server.py` o `uvicorn fran_v4.api:app --reload`.
- El código de la versión **3.x** en Flask (`app.py`) ha sido movido a la carpeta `_legacy` para referencias históricas.
- El catálogo se carga desde PostgreSQL mediante el proceso `release` de Railway o ejecutando manualmente
  `python -m fran_v4.catalog_to_postgres "$CATALOGO_CSV_URL" --table-name catalogo3 --drop-existing`.

## Fran 4.0 (arquitectura asincrónica lista para producción)
- **FastAPI** con uvicorn/gunicorn como servidor asíncrono preparado para Railway y contenedores.
- Persistencia transaccional y motor de búsqueda vectorial/full-text en **PostgreSQL con pgvector** (SQLAlchemy async)
  con fallback en memoria para resiliencia temporal si la base está caída.
- Orquestador sobre **LangGraph** con bucle de razonamiento, herramientas desacopladas y memoria de sesión
  persistente.
- Código modular en `fran_v4/` separando base de datos, motor de búsqueda, LLM y grafo del agente.

Constante	Valor	Descripción
MAX_SEARCH_RESULTS	60	Máx productos de búsqueda híbrida
MAX_PRODUCTS_FOR_LLM	15	Máx productos enviados al LLM
PRODUCTS_PER_CHUNK	30	Productos por chunk en mensajes largos
RELEVANCE_MIN_SCORE	65.0	Score mínimo post-validación
QUALITY_HIGH_THRESHOLD	70.0	Calidad excelente
QUALITY_MEDIUM_THRESHOLD	60.0	Calidad aceptable
WHATSAPP_MSG_LIMIT	1600	Caracteres máx por mensaje Twilio
RATE_LIMIT	20	Mensajes máx por ventana
RATE_WINDOW	60	Ventana de rate limit (segundos)
INSTANT_THRESHOLD	15	Items para procesamiento instantáneo
ASYNC_QUICK	40	Umbral para async rápido
MAX_ITEMS	150	Máx items en last_search
CART_TTL	168h	TTL del carrito (7 días)
PENDING_ACTION_TTL	30min	TTL de pending actions

## Embeddings y pipeline v3.17
- El modelo de embeddings se estandarizó con la variable `OPENAI_EMBEDDING_MODEL` (por defecto `text-embedding-3-large`) para el pipeline híbrido y la generación de caché.
- El orquestador `orquestar_v317` ahora está integrado en `app.py` y participa del enrutamiento principal (40% del tráfico por hash y 100% si se setea `USE_FRAN_317=true`).

### Variables de entorno de embeddings
- `OPENAI_EMBEDDING_MODEL`: modelo de embeddings a usar. Se infiere automáticamente la dimensión para `text-embedding-3-large` (3072), `text-embedding-3-small`/`text-embedding-ada-002` (1536) y `paraphrase-multilingual-MiniLM` (384).
- `EMBEDDING_DIM`: opcional para forzar la dimensión cuando el modelo no está en la lista anterior; debe coincidir con la definición `embedding VECTOR(<dim>)` en PostgreSQL.

## Notas sobre los arreglos recientes
- Se guarda un *snapshot* de las respuestas de búsquedas múltiples cuando se devuelven listas de productos. Así, si el usuario luego pide acciones en bloque (por ejemplo, "dame 10 de cada producto"), el sistema reutiliza esa lista sin tener que repetirla.
- La lógica de guardado de estos *snapshots* se unificó en un helper (`_persist_search_snapshot`) para evitar duplicación y asegurar que todas las rutas que generan listas queden alineadas.

## Diagnóstico rápido: `ConnectionRefusedError` con PostgreSQL
Un `ConnectionRefusedError: [Errno 111] Connection refused` aparece **antes** de autenticar porque ningún servicio está escuchando en el host/puerto configurados. Antes de revisar credenciales o `pg_hba.conf`, valida lo siguiente:

1. **Servicio en marcha**: verifica que PostgreSQL esté activo y escuchando en el puerto esperado (`psql -h <host> -U <usuario> -d <db>`).
2. **Host y puerto correctos**: confirma que la URL de conexión sea alcanzable desde el contenedor donde corre la app.
3. **Red Docker**: si usas contenedores, comprueba que app y base estén en la misma red (`docker network ls` + `docker network inspect <red>`).
4. **Puertos expuestos**: valida que el puerto de PostgreSQL esté publicado y sin bloqueos de firewall.

Solo después de confirmar la conectividad de red tiene sentido revisar errores de autenticación (p. ej., `FATAL: password authentication failed`).

### Reintentos de arranque y reconexión
- La inicialización de la base hace reintentos con backoff y, si agota el límite, sigue reintentando en segundo plano sin caer el proceso.
- Variables de entorno ajustables:
  - `DB_INIT_MAX_RETRIES` (por defecto `10`; deja vacío para reintentos infinitos en primer plano).
  - `DB_INIT_BASE_DELAY` (segundos; por defecto `1.0`).
  - `DB_INIT_MAX_DELAY` (límite superior del backoff; por defecto `10.0`).

## Carga automática del catálogo en Railway
El proceso de `release` en Railway ahora ejecuta el cargador dinámico que crea la tabla `catalogo3`
a partir del CSV indicado. Configura la variable de entorno `CATALOGO_CSV_URL` con la URL raw del
CSV (por ejemplo, la de GitHub) y, en cada deploy, Railway descargará ese CSV y recreará la tabla.

Si quieres probar el cargador manualmente desde tu máquina o desde una consola en Railway, ejecuta:

```bash
python -m fran_v4.catalog_to_postgres "$CATALOGO_CSV_URL" --table-name catalogo3 --drop-existing
```

El cargador también acepta rutas locales a archivos CSV si prefieres cargar uno desde disco.

### Configuración rápida para `Subir_catalogo`

El script `Subir_catalogo` acepta dos formas de credenciales para conectarse a Postgres:

- **DATABASE_URL**: una URL completa (`postgresql://usuario:password@host:puerto/db`).
- `POSTGRES_*`: variables individuales `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_HOST`, `POSTGRES_DB` y opcionalmente `POSTGRES_PORT` (5432 por defecto).

En Railway normalmente dispones de `DATABASE_URL`. Si prefieres usar las variables separadas, el script mostrará qué host/puerto/DB está usando y te avisará si falta alguna. Si ninguna está definida, el mensaje de error te recordará qué variables debes completar.
