import numpy as np

from pipeline.phases_v317 import HybridSearchConfig, fase2_hybrid_search


def _controlled_embeddings(texts):
    vectors = []
    for text in texts:
        norm_text = text.lower()
        if "wave" in norm_text:
            vec = np.array([0.9, 0.1], dtype="float32")
        elif "bateria" in norm_text:
            vec = np.array([0.7, 0.05], dtype="float32")
        else:
            vec = np.array([0.05, 0.95], dtype="float32")
        vec = vec / (np.linalg.norm(vec) or 1.0)
        vectors.append(vec.astype("float32"))
    return vectors


def test_hybrid_rrf_filters_noise():
    catalog = [
        {
            "codigo": "A1",
            "descripcion": "Bateria de moto Honda Wave 110",
            "descripcion_normalizada": "bateria de moto honda wave 110",
            "familia_nombre": "baterias",
        },
        {
            "codigo": "B1",
            "descripcion": "Casco integral deportivo",
            "descripcion_normalizada": "casco integral deportivo moto",
            "familia_nombre": "cascos",
        },
        {
            "codigo": "C1",
            "descripcion": "Bateria generica 12v",
            "descripcion_normalizada": "bateria generica 12v",
            "familia_nombre": "baterias",
        },
    ]

    config = HybridSearchConfig(
        top_k=2,
        component_k=5,
        faiss_min_score=0.3,
        bm25_min_ratio=0.3,
        fuzzy_min_ratio=70,
        fuzzy_max_ratio=95,
    )

    output = fase2_hybrid_search(
        "bateria para wave",
        catalog,
        embedding_fn=_controlled_embeddings,
        config=config,
    )

    codes = [r["product_id"] for r in output["results"]]
    assert "A1" in codes
    assert "B1" not in codes  # casco debe ser filtrado por umbrales


def test_rrf_prefers_consensus_over_single_signal():
    catalog = [
        {
            "codigo": "A1",
            "descripcion": "Bateria Honda Wave original",
            "descripcion_normalizada": "bateria honda wave original",
        },
        {
            "codigo": "B1",
            "descripcion": "Bateria generica economica",
            "descripcion_normalizada": "bateria generica economica",
        },
    ]

    config = HybridSearchConfig(top_k=2, component_k=3, faiss_min_score=0.2)
    output = fase2_hybrid_search(
        "bateria wave",
        catalog,
        embedding_fn=_controlled_embeddings,
        config=config,
    )

    top_order = [r["product_id"] for r in output["results"]]
    assert top_order[0] == "A1"
