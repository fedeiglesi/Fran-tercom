"""Async database layer for Fran 4.0 using PostgreSQL."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, Text, func
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

metadata = MetaData()

conversation_events = Table(
    "conversation_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", String(128), nullable=False, index=True),
    Column("role", String(16), nullable=False),
    Column("content", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)


class Database:
    """Small async wrapper around SQLAlchemy for conversation logging."""

    def __init__(self, url: Optional[str] = None) -> None:
        self.url = url or os.getenv(
            "DATABASE_URL",
            "postgresql+asyncpg://postgres:postgres@localhost:5432/fran",
        )
        self.engine: AsyncEngine = create_async_engine(self.url, future=True, echo=False)
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

    async def init_models(self) -> None:
        async with self.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    async def log_event(self, session_id: str, role: str, content: str) -> None:
        async with self.session_factory() as session:
            await session.execute(
                conversation_events.insert().values(
                    session_id=session_id,
                    role=role,
                    content=content,
                )
            )
            await session.commit()

    async def get_history(self, session_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        async with self.session_factory() as session:
            result = await session.execute(
                conversation_events.select()
                .where(conversation_events.c.session_id == session_id)
                .order_by(conversation_events.c.created_at.desc())
                .limit(limit)
            )
            rows = result.fetchall()
            return [dict(row._mapping) for row in reversed(rows)]

    async def dispose(self) -> None:
        await self.engine.dispose()
