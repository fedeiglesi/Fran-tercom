"""
Utilidades compartidas para búsqueda entre versiones de Fran.

Este módulo centraliza funciones de búsqueda, normalización y RRF
para garantizar coherencia entre v3.16 y v3.17.
"""

from __future__ import annotations
import re
import unicodedata
import numpy as np
from typing import List, Dict, Any, Tuple
from decimal import Decimal


# ============================================================
# NORMALIZACIÓN UNIFICADA
# ============================================================

def normalize_text(text: str) -> str:
    """
    Normalización estándar de texto.

    Procesos:
    1. Lowercase
    2. Quita acentos (NFD normalization)
    3. Remueve caracteres especiales
    4. Colapsa espacios

    Usado en: queries, catálogo, compatibilidad
    """
    if not text:
        return ""

    text = text.lower().strip()
    # NFD normalization + quitar marcas diacríticas
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    # Mantener solo alfanuméricos, espacios, /, -, .
    text = re.sub(r"[^a-z0-9\s/.-]+", " ", text)
    # Colapsar espacios
    return " ".join(text.split())


def normalize_query_noise(text: str, deduplicate: bool = True) -> str:
    """
    Reduce repeticiones y ruido en queries.

    Args:
        text: Texto a normalizar
        deduplicate: Si True, elimina tokens duplicados adyacentes y n-gramas repetidos

    Returns:
        Texto normalizado sin ruido

    Ejemplos:
        >>> normalize_query_noise("batería batería honda")
        "bateria honda"
        >>> normalize_query_noise("batería batería honda", deduplicate=False)
        "bateria bateria honda"
    """
    normalized = normalize_text(text)

    if not deduplicate:
        return normalized

    tokens = normalized.split()
    if not tokens:
        return normalized

    deduped: List[str] = []
    seen_ngrams = set()
    window = 3  # Ventana de n-gramas a detectar

    for idx, token in enumerate(tokens):
        # Skip duplicados consecutivos
        if deduped and token == deduped[-1]:
            continue

        # Detectar n-gramas repetidos
        start = max(0, idx - window)
        ngram = tuple(tokens[start : idx + 1])
        if ngram in seen_ngrams:
            continue

        seen_ngrams.add(ngram)
        deduped.append(token)

    return " ".join(deduped)


def tokenize_text(text: str, deduplicate: bool = True) -> List[str]:
    """
    Tokeniza texto aplicando normalización.

    Args:
        text: Texto a tokenizar
        deduplicate: Si True, deduplica tokens

    Returns:
        Lista de tokens normalizados
    """
    return normalize_query_noise(text, deduplicate=deduplicate).split()


# ============================================================
# RRF FUSION UNIFICADA
# ============================================================

class RRFConfig:
    """Configuración para Reciprocal Rank Fusion."""

    def __init__(
        self,
        k_rrf: int = 60,
        bm25_weight: float = 1.0,
        faiss_weight: float = 1.2,
        fuzzy_weight: float = 0.8,
        consensus_boost: float = 0.15,
        partial_consensus_boost: float = 0.05,
        calibration_weight: float = 0.25,
        use_consensus: bool = True,
        use_calibration: bool = True,
    ):
        """
        Args:
            k_rrf: Constante k para RRF (default: 60)
            bm25_weight: Peso base para BM25 (default: 1.0)
            faiss_weight: Peso base para FAISS (default: 1.2)
            fuzzy_weight: Peso base para Fuzzy (default: 0.8)
            consensus_boost: Boost multiplicativo si hay consenso total (default: 0.15)
            partial_consensus_boost: Boost si hay consenso parcial (default: 0.05)
            calibration_weight: Peso de calibración con scores crudos (default: 0.25)
            use_consensus: Si True, aplica consensus boost (v3.17 feature)
            use_calibration: Si True, calibra con scores crudos (v3.17 feature)
        """
        self.k_rrf = k_rrf
        self.bm25_weight = bm25_weight
        self.faiss_weight = faiss_weight
        self.fuzzy_weight = fuzzy_weight
        self.consensus_boost = consensus_boost
        self.partial_consensus_boost = partial_consensus_boost
        self.calibration_weight = calibration_weight
        self.use_consensus = use_consensus
        self.use_calibration = use_calibration


def rrf_fusion(
    bm25_ranks: Dict[int, Tuple[int, float]],  # idx -> (rank, score)
    faiss_ranks: Dict[int, Tuple[int, float]],
    fuzzy_ranks: Dict[int, Tuple[int, float]] | None = None,
    config: RRFConfig | None = None,
) -> List[Tuple[int, float, Dict[str, float]]]:
    """
    Reciprocal Rank Fusion unificado para combinar rankings.

    Args:
        bm25_ranks: Dict {idx: (rank, score)} para BM25
        faiss_ranks: Dict {idx: (rank, score)} para FAISS
        fuzzy_ranks: Dict {idx: (rank, score)} para Fuzzy (opcional)
        config: Configuración RRF (usa defaults si None)

    Returns:
        Lista de (idx, combined_score, breakdown) ordenada por score desc

    Comportamiento:
        - config.use_consensus=False: RRF simple (compatible v3.16)
        - config.use_consensus=True: RRF adaptativo con consensus boost (v3.17)
    """
    if config is None:
        config = RRFConfig()

    # Obtener todos los índices únicos
    all_indices = set(bm25_ranks.keys()) | set(faiss_ranks.keys())
    if fuzzy_ranks:
        all_indices |= set(fuzzy_ranks.keys())

    results = []

    for idx in all_indices:
        # Obtener ranks y scores (None si no está presente)
        bm25_rank, bm25_score = bm25_ranks.get(idx, (float('inf'), 0.0))
        faiss_rank, faiss_score = faiss_ranks.get(idx, (float('inf'), 0.0))
        fuzzy_rank, fuzzy_score = (fuzzy_ranks or {}).get(idx, (float('inf'), 0.0))

        # Detectar consenso
        bm25_present = idx in bm25_ranks
        faiss_present = idx in faiss_ranks
        fuzzy_present = fuzzy_ranks is not None and idx in fuzzy_ranks

        consensus = bm25_present and faiss_present
        partial_consensus = (
            (bm25_present and fuzzy_present)
            or (faiss_present and fuzzy_present)
        )

        # Pesos adaptativos (solo si use_consensus=True)
        if config.use_consensus and consensus:
            adaptive_bm25_w = config.bm25_weight * 1.15
            adaptive_faiss_w = config.faiss_weight * 1.1
            adaptive_fuzzy_w = config.fuzzy_weight * 0.9
        else:
            adaptive_bm25_w = config.bm25_weight
            adaptive_faiss_w = config.faiss_weight
            adaptive_fuzzy_w = config.fuzzy_weight

        # RRF score
        rrf_score = 0.0
        if bm25_present:
            rrf_score += adaptive_bm25_w / (config.k_rrf + bm25_rank)
        if faiss_present:
            rrf_score += adaptive_faiss_w / (config.k_rrf + faiss_rank)
        if fuzzy_present:
            rrf_score += adaptive_fuzzy_w / (config.k_rrf + fuzzy_rank)

        # Calibración con scores crudos (solo si use_calibration=True)
        calibrated = rrf_score
        if config.use_calibration:
            # Normalizar scores a [0, 1]
            bm25_norm = bm25_score if bm25_present else 0.0
            faiss_norm = faiss_score if faiss_present else 0.0
            fuzzy_norm = max(fuzzy_score - 0.1, 0.0) if fuzzy_present else 0.0  # penalty

            calibration_term = (
                (adaptive_bm25_w * bm25_norm)
                + (adaptive_faiss_w * faiss_norm)
                + (adaptive_fuzzy_w * fuzzy_norm)
            )
            calibrated = rrf_score + config.calibration_weight * calibration_term

        # Consensus boost
        if config.use_consensus:
            if consensus:
                calibrated *= (1.0 + config.consensus_boost)
            elif partial_consensus:
                calibrated *= (1.0 + config.partial_consensus_boost)

        # Breakdown para debugging
        breakdown = {
            "rrf_score": rrf_score,
            "bm25_norm": bm25_score,
            "faiss_norm": faiss_score,
            "fuzzy_norm": fuzzy_score if fuzzy_present else 0.0,
            "consensus": consensus,
            "partial_consensus": partial_consensus,
        }

        results.append((idx, calibrated, breakdown))

    # Ordenar por score descendente
    results.sort(key=lambda x: x[1], reverse=True)
    return results


# ============================================================
# CÁLCULO DE RELEVANCIA
# ============================================================

def calculate_relevance_score(query: str, product: dict, deduplicate: bool = True) -> float:
    """
    Calcula relevancia de un producto para una query.

    Score compuesto:
    - 40% overlap de keywords
    - 40% fuzzy match del nombre
    - 20% match de categoría

    Args:
        query: Query normalizada
        product: Dict con keys: name, categoria_final, search_text
        deduplicate: Si True, deduplica tokens en query

    Returns:
        Score 0-100
    """
    try:
        from rapidfuzz import fuzz
    except ImportError:
        # Fallback sin fuzzy
        def fuzz_ratio(a, b):
            return 50.0 if a and b else 0.0
        fuzz.ratio = fuzz_ratio

    query_norm = normalize_query_noise(query, deduplicate=deduplicate)
    query_tokens = set(query_norm.split())

    # Nombre del producto
    name = normalize_text(product.get("name", ""))
    name_tokens = set(name.split())

    # Search text (usado en FAISS/BM25)
    search_text = normalize_text(product.get("search_text", ""))
    search_tokens = set(search_text.split())

    # Categoría
    categoria = normalize_text(product.get("categoria_final", ""))

    # 1. Keyword overlap (40%)
    all_product_tokens = name_tokens | search_tokens
    if all_product_tokens:
        precision = len(query_tokens & all_product_tokens) / max(len(query_tokens), 1)
        recall = len(query_tokens & all_product_tokens) / max(len(all_product_tokens), 1)
        # Penalizamos queries parciales usando mezcla de precisión/recall
        overlap = (0.7 * precision) + (0.3 * recall)
    else:
        overlap = 0.0

    # 2. Fuzzy match del nombre (40%)
    fuzzy_score = fuzz.ratio(query_norm, name) / 100.0

    # 3. Categoría match (20%)
    cat_match = 1.0 if any(token in categoria for token in query_tokens) else 0.0

    total = (0.4 * overlap) + (0.4 * fuzzy_score) + (0.2 * cat_match)
    return total * 100.0


# ============================================================
# EXTRACCIÓN DE PRECIOS
# ============================================================

def extract_prices(product: dict) -> Tuple[Decimal, Decimal]:
    """
    Extrae precios de un producto con fallbacks robustos.

    Args:
        product: Dict con keys: price_ars, price_usd, precio_lista_ars, etc.

    Returns:
        (price_ars, price_usd) como Decimal
    """
    # ARS
    price_ars = Decimal("0")
    for key in ["price_ars", "precio_lista_ars", "precio_ars"]:
        val = product.get(key)
        if val:
            try:
                price_ars = Decimal(str(val))
                break
            except:
                pass

    # USD
    price_usd = Decimal("0")
    for key in ["price_usd", "precio_usd"]:
        val = product.get(key)
        if val:
            try:
                price_usd = Decimal(str(val))
                break
            except:
                pass

    return price_ars, price_usd
