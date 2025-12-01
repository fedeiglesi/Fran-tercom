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
- El modelo de embeddings se estandarizó con la variable `OPENAI_EMBEDDING_MODEL` (por defecto `text-embedding-3-small`) para el pipeline híbrido y la generación de caché.
- El orquestador `orquestar_v317` ahora está integrado en `app.py` y participa del enrutamiento principal (40% del tráfico por hash y 100% si se setea `USE_FRAN_317=true`).
