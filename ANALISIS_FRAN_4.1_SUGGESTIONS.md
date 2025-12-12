## Análisis de Coherencia y Mejoras para Fran-4.1

### 1. Visión General de Fran-4.0

Fran-4.0 representa una modernización significativa de la arquitectura, migrando de un monolito en Flask a una solución asíncrona basada en FastAPI, LangGraph y PostgreSQL con pgvector. Esta arquitectura es robusta, escalable y resiliente.

**Puntos Fuertes:**

*   **Arquitectura Asíncrona:** El uso de FastAPI y `async` en todo el stack permite un alto rendimiento y concurrencia.
*   **Manejo de Estado Desacoplado:** LangGraph orquesta el flujo del agente de forma clara y modular, separando la lógica de las herramientas.
*   **Persistencia Robusta:** PostgreSQL con pgvector ofrece una base de datos transaccional y un motor de búsqueda vectorial en una única solución. La lógica de reintentos y el fallback en memoria aumentan la resiliencia.
*   **Configuración Centralizada:** El módulo `fran_v4/config.py` centraliza las variables de entorno, facilitando la gestión.
*   **Middleware de Readiness:** El middleware que devuelve `503` hasta que la aplicación está lista es una excelente práctica para entornos de producción, evitando procesar peticiones antes de que el catálogo esté cargado.

### 2. Puntos de Incoherencia y Áreas de Mejora

#### a. Inconsistencia en la Invocación del Agente

*   **Problema:** En `fran_v4/api.py`, la herramienta `choose_tool` se invoca dos veces sin necesidad. Primero, se llama en `run_agent` para establecer el estado inicial. Segundo, se llama de nuevo dentro del nodo `understand` del grafo. Esto es redundante y puede llevar a comportamientos inesperados si la lógica de `choose_tool` cambiara.
*   **Solución para 4.1:** Eliminar la llamada a `choose_tool` en `run_agent` y confiar únicamente en la que se ejecuta dentro del nodo `understand`. El estado inicial del `tool` puede ser una cadena vacía o `None`.

#### b. Manejo de "parsed_item"

*   **Problema:** El estado `parsed_item` se inicializa como un diccionario vacío en `run_agent` pero nunca se puebla. La herramienta `update_cart` espera recibir datos en `parsed_item`, pero no hay ningún paso en el grafo que extraiga entidades del mensaje del usuario (como producto y cantidad) para rellenarlo. Actualmente, `update_cart` solo puede funcionar si se le pasaran los datos directamente, lo cual no ocurre.
*   **Solución para 4.1:**
    1.  **Crear un nodo de "parseo":** Añadir un nuevo nodo en el grafo (`parse_message`) que se ejecute después de `understand`.
    2.  Este nodo usará el LLM para extraer entidades estructuradas del mensaje del usuario (ej. `{"action": "add", "item": "tornillo", "quantity": 10}`).
    3.  El resultado de este parseo se almacenará en `parsed_item`.
    4.  El nodo `act` usará `parsed_item` para invocar `update_cart` con los datos correctos.

#### c. Abuso del Re-query

*   **Problema:** El mecanismo de `requery` es una buena idea para mejorar la búsqueda, pero su condición de activación es demasiado simple. Se activa si el `best_score` es menor a `RELEVANCE_MIN_SCORE`. Esto no distingue entre una búsqueda genuinamente mala y una consulta que no es una búsqueda (ej. "hola, ¿cómo estás?"). Reformular "hola" no aporta valor y consume tokens innecesariamente.
*   **Solución para 4.1:**
    1.  **Mejorar la condición de `evaluate`:** El nodo `evaluate` no solo debe comprobar el `best_score`, sino también la intención detectada en el nodo `understand`.
    2.  Solo se debería pasar a `requery` si la intención es claramente una búsqueda de producto (`search_products`) y el score es bajo. Si la intención es otra (saludo, pregunta sobre el carrito, etc.), debería pasar directamente a `respond`.

#### d. Falta de Feedback al Usuario en Acciones de Carrito

*   **Problema:** Las herramientas `update_cart` y `get_pricing` devuelven el estado actual del carrito como contexto. Sin embargo, el nodo `respond` está diseñado principalmente para responder a búsquedas de productos. No hay una lógica clara para formatear y presentar el contenido del carrito o confirmar una acción (ej. "He añadido 10 tornillos a tu carrito").
*   **Solución para 4.1:**
    1.  **Prompts de respuesta especializados:** En el nodo `respond`, adaptar el "system prompt" según la herramienta (`tool`) que se ejecutó.
    2.  Si `tool` fue `update_cart`, el prompt debería ser algo como: "Confirma al usuario la acción realizada sobre el carrito y muéstrale el estado actual de forma clara y concisa".
    3.  Si `tool` fue `get_pricing`, el prompt sería: "Presenta el resumen del carrito con el total de precios".

#### e. Código Legado y Confusión de Entrypoints

*   **Problema:** La raíz del proyecto contiene múltiples archivos (`app.py`, `Fran_4.0.py`, `main.py`) que pueden confundir sobre cuál es el punto de entrada correcto. Aunque `README.md` lo aclara, una estructura de proyecto más limpia eliminaría esta ambigüedad.
*   **Solución para 4.1:**
    1.  **Mover el código legado:** Crear un directorio `_legacy` y mover allí `app.py` y otros archivos de Fran v3.x.
    2.  **Renombrar `main.py`:** Renombrar `main.py` a `run_server.py` o un nombre más explícito para evitar colisiones con el concepto de "módulo principal".
    3.  Añadir un `README.md` dentro de `_legacy` explicando su contenido.

### 3. Propuesta de Plan para Fran-4.1

1.  **Refactorizar Grafo del Agente:**
    *   Eliminar la llamada redundante a `choose_tool` en `run_agent`.
    *   Añadir un nodo `parse_message` para extraer entidades y poblar `parsed_item`.
    *   Modificar `update_cart` para usar `parsed_item`.

2.  **Optimizar Lógica de Re-query:**
    *   Actualizar el nodo `evaluate` para que solo active `requery` cuando la intención sea `search_products` y el score es bajo.

3.  **Mejorar Respuestas de Carrito:**
    *   En el nodo `respond`, implementar lógica condicional para generar prompts de respuesta especializados según la herramienta ejecutada (`update_cart`, `get_pricing`).

4.  **Limpieza del Repositorio:**
    *   Crear un directorio `_legacy` y mover el código antiguo.
    *   Renombrar `main.py` a `run_server.py` y actualizar las referencias (si es necesario).

5.  **Documentación:**
    *   Actualizar `README.md` para reflejar los cambios en la arquitectura del agente y la nueva estructura del proyecto.
    *   Añadir documentación en el código (docstrings) explicando el propósito de cada nodo del grafo y cada herramienta.

Este plan de acción aborda las incoherencias detectadas, mejora la inteligencia y eficiencia del agente, y organiza el repositorio para facilitar el mantenimiento futuro.
