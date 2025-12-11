from fastapi import APIRouter, Request, Response
from twilio.twiml.messaging_response import MessagingResponse

router = APIRouter()


@router.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    form = await request.form()
    incoming = form.get("Body", "")

    # Ejemplo mínimo
    resp = MessagingResponse()
    resp.message("Mensaje recibido")

    return Response(content=str(resp), media_type="application/xml")
