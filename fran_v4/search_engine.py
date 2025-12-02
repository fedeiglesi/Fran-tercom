"""Hybrid search abstraction backed by Qdrant."""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List, Optional

from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels


class HybridSearchEngine:
    """Encapsulates hybrid vector/search logic using Qdrant."""

    def __init__(self, url: Optional[str] = None, collection: Optional[str] = None) -> None:
        self.url = url or os.getenv("QDRANT_URL", "http://localhost:6333")
        self.collection = collection or os.getenv("QDRANT_COLLECTION", "fran_catalog")
        api_key = os.getenv("QDRANT_API_KEY")
        self.client = QdrantClient(url=self.url, api_key=api_key)

    async def upsert_documents(self, payloads: List[Dict[str, Any]]) -> None:
        if not payloads:
            return
        points = [
            qmodels.PointStruct(
                id=str(item.get("id")),
                payload=item,
                vector=item.get("vector"),
            )
            for item in payloads
            if "vector" in item
        ]
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: self.client.upsert(collection_name=self.collection, points=points),
        )

    async def hybrid_search(
        self, query_vector: List[float], query_text: str, limit: int = 10, filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        query_filter = None
        if filters:
            must_clauses = [qmodels.FieldCondition(key=key, match=qmodels.MatchValue(value=value)) for key, value in filters.items()]
            query_filter = qmodels.Filter(must=must_clauses)

        search_request = qmodels.SearchRequest(
            vector=query_vector,
            limit=limit,
            filter=query_filter,
            with_payload=True,
            score_threshold=0.2,
        )

        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self.client.search_batch(
                collection_name=self.collection,
                requests=[search_request],
                search_params=qmodels.SearchParams(hnsw_ef=128, exact=False),
                query_vector=qmodels.NamedVectorParams(name="text", vector=query_vector),
                with_payload=True,
            ),
        )
        # search_batch returns a list of lists
        matches = result[0] if result else []
        return [match.payload for match in matches]
