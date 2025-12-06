"""Script de setup para cargar el catálogo en Qdrant/PostgreSQL.

Este módulo se ejecuta fuera del ciclo de vida de FastAPI para que la
aplicación principal arranque rápido en Railway.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Iterable

import pandas as pd

from fran_v4 import config
from fran_v4.database import Database
from fran_v4.search_engine import HybridSearchEngine


logger = logging.getLogger(__name__)


async def _prepare_payloads(engine: HybridSearchEngine, rows: Iterable[dict]) -> list[dict]:
    payloads = []
    for row in rows:
        text = f"{row.get('name', '')} {row.get('brand', '')} {row.get('family', '')}"
        vector = await engine._embed(text)
        payloads.append({
            "id": row.get("code") or row.get("id"),
            "code": row.get("code"),
            "name": row.get("name"),
            "brand": row.get("brand"),
            "family": row.get("family"),
            "price_ars": row.get("price_ars"),
            "vector": vector,
        })
    return payloads


async def ingest_catalog(csv_path: str) -> None:
    db = Database()
    engine = HybridSearchEngine()
    await db.init_models()

    df = pd.read_csv(csv_path)
    logger.info(f"📦 Cargando {len(df)} productos desde CSV...")
    rows = df.to_dict(orient="records")

    payloads = await _prepare_payloads(engine, rows)
    logger.info("✅ Embeddings generados exitosamente")
    await engine.upsert_documents(payloads)
    logger.info(f"📊 Productos en DB: {await engine.count_products()}")
    for row in rows:
        await db.upsert_product(row)

    await db.dispose()


if __name__ == "__main__":
    path = Path(config.CATALOG_URL) if hasattr(config, "CATALOG_URL") else None
    if path and path.exists():
        asyncio.run(ingest_catalog(str(path)))
    else:
        raise SystemExit("Debes pasar un CSV local con el catálogo para ingestar.")

