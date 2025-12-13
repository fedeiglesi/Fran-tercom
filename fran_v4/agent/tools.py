"""Herramientas asíncronas que el grafo de LangGraph puede invocar."""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from fran_v4 import config
from fran_v4.database import Database
from fran_v4.memory import SessionMemory
from fran_v4.search_engine import SearchEngine


def choose_tool(message: str) -> str:
    text = message.lower().strip()
    if text in ["hola", "buenas", "buenos dias", "buen dia"]:
        return "greet_user"
    if any(keyword in text for keyword in ["carrito", "agrega", "agregar", "sumar"]):
        return "update_cart"
    if "precio" in text or "total" in text:
        return "get_pricing"
    return "search_products"


async def search_products(
    search_engine: SearchEngine, query: str, filters: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], float]:
    results = await search_engine.search(query_text=query, limit=config.MAX_SEARCH_RESULTS, filters=filters)
    best_score = max([item.get("score", 0.0) for item in results], default=0.0)
    return results, best_score


async def update_cart(
    database: Database, session_id: str, parsed_item: Dict[str, Any] | None
) -> Tuple[List[Dict[str, Any]], float]:
    if parsed_item and parsed_item.get("code") and parsed_item.get("quantity"):
        await database.update_cart_item(
            session_id=session_id,
            code=str(parsed_item["code"]),
            quantity=int(parsed_item["quantity"]),
            name=parsed_item.get("name"),
            price_ars=parsed_item.get("price_ars"),
        )
    cart_snapshot = await database.get_cart(session_id)
    return cart_snapshot, 100.0 if cart_snapshot else 0.0


async def get_pricing(database: Database, session_id: str) -> Tuple[List[Dict[str, Any]], float]:
    cart = await database.get_cart(session_id)
    return cart, 100.0 if cart else 0.0


async def persist_snapshot(memory: SessionMemory, session_id: str, results: List[Dict[str, Any]]) -> None:
    if results:
        await memory.persist_search_snapshot(session_id, results)


async def greet_user() -> Tuple[List[Dict[str, Any]], float]:
    greeting = {
        "message": "Hola, soy Fran, tu asistente de ventas de KMF. ¿En qué puedo ayudarte hoy?"
    }
    return [greeting], 100.0
