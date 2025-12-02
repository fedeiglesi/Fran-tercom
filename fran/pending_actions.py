"""
Sistema de pending actions con queue para evitar race conditions.

Fix para inconsistencia #5: Race condition en pending actions.
"""

from __future__ import annotations
import json
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List
from contextlib import contextmanager

logger = logging.getLogger("fran313")


class PendingActionsQueue:
    """
    Queue de pending actions con soporte para múltiples acciones por usuario.

    Mejoras sobre sistema anterior:
    - Múltiples acciones pending por usuario (no overwrite)
    - Orden FIFO
    - TTL individual por acción
    - Cleanup automático de acciones expiradas
    """

    def __init__(self, db_path: str):
        """
        Args:
            db_path: Path a base de datos SQLite
        """
        self.db_path = db_path
        self._memory_connection = None
        self._is_memory = db_path == ":memory:"

        if self._is_memory:
            # Mantener una única conexión en memoria para que la tabla exista
            # durante toda la vida útil de la instancia.
            self._memory_connection = sqlite3.connect(
                ":memory:",
                timeout=10.0,
            )
            self._memory_connection.row_factory = sqlite3.Row

        self._ensure_table()

    @contextmanager
    def _get_connection(self):
        """Context manager para conexión DB."""
        if self._is_memory:
            conn = self._memory_connection
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        else:
            conn = sqlite3.connect(self.db_path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _ensure_table(self):
        """Crea tabla de pending_actions_queue si no existe."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_actions_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT NOT NULL,
                    action_type TEXT NOT NULL,
                    action_data TEXT NOT NULL,
                    context TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    processed BOOLEAN DEFAULT 0,
                    processed_at TEXT
                )
            """)
            # Índices para performance
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_pending_phone_processed
                ON pending_actions_queue(phone, processed)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_pending_expires
                ON pending_actions_queue(expires_at)
            """)

    def add(
        self,
        phone: str,
        action_type: str,
        action_data: Dict[str, Any],
        context: str = "",
        ttl_minutes: int = 30,
    ) -> int:
        """
        Agrega una pending action a la queue.

        Args:
            phone: Teléfono del usuario
            action_type: Tipo de acción (ej: "add_each_quantity", "stock_check")
            action_data: Data de la acción (dict)
            context: Contexto textual (ej: mensaje del usuario)
            ttl_minutes: TTL en minutos (default: 30)

        Returns:
            ID de la acción creada

        Diferencia con sistema anterior:
        - NO sobrescribe acciones existentes
        - Permite múltiples pending actions por usuario
        """
        now = datetime.now()
        expires_at = now + timedelta(minutes=ttl_minutes)

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO pending_actions_queue
                (phone, action_type, action_data, context, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    phone,
                    action_type,
                    json.dumps(action_data, ensure_ascii=False),
                    context,
                    now.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            action_id = cursor.lastrowid

        logger.info(
            f"Added pending action {action_id}: {action_type} for {phone} "
            f"(expires in {ttl_minutes} min)"
        )
        return action_id

    def get_next(self, phone: str) -> Dict[str, Any] | None:
        """
        Obtiene la siguiente pending action no procesada para un usuario.

        Args:
            phone: Teléfono del usuario

        Returns:
            Dict con action_type, action_data, context, id
            o None si no hay pending actions

        Orden: FIFO (oldest first)
        Auto-limpieza: Elimina acciones expiradas
        """
        self._cleanup_expired()

        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT id, action_type, action_data, context, created_at
                FROM pending_actions_queue
                WHERE phone = ?
                  AND processed = 0
                  AND expires_at > ?
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (phone, datetime.now().isoformat()),
            ).fetchone()

            if not row:
                return None

            return {
                "id": row["id"],
                "action_type": row["action_type"],
                "action_data": json.loads(row["action_data"]),
                "context": row["context"],
                "created_at": row["created_at"],
            }

    def get_all(self, phone: str, include_processed: bool = False) -> List[Dict[str, Any]]:
        """
        Obtiene todas las pending actions de un usuario.

        Args:
            phone: Teléfono del usuario
            include_processed: Si True, incluye acciones ya procesadas

        Returns:
            Lista de dicts con action_type, action_data, etc.
        """
        self._cleanup_expired()

        processed_filter = "" if include_processed else "AND processed = 0"

        with self._get_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT id, action_type, action_data, context, created_at, processed
                FROM pending_actions_queue
                WHERE phone = ?
                  {processed_filter}
                  AND expires_at > ?
                ORDER BY created_at ASC
                """,
                (phone, datetime.now().isoformat()),
            ).fetchall()

            return [
                {
                    "id": row["id"],
                    "action_type": row["action_type"],
                    "action_data": json.loads(row["action_data"]),
                    "context": row["context"],
                    "created_at": row["created_at"],
                    "processed": bool(row["processed"]),
                }
                for row in rows
            ]

    def mark_processed(self, action_id: int):
        """
        Marca una acción como procesada.

        Args:
            action_id: ID de la acción a marcar
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE pending_actions_queue
                SET processed = 1, processed_at = ?
                WHERE id = ?
                """,
                (datetime.now().isoformat(), action_id),
            )

        logger.info(f"Marked action {action_id} as processed")

    def clear(self, phone: str):
        """
        Elimina todas las pending actions de un usuario (procesadas y no procesadas).

        Args:
            phone: Teléfono del usuario
        """
        with self._get_connection() as conn:
            conn.execute(
                "DELETE FROM pending_actions_queue WHERE phone = ?",
                (phone,),
            )

        logger.info(f"Cleared all pending actions for {phone}")

    def _cleanup_expired(self):
        """Elimina pending actions expiradas."""
        with self._get_connection() as conn:
            result = conn.execute(
                "DELETE FROM pending_actions_queue WHERE expires_at < ?",
                (datetime.now().isoformat(),),
            )
            if result.rowcount > 0:
                logger.debug(f"Cleaned up {result.rowcount} expired pending actions")

    def count_pending(self, phone: str) -> int:
        """
        Cuenta pending actions no procesadas de un usuario.

        Args:
            phone: Teléfono del usuario

        Returns:
            Número de acciones pendientes
        """
        self._cleanup_expired()

        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) as cnt
                FROM pending_actions_queue
                WHERE phone = ?
                  AND processed = 0
                  AND expires_at > ?
                """,
                (phone, datetime.now().isoformat()),
            ).fetchone()

            return row["cnt"]


# ============================================================
# SINGLETON GLOBAL
# ============================================================

_GLOBAL_PENDING_ACTIONS: PendingActionsQueue | None = None


def get_pending_actions_queue(db_path: str = "tercom.db") -> PendingActionsQueue:
    """
    Obtiene queue de pending actions global (singleton).

    Args:
        db_path: Path a DB SQLite

    Returns:
        PendingActionsQueue configurado
    """
    global _GLOBAL_PENDING_ACTIONS

    if _GLOBAL_PENDING_ACTIONS is None:
        _GLOBAL_PENDING_ACTIONS = PendingActionsQueue(db_path)

    return _GLOBAL_PENDING_ACTIONS
