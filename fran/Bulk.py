# coding: utf-8
"""
Módulo: bulk.py
Procesa cotizaciones masivas en Fran 3.8
-------------------------------------------------
Funciones:
- Detecta listas masivas en mensajes de WhatsApp
- Procesa todas las líneas (sin IA)
- Devuelve cotización con precios unitarios y totales
"""

import re
import time
from decimal import Decimal
from typing import List, Tuple, Dict
from fran.search import search_products
from fran.db import log_interaction
from fran.config import INSTANT_THRESHOLD, logger
from fran.utils import normalize_search_query, format_price, to_decimal_money


# =========================================================
# DETECCIÓN DE LISTAS MASIVAS
# =========================================================

def is_bulk_list_request(text: str) -> Tuple[bool, int]:
    """
    Detecta si un mensaje parece una lista masiva.
    Retorna (True, cantidad_items) si tiene varias líneas válidas.
    """
    if not text:
        return False, 0

    lines = [l.strip() for l in text.split("\n") if l.strip()]
    valid_lines = [l for l in lines if re.search(r"[A-Za-z0-9]", l)]

    if len(valid_lines) >= 3:
        logger.info(f"📋 Detectada lista masiva ({len(valid_lines)} ítems)")
        return True, len(valid_lines)
    return False, len(valid_lines)


# =========================================================
# PARSEO DE LISTA MASIVA
# =========================================================

def parse_bulk_list(text: str) -> List[str]:
    """
    Convierte texto multilinea en lista de consultas limpias.
    Ejemplo:
        "1179/00035-038\nTapa valvula ybr\nllave contacto"
    → ["1179/00035-038", "tapa valvula ybr", "llave contacto"]
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    return [normalize_search_query(l) for l in lines]


# =========================================================
# PROCESAMIENTO SINCRÓNICO
# =========================================================

def process_bulk_sync(phone: str, user_text: str) -> Dict[str, object]:
    """
    Procesa una lista corta (por debajo del INSTANT_THRESHOLD) en modo sincrónico.
    Retorna un diccionario con resultados listos para enviar al usuario.
    """
    try:
        start_time = time.time()
        lines = parse_bulk_list(user_text)
        results = []

        for line in lines:
            matches = search_products(line, top_k=1)
            if matches:
                p = matches[0]
                results.append({
                    "query": line,
                    "found": p["name"],
                    "code": p["code"],
                    "price_unit": float(p["price"]),
                })
            else:
                results.append({
                    "query": line,
                    "found": None,
                    "code": None,
                    "price_unit": 0.0,
                })

        total_items = sum(1 for r in results if r["found"])
        duration = round(time.time() - start_time, 2)

        log_interaction(phone, user_text, "bulk_quote", len(lines))

        logger.info(f"🧾 Lista masiva procesada ({total_items}/{len(lines)} encontrados) en {duration}s")

        total_usd = sum(r["price_unit"] for r in results if r["price_unit"])
        total_ars = to_decimal_money(total_usd * 1600)  # valor referencial para testeo

        return {
            "success": True,
            "duration": duration,
            "results": results,
            "total_usd": format_price(to_decimal_money(total_usd), "USD"),
            "total_ars": format_price(total_ars, "ARS"),
            "count_found": total_items,
        }

    except Exception as e:
        logger.error(f"❌ Error en process_bulk_sync: {e}")
        return {"success": False, "error": str(e), "results": []}


# =========================================================
# FORMATEO DE RESPUESTA
# =========================================================

def format_bulk_response(bulk_result: Dict[str, object]) -> str:
    """
    Convierte el resultado del proceso masivo en texto legible.
    """
    if not bulk_result.get("success"):
        return "⚠️ Ocurrió un error procesando la lista."

    lines = []
    for r in bulk_result["results"]:
        if r["found"]:
            lines.append(f"✅ {r['found']} ({r['code']}) — {format_price(r['price_unit'], 'USD')}")
        else:
            lines.append(f"❌ {r['query']} — no encontrado")

    lines.append("\n")
    lines.append(f"🧾 TOTAL: {bulk_result['total_usd']}  |  {bulk_result['total_ars']}")
    lines.append(f"⏱️ Tiempo: {bulk_result['duration']}s")
    return "\n".join(lines)
