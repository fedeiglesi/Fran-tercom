"""FastAPI app para Fran 4.0 (Arquitectura SOTA)."""
from __future__ import annotations

import logging
import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError

from fran_v4 import config
from fran_v4.agent import AgentRequest, AgentResponse, build_agent_graph, run_agent
from fran_v4.database import Database, rate_limits
from fran_v4.memory import SessionMemory
from fran_v4.search_engine import HybridSearchEngine
from fran_v4.startup import initialize_catalog

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(title="Fran 4.0", version="4.0.0")
    app.state.is_ready = False

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def readiness_middleware(request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)
        if not app.state.is_ready:
            return JSONResponse(status_code=503, content={"detail": "Service Unavailable: Initializing"})
        return await call_next(request)

    database = Database()
    memory = SessionMemory(session_factory=database.session_factory)
    search_engine = HybridSearchEngine()
    agent_graph = build_agent_graph(search_engine=search_engine, database=database, memory=memory)

    async def rate_limiter(request: Request) -> None:
        # Use client IP for rate limiting to avoid reading the request body
        # which would consume the stream and make it unavailable in the endpoint handler
        if not database.available:
            logger.warning("Base de datos no disponible; se omite rate limiting.")
            return

        client_ip = request.client.host if request.client else "unknown"
        cutoff = datetime.utcnow() - timedelta(minutes=1)

        try:
            async with database.session_factory() as session:
                await session.execute(
                    delete(rate_limits).where(rate_limits.c.created_at < cutoff)
                )

                result = await session.execute(
                    select(func.count())
                    .select_from(rate_limits)
                    .where(
                        rate_limits.c.ip_address == client_ip,
                        rate_limits.c.created_at >= cutoff,
                    )
                )
                count = result.scalar() or 0

                if count >= config.RATE_LIMIT_PER_MINUTE:
                    raise HTTPException(status_code=429, detail="Rate limit exceeded. Please try again later.")

                await session.execute(
                    rate_limits.insert().values(ip_address=client_ip, created_at=datetime.utcnow())
                )
                await session.commit()
        except HTTPException:
            raise
        except SQLAlchemyError as exc:  # pragma: no cover - infra fallback
            logger.warning(
                "Error al aplicar rate limiting en PostgreSQL (%s); se permite la solicitud.",
                exc,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error in PostgreSQL rate limiter: %s", exc)

    @app.on_event("startup")
    async def _startup() -> None:
        async def initialize_app() -> None:
            await database.init_models()
            if not database.available:
                logger.warning(
                    "La base de datos no está disponible; se reintentará en segundo plano y las "
                    "operaciones persistentes se omitirán hasta reconectar."
                )

            try:
                await initialize_catalog(search_engine=search_engine, database=database)
            except Exception as exc:  # pragma: no cover - defensive fallback
                logger.warning("Inicialización de catálogo fallida: %s", exc)

            app.state.is_ready = True

        asyncio.create_task(initialize_app())

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await memory.close()
        await database.dispose()

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "version": "4.0",
            "components": {
                "fastapi_async": True,
                "postgresql": database.available,
                "langgraph": True,
            },
        }

    @app.post("/whatsapp", response_model=AgentResponse)
    async def whatsapp_webhook(request: Request, _: None = Depends(rate_limiter)) -> AgentResponse:
        payload_data: Dict[str, Any] = {}
        try:
            form = await request.form()
            if form:
                payload_data["session_id"] = form.get("From") or form.get("session_id")
                payload_data["message"] = form.get("Body") or form.get("message")
        except Exception:
            payload_data = await request.json()

        if not payload_data:
            payload_data = await request.json()

        if not payload_data.get("session_id") or not payload_data.get("message"):
            raise HTTPException(status_code=400, detail="Missing session_id or message")

        agent_request = AgentRequest(**payload_data)
        response = await run_agent(agent_graph, agent_request)
        return response

    @app.post("/agent", response_model=AgentResponse)
    async def invoke_agent(payload: AgentRequest, _: None = Depends(rate_limiter)) -> AgentResponse:
        response = await run_agent(agent_graph, payload)
        return response

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_, exc: HTTPException):
        logger.error(f"HTTP error: {exc.detail}")
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    return app


app = create_app()
