"""FastAPI app para Fran 4.0 (Arquitectura SOTA)."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import ConnectionError, TimeoutError

from fran_v4 import config
from fran_v4.agent import AgentRequest, AgentResponse, build_agent_graph, run_agent
from fran_v4.database import Database
from fran_v4.memory import SessionMemory
from fran_v4.search_engine import HybridSearchEngine

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(title="Fran 4.0", version="4.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    redis_client = Redis.from_url(
        config.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    redis_available: bool | None = None
    memory = SessionMemory(config.REDIS_URL)
    database = Database()
    search_engine = HybridSearchEngine()
    agent_graph = build_agent_graph(search_engine=search_engine, database=database, memory=memory)

    async def _mark_redis_state(state: bool, message: str) -> None:
        nonlocal redis_available
        if redis_available != state:
            redis_available = state
            log_method = logger.info if state else logger.warning
            log_method(message)
        else:
            redis_available = state

    async def is_redis_available(force_check: bool = False) -> bool:
        """Perform a lightweight health check to Redis with caching."""

        if redis_available is not None and not force_check:
            return redis_available

        try:
            await asyncio.wait_for(redis_client.ping(), timeout=1.0)
            await _mark_redis_state(True, "Redis connection restored; rate limiting enabled.")
        except (ConnectionError, TimeoutError, asyncio.TimeoutError) as exc:
            await _mark_redis_state(False, f"Redis unavailable ({exc}); skipping rate limiting.")
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error checking Redis availability: %s", exc)
            await _mark_redis_state(False, "Redis unavailable due to unexpected error; skipping rate limiting.")

        return bool(redis_available)

    async def rate_limiter(request: Request) -> None:
        session_id: str | None = None
        try:
            if request.headers.get("content-type", "").startswith("application/json"):
                data = await request.json()
                session_id = data.get("session_id")
            else:
                form = await request.form()
                session_id = form.get("From") or form.get("session_id")
        except Exception:  # noqa: BLE001
            session_id = None

        if not session_id:
            raise HTTPException(status_code=400, detail="Missing session_id for rate limit")

        if not await is_redis_available():
            return

        key = f"rate:{session_id}"
        try:
            count = await asyncio.wait_for(redis_client.incr(key), timeout=2.0)
            if count == 1:
                await asyncio.wait_for(redis_client.expire(key, 60), timeout=2.0)
            if count > config.RATE_LIMIT_PER_MINUTE:
                raise HTTPException(status_code=429, detail="Rate limit exceeded for session")
        except (ConnectionError, TimeoutError, asyncio.TimeoutError) as exc:
            await _mark_redis_state(False, f"Redis error during rate limiting ({exc}); allowing request.")
        except Exception as exc:  # noqa: BLE001
            logger.error("Unexpected error in rate limiter: %s", exc)
            await _mark_redis_state(False, "Redis unavailable due to unexpected error; allowing request.")

    @app.on_event("startup")
    async def _startup() -> None:
        await database.init_models()
        if not database.available:
            logger.warning(
                "La base de datos no está disponible; se reintentará en segundo plano y las "
                "operaciones persistentes se omitirán hasta reconectar."
            )

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        if redis_client:
            await redis_client.aclose()
        await memory.close()
        await database.dispose()

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        redis_status = await is_redis_available(force_check=True)
        return {
            "status": "ok",
            "version": "4.0",
            "components": {
                "fastapi_async": True,
                "postgresql": database.available,
                "redis": redis_status,
                "qdrant": True,
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
