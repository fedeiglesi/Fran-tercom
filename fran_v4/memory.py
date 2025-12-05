"""Memoria de sesión basada en Redis para Fran 4.0."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from redis import exceptions as redis_exceptions
from redis.asyncio import Redis

from fran_v4 import config


class SessionMemory:
    """Almacena historial, acciones pendientes y snapshots de búsqueda en Redis."""

    def __init__(self, redis_url: str | None = None) -> None:
        self.redis = Redis.from_url(redis_url or config.REDIS_URL, decode_responses=True)
        self._fallback_store: Dict[str, Dict[str, Any]] = {}

    async def close(self) -> None:
        await self.redis.aclose()

    # ------------------------------------------------------------------
    # Almacenamiento en memoria (fallback cuando Redis no está disponible)
    # ------------------------------------------------------------------
    def _get_session_bucket(self, session_id: str) -> Dict[str, Any]:
        bucket = self._fallback_store.get(session_id)
        now = time.time()
        if bucket and bucket.get("expires_at", 0) < now:
            bucket = None
        if not bucket:
            bucket = {"history": [], "pending_action": None, "last_search": [], "expires_at": now + config.SESSION_TTL_SECONDS}
            self._fallback_store[session_id] = bucket
        else:
            bucket["expires_at"] = now + config.SESSION_TTL_SECONDS
        return bucket

    # ------------------------------------------------------------------
    # Métodos públicos
    # ------------------------------------------------------------------

    async def append_message(self, session_id: str, role: str, content: str) -> None:
        key = f"session:{session_id}:history"
        payload = json.dumps({"role": role, "content": content})
        try:
            await self.redis.rpush(key, payload)
            await self.redis.expire(key, config.SESSION_TTL_SECONDS)
        except redis_exceptions.RedisError:
            bucket = self._get_session_bucket(session_id)
            bucket["history"].append({"role": role, "content": content})

    async def get_history(self, session_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        key = f"session:{session_id}:history"
        try:
            items = await self.redis.lrange(key, -limit, -1)
            history: List[Dict[str, Any]] = []
            for raw in items:
                try:
                    history.append(json.loads(raw))
                except Exception:
                    continue
            return history
        except redis_exceptions.RedisError:
            bucket = self._get_session_bucket(session_id)
            return bucket.get("history", [])[-limit:]

    async def set_pending_action(self, session_id: str, action: Dict[str, Any]) -> None:
        key = f"session:{session_id}:pending_action"
        try:
            await self.redis.set(key, json.dumps(action), ex=config.SESSION_TTL_SECONDS)
        except redis_exceptions.RedisError:
            bucket = self._get_session_bucket(session_id)
            bucket["pending_action"] = action

    async def get_pending_action(self, session_id: str) -> Optional[Dict[str, Any]]:
        key = f"session:{session_id}:pending_action"
        try:
            raw = await self.redis.get(key)
            if raw:
                try:
                    return json.loads(raw)
                except Exception:
                    return None
            return None
        except redis_exceptions.RedisError:
            bucket = self._get_session_bucket(session_id)
            return bucket.get("pending_action")

    async def persist_search_snapshot(self, session_id: str, products: List[Dict[str, Any]]) -> None:
        key = f"session:{session_id}:last_search"
        snapshot = products[: config.MAX_ITEMS]
        try:
            await self.redis.set(key, json.dumps(snapshot), ex=config.SESSION_TTL_SECONDS)
        except redis_exceptions.RedisError:
            bucket = self._get_session_bucket(session_id)
            bucket["last_search"] = snapshot

    async def get_last_search_snapshot(self, session_id: str) -> List[Dict[str, Any]]:
        key = f"session:{session_id}:last_search"
        try:
            raw = await self.redis.get(key)
            if not raw:
                return []
            try:
                data = json.loads(raw)
                return data if isinstance(data, list) else []
            except Exception:
                return []
        except redis_exceptions.RedisError:
            bucket = self._get_session_bucket(session_id)
            data = bucket.get("last_search", [])
            return data if isinstance(data, list) else []

