"""Aplicación FastAPI para el webhook de Twilio usando Fran 4.0."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from fran_v4.agent import build_agent_graph
from fran_v4.database import Database
from fran_v4.memory import SessionMemory
from fran_v4.search_engine import SearchEngine
from fran_v4.startup import initialize_catalog

from .twilio_router import create_twilio_router

# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(title="Fran WhatsApp")

    database = Database()
    memory = SessionMemory(session_factory=database.session_factory)
    search_engine = SearchEngine()
    agent_graph = build_agent_graph(
        search_engine=search_engine, database=database, memory=memory
    )

    @app.on_event("startup")
    async def _startup() -> None:
        await database.init_models()
        try:
            await initialize_catalog(search_engine=search_engine, database=database)
        except Exception as exc:  # noqa: BLE001 - fallback defensivo
            logger.warning("No se pudo inicializar el catálogo en arranque: %s", exc)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await memory.close()
        await search_engine.dispose()
        await database.dispose()

    @app.get("/")
    async def home():
        return {"status": "ok"}

    app.include_router(create_twilio_router(agent_graph))

    return app


app = create_app()
