"""Capa de base de datos asíncrona para Fran 4.0 (PostgreSQL)."""
from __future__ import annotations

import asyncio
import contextlib
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

rate_limits = Table(
    "rate_limits",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ip_address", String(45), nullable=False, index=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False, index=True),
)

session_messages = Table(
    "session_messages",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", String(255), nullable=False, index=True),
    Column("role", String(50), nullable=False),
    Column("content", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now(), nullable=False, index=True),
)

session_pending_actions = Table(
    "session_pending_actions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", String(255), nullable=False, unique=True, index=True),
    Column("action", Text, nullable=True),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    ),
)

session_search_snapshots = Table(
    "session_search_snapshots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("session_id", String(255), nullable=False, unique=True, index=True),
    Column("snapshot", Text, nullable=True),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    ),
)


class Database:
    """Wrapper asíncrono de SQLAlchemy para conversación y carritos."""

    def __init__(self, url: Optional[str] = None) -> None:
        self._logger = logging.getLogger(__name__)
        self.url = url or config.DATABASE_URL
        self._logger.info("Inicializando Database con URL: %s", config.mask_database_url(self.url))
        self.engine: AsyncEngine = create_async_engine(self.url, future=True, echo=False)
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )
        self.available: bool = True
        self._reconnect_task: Optional[asyncio.Task[None]] = None

    async def init_models(
        self,
        retries: Optional[int] = None,
        base_delay: Optional[float] = None,
        max_delay: Optional[float] = None,
    ) -> None:
        """Inicializa el esquema con reintentos y reconexión en segundo plano."""

        retries = retries if retries is not None else config.DB_INIT_MAX_RETRIES
        base_delay = base_delay if base_delay is not None else config.DB_INIT_BASE_DELAY
        max_delay = max_delay if max_delay is not None else config.DB_INIT_MAX_DELAY

        attempt = 0
        while True:
            try:
                self._logger.info("Creando tablas en la base de datos...")
                async with self.engine.begin() as conn:
                    await conn.run_sync(metadata.create_all)
                self.available = True
                if attempt:
                    self._logger.info("Base de datos inicializada tras %s intentos", attempt + 1)
                return
            except Exception as exc:  # pragma: no cover - defensive fallback for infra issues
                attempt += 1
                delay = min(base_delay * attempt, max_delay)
                if retries is not None and attempt >= retries:
                    self.available = False
                    self._logger.error(
                        "No se pudo inicializar la base de datos tras %s intentos: %s", attempt, exc
                    )
                    await self._schedule_background_reconnect(attempt + 1, base_delay, max_delay)
                    return

                self._logger.warning(
                    "Fallo al conectar con la base (intento %s/%s): %s; reintentando en %.1fs",
                    attempt,
                    retries if retries is not None else "∞",
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)

    async def _schedule_background_reconnect(
        self, starting_attempt: int, base_delay: float, max_delay: float
    ) -> None:
        if self._reconnect_task and not self._reconnect_task.done():
            return

        async def _runner() -> None:
            attempt = starting_attempt
            while True:
                try:
                    async with self.engine.begin() as conn:
                        await conn.run_sync(metadata.create_all)
                    self.available = True
                    self._logger.info(
                        "Base de datos reconectada tras %s intentos totales", attempt
                    )
                    return
                except Exception as exc:  # pragma: no cover - infra fallback
                    delay = min(base_delay * attempt, max_delay)
                    self._logger.warning(
                        "Reconexión fallida (intento %s): %s; reintentando en %.1fs",
                        attempt,
                        exc,
                        delay,
                    )
                    attempt += 1
                    await asyncio.sleep(delay)

        self._reconnect_task = asyncio.create_task(_runner())

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
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconnect_task
        await self.engine.dispose()
