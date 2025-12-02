"""FastAPI entrypoint for Fran 4.0 running on Railway."""
from __future__ import annotations

import os
from typing import Dict

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from fran_v4.agent_v4 import AgentRequest, AgentResponse, build_fran_graph, run_agent
from fran_v4.database import Database
from fran_v4.search_engine import HybridSearchEngine


def create_app() -> FastAPI:
    app = FastAPI(title="Fran 4.0", version="4.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    redis_client = Redis.from_url(redis_url, decode_responses=True)
    database = Database()
    search_engine = HybridSearchEngine()
    agent_graph = build_fran_graph(search_engine=search_engine, database=database)

    async def get_rate_limiter(request: AgentRequest) -> None:
        key = f"rate:{request.session_id}"
        count = await redis_client.incr(key)
        if count == 1:
            await redis_client.expire(key, 60)
        if count > int(os.getenv("RATE_LIMIT_PER_MINUTE", "30")):
            raise HTTPException(status_code=429, detail="Rate limit exceeded for session")

    @app.on_event("startup")
    async def _startup() -> None:
        await database.init_models()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await redis_client.aclose()
        await database.dispose()

    @app.get("/health")
    async def health() -> Dict[str, str]:
        return {"status": "ok"}

    @app.post("/agent", response_model=AgentResponse)
    async def invoke_agent(payload: AgentRequest, _: None = Depends(get_rate_limiter)) -> AgentResponse:
        await redis_client.lpush(f"session:{payload.session_id}:history", payload.message)
        response = await run_agent(agent_graph, payload)
        await redis_client.lpush(f"session:{payload.session_id}:history", response.reply)
        return response

    return app


app = create_app()
