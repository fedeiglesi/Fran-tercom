import json
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import faiss
import numpy as np
from rapidfuzz import fuzz

try:
    from rank_bm25 import BM25Okapi
except ImportError:
    class BM25Okapi:  # type: ignore
        def __init__(self, corpus):
            self.corpus = corpus or []

        def get_scores(self, tokens):
            token_set = set(tokens or [])
            scores = []
            for doc in self.corpus:
                scores.append(float(sum(1 for t in doc if t in token_set)))
            return np.array(scores, dtype=float)

try:
    from jsonschema import validate
except ImportError:
    def validate(instance, schema):
        required = schema.get("required", [])
        for field in required:
            if field not in instance:
                raise ValueError(f"Missing required field: {field}")


EMBEDDING_MODEL = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
WHATSAPP_MESSAGE_LIMIT = int(os.environ.get("WHATSAPP_MESSAGE_LIMIT", "1600"))


def _normalize_text(text: str) -> str:
    text = (text or "").lower().strip()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"[^a-z0-9\s/]+", " ", text)
    return " ".join(text.split())


def _tokenize(text: str) -> List[str]:
    return _normalize_text(text).split()


def _default_embedding_fn(texts: List[str]) -> List[np.ndarray]:
    """Generate normalized embeddings using the configured OpenAI model with fallbacks."""

    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        try:
            from openai import OpenAI

            client = OpenAI(api_key=api_key)
            response = client.embeddings.create(
                model=EMBEDDING_MODEL,
                input=texts,
            )
            vectors = [np.array(item.embedding, dtype="float32") for item in response.data]
            return [vec / (np.linalg.norm(vec) or 1.0) for vec in vectors]
        except Exception:
            pass

    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer("all-MiniLM-L6-v2")
        return [
            vec.astype("float32")
            for vec in model.encode(texts, convert_to_numpy=True, normalize_embeddings=True)
        ]
    except Exception:
        embeddings: List[np.ndarray] = []
        for text in texts:
            tokens = _tokenize(text)
            stats = [
                float(len(tokens)),
                float(sum(len(t) for t in tokens)),
                float(sum(ord(ch) for ch in text) % 997),
            ]
            norm = np.linalg.norm(stats) or 1.0
            embeddings.append(np.array(stats, dtype="float32") / norm)
        return embeddings


PHASE2_SCHEMA = {
    "type": "object",
    "required": ["phase", "results"],
    "properties": {
        "phase": {"const": "search"},
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["product_id", "name", "bm25_score", "faiss_score", "hybrid_rank", "catalog_data"],
                "properties": {
                    "product_id": {"type": "string"},
                    "name": {"type": "string"},
                    "price_ars": {"type": ["number", "null"]},
                    "price_usd": {"type": ["number", "null"]},
                    "bm25_score": {"type": "number"},
                    "faiss_score": {"type": "number"},
                    "hybrid_rank": {"type": "integer"},
                    "catalog_data": {
                        "type": "object",
                        "properties": {
                            "categoria": {"type": ["string", "null"]},
                            "marca_moto": {"type": ["string", "null"]},
                            "modelo_moto": {"type": ["string", "null"]},
                            "cilindrada": {"type": ["string", "null"]},
                            "compatibilidad_declarada": {"type": ["string", "null"]},
                        },
                    },
                },
            },
        },
    },
}

PHASE3_SCHEMA = {
    "type": "object",
    "required": ["phase", "candidates"],
    "properties": {
        "phase": {"const": "compatibility_filter"},
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["product_id", "status", "confidence"],
                "properties": {
                    "product_id": {"type": "string"},
                    "status": {"enum": ["hard_compatible", "hard_incompatible", "pending_reasoning"]},
                    "confidence": {"type": "number"},
                },
            },
        },
    },
}

PHASE4_SCHEMA = {
    "type": "object",
    "required": ["phase", "candidates_evaluated", "needs_requery"],
    "properties": {
        "phase": {"const": "llm2_reasoning"},
        "candidates_evaluated": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "product_id",
                    "compatibility_decision",
                    "confidence_score",
                    "technical_reasoning",
                    "risk_level",
                ],
                "properties": {
                    "product_id": {"type": "string"},
                    "compatibility_decision": {"enum": ["compatible", "incompatible", "marginal"]},
                    "confidence_score": {"type": "number"},
                    "technical_reasoning": {"type": "string"},
                    "risk_level": {"enum": ["low", "medium", "high"]},
                    "name": {"type": ["string", "null"]},
                    "price_ars": {"type": ["number", "null"]},
                    "price_usd": {"type": ["number", "null"]},
                },
            },
        },
        "needs_requery": {"type": "boolean"},
        "reranked_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["product_id", "rank", "score"],
                "properties": {
                    "product_id": {"type": "string"},
                    "rank": {"type": "integer"},
                    "score": {"type": "number"},
                    "justification": {"type": ["string", "null"]},
                },
            },
        },
    },
}

PHASE5_SCHEMA = {
    "type": "object",
    "required": ["phase", "requery_strategy", "new_query", "attempt_number"],
    "properties": {
        "phase": {"const": "requery"},
        "requery_strategy": {"type": "string"},
        "new_query": {"type": "string"},
        "attempt_number": {"type": "integer"},
    },
}

PHASE6_SCHEMA = {
    "type": "object",
    "required": ["phase", "disclaimer", "items"],
    "properties": {
        "phase": {"const": "fallback"},
        "disclaimer": {"type": "string"},
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["product_id"],
                "properties": {
                    "product_id": {"type": "string"},
                    "name": {"type": ["string", "null"]},
                    "price_ars": {"type": ["number", "null"]},
                    "price_usd": {"type": ["number", "null"]},
                    "confidence_score": {"type": ["number", "null"]},
                },
            },
        },
    },
}

PHASE7_SCHEMA = {
    "type": "object",
    "required": ["phase", "message", "type"],
    "properties": {
        "phase": {"const": "whatsapp_response"},
        "message": {"type": "string"},
        "type": {"enum": ["confident_match", "partial_match", "clarification_needed"]},
        "badge": {"type": "string"},
        "products": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["product_id"],
                "properties": {
                    "product_id": {"type": "string"},
                    "product_code": {"type": ["string", "null"]},
                    "name": {"type": ["string", "null"]},
                    "price_ars": {"type": ["number", "null"]},
                    "price_usd": {"type": ["number", "null"]},
                    "compatibility": {"type": ["string", "null"]},
                    "badge": {"type": "string"},
                },
            },
        },
    },
}


RERANK_SCHEMA = {
    "type": "object",
    "required": ["reranked"],
    "properties": {
        "reranked": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["product_id", "score", "rank"],
                "properties": {
                    "product_id": {"type": "string"},
                    "score": {"type": "number"},
                    "rank": {"type": "integer"},
                    "justification": {"type": ["string", "null"]},
                },
            },
        }
    },
}


def _prepare_catalog(df: Any) -> List[Dict[str, Any]]:
    if hasattr(df, "to_dict") and callable(getattr(df, "to_dict", None)):
        try:
            return df.to_dict("records")
        except Exception:
            pass
    return list(df or [])


def _extract_prices(row: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    ars = row.get("precio_pesos") if isinstance(row, dict) else None
    usd = row.get("precio_dolares") if isinstance(row, dict) else None
    try:
        ars_float = float(ars) if ars not in (None, "", "nan") else None
    except Exception:
        ars_float = None
    try:
        usd_float = float(usd) if usd not in (None, "", "nan") else None
    except Exception:
        usd_float = None
    return ars_float, usd_float


def _build_bm25_index(catalog: List[Dict[str, Any]]) -> Tuple[Optional[BM25Okapi], List[List[str]]]:
    corpus_tokens: List[List[str]] = []
    for row in catalog:
        text_parts = [
            row.get("descripcion_normalizada"),
            row.get("descripcion"),
            row.get("sinonimos"),
            row.get("familia_nombre"),
        ]
        corpus_tokens.append(_tokenize(" ".join([t for t in text_parts if t])))

    if not any(corpus_tokens):
        return None, []

    return BM25Okapi(corpus_tokens), corpus_tokens


def _neural_rerank(
    query: str,
    candidates: List[Dict[str, Any]],
    embedding_fn: Optional[Callable[[List[str]], List[np.ndarray]]] = None,
) -> List[Dict[str, Any]]:
    if not candidates:
        return candidates

    embedder = embedding_fn or _default_embedding_fn
    texts = [query]
    for cand in candidates:
        catalog_data = cand.get("catalog_data", {})
        text_parts = [
            cand.get("name") or "",
            catalog_data.get("compatibilidad_declarada") or "",
            catalog_data.get("categoria") or "",
            catalog_data.get("marca_moto") or "",
            catalog_data.get("modelo_moto") or "",
        ]
        texts.append(" ".join(part for part in text_parts if part))

    vectors = embedder(texts)
    if not vectors or len(vectors) != len(texts):
        return candidates

    query_vec = vectors[0]
    query_norm = float(np.linalg.norm(query_vec)) or 1.0
    query_vec = query_vec / query_norm

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for cand, vec in zip(candidates, vectors[1:]):
        norm = float(np.linalg.norm(vec)) or 0.0
        if norm == 0.0 or np.isnan(norm):
            score = 0.0
        else:
            score = float(np.dot(query_vec, vec / norm))
        enriched = dict(cand)
        enriched["rerank_score"] = score
        scored.append((score, enriched))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored]


@dataclass
class HybridSearchConfig:
    top_k: int = 15
    component_k: int = 50
    rrf_k: float = 60.0
    bm25_weight: float = 1.0
    faiss_weight: float = 1.0
    fuzzy_weight: float = 0.6
    bm25_min_ratio: float = 0.25
    faiss_min_score: float = 0.35
    fuzzy_min_ratio: float = 65.0
    fuzzy_max_ratio: float = 90.0
    reranker_max_candidates: int = 10
    reranker: Optional[Callable[[str, List[Dict[str, Any]]], List[Dict[str, Any]]]] = None


def _build_faiss_index(catalog: List[Dict[str, Any]], embedding_fn: Callable[[List[str]], List[np.ndarray]]):
    texts = [
        " ".join(
            [
                row.get("descripcion_normalizada") or row.get("descripcion") or "",
                row.get("sinonimos") or "",
                row.get("familia_nombre") or "",
            ]
        )
        for row in catalog
    ]

    vectors = embedding_fn(texts)
    if not vectors:
        return None, []

    matrix = np.vstack(vectors)
    dim = matrix.shape[1]
    index = faiss.IndexFlatIP(dim)
    faiss.normalize_L2(matrix)
    index.add(matrix)
    return index, matrix


def _adaptive_fuzzy_threshold(query_tokens: List[str], config: HybridSearchConfig) -> float:
    base = 55 + (len(query_tokens) * 3)
    return max(config.fuzzy_min_ratio, min(config.fuzzy_max_ratio, float(base)))


def fase2_hybrid_search(
    query: str,
    df: Any,
    embedding_fn: Optional[Callable[[List[str]], List[np.ndarray]]] = None,
    top_k: int = 15,
    config: Optional[HybridSearchConfig] = None,
):
    catalog = _prepare_catalog(df)
    embedding_fn = embedding_fn or _default_embedding_fn
    config = config or HybridSearchConfig(top_k=top_k, component_k=max(top_k * 3, 30))
    reranker_fn = config.reranker or (lambda q, cands: _neural_rerank(q, cands, embedding_fn))
    component_k = max(config.component_k, config.top_k)

    bm25_index, corpus_tokens = _build_bm25_index(catalog)
    faiss_index, _ = _build_faiss_index(catalog, embedding_fn) if catalog else (None, [])

    query_tokens = _tokenize(query)
    bm25_scores = (
        bm25_index.get_scores(query_tokens).tolist()
        if bm25_index and query_tokens
        else [0.0 for _ in catalog]
    )

    faiss_scores: List[float] = []
    faiss_max = 0.0
    if faiss_index:
        query_vec = embedding_fn([query])
        if query_vec:
            vector = np.array(query_vec[0], dtype="float32").reshape(1, -1)
            faiss.normalize_L2(vector)
            sims, idxs = faiss_index.search(vector, len(catalog))
            faiss_scores = [0.0 for _ in catalog]
            for score, idx in zip(sims[0].tolist(), idxs[0].tolist()):
                if 0 <= idx < len(faiss_scores):
                    faiss_scores[idx] = score
            faiss_max = float(max(faiss_scores) if len(faiss_scores) else 0.0)
    if not faiss_scores:
        faiss_scores = [0.0 for _ in catalog]

    bm25_sorted = sorted(enumerate(bm25_scores), key=lambda x: x[1], reverse=True)
    bm25_top = bm25_sorted[:component_k]
    bm25_max = float(bm25_top[0][1]) if bm25_top else 0.0
    bm25_min_allowed = bm25_max * config.bm25_min_ratio if bm25_max > 0 else float("inf")
    bm25_candidates = {i for i, score in bm25_top if score >= bm25_min_allowed}

    faiss_sorted = sorted(enumerate(faiss_scores), key=lambda x: x[1], reverse=True)
    faiss_top = [(i, score) for i, score in faiss_sorted[:component_k] if score >= config.faiss_min_score]
    faiss_candidates = {i for i, _ in faiss_top}

    fuzzy_threshold = _adaptive_fuzzy_threshold(query_tokens, config)
    fuzzy_scores: List[float] = []
    for row in catalog:
        text = _normalize_text(
            " ".join(
                [
                    row.get("descripcion_normalizada") or row.get("descripcion") or "",
                    row.get("sinonimos") or "",
                    row.get("familia_nombre") or "",
                ]
            )
        )
        try:
            fuzzy_ratio = float(fuzz.partial_ratio(" ".join(query_tokens), text))
        except Exception:
            fuzzy_ratio = 0.0
        fuzzy_scores.append(fuzzy_ratio)
    fuzzy_sorted = sorted(enumerate(fuzzy_scores), key=lambda x: x[1], reverse=True)
    fuzzy_top = [(i, score) for i, score in fuzzy_sorted[:component_k] if score >= fuzzy_threshold]
    fuzzy_candidates = {i for i, _ in fuzzy_top}

    candidate_pool = bm25_candidates | faiss_candidates | fuzzy_candidates
    if not candidate_pool:
        candidate_pool = set(range(len(catalog)))

    def _rank_lookup(sorted_list: List[Tuple[int, float]]) -> Dict[int, int]:
        return {idx: position + 1 for position, (idx, _) in enumerate(sorted_list)}

    bm25_ranks = _rank_lookup(bm25_sorted)
    faiss_ranks = _rank_lookup(faiss_sorted)
    fuzzy_ranks = _rank_lookup(fuzzy_sorted)

    faiss_min = float(min(faiss_scores)) if faiss_scores else 0.0
    faiss_range = faiss_max - faiss_min

    ranks: List[Tuple[int, float, Dict[str, float]]] = []
    for idx in candidate_pool:
        rank_bm25 = bm25_ranks.get(idx, len(catalog) + 1)
        rank_faiss = faiss_ranks.get(idx, len(catalog) + 1)
        rank_fuzzy = fuzzy_ranks.get(idx, len(catalog) + 1)

        bm25_norm = (bm25_scores[idx] / bm25_max) if bm25_max > 0 else 0.0
        faiss_norm = (
            (faiss_scores[idx] - faiss_min) / faiss_range
            if faiss_range > 1e-6
            else 0.0
        )
        fuzzy_norm = fuzzy_scores[idx] / 100.0

        rrf_score = (
            (config.bm25_weight / (config.rrf_k + rank_bm25))
            + (config.faiss_weight / (config.rrf_k + rank_faiss))
            + (config.fuzzy_weight / (config.rrf_k + rank_fuzzy))
        )
        calibrated = rrf_score + 0.25 * (
            (config.bm25_weight * bm25_norm)
            + (config.faiss_weight * faiss_norm)
            + (config.fuzzy_weight * fuzzy_norm)
        )

        ranks.append(
            (
                idx,
                calibrated,
                {
                    "rrf_score": rrf_score,
                    "bm25_norm": bm25_norm,
                    "faiss_norm": faiss_norm,
                    "fuzzy_norm": fuzzy_norm,
                },
            )
        )

    ranks.sort(key=lambda x: x[1], reverse=True)
    rerank_limit = min(len(ranks), config.reranker_max_candidates)

    def _candidate_from_rank(hybrid_rank: int, idx: int, combined_score: float, breakdown: Dict[str, float]):
        row = catalog[idx]
        price_ars, price_usd = _extract_prices(row)
        catalog_data = {
            "categoria": row.get("categoria_final") or row.get("categoria") or None,
            "marca_moto": row.get("marca_moto") or None,
            "modelo_moto": row.get("modelo_moto") or None,
            "cilindrada": row.get("cilindrada") or None,
            "compatibilidad_declarada": row.get("compatibilidad_declarada") if "compatibilidad_declarada" in row else None,
        }
        return {
            "product_id": str(row.get("codigo") or row.get("id") or str(idx)),
            "name": str(row.get("descripcion") or row.get("descripcion_normalizada") or ""),
            "price_ars": price_ars,
            "price_usd": price_usd,
            "bm25_score": float(bm25_scores[idx]) if bm25_scores else 0.0,
            "faiss_score": float(faiss_scores[idx]) if faiss_scores else 0.0,
            "fuzzy_score": float(fuzzy_scores[idx]) if fuzzy_scores else 0.0,
            "hybrid_rank": hybrid_rank,
            "combined_score": float(combined_score),
            "score_breakdown": breakdown,
            "catalog_data": catalog_data,
        }

    pre_results = [
        _candidate_from_rank(hybrid_rank, idx, combined, meta)
        for hybrid_rank, (idx, combined, meta) in enumerate(ranks[: config.top_k], start=1)
    ]

    results = pre_results
    if reranker_fn and pre_results:
        try:
            reranked = reranker_fn(query, pre_results[:rerank_limit])
            order = {c["product_id"]: pos for pos, c in enumerate(reranked)} if isinstance(reranked, list) else {}
            if order:
                results = sorted(pre_results, key=lambda c: order.get(c["product_id"], len(order) + c["hybrid_rank"]))
        except Exception:
            results = pre_results

    payload = {"phase": "search", "results": results[: config.top_k]}
    validate(instance=payload, schema=PHASE2_SCHEMA)
    return payload


def fase3_compatibility_filter(search_output: Dict[str, Any], classifier_output: Dict[str, Any]):
    candidates = []
    user_brand = _normalize_text(classifier_output.get("brand") or "")
    user_model = _normalize_text(classifier_output.get("model") or "")

    for result in search_output.get("results", []):
        data = result.get("catalog_data", {})
        brand = _normalize_text(data.get("marca_moto") or "")
        model = _normalize_text(data.get("modelo_moto") or "")

        if brand and model and user_brand and user_model:
            if brand == user_brand and model == user_model:
                status = "hard_compatible"
                confidence = 0.92
            else:
                status = "hard_incompatible"
                confidence = 0.6
        else:
            status = "pending_reasoning"
            confidence = 0.35 if brand or model else 0.25

        candidates.append(
            {
                "product_id": result["product_id"],
                "status": status,
                "confidence": float(confidence),
            }
        )

    payload = {"phase": "compatibility_filter", "candidates": candidates}
    validate(instance=payload, schema=PHASE3_SCHEMA)
    return payload


def _risk_from_conf(conf: float) -> str:
    if conf >= 0.75:
        return "low"
    if conf >= 0.5:
        return "medium"
    return "high"


def fase4_llm2_reasoning(
    search_output: Dict[str, Any],
    compatibility_output: Dict[str, Any],
    classifier_output: Dict[str, Any],
):
    catalog_lookup = {r["product_id"]: r for r in search_output.get("results", [])}
    user_brand = _normalize_text(classifier_output.get("brand") or "")
    user_model = _normalize_text(classifier_output.get("model") or "")
    displacement = classifier_output.get("displacement_cc")

    evaluated = []
    needs_requery = False

    for candidate in compatibility_output.get("candidates", []):
        if candidate.get("status") != "pending_reasoning":
            decision = "compatible" if candidate["status"] == "hard_compatible" else "incompatible"
            confidence = candidate.get("confidence", 0.5)
            reasoning = "Compatibilidad determinada por datos estructurados del catálogo."
            evaluated.append(
                {
                    "product_id": candidate["product_id"],
                    "compatibility_decision": decision,
                    "confidence_score": float(confidence),
                    "technical_reasoning": reasoning,
                    "risk_level": _risk_from_conf(confidence),
                    "name": catalog_lookup.get(candidate["product_id"], {}).get("name"),
                    "price_ars": catalog_lookup.get(candidate["product_id"], {}).get("price_ars"),
                    "price_usd": catalog_lookup.get(candidate["product_id"], {}).get("price_usd"),
                }
            )
            continue

        catalog_item = catalog_lookup.get(candidate["product_id"], {})
        data = catalog_item.get("catalog_data", {})
        brand = _normalize_text(data.get("marca_moto") or "")
        model = _normalize_text(data.get("modelo_moto") or "")
        cil = data.get("cilindrada")

        confidence = 0.4
        decision = "marginal"
        if brand and user_brand and brand == user_brand:
            confidence = 0.65
            decision = "compatible"
        elif brand and user_brand and brand != user_brand:
            confidence = 0.25
            decision = "incompatible"
        elif model and user_model and model == user_model:
            confidence = 0.6
            decision = "compatible"
        elif displacement and cil and str(displacement) not in str(cil):
            decision = "incompatible"
            confidence = 0.3

        if decision != "incompatible" and confidence < 0.45:
            needs_requery = True

        reasoning = (
            f"Catálogo: marca={data.get('marca_moto') or 'desconocida'}, modelo={data.get('modelo_moto') or 'desconocido'}, "
            f"cilindrada={cil or 's/d'}. Usuario: marca={classifier_output.get('brand') or 'sin marca'}, "
            f"modelo={classifier_output.get('model') or 'sin modelo'}."
        )

        evaluated.append(
            {
                "product_id": candidate["product_id"],
                "compatibility_decision": decision,
                "confidence_score": float(confidence),
                "technical_reasoning": reasoning,
                "risk_level": _risk_from_conf(confidence),
                "name": catalog_item.get("name"),
                "price_ars": catalog_item.get("price_ars"),
                "price_usd": catalog_item.get("price_usd"),
            }
        )

    reranked = _structured_rerank(evaluated)

    payload = {
        "phase": "llm2_reasoning",
        "candidates_evaluated": evaluated,
        "needs_requery": bool(needs_requery),
        "reranked_candidates": reranked,
    }
    validate(instance=payload, schema=PHASE4_SCHEMA)
    return payload


def fase5_requery(original_query: str, classifier_output: Dict[str, Any], attempt: int, search_output: Dict[str, Any]):
    strategy_parts = []
    new_query = _normalize_text(original_query)

    if not classifier_output.get("brand"):
        brands = [r.get("catalog_data", {}).get("marca_moto") for r in search_output.get("results", [])]
        brands = [b for b in brands if b]
        if brands:
            candidate_brand = brands[0]
            new_query = f"{new_query} {candidate_brand}"
            strategy_parts.append("brand_from_catalog")

    if not classifier_output.get("model"):
        models = [r.get("catalog_data", {}).get("modelo_moto") for r in search_output.get("results", [])]
        models = [m for m in models if m]
        if models:
            new_query = f"{new_query} {models[0]}"
            strategy_parts.append("model_from_catalog")

    if not strategy_parts:
        strategy_parts.append("semantic_expansion")
        if search_output.get("results"):
            new_query = f"{new_query} {search_output['results'][0].get('name', '')}"

    payload = {
        "phase": "requery",
        "requery_strategy": ",".join(strategy_parts),
        "new_query": new_query.strip(),
        "attempt_number": attempt,
    }
    validate(instance=payload, schema=PHASE5_SCHEMA)
    return payload


def fase6_fallback(search_output: Dict[str, Any], reasoning_output: Dict[str, Any]):
    confident = [c for c in reasoning_output.get("candidates_evaluated", []) if c.get("confidence_score", 0) >= 0.6 and c.get("compatibility_decision") == "compatible"]

    if confident:
        items = [
            {
                "product_id": c.get("product_id", ""),
                "name": c.get("name"),
                "price_ars": c.get("price_ars"),
                "price_usd": c.get("price_usd"),
                "confidence_score": c.get("confidence_score"),
            }
            for c in confident
        ]
        disclaimer = "Opciones confirmadas según datos actuales."
    else:
        items = [
            {
                "product_id": r.get("product_id", ""),
                "name": r.get("name"),
                "price_ars": r.get("price_ars"),
                "price_usd": r.get("price_usd"),
                "confidence_score": r.get("hybrid_rank"),
            }
            for r in search_output.get("results", [])[:5]
        ]
        disclaimer = "Resultados sugeridos con información parcial, confirma compatibilidad."

    payload = {"phase": "fallback", "disclaimer": disclaimer, "items": items}
    validate(instance=payload, schema=PHASE6_SCHEMA)
    return payload


def fase7_whatsapp_response(
    search_output: Dict[str, Any],
    reasoning_output: Dict[str, Any],
    fallback_output: Dict[str, Any],
    classifier_output: Optional[Dict[str, Any]] = None,
):
    evaluated = reasoning_output.get("candidates_evaluated", [])
    confident = [c for c in evaluated if c.get("compatibility_decision") == "compatible" and c.get("confidence_score", 0) >= 0.6]

    search_lookup = {r.get("product_id"): r for r in search_output.get("results", [])}
    follow_up_pid = _resolve_product_reference(classifier_output or {}, search_output) if classifier_output else None
    requested_qty = _extract_requested_quantity(classifier_output or {}) if classifier_output else None

    if classifier_output and classifier_output.get("intent") in {"cart_action", "follow_up"} and follow_up_pid and follow_up_pid in search_lookup:
        badge = "🟢"
        resp_type = "confident_match"
        result = search_lookup[follow_up_pid]
        price = _format_price(result.get("price_ars"), result.get("price_usd"))
        compatibility = _compatibility_text(result)
        qty = requested_qty or 1
        message_parts = [
            f"Perfecto, agregué {qty} unidades de {result.get('name','')} (Código {follow_up_pid}) al carrito.",
            f"Precio: {price}",
        ]
        if compatibility:
            message_parts.append(f"Compatibilidad: {compatibility}")
        message = " ".join([part for part in message_parts if part])
        products = [
            {
                "product_id": follow_up_pid,
                "product_code": follow_up_pid,
                "name": result.get("name"),
                "price_ars": result.get("price_ars"),
                "price_usd": result.get("price_usd"),
                "compatibility": compatibility,
                "badge": badge,
            }
        ]
    elif confident:
        chosen = confident[:3]
        resp_type = "confident_match"
        badge = "🟢"
        formatted = []
        products = []
        for c in chosen:
            lookup = search_lookup.get(c.get("product_id"), {})
            price = _format_price(c.get("price_ars") or lookup.get("price_ars"), c.get("price_usd") or lookup.get("price_usd"))
            compatibility = _compatibility_text(lookup)
            formatted.append(
                f"{badge} {c['product_id']} · {c.get('name','')} · {price} ({c['confidence_score']:.2f})"
                + (f" · Compatibilidad: {compatibility}" if compatibility else "")
            )
            products.append(
                {
                    "product_id": c.get("product_id", ""),
                    "product_code": c.get("product_id", ""),
                    "name": c.get("name") or lookup.get("name"),
                    "price_ars": c.get("price_ars") or lookup.get("price_ars"),
                    "price_usd": c.get("price_usd") or lookup.get("price_usd"),
                    "compatibility": compatibility,
                    "badge": badge,
                }
            )
        message = "Encontré opciones compatibles:\n" + "\n".join(formatted)
    elif fallback_output.get("items"):
        resp_type = "partial_match"
        badge = "🟠"
        items = fallback_output["items"]
        formatted = []
        products = []
        for item in items[:3]:
            price = _format_price(item.get("price_ars"), item.get("price_usd"))
            lookup = search_lookup.get(item.get("product_id"), {})
            compatibility = _compatibility_text(lookup)
            formatted.append(
                f"{badge} {item.get('product_id')} · {item.get('name','')} · {price}" + (f" · Compatibilidad: {compatibility}" if compatibility else "")
            )
            products.append(
                {
                    "product_id": item.get("product_id", ""),
                    "product_code": item.get("product_id", ""),
                    "name": item.get("name") or lookup.get("name"),
                    "price_ars": item.get("price_ars") or lookup.get("price_ars"),
                    "price_usd": item.get("price_usd") or lookup.get("price_usd"),
                    "compatibility": compatibility,
                    "badge": badge,
                }
            )
        message = fallback_output.get("disclaimer", "") + "\n" + "\n".join(formatted)
    else:
        resp_type = "clarification_needed"
        badge = "⚪"
        message = "Necesito más datos para asegurarte compatibilidad. ¿Marca y modelo de la moto?"
        products = []

    limit = max(10, WHATSAPP_MESSAGE_LIMIT)
    if len(message) > limit:
        message = message[: limit - 3] + "..."

    payload = {"phase": "whatsapp_response", "message": message, "type": resp_type, "badge": badge, "products": products}
    validate(instance=payload, schema=PHASE7_SCHEMA)
    return payload


def _format_price(price_ars: Optional[float], price_usd: Optional[float]) -> str:
    if price_ars is not None:
        return f"ARS {price_ars:,.2f}"
    if price_usd is not None:
        return f"USD {price_usd:,.2f}"
    return "precio a confirmar"


def _structured_rerank(evaluated: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ordered = sorted(
        evaluated,
        key=lambda item: (
            0 if item.get("compatibility_decision") == "incompatible" else 1,
            float(item.get("confidence_score") or 0),
        ),
        reverse=True,
    )

    reranked = []
    for rank, item in enumerate(ordered, start=1):
        reranked.append(
            {
                "product_id": item.get("product_id", ""),
                "score": float(item.get("confidence_score") or 0.0),
                "rank": rank,
                "justification": item.get("technical_reasoning"),
            }
        )

    payload = {"reranked": reranked}
    validate(instance=payload, schema=RERANK_SCHEMA)
    return reranked


def _compatibility_text(result: Dict[str, Any]) -> Optional[str]:
    data = result.get("catalog_data", {}) if result else {}
    compat = data.get("compatibilidad_declarada")
    if compat:
        return compat

    parts = [p for p in [data.get("marca_moto"), data.get("modelo_moto"), data.get("cilindrada")] if p]
    if parts:
        return " | ".join(str(p) for p in parts if p)
    return None


def _resolve_product_reference(classifier_output: Dict[str, Any], search_output: Dict[str, Any]) -> Optional[str]:
    reference = classifier_output.get("product_reference") or ""
    intents = classifier_output.get("multi_intent") or []
    if not reference:
        for intent in intents:
            if intent.get("product_reference"):
                reference = intent.get("product_reference") or ""
                break
    if not reference:
        return None

    ref_norm = _normalize_text(reference)
    if not ref_norm:
        return None

    best_id = None
    best_score = 0.0
    for result in search_output.get("results", []):
        pid = str(result.get("product_id", ""))
        text = " ".join([pid, result.get("name", ""), result.get("catalog_data", {}).get("compatibilidad_declarada", "")])
        cand_norm = _normalize_text(text)
        tokens_ref = set(_tokenize(ref_norm))
        tokens_cand = set(_tokenize(cand_norm))
        if not tokens_cand:
            continue
        overlap = len(tokens_ref & tokens_cand) / max(1, len(tokens_ref))
        direct_hit = 1.0 if ref_norm in cand_norm or ref_norm in pid.lower() else 0.0
        score = direct_hit or overlap
        if score > best_score:
            best_score = score
            best_id = pid

    if best_score < 0.2:
        return None
    return best_id


def _extract_requested_quantity(classifier_output: Dict[str, Any]) -> Optional[int]:
    quantity = classifier_output.get("quantity")
    if quantity is not None:
        try:
            return int(quantity)
        except Exception:
            return None

    for intent in classifier_output.get("multi_intent") or []:
        qty = intent.get("quantity")
        if qty is not None:
            try:
                return int(qty)
            except Exception:
                continue
    return None

