"""Definición del LangGraph para Fran 4.0."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from fran_v4 import config
from fran_v4.agent import tools
from fran_v4.database import Database
from fran_v4.llm import LLMService
from fran_v4.memory import SessionMemory
from fran_v4.search_engine import HybridSearchEngine


class AgentRequest(BaseModel):
    message: str = Field(..., description="Texto del cliente")
    session_id: str = Field(..., description="Identificador de sesión")
    filters: Optional[Dict[str, Any]] = Field(default=None, description="Filtros para Qdrant")


class AgentResponse(BaseModel):
    session_id: str
    reply: str
    context: List[Dict[str, Any]]


class AgentState(TypedDict):
    message: str
    session_id: str
    filters: Dict[str, Any]
    plan: str
    context: List[Dict[str, Any]]
    reply: str
    best_score: float
    attempts: int
    tool: str
    parsed_item: Dict[str, Any]


def _build_plan_prompt(query: str) -> str:
    return (
        "Planificá la respuesta de Fran 4.0: detectá intención, ejecutá la herramienta correcta "
        "(búsqueda híbrida, carrito o precios) y devolvé contexto verificable. "
        f"Consulta: {query}"
    )


def build_agent_graph(
    llm: Optional[LLMService] = None,
    search_engine: Optional[HybridSearchEngine] = None,
    database: Optional[Database] = None,
    memory: Optional[SessionMemory] = None,
) -> Any:
    llm_service = llm or LLMService()
    search = search_engine or HybridSearchEngine()
    db = database or Database()
    session_memory = memory or SessionMemory()

    async def understand(state: AgentState) -> AgentState:
        await session_memory.append_message(state["session_id"], "user", state["message"])
        plan = _build_plan_prompt(state["message"])
        tool = tools.choose_tool(state["message"])
        await db.log_event(state["session_id"], "system", f"Plan: {plan} | Tool: {tool}")
        return {**state, "plan": plan, "tool": tool}

    async def act(state: AgentState) -> AgentState:
        tool = state.get("tool", "search_products")
        filters = state.get("filters", {})
        if tool == "update_cart":
            context, best_score = await tools.update_cart(db, state["session_id"], state.get("parsed_item"))
        elif tool == "get_pricing":
            context, best_score = await tools.get_pricing(db, state["session_id"])
        else:
            context, best_score = await tools.search_products(search, state["message"], filters)
            await tools.persist_snapshot(session_memory, state["session_id"], context)
        await db.log_event(state["session_id"], "tool", f"{tool} -> {len(context)} resultados (score {best_score:.2f})")
        return {**state, "context": context, "best_score": best_score}

    async def evaluate(state: AgentState) -> str:
        if state.get("tool") != "search_products":
            return "respond"
        if state.get("best_score", 0.0) >= config.RELEVANCE_MIN_SCORE or state.get("attempts", 0) >= 1:
            return "respond"
        return "requery"

    async def requery(state: AgentState) -> AgentState:
        prompt = [
            {"role": "system", "content": "Reformula la consulta para mejorar la búsqueda en catálogo."},
            {"role": "user", "content": state["message"]},
        ]
        new_query = await llm_service.chat(prompt, temperature=0.2, max_tokens=64)
        await db.log_event(state["session_id"], "system", f"Re-query generado: {new_query}")
        return {**state, "message": new_query, "attempts": state.get("attempts", 0) + 1}

    async def respond(state: AgentState) -> AgentState:
        history = await session_memory.get_history(state["session_id"], limit=6)
        messages: List[Dict[str, str]] = [
            {"role": "system", "content": "Eres Fran 4.0, agente de ventas asíncrono basado en grafos."},
            {"role": "system", "content": state.get("plan", "")},
        ]
        for item in history:
            messages.append({"role": item.get("role", "user"), "content": item.get("content", "")})
        messages.append({"role": "user", "content": state["message"]})
        if state.get("context"):
            context_text = "\n".join([str(chunk) for chunk in state.get("context", [])])
            messages.append({"role": "system", "content": f"Contexto recuperado:\n{context_text}"})
        reply = await llm_service.chat(messages, temperature=0.35)
        await session_memory.append_message(state["session_id"], "assistant", reply)
        await db.log_event(state["session_id"], "assistant", reply)
        return {**state, "reply": reply}

    graph = StateGraph(AgentState)
    graph.add_node("understand", understand)
    graph.add_node("act", act)
    graph.add_node("requery", requery)
    graph.add_node("respond", respond)

    graph.set_entry_point("understand")
    graph.add_edge("understand", "act")
    graph.add_conditional_edges("act", evaluate, {"respond": "respond", "requery": "requery"})
    graph.add_edge("requery", "act")
    graph.add_edge("respond", END)

    return graph.compile()


async def run_agent(agent_graph: Any, payload: AgentRequest) -> AgentResponse:
    initial_state: AgentState = {
        "message": payload.message,
        "session_id": payload.session_id,
        "filters": payload.filters or {},
        "plan": "",
        "context": [],
        "reply": "",
        "best_score": 0.0,
        "attempts": 0,
        "tool": tools.choose_tool(payload.message),
        "parsed_item": {},
    }
    result_state: AgentState = await agent_graph.ainvoke(initial_state)
    return AgentResponse(
        session_id=payload.session_id, reply=result_state.get("reply", ""), context=result_state.get("context", [])
    )

