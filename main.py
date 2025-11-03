# coding: utf-8
"""
Fran 3.8 - Bot Mayorista Inteligente
------------------------------------
Punto de entrada principal del sistema.
Ejecuta la aplicación Flask definida en fran.routes
"""

from fran.routes import app
from fran.config import logger, PORT

if __name__ == "__main__":
    logger.info("🚀 Iniciando Fran 3.8 (Flask App)...")
    app.run(host="0.0.0.0", port=PORT)
