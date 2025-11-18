import os

PROJECT_ID = os.getenv("GOOGLE_PROJECT_ID")
REGION = os.getenv("GOOGLE_REGION", "us-central1")

EMBED_MODEL = "text-embedding-004"
CHAT_MODEL = "gemini-2.0-pro"

INDEX_ENDPOINT_ID = os.getenv("CATALOGO_INDEX_ENDPOINT_ID")
DEPLOYED_INDEX_ID = os.getenv("CATALOGO_DEPLOYED_INDEX_ID")

DB_PATH = "fran.db"
