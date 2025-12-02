"""FastAPI app para Fran 4.0 (Arquitectura SOTA)."""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

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

    redis_client = Redis.from_url(config.REDIS_URL, decode_responses=True)
    memory = SessionMemory(config.REDIS_URL)
    database = Database()
    search_engine = HybridSearchEngine()
    agent_graph = build_agent_graph(search_engine=search_engine, database=database, memory=memory)

    async def rate_limiter(request: Request) -> None:
        session_id: str | None = None
        try:
            if request.headers.get("content-type", "").startswith("application/json"):
                data = await request.json()
                session_id = data.get("session_id")
            else:
                form = await request.form()
                session_id = form.get("From") or form.get("session_id")
        except Exception:
            session_id = None

        if not session_id:
            raise HTTPException(status_code=400, detail="Missing session_id for rate limit")

        key = f"rate:{session_id}"
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, 60)
        if count > config.RATE_LIMIT_PER_MINUTE:
            raise HTTPException(status_code=429, detail="Rate limit exceeded for session")

    @app.on_event("startup")
    async def _startup() -> None:
        await database.init_models()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await redis_client.aclose()
        await memory.close()
        await database.dispose()

    @app.get("/health")
    async def health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "version": "4.0",
            "components": {
                "fastapi_async": True,
                "postgresql": True,
                "redis": True,
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
