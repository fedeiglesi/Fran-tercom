"""Motor de búsqueda híbrida (denso + texto) usando Qdrant."""
from __future__ import annotations

import asyncio
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

import logging
import re
from openai import AsyncOpenAI
from qdrant_client.async_qdrant_client import AsyncQdrantClient
from qdrant_client.http import models as qmodels

from fran_v4 import config


logger = logging.getLogger(__name__)


class HybridSearchEngine:
    """Encapsula la búsqueda híbrida con embeddings OpenAI y Qdrant."""

    def __init__(self, url: Optional[str] = None, collection: Optional[str] = None) -> None:
        self.collection = collection or config.QDRANT_COLLECTION
        self.client = AsyncQdrantClient(url=url or config.QDRANT_URL, api_key=config.QDRANT_API_KEY)
        self.embedding_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
        self.embedding_model = config.OPENAI_EMBEDDING_MODEL

    async def _embed(self, text: str) -> List[float]:
        response = await self.embedding_client.embeddings.create(model=self.embedding_model, input=text)
        return response.data[0].embedding  # type: ignore[return-value]

    async def upsert_documents(self, payloads: List[Dict[str, Any]]) -> None:
        if not payloads:
            return
        points = []
        for item in payloads:
            if "vector" not in item:
                continue
            vector_data = item.get("vector")
            vectors = {"dense": vector_data} if isinstance(vector_data, list) else vector_data
            points.append(
                qmodels.PointStruct(
                    id=str(item.get("id")),
                    payload={k: v for k, v in item.items() if k != "vector"},
                    vector=vectors,
                )
            )
        await self.client.upsert(collection_name=self.collection, points=points)

    async def _dense_search(
        self, vector: List[float], limit: int, query_filter: Optional[qmodels.Filter]
    ) -> List[qmodels.ScoredPoint]:
        query_vector = self._build_dense_vector(vector)
        return await self.client.search(
            collection_name=self.collection,
            query_vector=query_vector,
            limit=limit,
            with_payload=True,
            score_threshold=config.RELEVANCE_MIN_SCORE / 100,
            query_filter=query_filter,
        )

    async def _sparse_search(
        self, query_text: str, limit: int, query_filter: Optional[qmodels.Filter]
    ) -> List[qmodels.ScoredPoint]:
        try:
            sparse_vector = self._create_sparse_vector(query_text)

            if not sparse_vector["indices"] or not sparse_vector["values"]:
                logger.warning("Empty sparse vector for query: %s", query_text)
                return []

            logger.debug(
                "Sparse vector size: %d tokens", len(sparse_vector["indices"])
            )

            results = await self.client.query_points(
                collection_name=self.collection,
                query=qmodels.SparseVector(
                    indices=sparse_vector["indices"], values=sparse_vector["values"]
                ),
                limit=limit,
                with_payload=True,
                with_vectors=False,
                query_filter=query_filter,
            )

            return results.points

        except Exception as exc:  # pragma: no cover - defensive logging
            logger.error("Error en sparse search: %s", exc, exc_info=True)
            return []

    def _create_sparse_vector(self, query_text: str) -> Dict[str, List[float]]:
        """Crear vector disperso simple a partir de tokens del texto."""

        try:
            from qdrant_client.fastembed_sparse import FastEmbedSparse

            encoder = FastEmbedSparse()
            vector = encoder.encode(query_text)
            return {"indices": vector.indices, "values": vector.values}
        except Exception:
            pass

        tokens = re.findall(r"\b\w+\b", query_text.lower())
        tokens = [t for t in tokens if len(t) > 2]
        token_counts = Counter(tokens)
        indices = [abs(hash(token)) % 65535 for token in token_counts]
        values = [float(count) for count in token_counts.values()]
        return {"indices": indices, "values": values}

    @staticmethod
    def _merge_results(
        dense: List[qmodels.ScoredPoint], sparse: List[qmodels.ScoredPoint]
    ) -> List[Tuple[str, float, Dict[str, Any]]]:
        scores: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        for rank, item in enumerate(dense, start=1):
            payload = item.payload or {}
            scores[str(item.id)] = (1 / rank + float(item.score or 0), payload)
        for rank, item in enumerate(sparse, start=1):
            payload = item.payload or {}
            current_score, _ = scores.get(str(item.id), (0.0, payload))
            scores[str(item.id)] = (current_score + 1 / rank + float(item.score or 0), payload)
        sorted_items = sorted(scores.items(), key=lambda x: x[1][0], reverse=True)
        return [(item_id, score_payload[0], score_payload[1]) for item_id, score_payload in sorted_items]

    @staticmethod
    def _build_dense_vector(vector: List[float]) -> Any:
        """Compatibilidad entre versiones de qdrant-client."""

        named_vector_cls = getattr(qmodels, "NamedVector", None)
        if named_vector_cls:
            return named_vector_cls(name="dense", vector=vector)

        named_vector_params_cls = getattr(qmodels, "NamedVectorParams", None)
        if named_vector_params_cls:
            return named_vector_params_cls(name="dense", vector=vector)

        return vector

    async def hybrid_search(
        self, query_text: str, limit: int = 10, filters: Optional[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        query_filter = None
        if filters:
            must = [qmodels.FieldCondition(key=k, match=qmodels.MatchValue(value=v)) for k, v in filters.items()]
            query_filter = qmodels.Filter(must=must)

        vector = await self._embed(query_text)
        dense_results, sparse_results = await asyncio.gather(
            self._dense_search(vector, limit, query_filter),
            self._sparse_search(query_text, limit, query_filter),
        )

        merged = self._merge_results(dense_results, sparse_results)
        formatted: List[Dict[str, Any]] = []
        for _, score, payload in merged[:limit]:
            if score * 100 < config.RELEVANCE_MIN_SCORE:
                continue
            enriched = dict(payload)
            enriched["score"] = float(score * 100)
            formatted.append(enriched)
        return formatted
