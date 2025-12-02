"""Memoria de sesión basada en Redis para Fran 4.0."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from redis.asyncio import Redis

from fran_v4 import config


class SessionMemory:
    """Almacena historial, acciones pendientes y snapshots de búsqueda en Redis."""

    def __init__(self, redis_url: str | None = None) -> None:
        self.redis = Redis.from_url(redis_url or config.REDIS_URL, decode_responses=True)

    async def close(self) -> None:
        await self.redis.aclose()

    async def append_message(self, session_id: str, role: str, content: str) -> None:
        key = f"session:{session_id}:history"
        await self.redis.rpush(key, json.dumps({"role": role, "content": content}))
        await self.redis.expire(key, config.SESSION_TTL_SECONDS)

    async def get_history(self, session_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        key = f"session:{session_id}:history"
        items = await self.redis.lrange(key, -limit, -1)
        history: List[Dict[str, Any]] = []
        for raw in items:
            try:
                history.append(json.loads(raw))
            except Exception:
                continue
        return history

    async def set_pending_action(self, session_id: str, action: Dict[str, Any]) -> None:
        key = f"session:{session_id}:pending_action"
        await self.redis.set(key, json.dumps(action), ex=config.SESSION_TTL_SECONDS)

    async def get_pending_action(self, session_id: str) -> Optional[Dict[str, Any]]:
        key = f"session:{session_id}:pending_action"
        raw = await self.redis.get(key)
        if raw:
            try:
                return json.loads(raw)
            except Exception:
                return None
        return None

    async def persist_search_snapshot(self, session_id: str, products: List[Dict[str, Any]]) -> None:
        key = f"session:{session_id}:last_search"
        snapshot = products[: config.MAX_ITEMS]
        await self.redis.set(key, json.dumps(snapshot), ex=config.SESSION_TTL_SECONDS)

    async def get_last_search_snapshot(self, session_id: str) -> List[Dict[str, Any]]:
        key = f"session:{session_id}:last_search"
        raw = await self.redis.get(key)
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except Exception:
            return []

