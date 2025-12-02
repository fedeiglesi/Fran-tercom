from pipeline.llm_classifier_dynamic import fase1_llm_classifier_dynamic
from pipeline.phases_v317 import (
    fase2_hybrid_search,
    fase3_compatibility_filter,
    fase4_llm2_reasoning,
    fase5_requery,
    fase6_fallback,
    fase7_whatsapp_response,
    _normalize_text,
)
from pipeline.router_dynamic import router_fase0_dynamic


def _compose_search_query(base_query: str, classifier: dict) -> str:
    """Combina la consulta base con entidades detectadas para enriquecer la búsqueda."""

    normalized_base = _normalize_text(base_query)
    additions = []

    for field in ("product_type", "brand", "model"):
        value = classifier.get(field)
        normalized_value = _normalize_text(value or "")
        if normalized_value and normalized_value not in normalized_base:
            additions.append(str(value))

    if additions:
        return " ".join([base_query] + additions).strip()

    return base_query.strip()


def orquestar_v317(message, df, catalog_centroid, schema, embedding_fn=None, max_requeries: int = 2):
    trace = []

    # --- FASE 0 ---
    fase0 = router_fase0_dynamic(message, catalog_centroid)
    fase0["phase"] = "router"
    trace.append(fase0)
    if fase0["route"] == "social":
        return {"trace": trace, "final_response": {"message": "¿En qué te puedo ayudar?", "fase": "0"}}

    # --- FASE 1 ---
    clasif = fase1_llm_classifier_dynamic(message, df, schema)
    clasif["phase"] = "classifier"
    trace.append(clasif)

    if clasif.get("intent") == "social":
        return {"trace": trace, "final_response": {"message": "¿Qué repuesto estás buscando exactamente?", "fase": "1"}}

    attempts = 0
    current_query = message

    multi = clasif.get("multi_intent") or []
    # Si hay múltiples intents, usamos el span de búsqueda de mayor confianza
    # como consulta primaria sin descartar el resto.
    if multi:
        product_intents = [m for m in multi if m.get("intent") in {"product_search", "busca_producto"}]
        if product_intents:
            best = max(product_intents, key=lambda x: x.get("confidence", 0))
            if best.get("confidence", 0) >= 0.4 and best.get("span"):
                current_query = best["span"]
                clasif["primary_query_from_multi_intent"] = best

    while True:
        enriched_query = _compose_search_query(current_query, clasif)
        # --- FASE 2 ---
        search_output = fase2_hybrid_search(
            enriched_query,
            df,
            embedding_fn=embedding_fn,
            preferred_product_type=clasif.get("product_type"),
        )
        trace.append(search_output)

        # --- FASE 3 ---
        compat_output = fase3_compatibility_filter(search_output, clasif)
        trace.append(compat_output)

        # --- FASE 4 ---
        reasoning_output = fase4_llm2_reasoning(search_output, compat_output, clasif)
        trace.append(reasoning_output)

        if reasoning_output.get("needs_requery") and attempts < max_requeries:
            attempts += 1
            requery_output = fase5_requery(current_query, clasif, attempts, search_output)
            trace.append(requery_output)
            current_query = requery_output["new_query"]
            continue

        # --- FASE 6 ---
        fallback_output = fase6_fallback(search_output, reasoning_output)
        trace.append(fallback_output)

        # --- FASE 7 ---
        response_output = fase7_whatsapp_response(search_output, reasoning_output, fallback_output, clasif)
        trace.append(response_output)

        return {"trace": trace, "final_response": response_output}
