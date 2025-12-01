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
    enum_product = build_enum_from_catalog(df, "categoria")
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
                "enum": ["add", "remove", "set", "clear", "info"],
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
                "type": "array",
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
        schema["properties"]["product_type"]["enum"] = enum_product

    if enum_brand:
        schema["properties"]["brand"]["enum"] = enum_brand

    if enum_model:
        schema["properties"]["model"]["enum"] = enum_model

    return schema

def fase1_llm_classifier_dynamic(message, df, schema, *, stream: bool = False):
    """
    Clasificación sin hardcodeos, respetando enums dinámicos.
    """
    prompt = f"""
Sos un clasificador del dominio MOTOPARTES.
No inventes modelos ni marcas.
Si no estás seguro → usa null.
Solo usa valores que aparezcan en el catálogo.
Detectá follow-ups y acciones de carrito aunque sean implícitas ("me das dos?", "sumame esas").
Para follow-ups, marcá is_follow_up=true y mantené product_reference con la mención textual.
Si ves cantidades, completa quantity con número entero.
Para acciones de carrito usa action_type=add|remove|set|clear|info.
Devolvé JSON válido usando el schema provisto por el sistema.
Mensaje del usuario:
{message}
"""

    should_stream = stream or len(message) > 1500

    response = client.responses.create(
        model="gpt-4o-mini",
        input=prompt,
        response_format={"type": "json_schema", "json_schema": schema},
        temperature=0,
        stream=should_stream,
        max_output_tokens=500,
    )

    if should_stream:
        chunks = []
        final_text = None
        for event in response:
            delta = getattr(event, "output_text_delta", None) or ""
            if delta:
                chunks.append(delta)

            event_output = getattr(event, "output_text", None)
            if event_output:
                final_text = event_output

        content_text = "".join(chunks) if chunks else final_text
        if content_text is None:
            content_text = getattr(response, "output_text", None)
    else:
        content_text = response.output_text

    content = json.loads(content_text)
    # Validación JSON-first
    validate(instance=content, schema=schema)
    return content
