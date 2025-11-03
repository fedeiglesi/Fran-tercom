# coding: utf-8
"""
Módulo de búsqueda híbrida (Fran 3.8)

Combina:
- Búsqueda semántica (FAISS)
- Búsqueda por similitud de texto (RapidFuzz)
- Limpieza y alias automáticos
"""

from rapidfuzz import process, fuzz
from typing import List, Dict
from fran.catalog import get_catalog_and_index, search_catalog
from fran.utils import normalize_search_query, strip_accents
from fran.config import MAX_SEARCH_RESULTS, logger


# =========================================================
# ALIAS Y NORMALIZACIÓN
# =========================================================

SEARCH_ALIASES = {
    "ybr": "yamaha ybr",
    "cg": "honda cg",
    "cb": "honda cb",
    "xr": "honda xr",
    "glh": "honda glh",
    "motul": "aceite motul",
    "xmax": "yamaha xmax",
    "tapa valvula": "tapa registro valvula",
    "llave": "llave contacto",
}


def expand_aliases(query: str) -> str:
    """Reemplaza abreviaturas o alias comunes en la consulta."""
    q = query.lower()
    for alias, full in SEARCH_ALIASES.items():
        if alias in q:
            q = q.replace(alias, full)
    return q


# =========================================================
# BÚSQUEDA FUZZY
# =========================================================

def fuzzy_search_catalog(query: str, catalog: List[Dict[str, str]], limit: int = 50) -> List[Dict[str, str]]:
    """Búsqueda por similitud de texto usando RapidFuzz."""
    names = [p["name"] for p in catalog]
    results = process.extract(
        query,
        names,
        scorer=fuzz.WRatio,
        limit=limit,
    )

    found = []
    for name, score, idx in results:
        if score > 70:
            item = catalog[idx].copy()
            item["score"] = score / 100
            found.append(item)
    return found


# =========================================================
# BÚSQUEDA HÍBRIDA (FAISS + FUZZY)
# =========================================================

def hybrid_search(query: str, top_k: int = 50) -> List[Dict[str, str]]:
    """
    Combina FAISS (búsqueda semántica) + RapidFuzz (texto).
    Devuelve los resultados más relevantes ordenados por score.
    """
    if not query or len(query.strip()) < 2:
        return []

    query_norm = normalize_search_query(expand_aliases(strip_accents(query)))
    catalog, _, _ = get_catalog_and_index()

    # 1️⃣ FAISS (semántico)
    semantic_results = search_catalog(query_norm, top_k=top_k)
    # 2️⃣ Fuzzy (texto)
    fuzzy_results = fuzzy_search_catalog(query_norm, catalog, limit=top_k)

    # 3️⃣ Unimos y normalizamos
    merged = {}
    for r in semantic_results + fuzzy_results:
        code = r.get("code", "")
        if code not in merged:
            merged[code] = r
        else:
            merged[code]["score"] = max(merged[code]["score"], r["score"])

    combined = sorted(merged.values(), key=lambda x: x["score"], reverse=True)
    return combined[:MAX_SEARCH_RESULTS]


# =========================================================
# FUNCIÓN PRINCIPAL DE BUSCA
# =========================================================

def search_products(user_query: str, top_k: int = 50) -> List[Dict[str, str]]:
    """Busca productos en el catálogo combinando FAISS y fuzzy."""
    try:
        results = hybrid_search(user_query, top_k=top_k)
        if not results:
            logger.info(f"🔍 Sin resultados para: {user_query}")
        return results
    except Exception as e:
        logger.error(f"❌ Error en search_products: {e}")
        return []


# =========================================================
# TEST MANUAL (opcional)
# =========================================================

if __name__ == "__main__":
    q = input("🔎 Buscar producto: ")
    results = search_products(q, 10)
    for i, r in enumerate(results, start=1):
        print(f"{i}. {r['code']} - {r['name']} (${r['price']}) [{r['score']:.2f}]")
