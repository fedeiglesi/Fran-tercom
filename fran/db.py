# coding: utf-8
"""
Módulo de base de datos (SQLite) para Fran 3.8

Incluye:
- Conexión y contexto seguro con SQLite
- Creación automática de tablas
- Guardado de mensajes e interacciones
- Log de rendimiento y estadísticas
"""

import sqlite3
import time
import threading
from contextlib import contextmanager
from datetime import datetime
from typing import Optional
from fran.config import DB_PATH, logger, DEBUG_SQL


# =========================================================
# CONEXIÓN A BASE DE DATOS
# =========================================================

_db_lock = threading.Lock()

@contextmanager
def get_db_connection():
    """Crea una conexión SQLite con cierre automático."""
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


# =========================================================
# INICIALIZACIÓN DE TABLAS
# =========================================================

def init_db():
    """Crea las tablas si no existen."""
    with get_db_connection() as conn:
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT,
                role TEXT,
                message TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS carts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT,
                code TEXT,
                name TEXT,
                price REAL,
                qty INTEGER DEFAULT 1,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT,
                message TEXT,
                intent_detected TEXT,
                count INTEGER DEFAULT 1,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT,
                action TEXT,
                duration REAL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()
        logger.info("✅ Tablas creadas o verificadas correctamente")


# =========================================================
# GUARDADO DE MENSAJES
# =========================================================

def save_message(phone: str, message: str, role: str):
    """Guarda un mensaje del usuario o del bot en la tabla conversations."""
    try:
        with _db_lock, get_db_connection() as conn:
            conn.execute(
                "INSERT INTO conversations (phone, role, message) VALUES (?, ?, ?)",
                (phone, role, message),
            )
            conn.commit()
            if DEBUG_SQL:
                logger.info(f"[DB] Saved message ({role}) for {phone}")
    except Exception as e:
        logger.error(f"❌ Error guardando mensaje ({phone}): {e}")


# =========================================================
# REGISTRO DE INTERACCIONES
# =========================================================

def log_interaction(phone: str, message: str, intent: str, count: int = 1):
    """Guarda una interacción analítica con el intent detectado."""
    try:
        with _db_lock, get_db_connection() as conn:
            conn.execute(
                "INSERT INTO interactions (phone, message, intent_detected, count) VALUES (?, ?, ?, ?)",
                (phone, message, intent, count),
            )
            conn.commit()
            if DEBUG_SQL:
                logger.info(f"[DB] Logged interaction: {intent} ({phone})")
    except Exception as e:
        logger.error(f"❌ Error log_interaction ({phone}): {e}")


# =========================================================
# LOG DE RENDIMIENTO
# =========================================================

def log_performance(phone: str, action: str, start_time: float):
    """Registra la duración de una acción específica."""
    try:
        duration = round(time.time() - start_time, 3)
        with _db_lock, get_db_connection() as conn:
            conn.execute(
                "INSERT INTO performance (phone, action, duration) VALUES (?, ?, ?)",
                (phone, action, duration),
            )
            conn.commit()
        if DEBUG_SQL:
            logger.info(f"[PERF] {action} ({duration}s) -> {phone}")
    except Exception as e:
        logger.error(f"❌ Error log_performance: {e}")


# =========================================================
# FUNCIONES DE CONSULTA AUXILIARES
# =========================================================

def get_last_messages(phone: str, limit: int = 10):
    """Devuelve los últimos mensajes de un usuario."""
    with get_db_connection() as conn:
        cur = conn.execute(
            "SELECT role, message, timestamp FROM conversations WHERE phone = ? ORDER BY id DESC LIMIT ?",
            (phone, limit),
        )
        return [dict(row) for row in cur.fetchall()]


def get_cart(phone: str):
    """Obtiene los productos actuales del carrito de un usuario."""
    with get_db_connection() as conn:
        cur = conn.execute(
            "SELECT code, name, price, qty FROM carts WHERE phone = ? ORDER BY id DESC",
            (phone,),
        )
        return [dict(row) for row in cur.fetchall()]


def clear_cart(phone: str):
    """Vacía el carrito de un usuario."""
    with get_db_connection() as conn:
        conn.execute("DELETE FROM carts WHERE phone = ?", (phone,))
        conn.commit()


# =========================================================
# AUTO-EJECUCIÓN AL IMPORTAR
# =========================================================

try:
    init_db()
    logger.info("🧠 Módulo DB inicializado correctamente")

except Exception as e:
    logger.warning(f"⚠️ No se pudo inicializar DB: {e}")
