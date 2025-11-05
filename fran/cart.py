# coding: utf-8
"""
Módulo de carrito de compras (Fran 3.8)

Funciones:
- Agregar productos (sin race conditions)
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
        # ✅ Usamos INSERT OR IGNORE + UPDATE para evitar race conditions
        cur.execute("""
            INSERT OR IGNORE INTO carts (phone, code, name, price, qty)
            VALUES (?, ?, ?, ?, ?)
        """, (phone, code, name, price, qty))
        
        cur.execute("""
            UPDATE carts SET qty = qty + ? 
            WHERE phone = ? AND code = ? AND (SELECT qty FROM carts WHERE phone = ? AND code = ?) > 0
        """, (qty, phone, code, phone, code))
        
        conn.commit()
        logger.info(f"🛒 Producto agregado/actualizado: {code} x{qty} para {phone}")


def cart_update_qty(phone: str, code: str, qty: int):
    """Actualiza manualmente la cantidad de un producto."""
    if qty <= 0:
        return cart_remove(phone, code)
    with cart_lock, get_db_connection() as conn:
        conn.execute("UPDATE carts SET qty = ? WHERE phone = ? AND code = ?", (qty, phone, code))
        conn.commit()
        logger.info(f"🔢 Cantidad actualizada {code} = {qty} para {phone}")


def cart_remove(phone: str, code: str):
    """Elimina un producto del carrito."""
    with cart_lock, get_db_connection() as conn:
        conn.execute("DELETE FROM carts WHERE phone = ? AND code = ?", (phone, code))
        conn.commit()
        logger.info(f"❌ Producto eliminado: {code} para {phone}")


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
        lines.append(f"• {p['name']} ({p['code']}) x{p['qty']} — {format_price(subtotal, 'USD')}")

    lines.append(f"\nTOTAL: {format_price(total, 'USD')}")
    return "\n".join(lines)
