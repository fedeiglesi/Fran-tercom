# coding: utf-8
"""
Módulo de carrito de compras (Fran 3.8)

Funciones:
- Agregar productos
- Eliminar / actualizar cantidades
- Consultar y limpiar carrito
- Calcular totales
"""

import threading
from decimal import Decimal
from typing import List, Dict
from fran.db import get_db_connection
from fran.utils import format_price, to_decimal_money
from fran.config import logger

# Lock global para evitar conflictos entre hilos
cart_lock = threading.Lock()


# =========================================================
# OPERACIONES BÁSICAS DE CARRITO
# =========================================================

def cart_add(phone: str, code: str, name: str, price: float, qty: int = 1):
    """Agrega un producto al carrito, o incrementa cantidad si ya existe."""
    with cart_lock, get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, qty FROM carts WHERE phone = ? AND code = ?",
            (phone, code)
        )
        row = cur.fetchone()
        if row:
            new_qty = row["qty"] + qty
            cur.execute("UPDATE carts SET qty = ? WHERE id = ?", (new_qty, row["id"]))
            logger.info(f"🛒 Cantidad actualizada: {code} x{new_qty}")
        else:
            cur.execute(
                "INSERT INTO carts (phone, code, name, price, qty) VALUES (?, ?, ?, ?, ?)",
                (phone, code, name, price, qty),
            )
            logger.info(f"🛒 Producto agregado: {code} x{qty}")
        conn.commit()


def cart_update_qty(phone: str, code: str, qty: int):
    """Actualiza manualmente la cantidad de un producto."""
    if qty <= 0:
        return cart_remove(phone, code)
    with cart_lock, get_db_connection() as conn:
        conn.execute("UPDATE carts SET qty = ? WHERE phone = ? AND code = ?", (qty, phone, code))
        conn.commit()
        logger.info(f"🔢 Cantidad actualizada {code} = {qty}")


def cart_remove(phone: str, code: str):
    """Elimina un producto del carrito."""
    with cart_lock, get_db_connection() as conn:
        conn.execute("DELETE FROM carts WHERE phone = ? AND code = ?", (phone, code))
        conn.commit()
        logger.info(f"❌ Producto eliminado: {code}")


def cart_clear(phone: str):
    """Vacía el carrito completamente."""
    with cart_lock, get_db_connection() as conn:
        conn.execute("DELETE FROM carts WHERE phone = ?", (phone,))
        conn.commit()
        logger.info(f"🧹 Carrito limpiado ({phone})")


# =========================================================
# CONSULTAS Y TOTALES
# =========================================================

def cart_get(phone: str) -> List[Dict[str, str]]:
    """Obtiene los productos actuales del carrito."""
    with get_db_connection() as conn:
        cur = conn.execute(
            "SELECT code, name, price, qty FROM carts WHERE phone = ? ORDER BY id DESC",
            (phone,),
        )
        return [dict(row) for row in cur.fetchall()]


def cart_totals(phone: str) -> Dict[str, str]:
    """Calcula el total del carrito."""
    with get_db_connection() as conn:
        cur = conn.execute(
            "SELECT SUM(price * qty) as total, SUM(qty) as cantidad FROM carts WHERE phone = ?",
            (phone,),
        )
        row = cur.fetchone()
        total = to_decimal_money(row["total"] or 0)
        cantidad = int(row["cantidad"] or 0)
        return {"total": format_price(total, "USD"), "cantidad": cantidad}


# =========================================================
# FORMATEO DE CARRITO PARA RESPUESTA
# =========================================================

def cart_summary_text(phone: str) -> str:
    """Devuelve texto formateado con el contenido del carrito."""
    items = cart_get(phone)
    if not items:
        return "🛒 Tu carrito está vacío."

    lines = []
    total = Decimal("0.00")
    for p in items:
        subtotal = Decimal(str(p["price"])) * p["qty"]
        total += subtotal
        lines.append(f"• {p['name']} ({p['code']}) x{p['qty']} — ${subtotal:.2f}")

    lines.append(f"\nTOTAL: ${total:.2f}")
    return "\n".join(lines)
