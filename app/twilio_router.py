"""Webhook de Twilio que conecta con el agente Fran 4.0."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from twilio.twiml.messaging_response import MessagingResponse

from fran_v4.agent import AgentRequest, AgentResponse, run_agent

logger = logging.getLogger(__name__)


def create_twilio_router(agent_graph: Any) -> APIRouter:
    router = APIRouter()

    @router.post("/whatsapp")
    async def whatsapp_webhook(request: Request) -> Response:
        """Procesa mensajes entrantes de WhatsApp y responde con TwiML."""

        form = await request.form()
        session_id = form.get("From") or form.get("WaId")
        message = form.get("Body")

        if not session_id or not message:
            logger.warning("Webhook inválido: faltan session_id o mensaje")
            raise HTTPException(status_code=400, detail="Missing session_id or message")

        agent_request = AgentRequest(session_id=session_id, message=message)
        agent_response: AgentResponse = await run_agent(agent_graph, agent_request)

        resp = MessagingResponse()
        resp.message(agent_response.reply or "No pude generar una respuesta en este momento.")

        return Response(content=str(resp), media_type="application/xml")

    return router
