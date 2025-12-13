"""Definición del LangGraph para Fran 4.0."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from fran_v4 import config
from fran_v4.agent import tools
from fran_v4.database import Database
from fran_v4.llm import LLMService
from fran_v4.memory import SessionMemory
from fran_v4.search_engine import SearchEngine


class AgentRequest(BaseModel):
    message: str = Field(..., description="Texto del cliente")
    session_id: str = Field(..., description="Identificador de sesión")
    filters: Optional[Dict[str, Any]] = Field(
        default=None, description="Filtros para la búsqueda en PostgreSQL"
    )


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
    search_engine: Optional[SearchEngine] = None,
    database: Optional[Database] = None,
    memory: Optional[SessionMemory] = None,
) -> Any:
    llm_service = llm or LLMService()
    search = search_engine or SearchEngine()
    db = database or Database()
    # If memory is not provided, create one using the database's session_factory
    session_memory = memory or SessionMemory(session_factory=db.session_factory)

    async def understand(state: AgentState) -> AgentState:
        """
        Analiza el mensaje del usuario y define el plan y herramienta a ejecutar.

        Args:
            state: Estado actual del agente con el mensaje del usuario.

        Returns:
            Estado actualizado con el plan generado y la herramienta seleccionada.
        """
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
        elif tool == "greet_user":
            context, best_score = await tools.greet_user()
        else:
            context, best_score = await tools.search_products(search, state["message"], filters)
            await tools.persist_snapshot(session_memory, state["session_id"], context)
        await db.log_event(state["session_id"], "tool", f"{tool} -> {len(context)} resultados (score {best_score:.2f})")
        return {**state, "context": context, "best_score": best_score}

    async def evaluate(state: AgentState) -> str:
        tool = state.get("tool")
        if tool == "greet_user":
            return "respond"
        if tool != "search_products":
            return "respond"

        # Si es un saludo o consulta general, no hacer requery
        message_lower = state.get("message", "").lower()
        greetings = ["hola", "buenas", "buenos días", "buenas tardes", "hey", "ayuda", "gracias"]
        if any(greeting in message_lower for greeting in greetings):
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

        tool = state.get("tool", "")
        if tool == "update_cart":
            system_prompt = (
                "Eres Fran 4.0, asistente de ventas de repuestos para motos y bicicletas. "
                "El usuario acaba de agregar/modificar items en su carrito. "
                "Confirma la acción de forma clara y amigable, mostrando el estado actual del carrito. "
                "Si hay items en el carrito, menciona el total de productos y pregunta si necesita algo más."
            )
        elif tool == "get_pricing":
            system_prompt = "Presenta el resumen del carrito con el total de precios."
        elif tool == "greet_user":
            system_prompt = "Responde al saludo de forma amable y profesional."
        else:
            # Para búsqueda de productos
            system_prompt = (
                "Eres Fran 4.0, asistente de ventas experto en repuestos para motos y bicicletas. "
                "Tu trabajo es ayudar al cliente a encontrar el producto que necesita. "
                "\nInstrucciones: "
                "1. Si encontraste productos en el contexto, preséntale al cliente las mejores opciones "
                "2. Menciona características clave: marca, precio, compatibilidad "
                "3. Si hay varias opciones, destaca las diferencias principales "
                "4. Sé proactivo: sugiere alternativas si aplica "
                "5. Pregunta si necesita más información o quiere agregar algo al carrito "
                "\nTono: Profesional, amigable y servicial. "
                "\nNOTA: Si NO hay productos en el contexto, disculpate y ofrece buscar algo similar."
            )

        messages: List[Dict[str, str]] = [
            {"role": "system", "content": system_prompt},
        ]

        # Include conversation history
        for item in history:
            messages.append({"role": item.get("role", "user"), "content": item.get("content", "")})

        # Add current user message
        messages.append({"role": "user", "content": state["message"]})

        # Add context if available
        if state.get("context"):
            context_text = "\n".join([str(chunk) for chunk in state.get("context", [])])
            messages.append({"role": "system", "content": f"Productos encontrados:\n{context_text}"})

        reply = await llm_service.chat(messages, temperature=0.35)
        await session_memory.append_message(state["session_id"], "assistant", reply)
        await db.log_event(state["session_id"], "assistant", reply)
        return {**state, "reply": reply}

    async def parse_message(state: AgentState) -> AgentState:
        if state.get("tool") != "update_cart":
            return state

        prompt = [
            {"role": "system", "content": "Extrae la entidad de la consulta del usuario. Devuelve SOLO un JSON válido con 'code', 'quantity', 'name' (opcional) y 'price_ars' (opcional)."},
            {"role": "user", "content": state["message"]},
        ]
        parsed_item_str = await llm_service.chat(prompt, temperature=0.1, max_tokens=128)

        # Parse the JSON string into a dictionary
        try:
            parsed_item = json.loads(parsed_item_str)
        except json.JSONDecodeError:
            # If parsing fails, try to extract JSON from the response
            import re
            json_match = re.search(r'\{.*\}', parsed_item_str, re.DOTALL)
            if json_match:
                try:
                    parsed_item = json.loads(json_match.group())
                except json.JSONDecodeError:
                    parsed_item = {}
            else:
                parsed_item = {}

        await db.log_event(state["session_id"], "system", f"Mensaje parseado: {parsed_item}")
        return {**state, "parsed_item": parsed_item}

    graph = StateGraph(AgentState)
    graph.add_node("understand", understand)
    graph.add_node("parse_message", parse_message)
    graph.add_node("act", act)
    graph.add_node("requery", requery)
    graph.add_node("respond", respond)

    graph.set_entry_point("understand")
    graph.add_edge("understand", "parse_message")
    graph.add_edge("parse_message", "act")
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
        "tool": "",
        "parsed_item": {},
    }
    result_state: AgentState = await agent_graph.ainvoke(initial_state)
    return AgentResponse(
        session_id=payload.session_id,
        reply=result_state.get("reply", ""),
        context=result_state.get("context", []),
    )

