"""Compatibilidad para importaciones antiguas del agente Fran 4.0.

Este módulo reexpone el grafo definido en ``fran_v4.agent.graph`` para no
romper código existente mientras la estructura se migra al paquete
``fran_v4.agent``.
"""
from __future__ import annotations

from fran_v4.agent.graph import AgentRequest, AgentResponse, build_agent_graph, run_agent

# Alias mantenidos para retrocompatibilidad
build_fran_graph = build_agent_graph

__all__ = ["AgentRequest", "AgentResponse", "build_agent_graph", "build_fran_graph", "run_agent"]

