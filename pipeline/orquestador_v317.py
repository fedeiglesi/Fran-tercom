from pipeline.llm_classifier_dynamic import fase1_llm_classifier_dynamic
from pipeline.router_dynamic import router_fase0_dynamic


def orquestar_v317(message, df, catalog_centroid, schema):

    # --- FASE 0 ---
    r = router_fase0_dynamic(message, catalog_centroid)
    if r["route"] == "social":
        return {"message": "¿En qué te puedo ayudar?", "fase": "0"}

    # --- FASE 1 ---
    clasif = fase1_llm_classifier_dynamic(message, df, schema)

    if clasif["intent"] == "social":
        return {"message": "¿Qué repuesto estás buscando exactamente?", "fase": "1"}

    # continúa con FASE 2 (hybrid search), FASE 3 (filtro), FASE 4 (LLM2)…
