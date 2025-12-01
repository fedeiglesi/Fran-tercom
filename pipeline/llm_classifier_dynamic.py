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
          "enum": ["busca_producto", "social", "otros"]
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
          "type": "number"
        }
      },
      "required": ["intent", "confidence"]
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
Devolvé JSON válido.
Mensaje del usuario:
{message}
"""

    should_stream = stream or len(message) > 1500

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_schema", "json_schema": schema},
        temperature=0,
        stream=should_stream,
        max_output_tokens=500,
    )

    if should_stream:
        chunks = []
        for chunk in response:
            delta = chunk.choices[0].delta.content or ""
            chunks.append(delta)
        content_text = "".join(chunks)
    else:
        content_text = response.choices[0].message.content

    content = json.loads(content_text)
    # Validación JSON-first
    validate(instance=content, schema=schema)
    return content
