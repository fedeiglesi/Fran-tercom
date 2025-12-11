from fastapi import FastAPI

from .twilio_router import router as twilio_router

app = FastAPI()


@app.get("/")
def home():
    return {"status": "ok"}


app.include_router(twilio_router)
