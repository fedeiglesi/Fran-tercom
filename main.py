"""Punto de entrada de Fran 4.0 usando FastAPI + Uvicorn."""
from __future__ import annotations

import uvicorn

from fran_v4 import config
from fran_v4.api import app


if __name__ == "__main__":
    uvicorn.run("main:app", host=config.UVICORN_HOST, port=config.UVICORN_PORT)

