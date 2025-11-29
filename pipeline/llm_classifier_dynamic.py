import json
from openai import OpenAI
client = OpenAI()

def build_enum_from_catalog(df, col):
    """
    Convierte valores del catálogo en un enum dinámico deduplicado.
    """
    values = sorted(set(v for v in df[col].dropna().astype(str)))
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

def fase1_llm_classifier_dynamic(message, df, schema):
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

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_schema", "json_schema": schema},
        temperature=0
    )

    return json.loads(response.choices[0].message.content)
