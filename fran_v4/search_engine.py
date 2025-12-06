"""Motor de búsqueda híbrida (vectorial + texto) usando PostgreSQL/pgvector."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import asyncpg
from openai import AsyncOpenAI

from fran_v4 import config


logger = logging.getLogger(__name__)


DEFAULT_EMBEDDING_DIMENSIONS = {
    "text-embedding-3-large": 3072,
    "text-embedding-3-small": 1536,
    "text-embedding-ada-002": 1536,
    "paraphrase-multilingual-MiniLM": 384,
}


def _resolve_embedding_model() -> str:
    return os.getenv("OPENAI_EMBEDDING_MODEL", config.OPENAI_EMBEDDING_MODEL)


def _resolve_embedding_dim(model: str) -> int:
    env_dim = os.getenv("EMBEDDING_DIM")
    if env_dim:
        return int(env_dim)

    dim = DEFAULT_EMBEDDING_DIMENSIONS.get(model)
    if dim:
        return dim

    logger.warning(
        "Dimensión de embedding desconocida para el modelo %s; usando 1024 por defecto", model
    )
    return 1024


EMBEDDING_MODEL = _resolve_embedding_model()
EMBEDDING_DIM: int = _resolve_embedding_dim(EMBEDDING_MODEL)
RRF_K: float = float(os.getenv("RRF_K", "60"))


class HybridSearchEngine:
    """Encapsula la búsqueda híbrida con embeddings OpenAI y PostgreSQL."""

    def __init__(self, database_url: Optional[str] = None) -> None:
        self.database_url = self._normalize_db_url(database_url or config.DATABASE_URL)
        self.embedding_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
        self.embedding_model = EMBEDDING_MODEL
        self._pool: Optional[asyncpg.Pool] = None
        logger.info(
            "Usando modelo de embeddings %s con dimensión %s", self.embedding_model, EMBEDDING_DIM
        )

    # ------------------------------------------------------------------
    # Infra
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_db_url(url: str) -> str:
        if url.startswith("postgresql+asyncpg://"):
            return url.replace("postgresql+asyncpg://", "postgresql://", 1)
        return url

    async def _get_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            self._pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=5)
        return self._pool

    async def dispose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ------------------------------------------------------------------
    # Embeddings
    # ------------------------------------------------------------------
    async def _embed(self, text: str) -> List[float]:
        response = await self.embedding_client.embeddings.create(model=self.embedding_model, input=text)
        return response.data[0].embedding  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Esquema y carga
    # ------------------------------------------------------------------
    async def ensure_schema(self) -> None:
        """Crea la tabla e índices necesarios para búsqueda vectorial y full-text."""

        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.execute("CREATE EXTENSION IF NOT EXISTS unaccent")

            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS products (
                    codigo TEXT PRIMARY KEY,
                    nombre TEXT NOT NULL,
                    descripcion TEXT,
                    marca TEXT,
                    categoria TEXT,
                    precio NUMERIC(12,2),
                    stock INTEGER,
                    metadata JSONB,
                    embedding VECTOR({EMBEDDING_DIM}),
                    search_tsv tsvector GENERATED ALWAYS AS (
                        setweight(to_tsvector('spanish', coalesce(unaccent(nombre), '')), 'A') ||
                        setweight(to_tsvector('spanish', coalesce(unaccent(descripcion), '')), 'B') ||
                        setweight(to_tsvector('spanish', coalesce(unaccent(marca), '')), 'C') ||
                        setweight(to_tsvector('spanish', coalesce(unaccent(categoria), '')), 'C')
                    ) STORED
                )
                """
            )

            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS products_embedding_idx
                ON products USING ivfflat (embedding vector_cosine_ops)
                WITH (lists = 100)
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS products_search_idx
                ON products USING GIN (search_tsv)
                """
            )

    async def count_products(self) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            return int(await conn.fetchval("SELECT COUNT(*) FROM products"))

    async def upsert_documents(self, payloads: List[Dict[str, Any]]) -> None:
        if not payloads:
            return

        pool = await self._get_pool()
        records = []
        for item in payloads:
            embedding = item.get("vector")
            if embedding is None:
                continue

            codigo = item.get("codigo") or item.get("code") or item.get("id")
            if codigo is None:
                continue

            records.append(
                (
                    str(codigo),
                    item.get("nombre") or item.get("name") or "",
                    item.get("descripcion") or item.get("description"),
                    item.get("marca") or item.get("brand"),
                    item.get("categoria") or item.get("family"),
                    item.get("precio") or item.get("price") or item.get("price_ars"),
                    item.get("stock"),
                    item.get("metadata"),
                    embedding,
                )
            )

        if not records:
            return

        async with pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO products (codigo, nombre, descripcion, marca, categoria, precio, stock, metadata, embedding)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (codigo) DO UPDATE SET
                    nombre = EXCLUDED.nombre,
                    descripcion = EXCLUDED.descripcion,
                    marca = EXCLUDED.marca,
                    categoria = EXCLUDED.categoria,
                    precio = EXCLUDED.precio,
                    stock = EXCLUDED.stock,
                    metadata = EXCLUDED.metadata,
                    embedding = EXCLUDED.embedding
                """,
                records,
            )

    async def prepare_catalog_payloads(self, rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        payloads: List[Dict[str, Any]] = []
        for row in rows:
            nombre = row.get("nombre") or row.get("descripcion") or ""
            descripcion = row.get("descripcion") or row.get("descripcion_normalizada")
            categoria = row.get("categoria") or row.get("familia") or row.get("familia_nombre")
            marca = row.get("marca") or row.get("marca_moto") or row.get("proveedor_nombre")
            precio = (
                row.get("precio")
                or row.get("precio_pesos")
                or row.get("precio_ars")
                or row.get("precio_dolares")
            )

            synonyms = row.get("sinonimos") or ""
            base_text = " ".join(
                filter(
                    None,
                    [
                        nombre,
                        descripcion,
                        marca,
                        categoria,
                        synonyms,
                    ],
                )
            )

            vector = await self._embed(base_text)
            payloads.append(
                {
                    "codigo": row.get("codigo") or row.get("code") or row.get("id"),
                    "nombre": nombre,
                    "descripcion": descripcion or nombre,
                    "marca": marca,
                    "categoria": categoria,
                    "precio": precio,
                    "stock": row.get("stock"),
                    "vector": vector,
                    "metadata": {"sinonimos": synonyms} if synonyms else None,
                }
            )

        return payloads

    # ------------------------------------------------------------------
    # Búsqueda
    # ------------------------------------------------------------------
    @staticmethod
    def _build_filters(filters: Optional[Dict[str, Any]]) -> Tuple[str, List[Any]]:
        if not filters:
            return "", []

        clauses = []
        values: List[Any] = []
        for key, value in filters.items():
            clauses.append(f"{key} = ${len(values) + 1}")
            values.append(value)

        where = " AND ".join(clauses)
        return f" AND {where}" if where else "", values

    async def _dense_search(
        self, query_vector: Sequence[float], limit: int, filters: Optional[Dict[str, Any]]
    ) -> List[Tuple[str, float, Dict[str, Any]]]:
        filter_clause, values = self._build_filters(filters)
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT codigo, nombre, descripcion, marca, categoria, precio, stock, metadata,
                       (1.0 / ($3 + row_number() OVER (ORDER BY embedding <-> $1))) AS rrf,
                       (embedding <-> $1) AS distance
                FROM products
                WHERE embedding IS NOT NULL {filter_clause}
                ORDER BY embedding <-> $1
                LIMIT $2
                """,
                query_vector,
                limit,
                RRF_K,
                *values,
            )

        return [
            (
                row["codigo"],
                float(row["rrf"]),
                {
                    "codigo": row["codigo"],
                    "nombre": row["nombre"],
                    "descripcion": row["descripcion"],
                    "marca": row["marca"],
                    "categoria": row["categoria"],
                    "precio": float(row["precio"]) if row["precio"] is not None else None,
                    "stock": row["stock"],
                    "metadata": row["metadata"],
                },
            )
            for row in rows
        ]

    async def _text_search(
        self, query_text: str, limit: int, filters: Optional[Dict[str, Any]]
    ) -> List[Tuple[str, float, Dict[str, Any]]]:
        filter_clause, values = self._build_filters(filters)
        pool = await self._get_pool()
        ts_query = query_text.replace("'", " ")
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT codigo, nombre, descripcion, marca, categoria, precio, stock, metadata,
                       ts_rank_cd(search_tsv, plainto_tsquery('spanish', $1)) AS rank,
                       (1.0 / ($3 + row_number() OVER (
                            ORDER BY ts_rank_cd(search_tsv, plainto_tsquery('spanish', $1)) DESC
                       ))) AS rrf
                FROM products
                WHERE search_tsv @@ plainto_tsquery('spanish', $1) {filter_clause}
                ORDER BY rank DESC
                LIMIT $2
                """,
                ts_query,
                limit,
                RRF_K,
                *values,
            )

        return [
            (
                row["codigo"],
                float(row["rrf"]),
                {
                    "codigo": row["codigo"],
                    "nombre": row["nombre"],
                    "descripcion": row["descripcion"],
                    "marca": row["marca"],
                    "categoria": row["categoria"],
                    "precio": float(row["precio"]) if row["precio"] is not None else None,
                    "stock": row["stock"],
                    "metadata": row["metadata"],
                },
            )
            for row in rows
        ]

    @staticmethod
    def _merge_results(
        dense: List[Tuple[str, float, Dict[str, Any]]], text: List[Tuple[str, float, Dict[str, Any]]]
    ) -> List[Tuple[str, float, Dict[str, Any]]]:
        scores: Dict[str, Tuple[float, Dict[str, Any]]] = {}

        for doc_id, rrf, payload in dense:
            scores[doc_id] = (scores.get(doc_id, (0.0, payload))[0] + rrf, payload)

        for doc_id, rrf, payload in text:
            scores[doc_id] = (scores.get(doc_id, (0.0, payload))[0] + rrf, payload)

        merged = sorted(scores.items(), key=lambda item: item[1][0], reverse=True)
        return [(doc_id, score_payload[0], score_payload[1]) for doc_id, score_payload in merged]

    async def hybrid_search(
        self, query_text: str, limit: int = 10, filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        vector = await self._embed(query_text)
        dense_results, text_results = await asyncio.gather(
            self._dense_search(vector, limit * 2, filters),
            self._text_search(query_text, limit * 2, filters),
        )

        merged = self._merge_results(dense_results, text_results)
        formatted: List[Dict[str, Any]] = []
        for _, score, payload in merged[:limit]:
            enriched = dict(payload)
            enriched["score"] = float(score * 100)
            formatted.append(enriched)
        return formatted

