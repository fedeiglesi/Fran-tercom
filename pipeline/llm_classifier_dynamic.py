import json
import os
from typing import Iterable, List

try:
    from jsonschema import validate
except ImportError:
    def validate(instance, schema):
        required = schema.get("required", [])
        for field in required:
            if field not in instance:
                raise ValueError(f"Missing required field: {field}")

from openai import OpenAI

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", "test-key"))


class _StubResponses:
    def create(self, *args, **kwargs):
        raise RuntimeError("responses.create stubbed – monkeypatch en tests")


# Asegura que exista client.responses para poder hacer monkeypatch en tests
if not getattr(client, "responses", None):
    client.responses = _StubResponses()

def _iter_column(df, col: str) -> Iterable[str]:
    """Itera sobre una columna de forma dinámica sin asumir tipo de df."""

    if df is None:
        return []

    if hasattr(df, "columns") and col in getattr(df, "columns", []):
        series = df[col]
        if hasattr(series, "dropna"):
            series = series.dropna()
        return series.astype(str).tolist()

    values: List[str] = []
    for row in df or []:
        if isinstance(row, dict) and col in row:
            value = row.get(col)
            if value is not None:
                values.append(str(value))
    return values


def build_enum_from_catalog(df, col):
    """
    Convierte valores del catálogo en un enum dinámico deduplicado.
    """
    values = sorted(set(_iter_column(df, col)))
    if len(values) > 60:
        return None  # Si hay demasiados, no se usa enum
    return values

def build_classifier_schema(df):
    enum_product = (
        build_enum_from_catalog(df, "categoria")
        or build_enum_from_catalog(df, "categoria_final")
        or build_enum_from_catalog(df, "familia_nombre")
    )
    enum_brand = build_enum_from_catalog(df, "marca_moto")
    enum_model = build_enum_from_catalog(df, "modelo_moto")

    schema = {
        "type": "object",
        "properties": {
            "intent": {
                "type": "string",
                "description": "Intención principal detectada en el mensaje",
                "enum": [
                    "busca_producto",  # compatibilidad retro
                    "product_search",
                    "follow_up",
                    "cart_action",
                    "social",
                    "otros",
                ],
            },
            "action_type": {
                "type": ["string", "null"],
                "description": "Acción de carrito cuando aplica",
                # Permitimos null explícitamente para evitar fallos de validación cuando no hay
                # intención de carrito y el modelo devuelve null (None en Python).
                "enum": ["add", "remove", "set", "clear", "info", None],
            },
            "quantity": {
                "type": ["integer", "null"],
                "description": "Cantidad solicitada si el usuario la menciona",
            },
            "product_reference": {
                "type": ["string", "null"],
                "description": "Referencia textual al producto (código, nombre, alias)",
            },
            "is_follow_up": {
                "type": ["boolean", "null"],
                "description": "Marca si el mensaje depende del contexto previo",
            },
            "multi_intent": {
                "type": ["array", "null"],
                "description": "Lista opcional de intents detectados en el mismo mensaje",
                "items": {
                    "type": "object",
                    "required": ["intent", "confidence"],
                    "properties": {
                        "intent": {"type": "string"},
                        "confidence": {"type": "number"},
                        "action_type": {"type": ["string", "null"]},
                        "quantity": {"type": ["integer", "null"]},
                        "product_reference": {"type": ["string", "null"]},
                        "span": {"type": ["string", "null"]},
                    },
                },
            },
            "product_type": {
                "type": ["string", "null"],
            },
            "brand": {
                "type": ["string", "null"],
            },
            "model": {
                "type": ["string", "null"],
            },
            "displacement_cc": {
                "type": ["integer", "null"],
            },
            "confidence": {
                "type": "number",
            },
        },
        "required": ["intent", "confidence"],
    }

    # Insertar enums dinámicos cuando son manejables
    if enum_product:
        enum_product = enum_product + [None] if None not in enum_product else enum_product
        schema["properties"]["product_type"]["enum"] = enum_product

    if enum_brand:
        enum_brand = enum_brand + [None] if None not in enum_brand else enum_brand
        schema["properties"]["brand"]["enum"] = enum_brand

    if enum_model:
        enum_model = enum_model + [None] if None not in enum_model else enum_model
        schema["properties"]["model"]["enum"] = enum_model

    return schema


def _get_output_text(obj):
    text = getattr(obj, "output_text", None)
    if text:
        return text

    output = getattr(obj, "output", None)
    if output:
        first_output = output[0] if isinstance(output, (list, tuple)) and output else None
        if first_output:
            content = getattr(first_output, "content", None) or (
                first_output.get("content") if isinstance(first_output, dict) else None
            )
            if content:
                first_content = content[0] if isinstance(content, (list, tuple)) and content else None
                if first_content:
                    text_block = getattr(first_content, "text", None) or (
                        first_content.get("text") if isinstance(first_content, dict) else None
                    )
                    if text_block:
                        return getattr(text_block, "value", None) or (
                            text_block.get("value") if isinstance(text_block, dict) else None
                        )

    return None

def fase1_llm_classifier_dynamic(message, df, schema, *, stream: bool = False):
    """
    Clasificación sin hardcodeos, respetando enums dinámicos.
    """
    prompt = f"""
Sos un clasificador del dominio MOTOPARTES.
No inventes modelos ni marcas.
Si no estás seguro → usa null.
Solo usa valores que aparezcan en el catálogo.
En repuestos de motos, algunas palabras tienen interpretación por defecto: cadena → cadena de transmisión, pastillas → pastillas de freno. No confundas "cadena" con limpiadores o desengrasantes ("limpia cadenas").
Ejemplo: "cadenas para Yamaha FZ16" debe clasificarse como búsqueda de cadenas de transmisión compatibles con la moto mencionada.
Detectá follow-ups y acciones de carrito aunque sean implícitas ("me das dos?", "sumame esas").
Para follow-ups, marcá is_follow_up=true y mantené product_reference con la mención textual.
Si ves cantidades, completa quantity con número entero.
Para acciones de carrito usa action_type=add|remove|set|clear|info.
Si hay múltiples intenciones simultáneas (saludo + consulta + precio + carrito), listalas en multi_intent
con su span exacto y confidence independiente; no descartes intents a menos que su confidence < 0.4.
Devolvé JSON válido usando el schema provisto por el sistema.
Mensaje del usuario:
{message}
"""

    should_stream = stream or len(message) > 1500

    request_payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "system", "content": prompt}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "classifier_schema", "schema": schema}},
        "temperature": 0,
        "stream": should_stream,
        "max_tokens": 500,
    }

    def _run_llm():
        responder = getattr(client, "responses", None)
        if responder and hasattr(responder, "create"):
            try:
                return responder.create(**request_payload)
            except Exception:
                if not os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") == "test-key":
                    return responder.create(**request_payload)

        if not os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") == "test-key":
            raise RuntimeError("OPENAI_API_KEY no configurada y no hay stub de responses disponible")

        return client.chat.completions.create(**request_payload)

    response = _run_llm()

    content_text = _get_output_text(response)

    if content_text is None and should_stream:
        chunks = []
        for chunk in response:
            delta = None
            if chunk and getattr(chunk, "choices", None):
                first_choice = chunk.choices[0]
                delta = getattr(first_choice.delta, "content", None)
            if delta:
                chunks.append(delta)

        content_text = "".join(chunks) if chunks else None
    elif content_text is None and response and getattr(response, "choices", None):
        first_choice = response.choices[0]
        content_text = getattr(first_choice.message, "content", None)

    if content_text is None:
        raise ValueError("No output_text received from LLM response")

    content = json.loads(content_text)
    # Validación JSON-first
    validate(instance=content, schema=schema)

    if content.get("multi_intent") is None:
        content["multi_intent"] = []

    return content
