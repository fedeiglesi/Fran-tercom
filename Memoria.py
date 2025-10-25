import sqlite3
import logging
from datetime import datetime
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# -----------------------------------
# CONEXIÓN Y CREACIÓN DE TABLAS
# -----------------------------------
@contextmanager
def get_db_connection():
    conn = sqlite3.connect('tercom.db')
    try:
        yield conn
        conn.commit()
    except Exception as e:
        conn.rollback()
        logger.error(f"DB error: {e}")
    finally:
        conn.close()


def init_db():
    """Inicializa las tablas necesarias si no existen."""
    with get_db_connection() as conn:
        c = conn.cursor()
        # Conversaciones (memoria persistente)
        c.execute('''CREATE TABLE IF NOT EXISTS conversations
                     (phone TEXT, message TEXT, role TEXT, timestamp TEXT)''')

        # Carrito
        c.execute('''CREATE TABLE IF NOT EXISTS carts
                     (phone TEXT, code TEXT, quantity INTEGER, name TEXT,
                      price_usd REAL, price_ars REAL)''')

        # Estado del usuario (último producto o contexto)
        c.execute('''CREATE TABLE IF NOT EXISTS user_state
                     (phone TEXT PRIMARY KEY,
                      last_code TEXT,
                      last_name TEXT,
                      last_price_ars REAL,
                      updated_at TEXT)''')

        # Índices para mejorar velocidad
        c.execute('''CREATE INDEX IF NOT EXISTS idx_conv_phone
                     ON conversations(phone, timestamp DESC)''')
        c.execute('''CREATE INDEX IF NOT EXISTS idx_cart_phone
                     ON carts(phone)''')

        logger.info("✅ Base de datos inicializada correctamente.")


# -----------------------------------
# MEMORIA DE CONVERSACIÓN
# -----------------------------------
def save_message(phone, msg, role):
    """Guarda cada mensaje del usuario o asistente."""
    try:
        with get_db_connection() as conn:
            conn.execute(
                'INSERT INTO conversations VALUES (?, ?, ?, ?)',
                (phone, msg, role, datetime.now().isoformat())
            )
    except Exception as e:
        logger.error(f"Error guardando mensaje: {e}")


def get_history(phone, limit=8):
    """Devuelve los últimos mensajes (para contexto corto)."""
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                'SELECT message, role FROM conversations WHERE phone = ? ORDER BY timestamp DESC LIMIT ?',
                (phone, limit)
            )
            rows = cur.fetchall()
            return list(reversed(rows))
    except Exception as e:
        logger.error(f"Error leyendo historial: {e}")
        return []


def get_history_today(phone, limit=12):
    """Devuelve solo los mensajes del día actual (para memoria diaria)."""
    try:
        today_prefix = datetime.now().strftime("%Y-%m-%d")
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                'SELECT message, role FROM conversations WHERE phone = ? AND substr(timestamp,1,10)=? ORDER BY timestamp ASC LIMIT ?',
                (phone, today_prefix, limit)
            )
            return cur.fetchall()
    except Exception as e:
        logger.error(f"Error leyendo historial diario: {e}")
        return []


# -----------------------------------
# ESTADO DEL USUARIO
# -----------------------------------
def save_user_state(phone, prod):
    """Guarda el último producto o contexto mencionado."""
    try:
        with get_db_connection() as conn:
            conn.execute(
                '''INSERT INTO user_state (phone, last_code, last_name, last_price_ars, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(phone) DO UPDATE SET
                       last_code=excluded.last_code,
                       last_name=excluded.last_name,
                       last_price_ars=excluded.last_price_ars,
                       updated_at=excluded.updated_at''',
                (
                    phone,
                    prod.get("code", ""),
                    prod.get("name", ""),
                    float(prod.get("price_ars", 0.0)),
                    datetime.now().isoformat()
                )  # 👈 Paréntesis final correcto
            )
    except Exception as e:
        logger.error(f"Error guardando user_state: {e}")


def get_user_state(phone):
    """Recupera el último contexto guardado (producto o acción previa)."""
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute(
                'SELECT last_code, last_name, last_price_ars, updated_at FROM user_state WHERE phone=?',
                (phone,)
            )
            row = cur.fetchone()
            if not row:
                return None
            return {
                "last_code": row[0],
                "last_name": row[1],
                "last_price_ars": row[2],
                "updated_at": row[3]
            }
    except Exception as e:
        logger.error(f"Error leyendo user_state: {e}")
        return None
