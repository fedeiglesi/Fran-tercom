"""Memoria de sesión basada en PostgreSQL para Fran 4.0."""
from __future__ import annotations

import json
import time
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from fran_v4 import config
from fran_v4.database import (
    session_messages,
    session_pending_actions,
    session_search_snapshots,
)


class SessionMemory:
    """Almacena historial, acciones pendientes y snapshots de búsqueda en PostgreSQL."""

    def __init__(self, session_factory: Optional[async_sessionmaker[AsyncSession]] = None) -> None:
        self._logger = logging.getLogger(__name__)
        self._engine: Optional[AsyncEngine] = None
        if session_factory is not None:
            self.session_factory = session_factory
        else:
            self._engine = create_async_engine(config.DATABASE_URL, future=True, echo=False)
            self.session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        self._fallback_store: Dict[str, Dict[str, Any]] = {}

    async def close(self) -> None:
        if self._engine:
            await self._engine.dispose()

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
        payload = json.dumps({"role": role, "content": content})
        try:
            async with self.session_factory() as session:
                await session.execute(
                    session_messages.insert().values(
                        session_id=session_id,
                        role=role,
                        content=content,
                    )
                )
                await session.commit()
        except SQLAlchemyError as exc:  # pragma: no cover - infra fallback
            self._logger.warning(
                "No se pudo guardar historial en PostgreSQL (%s); usando fallback en memoria.",
                exc,
            )
            bucket = self._get_session_bucket(session_id)
            bucket["history"].append(json.loads(payload))

    async def get_history(self, session_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        limit = max(1, min(limit, 50))
        try:
            async with self.session_factory() as session:
                # Order by descending to get most recent messages first, then reverse
                result = await session.execute(
                    select(session_messages.c.role, session_messages.c.content)
                    .where(session_messages.c.session_id == session_id)
                    .order_by(session_messages.c.created_at.desc())
                    .limit(limit)
                )
                rows = result.fetchall()
                history: List[Dict[str, Any]] = []
                for row in rows:
                    history.append({"role": row.role, "content": row.content})
                # Reverse to get chronological order (oldest to newest)
                return list(reversed(history))
        except SQLAlchemyError as exc:  # pragma: no cover - infra fallback
            self._logger.warning(
                "No se pudo obtener historial desde PostgreSQL (%s); usando fallback en memoria.",
                exc,
            )
            bucket = self._get_session_bucket(session_id)
            return bucket.get("history", [])[-limit:]

    async def clear_messages(self, session_id: str) -> None:
        try:
            async with self.session_factory() as session:
                await session.execute(
                    delete(session_messages).where(session_messages.c.session_id == session_id)
                )
                await session.commit()
        except SQLAlchemyError as exc:  # pragma: no cover - infra fallback
            self._logger.warning(
                "No se pudo limpiar el historial en PostgreSQL (%s); usando fallback en memoria.",
                exc,
            )
            bucket = self._get_session_bucket(session_id)
            bucket["history"] = []

    async def set_pending_action(self, session_id: str, action: Dict[str, Any]) -> None:
        payload = json.dumps(action)
        try:
            async with self.session_factory() as session:
                stmt = insert(session_pending_actions).values(session_id=session_id, action=payload)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[session_pending_actions.c.session_id],
                    set_={"action": stmt.excluded.action},
                )
                await session.execute(stmt)
                await session.commit()
        except SQLAlchemyError as exc:  # pragma: no cover - infra fallback
            self._logger.warning(
                "No se pudo persistir la acción pendiente en PostgreSQL (%s); usando fallback en memoria.",
                exc,
            )
            bucket = self._get_session_bucket(session_id)
            bucket["pending_action"] = action

    async def get_pending_action(self, session_id: str) -> Optional[Dict[str, Any]]:
        try:
            async with self.session_factory() as session:
                result = await session.execute(
                    select(session_pending_actions.c.action).where(
                        session_pending_actions.c.session_id == session_id
                    )
                )
                row = result.fetchone()
                if not row or not row.action:
                    return None
                return json.loads(row.action)
        except SQLAlchemyError as exc:  # pragma: no cover - infra fallback
            self._logger.warning(
                "No se pudo recuperar la acción pendiente desde PostgreSQL (%s); usando fallback en memoria.",
                exc,
            )
            bucket = self._get_session_bucket(session_id)
            return bucket.get("pending_action")
        except json.JSONDecodeError:
            return None

    async def persist_search_snapshot(self, session_id: str, products: List[Dict[str, Any]]) -> None:
        snapshot = json.dumps(products[: config.MAX_ITEMS])
        try:
            async with self.session_factory() as session:
                stmt = insert(session_search_snapshots).values(session_id=session_id, snapshot=snapshot)
                stmt = stmt.on_conflict_do_update(
                    index_elements=[session_search_snapshots.c.session_id],
                    set_={"snapshot": stmt.excluded.snapshot},
                )
                await session.execute(stmt)
                await session.commit()
        except SQLAlchemyError as exc:  # pragma: no cover - infra fallback
            self._logger.warning(
                "No se pudo persistir la última búsqueda en PostgreSQL (%s); usando fallback en memoria.",
                exc,
            )
            bucket = self._get_session_bucket(session_id)
            bucket["last_search"] = json.loads(snapshot)

    async def get_last_search_snapshot(self, session_id: str) -> List[Dict[str, Any]]:
        try:
            async with self.session_factory() as session:
                result = await session.execute(
                    select(session_search_snapshots.c.snapshot).where(
                        session_search_snapshots.c.session_id == session_id
                    )
                )
                row = result.fetchone()
                if not row or not row.snapshot:
                    return []
                data = json.loads(row.snapshot)
                return data if isinstance(data, list) else []
        except SQLAlchemyError as exc:  # pragma: no cover - infra fallback
            self._logger.warning(
                "No se pudo recuperar la última búsqueda desde PostgreSQL (%s); usando fallback en memoria.",
                exc,
            )
            bucket = self._get_session_bucket(session_id)
            data = bucket.get("last_search", [])
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

