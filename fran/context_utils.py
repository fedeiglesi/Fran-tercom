"""
Utilidades para manejo de contexto y timestamps.

Fix para inconsistencia #4: Hydratación de contexto.
"""

from __future__ import annotations
import time
from datetime import datetime
from typing import Any, Dict


def parse_timestamp(ts_value: Any) -> float | None:
    """
    Parse timestamp robusto que maneja múltiples formatos.

    Soporta:
    - float/int: timestamp Unix directo
    - str ISO: "2025-12-02T10:30:00"
    - age_minutes: convierte a timestamp relativo

    Args:
        ts_value: Valor a parsear (float, int, str, None)

    Returns:
        Timestamp Unix (float) o None si no puede parsear

    Ejemplos:
        >>> parse_timestamp(1701518400.0)
        1701518400.0
        >>> parse_timestamp("2025-12-02T10:30:00")
        1733137800.0
        >>> parse_timestamp(30)  # 30 segundos atrás
        <timestamp actual - 30>
        >>> parse_timestamp(None)
        None
    """
    if ts_value is None:
        return None

    # Caso 1: Ya es timestamp Unix
    if isinstance(ts_value, (int, float)):
        ts = float(ts_value)
        # Validar que es razonable (entre 2020 y 2100)
        if 1577836800 <= ts <= 4102444800:  # 2020-01-01 to 2100-01-01
            return ts
        # Si es muy pequeño, asumimos es age_minutes
        if ts < 10000:  # Menor a 10k → probablemente age_minutes
            return time.time() - (ts * 60)
        return ts

    # Caso 2: String ISO
    if isinstance(ts_value, str):
        # Intentar parse ISO
        for fmt in [
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d",
        ]:
            try:
                dt = datetime.strptime(ts_value, fmt)
                return dt.timestamp()
            except ValueError:
                continue

        # Intentar convertir a float (por si es string numérico)
        try:
            return float(ts_value)
        except ValueError:
            pass

    return None


def is_context_expired(
    last_search_ts: float | None,
    last_products_ts: float | None,
    last_cart_ts: float | None,
    last_followup_ts: float | None,
    ttl_seconds: int = 1800,
) -> bool:
    """
    Determina si el contexto está expirado.

    Args:
        last_search_ts: Timestamp de última búsqueda (None si no hay)
        last_products_ts: Timestamp de últimos productos mostrados
        last_cart_ts: Timestamp de última acción de carrito
        last_followup_ts: Timestamp de último follow-up
        ttl_seconds: TTL en segundos (default: 1800 = 30 min)

    Returns:
        True si expiró, False si aún válido
    """
    timestamps = [
        ts for ts in [last_search_ts, last_products_ts, last_cart_ts, last_followup_ts]
        if ts is not None
    ]

    if not timestamps:
        return True  # No hay contexto → Expirado

    latest_ts = max(timestamps)
    age = time.time() - latest_ts
    return age > ttl_seconds


def build_context_from_search_history(search_history: Dict[str, Any]) -> Dict[str, Any]:
    """
    Construye contexto desde historial de búsqueda con hydratación robusta.

    Args:
        search_history: Dict con keys: query, products, metadata, age_minutes, timestamp

    Returns:
        Dict con last_search normalizado

    Fix para inconsistencia #4: Maneja múltiples formatos de timestamp.
    """
    if not search_history:
        return {}

    metadata = search_history.get("metadata", {})

    # Intentar obtener timestamp desde múltiples sources
    ts_raw = (
        metadata.get("timestamp")
        or search_history.get("timestamp")
        or search_history.get("age_minutes")
    )

    timestamp = parse_timestamp(ts_raw)

    return {
        "last_search": {
            "query": search_history.get("query", "").strip(),
            "results": search_history.get("products", []),
            "metadata": metadata,
            "timestamp": timestamp,
            "confidence": metadata.get("confidence", 0.0),
        }
    }
