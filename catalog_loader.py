# ============================================================
# Script: catalog_loader.py
# Sube todo tu catálogo a Google Vertex Vector Search
# ============================================================

import csv
import json
import os
from google.cloud import aiplatform
from vertexai.language_models import TextEmbeddingModel

PROJECT_ID = os.getenv("GOOGLE_PROJECT_ID")
REGION = os.getenv("GOOGLE_REGION", "us-central1")
INDEX_ID = os.getenv("CATALOGO_INDEX_ID")
CSV_PATH = "catalogo.csv"

aiplatform.init(project=PROJECT_ID, location=REGION)

index = aiplatform.MatchingEngineIndex(index_name=INDEX_ID)

embed_model = TextEmbeddingModel.from_pretrained("text-embedding-004")

def embed(text):
    r = embed_model.get_embeddings([text])
    return r[0].values

items = []

with open(CSV_PATH, newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for row in reader:
        texto = f"{row['code']} {row['description']} {row['family']} {row['brand_model']}"
        vector = embed(texto)

        metadata = {
            "code": row["code"],
            "description": row["description"],
            "family": row["family"],
            "brand_model": row["brand_model"],
            "price_usd": row["price_usd"]
        }

        items.append({"id": row["code"], "embedding": vector, "metadata": metadata})

# Subida masiva
index.upsert_datapoints(datapoints=items)

print("Catálogo cargado exitosamente.")
