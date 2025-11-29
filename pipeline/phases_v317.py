import math
import re
import unicodedata
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import faiss
import numpy as np

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


def _normalize_text(text: str) -> str:
    text = (text or "").lower().strip()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"[^a-z0-9\s/]+", " ", text)
    return " ".join(text.split())


def _tokenize(text: str) -> List[str]:
    return _normalize_text(text).split()


def _default_embedding_fn(texts: List[str]) -> List[np.ndarray]:
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
                },
            },
        },
        "needs_requery": {"type": "boolean"},
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
        "items": {"type": "array"},
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
    },
}


def _prepare_catalog(df: Any) -> List[Dict[str, Any]]:
    if hasattr(df, "to_dict") and callable(getattr(df, "to_dict", None)):
        try:
            return df.to_dict("records")
        except Exception:
            pass
    return list(df or [])


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


def fase2_hybrid_search(query: str, df: Any, embedding_fn: Optional[Callable[[List[str]], List[np.ndarray]]] = None, top_k: int = 15):
    catalog = _prepare_catalog(df)
    embedding_fn = embedding_fn or _default_embedding_fn

    bm25_index, corpus_tokens = _build_bm25_index(catalog)
    faiss_index, _ = _build_faiss_index(catalog, embedding_fn) if catalog else (None, [])

    query_tokens = _tokenize(query)
    bm25_scores = (
        bm25_index.get_scores(query_tokens).tolist()
        if bm25_index and query_tokens
        else [0.0 for _ in catalog]
    )

    faiss_scores: List[float] = []
    if faiss_index:
        query_vec = embedding_fn([query])
        if query_vec:
            vector = np.array(query_vec[0], dtype="float32").reshape(1, -1)
            faiss.normalize_L2(vector)
            sims, _ = faiss_index.search(vector, len(catalog))
            faiss_scores = sims.flatten().tolist()
    if not faiss_scores:
        faiss_scores = [0.0 for _ in catalog]

    ranks = []
    for idx in range(len(catalog)):
        rank_bm25 = sorted(range(len(bm25_scores)), key=lambda i: bm25_scores[i], reverse=True).index(idx) + 1
        rank_faiss = sorted(range(len(faiss_scores)), key=lambda i: faiss_scores[i], reverse=True).index(idx) + 1
        fused = (1 / (60 + rank_bm25)) + (1 / (60 + rank_faiss))
        ranks.append((idx, fused))

    ranks.sort(key=lambda x: x[1], reverse=True)
    results: List[Dict[str, Any]] = []
    for hybrid_rank, (idx, _) in enumerate(ranks[:top_k], start=1):
        row = catalog[idx]
        catalog_data = {
            "categoria": row.get("categoria_final") or row.get("categoria") or None,
            "marca_moto": row.get("marca_moto") or None,
            "modelo_moto": row.get("modelo_moto") or None,
            "cilindrada": row.get("cilindrada") or None,
            "compatibilidad_declarada": row.get("compatibilidad_declarada") if "compatibilidad_declarada" in row else None,
        }
        results.append(
            {
                "product_id": str(row.get("codigo") or row.get("id") or str(idx)),
                "name": str(row.get("descripcion") or row.get("descripcion_normalizada") or ""),
                "bm25_score": float(bm25_scores[idx]) if bm25_scores else 0.0,
                "faiss_score": float(faiss_scores[idx]) if faiss_scores else 0.0,
                "hybrid_rank": hybrid_rank,
                "catalog_data": catalog_data,
            }
        )

    payload = {"phase": "search", "results": results}
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

        if confidence < 0.45:
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
            }
        )

    payload = {
        "phase": "llm2_reasoning",
        "candidates_evaluated": evaluated,
        "needs_requery": bool(needs_requery),
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
        items = confident
        disclaimer = "Opciones confirmadas según datos actuales."
    else:
        items = search_output.get("results", [])[:5]
        disclaimer = "Resultados sugeridos con información parcial, confirma compatibilidad."

    payload = {"phase": "fallback", "disclaimer": disclaimer, "items": items}
    validate(instance=payload, schema=PHASE6_SCHEMA)
    return payload


def fase7_whatsapp_response(
    search_output: Dict[str, Any],
    reasoning_output: Dict[str, Any],
    fallback_output: Dict[str, Any],
):
    evaluated = reasoning_output.get("candidates_evaluated", [])
    confident = [c for c in evaluated if c.get("compatibility_decision") == "compatible" and c.get("confidence_score", 0) >= 0.6]

    if confident:
        chosen = confident[:3]
        resp_type = "confident_match"
        badge = "🟢"
        items_desc = [f"{badge} {c['product_id']} ({c['confidence_score']:.2f})" for c in chosen]
        message = "Encontré opciones compatibles:\n" + "\n".join(items_desc)
    elif fallback_output.get("items"):
        resp_type = "partial_match"
        badge = "🟠"
        items = fallback_output["items"]
        formatted = []
        for item in items[:3]:
            if "product_id" in item:
                formatted.append(f"{badge} {item.get('product_id')} - revisión sugerida")
            elif "name" in item:
                formatted.append(f"{badge} {item.get('name')}")
        message = fallback_output.get("disclaimer", "") + "\n" + "\n".join(formatted)
    else:
        resp_type = "clarification_needed"
        badge = "⚪"
        message = "Necesito más datos para asegurarte compatibilidad. ¿Marca y modelo de la moto?"

    if len(message) > 1000:
        message = message[:997] + "..."

    payload = {"phase": "whatsapp_response", "message": message, "type": resp_type, "badge": badge}
    validate(instance=payload, schema=PHASE7_SCHEMA)
    return payload

