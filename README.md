## Fran 4.0 (Arquitectura asincrónica)
- Migración a **FastAPI** con servidor uvicorn para soportar `async/await` y despliegues en Railway.
- Persistencia transaccional en **PostgreSQL** (SQLAlchemy async) y memoria de sesión/rate limiting en **Redis**.
- Motor RAG externalizado a **Qdrant** con búsqueda híbrida y filtrado de metadata.
- Orquestador reescrito sobre **LangGraph** con bucle de razonamiento y herramientas desacopladas.
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
