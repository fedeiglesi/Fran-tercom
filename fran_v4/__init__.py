"""Entry points and public API for Fran 4.0.

Este módulo expone la aplicación FastAPI lista para producción
(`fran_v4.api:app`) y el factory `create_app` para que los procesos
externos puedan importarla sin depender de rutas internas.
"""

from fran_v4.api import app, create_app

__all__ = ["app", "create_app"]
