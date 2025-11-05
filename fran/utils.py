# coding: utf-8
"""
Módulo de utilidades generales para Fran 3.8

Incluye:
- Normalización de texto y códigos
- Limpieza y sanitización de entrada
- Manejo de precios y decimales
- Validación de códigos Tercom
- Detección de duplicados en ventana corta
"""

import re
import unicodedata
import time
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from threading import Lock
from typing import Optional
from fran.config import logger, DEDUP_WINDOW


# =========================================================
# LIMPIEZA DE TEXTO
# =========================================================

def strip_accents(text: str) -> str:
    """Elimina tildes y acentos del texto."""
    if not isinstance(text, str):
        return text
    return "".join(
        c for c in unicodedata.normalize("NFD", text)
        if unicodedata.category(c) != "Mn"
    )


def normalize_whitespace(text: str) -> str:
    """Reemplaza múltiples espacios por uno solo y recorta extremos."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def sanitize_input(text: str) -> str:
    """Limpia entrada del usuario de caracteres peligrosos."""
    if not text:
        return ""
    text = text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r"[^\w\s\-\./%&]", "", text)
    return normalize_whitespace(text.lower())


def normalize_search_query(q: str) -> str:
    """Normaliza consultas de búsqueda (acentos, mayúsculas, símbolos)."""
    q = sanitize_input(strip_accents(q))
    q = re.sub(r"[,;:]+", " ", q)
    return normalize_whitespace(q)


# =========================================================
# VALIDACIÓN DE CÓDIGOS Y CAMPOS
# =========================================================

def validate_tercom_code(code: str) -> bool:
    """Valida formato de código Tercom (ej: 1179/00035-038)."""
    if not code:
        return False
    return bool(re.match(r"^\d{3,5}/\d{3,6}-\d{2,3}$", code))


def normalize_code(code: str) -> str:
    """Quita espacios y convierte código a formato estándar."""
    if not code:
        return ""
    return strip_accents(code).upper().replace(" ", "")


def is_valid_price(value: str) -> bool:
    """Chequea si un valor puede convertirse a Decimal válido."""
    try:
        Decimal(value)
        return True
    except (InvalidOperation, TypeError):
        return False


# =========================================================
# FORMATO DE PRECIOS
# =========================================================

def to_decimal_money(value) -> Decimal:
    """Convierte un string o float a Decimal redondeado a 2 decimales."""
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError):
        return Decimal("0.00")


def format_price(value: Decimal, currency: str = "ARS") -> str:
    """Devuelve precio formateado con separador de miles y símbolo."""
    try:
        v = Decimal(value)
    except (InvalidOperation, TypeError):
        return "0.00"
    if currency.upper() in ["USD", "U$D"]:
        return f"USD {v:,.2f}".replace(",", ".")
    else:
        return f"${v:,.2f}".replace(",", ".")


def parse_price_str(text: str) -> Optional[Decimal]:
    """Extrae valor decimal de un string de precio (soporta 1.250,75 y 1250.50)."""
    if not text:
        return None
    # Eliminar simbolos de moneda y espacios
    text = text.replace("$", "").replace("USD", "").strip()
    text = re.sub(r"[^\d.,-]", "", text)

    # Caso 1: hay coma y al menos un punto → asumir formato ARS: 1.250,75
    if "," in text and "." in text:
        # Eliminar puntos (miles), convertir coma a punto (decimal)
        text = text.replace(".", "").replace(",", ".")
    # Caso 2: solo hay coma → podría ser decimal (ej: 12,50)
    elif "," in text and text.count(",") == 1:
        text = text.replace(",", ".")
    # Caso 3: solo puntos → ya está en formato decimal (ej: 1250.75)
    # (no hacemos nada)

    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None

# =========================================================
# DEDUPLICACIÓN DE MENSAJES
# =========================================================

_last_messages = {}
_last_lock = Lock()


def is_duplicate_message(phone: str, message: str) -> bool:
    """Evita procesar el mismo mensaje dos veces en pocos segundos."""
    now = time.time()
    with _last_lock:
        prev = _last_messages.get(phone)
        if prev and prev["msg"] == message and now - prev["ts"] < DEDUP_WINDOW:
            logger.info(f"🟡 Mensaje duplicado ignorado ({phone}): {message}")
            return True
        _last_messages[phone] = {"msg": message, "ts": now}
    return False


# =========================================================
# CONVERSIÓN DE UNIDADES / FORMATOS
# =========================================================

def safe_int(value, default=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_float(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def ellipsis(text: str, max_len: int = 80) -> str:
    """Trunca texto largo con puntos suspensivos."""
    if not text:
        return ""
    return text if len(text) <= max_len else text[: max_len - 3] + "..."


def percent_diff(a: Decimal, b: Decimal) -> str:
    """Devuelve el porcentaje de diferencia entre dos valores."""
    try:
        if b == 0:
            return "0%"
        diff = (a - b) / b * 100
        return f"{diff:.1f}%"
    except Exception:
        return "0%"
