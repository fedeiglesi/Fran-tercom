"""
Abstracción de base de datos para soportar SQLite y PostgreSQL.

Detecta automáticamente DATABASE_URL (PostgreSQL) o usa SQLite local.
Proporciona un wrapper de conexión que adapta queries automáticamente.
"""

import os
import logging
import re
from contextlib import contextmanager
from typing import Any, Generator

logger = logging.getLogger("fran313")

# Detectar tipo de base de datos
DATABASE_URL = os.environ.get("DATABASE_URL", "")
DB_PATH = os.environ.get("DB_PATH", "tercom.db")

# Determinar backend
if DATABASE_URL and DATABASE_URL.startswith(("postgres://", "postgresql://")):
    DB_TYPE = "postgresql"
    # Railway usa postgres:// pero psycopg2 requiere postgresql://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
else:
    DB_TYPE = "sqlite"

logger.info(f"[Database] Usando backend: {DB_TYPE}")


class PostgresCursorWrapper:
    """
    Wrapper para cursor PostgreSQL que convierte placeholders ? a %s automáticamente.
    Esto permite usar la misma sintaxis SQLite en todo el código.
    """

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, query, params=None):
        # Convertir ? a %s para PostgreSQL
        adapted_query = re.sub(r'\?', '%s', query)
        if params:
            return self._cursor.execute(adapted_query, params)
        return self._cursor.execute(adapted_query)

    def executemany(self, query, params_list):
        adapted_query = re.sub(r'\?', '%s', query)
        return self._cursor.executemany(adapted_query, params_list)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchmany(self, size=None):
        if size:
            return self._cursor.fetchmany(size)
        return self._cursor.fetchmany()

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def description(self):
        return self._cursor.description

    def close(self):
        return self._cursor.close()

    def __iter__(self):
        return iter(self._cursor)


class PostgresConnectionWrapper:
    """
    Wrapper para conexión PostgreSQL que:
    - Proporciona cursores con conversión automática de placeholders
    - Permite acceso tipo dict a las filas (como sqlite3.Row)
    """

    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return PostgresCursorWrapper(self._conn.cursor())

    def execute(self, query, params=None):
        cursor = self.cursor()
        cursor.execute(query, params)
        return cursor

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    @property
    def row_factory(self):
        return None

    @row_factory.setter
    def row_factory(self, value):
        pass  # Ignorar, psycopg2 usa RealDictCursor


def _get_sqlite_connection():
    """Obtiene conexión SQLite."""
    import sqlite3

    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def _get_postgres_connection():
    """Obtiene conexión PostgreSQL con wrapper."""
    import psycopg2
    from psycopg2.extras import RealDictCursor

    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    return PostgresConnectionWrapper(conn)


def get_connection():
    """Obtiene una conexión a la base de datos (SQLite o PostgreSQL)."""
    if DB_TYPE == "postgresql":
        return _get_postgres_connection()
    return _get_sqlite_connection()


@contextmanager
def get_db_connection() -> Generator[Any, None, None]:
    """Context manager para conexión a base de datos con manejo de errores."""
    conn = None
    max_retries = 3

    for attempt in range(max_retries):
        try:
            conn = get_connection()
            yield conn
            conn.commit()
            return
        except Exception as e:
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            if attempt < max_retries - 1:
                import time
                time.sleep(0.5 * (attempt + 1))
                logger.warning(f"[Database] Retry {attempt + 1}/{max_retries}: {e}")
            else:
                logger.error(f"[Database] Error después de {max_retries} intentos: {e}")
                raise
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass


def _create_tables_sqlite(cursor):
    """Crea tablas para SQLite."""
    try:
        cursor.execute("PRAGMA journal_mode=WAL;")
    except Exception as e:
        logger.warning(f"No se pudo activar WAL: {e}")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            phone TEXT, message TEXT, role TEXT, timestamp TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_conv_phone_timestamp ON conversations(phone, timestamp DESC)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS carts (
            phone TEXT, code TEXT, quantity INTEGER, name TEXT,
            price_ars TEXT, price_usd TEXT, created_at TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cart_phone ON carts(phone)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            phone TEXT PRIMARY KEY, last_code TEXT, last_name TEXT,
            last_price_ars TEXT, updated_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS search_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT, products_json TEXT, query TEXT, timestamp TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_search_phone_timestamp ON search_history(phone, timestamp DESC)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS last_search (
            phone TEXT PRIMARY KEY, products_json TEXT, query TEXT, timestamp TEXT, metadata TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY, phone TEXT, customer_name TEXT,
            customer_address TEXT, items_json TEXT, total_ars TEXT,
            status TEXT, created_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bulk_jobs (
            job_id TEXT PRIMARY KEY, phone TEXT, raw_list TEXT,
            total_items INTEGER, processed_items INTEGER, found_items INTEGER,
            results_json TEXT, status TEXT, created_at TEXT, completed_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS interactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT, message TEXT, intent_detected TEXT,
            products_count INTEGER, timestamp TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_interactions_phone_timestamp ON interactions(phone, timestamp DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_interactions_intent ON interactions(intent_detected)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS performance_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT, intent TEXT, duration_ms INTEGER,
            results_count INTEGER, timestamp TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quality_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT,
            query TEXT,
            avg_score REAL,
            max_score REAL,
            relevant_count INTEGER,
            created_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_actions (
            phone TEXT PRIMARY KEY,
            action_type TEXT,
            action_data TEXT,
            context TEXT,
            created_at TEXT,
            expires_at TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_actions_expires ON pending_actions(expires_at)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversation_phase (
            phone TEXT PRIMARY KEY,
            phase TEXT,
            updated_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS moto_context (
            phone TEXT PRIMARY KEY,
            brand TEXT,
            model TEXT,
            updated_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS template_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT,
            template_name TEXT,
            input_json TEXT,
            output_json TEXT,
            duration_ms INTEGER,
            created_at TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_template_logs_phone_created ON template_logs(phone, created_at DESC)")


def _create_tables_postgres(cursor):
    """Crea tablas para PostgreSQL."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id SERIAL PRIMARY KEY,
            phone TEXT, message TEXT, role TEXT, timestamp TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_conv_phone_timestamp ON conversations(phone, timestamp DESC)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS carts (
            id SERIAL PRIMARY KEY,
            phone TEXT, code TEXT, quantity INTEGER, name TEXT,
            price_ars TEXT, price_usd TEXT, created_at TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_cart_phone ON carts(phone)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_state (
            phone TEXT PRIMARY KEY, last_code TEXT, last_name TEXT,
            last_price_ars TEXT, updated_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS search_history (
            id SERIAL PRIMARY KEY,
            phone TEXT, products_json TEXT, query TEXT, timestamp TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_search_phone_timestamp ON search_history(phone, timestamp DESC)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS last_search (
            phone TEXT PRIMARY KEY, products_json TEXT, query TEXT, timestamp TEXT, metadata TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY, phone TEXT, customer_name TEXT,
            customer_address TEXT, items_json TEXT, total_ars TEXT,
            status TEXT, created_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bulk_jobs (
            job_id TEXT PRIMARY KEY, phone TEXT, raw_list TEXT,
            total_items INTEGER, processed_items INTEGER, found_items INTEGER,
            results_json TEXT, status TEXT, created_at TEXT, completed_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS interactions (
            id SERIAL PRIMARY KEY,
            phone TEXT, message TEXT, intent_detected TEXT,
            products_count INTEGER, timestamp TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_interactions_phone_timestamp ON interactions(phone, timestamp DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_interactions_intent ON interactions(intent_detected)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS performance_metrics (
            id SERIAL PRIMARY KEY,
            phone TEXT, intent TEXT, duration_ms INTEGER,
            results_count INTEGER, timestamp TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS quality_metrics (
            id SERIAL PRIMARY KEY,
            phone TEXT,
            query TEXT,
            avg_score REAL,
            max_score REAL,
            relevant_count INTEGER,
            created_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_actions (
            phone TEXT PRIMARY KEY,
            action_type TEXT,
            action_data TEXT,
            context TEXT,
            created_at TEXT,
            expires_at TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_pending_actions_expires ON pending_actions(expires_at)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversation_phase (
            phone TEXT PRIMARY KEY,
            phase TEXT,
            updated_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS moto_context (
            phone TEXT PRIMARY KEY,
            brand TEXT,
            model TEXT,
            updated_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS template_logs (
            id SERIAL PRIMARY KEY,
            phone TEXT,
            template_name TEXT,
            input_json TEXT,
            output_json TEXT,
            duration_ms INTEGER,
            created_at TEXT
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_template_logs_phone_created ON template_logs(phone, created_at DESC)")


def init_database():
    """
    Inicializa la base de datos creando todas las tablas necesarias.
    Detecta automáticamente si usar SQLite o PostgreSQL.
    """
    logger.info(f"[Database] Inicializando base de datos ({DB_TYPE})...")

    with get_db_connection() as conn:
        cursor = conn.cursor()

        if DB_TYPE == "postgresql":
            _create_tables_postgres(cursor)
        else:
            _create_tables_sqlite(cursor)

        conn.commit()

    logger.info(f"[Database] Base de datos inicializada correctamente ({DB_TYPE})")


def get_placeholder():
    """Retorna el placeholder correcto según el backend (%s para Postgres, ? para SQLite)."""
    return "%s" if DB_TYPE == "postgresql" else "?"


def adapt_query(query: str) -> str:
    """
    Adapta una query SQLite para PostgreSQL si es necesario.
    Convierte ? a %s para PostgreSQL.

    Nota: El PostgresConnectionWrapper hace esto automáticamente,
    pero esta función está disponible para casos especiales.
    """
    if DB_TYPE == "postgresql":
        return re.sub(r'\?', '%s', query)
    return query
