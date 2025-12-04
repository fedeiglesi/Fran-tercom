"""Capa de base de datos asíncrona para Fran 4.0 (PostgreSQL)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from fran_v4 import config

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

products = Table(
    "products",
    metadata,
    Column("code", String(64), primary_key=True),
    Column("name", Text, nullable=False),
    Column("price_ars", Numeric(12, 2), nullable=True),
    Column("family", String(128), nullable=True),
    Column("brand", String(128), nullable=True),
    Column("metadata", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
)

carts = Table(
    "carts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", String(128), nullable=False, index=True),
    Column("code", String(64), nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("name", Text, nullable=True),
    Column("price_ars", Numeric(12, 2), nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False),
    UniqueConstraint("session_id", "code", name="uq_cart_session_code"),
)


class Database:
    """Wrapper asíncrono de SQLAlchemy para conversación y carritos."""

    def __init__(self, url: Optional[str] = None) -> None:
        self._logger = logging.getLogger(__name__)
        self.url = url or config.DATABASE_URL
        self.engine: AsyncEngine = create_async_engine(self.url, future=True, echo=False)
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )
        self.available: bool = True

    async def init_models(self, retries: int = 3, base_delay: float = 1.0) -> None:
        """Inicializa el esquema con reintentos para tolerar arranques lentos."""

        attempt = 0
        while True:
            try:
                async with self.engine.begin() as conn:
                    await conn.run_sync(metadata.create_all)
                return
            except Exception as exc:  # pragma: no cover - defensive fallback for infra issues
                attempt += 1
                if attempt > retries:
                    self.available = False
                    self._logger.error("No se pudo inicializar la base de datos: %s", exc)
                    await self.dispose()
                    return

                delay = base_delay * attempt
                self._logger.warning(
                    "Fallo al conectar con la base (intento %s/%s): %s; reintentando en %.1fs",
                    attempt,
                    retries,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

    async def log_event(self, session_id: str, role: str, content: str) -> None:
        if not self.available:
            return
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
        if not self.available:
            return []
        async with self.session_factory() as session:
            result = await session.execute(
                conversation_events.select()
                .where(conversation_events.c.session_id == session_id)
                .order_by(conversation_events.c.created_at.desc())
                .limit(limit)
            )
            rows = result.fetchall()
            return [dict(row._mapping) for row in reversed(rows)]

    async def upsert_product(self, payload: Dict[str, Any]) -> None:
        async with self.session_factory() as session:
            stmt = insert(products).values(
                code=str(payload.get("code")),
                name=payload.get("name", ""),
                price_ars=payload.get("price_ars"),
                family=payload.get("family"),
                brand=payload.get("brand"),
                metadata=payload.get("metadata"),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[products.c.code],
                set_={
                    "name": stmt.excluded.name,
                    "price_ars": stmt.excluded.price_ars,
                    "family": stmt.excluded.family,
                    "brand": stmt.excluded.brand,
                    "metadata": stmt.excluded.metadata,
                },
            )
            await session.execute(stmt)
            await session.commit()

    async def update_cart_item(
        self,
        session_id: str,
        code: str,
        quantity: int,
        name: Optional[str] = None,
        price_ars: Optional[float] = None,
    ) -> None:
        if not self.available:
            return
        async with self.session_factory() as session:
            stmt = insert(carts).values(
                session_id=session_id,
                code=code,
                quantity=quantity,
                name=name,
                price_ars=price_ars,
            )
            stmt = stmt.on_conflict_do_update(
                constraint="uq_cart_session_code",
                set_={"quantity": stmt.excluded.quantity, "name": stmt.excluded.name, "price_ars": stmt.excluded.price_ars},
            )
            await session.execute(stmt)
            await session.commit()

    async def get_cart(self, session_id: str) -> List[Dict[str, Any]]:
        if not self.available:
            return []
        async with self.session_factory() as session:
            result = await session.execute(
                carts.select().where(carts.c.session_id == session_id).order_by(carts.c.created_at.desc())
            )
            rows = result.fetchall()
            return [dict(row._mapping) for row in rows]

    async def dispose(self) -> None:
        await self.engine.dispose()
