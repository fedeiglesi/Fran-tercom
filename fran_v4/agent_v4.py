"""LangGraph-based agent for Fran 4.0."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from fran_v4.database import Database
from fran_v4.llm import LLMService
from fran_v4.search_engine import HybridSearchEngine


class AgentRequest(BaseModel):
    message: str = Field(..., description="Texto del cliente")
    session_id: str = Field(..., description="Identificador de sesión")
    filters: Optional[Dict[str, Any]] = Field(default=None, description="Filtros de metadata para Qdrant")


class AgentResponse(BaseModel):
    session_id: str
    reply: str
    context: List[Dict[str, Any]]


class AgentState(TypedDict):
    message: str
    session_id: str
    plan: str
    context: List[Dict[str, Any]]
    reply: str
    filters: Dict[str, Any]


def _format_plan(query: str) -> str:
    return (
        "Entiende la intención, busca en Qdrant con filtros de metadata, "
        "consulta servicios de carrito/precios si corresponde y responde de forma verificable. "
        f"Consulta: {query}"
    )


def build_fran_graph(
    llm: Optional[LLMService] = None,
    search_engine: Optional[HybridSearchEngine] = None,
    database: Optional[Database] = None,
) -> Any:
    llm_service = llm or LLMService()
    search = search_engine or HybridSearchEngine()
    db = database or Database()

    async def plan_step(state: AgentState) -> AgentState:
        plan = _format_plan(state["message"])
        await db.log_event(state["session_id"], "system", plan)
        return {**state, "plan": plan}

    async def search_step(state: AgentState) -> AgentState:
        # Placeholder vector to allow hybrid search even when embeddings are delegated elsewhere
        fake_vector = [0.0] * 5
        filters = state.get("filters") if isinstance(state, dict) else None
        results = await search.hybrid_search(fake_vector, state["message"], limit=8, filters=filters)  # type: ignore[arg-type]
        await db.log_event(state["session_id"], "tool", f"Qdrant results: {len(results)}")
        return {**state, "context": results}

    async def reason_step(state: AgentState) -> AgentState:
        history = await db.get_history(state["session_id"], limit=6)
        messages = [
            {"role": "system", "content": "Eres Fran 4.0, agente de ventas con recuperación externa."},
            {"role": "system", "content": state.get("plan", "")},
        ]
        for item in history:
            messages.append({"role": item["role"], "content": item["content"]})
        messages.append({"role": "user", "content": state["message"]})
        context_chunks = state.get("context", [])
        if context_chunks:
            context_text = "\n".join([str(chunk) for chunk in context_chunks])
            messages.append({"role": "system", "content": f"Contexto recuperado:\n{context_text}"})
        reply = await llm_service.chat(messages, temperature=0.3)
        await db.log_event(state["session_id"], "assistant", reply)
        return {**state, "reply": reply}

    async def requery_router(state: AgentState) -> str:
        if state.get("context"):
            return "respond"
        return "search"

    async def respond_step(state: AgentState) -> AgentState:
        return state

    graph = StateGraph(AgentState)
    graph.add_node("plan", plan_step)
    graph.add_node("search", search_step)
    graph.add_node("reason", reason_step)
    graph.add_node("respond", respond_step)

    graph.set_entry_point("plan")
    graph.add_edge("plan", "search")
    graph.add_conditional_edges("search", requery_router, {"respond": "respond", "search": "search"})
    graph.add_edge("respond", "reason")
    graph.add_edge("reason", END)

    return graph.compile(asyncio=True)


async def run_agent(agent_graph: Any, payload: AgentRequest) -> AgentResponse:
    initial_state: AgentState = {
        "message": payload.message,
        "session_id": payload.session_id,
        "plan": "",
        "context": [],
        "reply": "",
        "filters": payload.filters or {},
    }
    result_state: AgentState = await agent_graph.ainvoke(initial_state)
    return AgentResponse(session_id=payload.session_id, reply=result_state.get("reply", ""), context=result_state.get("context", []))
