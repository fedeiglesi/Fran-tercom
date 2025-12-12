"""Motor de búsqueda de texto usando PostgreSQL."""
from __future__ import annotations
import logging
import os
from typing import Any, Dict, List, Optional, Tuple
import asyncpg
from fran_v4 import config

logger = logging.getLogger(__name__)

class SearchEngine:
    """Encapsula la búsqueda full-text con PostgreSQL."""

    def __init__(self, database_url: Optional[str] = None) -> None:
        self.database_url = self._normalize_db_url(database_url or config.DATABASE_URL)
        self._pool: Optional[asyncpg.Pool] = None

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

    @staticmethod
    def _build_filters(filters: Optional[Dict[str, Any]]) -> Tuple[str, List[Any]]:
        if not filters:
            return "", []

        clauses = []
        values: List[Any] = []
        param_idx = 1
        for key, value in filters.items():
            clauses.append(f"LOWER({key}) = LOWER(${param_idx})")
            values.append(value)
            param_idx += 1

        where = " AND ".join(clauses)
        return f" AND {where}" if where else "", values

    async def search(
        self, query_text: str, limit: int = 10, filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        filter_clause, filter_values = self._build_filters(filters)
        pool = await self._get_pool()
        # Use simple_query to avoid issues with parameter indexes
        ts_query = " | ".join(query_text.replace("'", " ").split())

        # Parameters need to be passed correctly
        # The ts_query is $1, limit is $2, and filter_values start from $3

        sql_query = f"""
            SELECT
                codigo,
                descripcion,
                precio_pesos,
                familia_nombre,
                proveedor_nombre,
                ts_rank_cd(
                    to_tsvector('spanish', coalesce(descripcion_normalizada, '')),
                    to_tsquery('spanish', $1)
                ) AS rank
            FROM products
            WHERE
                to_tsvector('spanish', coalesce(descripcion_normalizada, '')) @@ to_tsquery('spanish', $1)
                {filter_clause.replace('$', f'${len(filter_values) + 2}')}
            ORDER BY rank DESC
            LIMIT $2
        """

        params = [ts_query, limit] + filter_values

        async with pool.acquire() as conn:
            rows = await conn.fetch(sql_query, *params)

        return [
            {
                "codigo": row["codigo"],
                "descripcion": row["descripcion"],
                "precio_pesos": float(row["precio_pesos"]) if row["precio_pesos"] is not None else None,
                "familia": row["familia_nombre"],
                "proveedor": row["proveedor_nombre"],
                "score": float(row["rank"] * 100) if row["rank"] is not None else 0.0,
            }
            for row in rows
        ]
