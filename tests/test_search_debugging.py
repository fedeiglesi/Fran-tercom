import logging
import sys
import types
from typing import List

import numpy as np
import pytest


class DummyBM25:
    def __init__(self, corpus):
        self.corpus = corpus or []

    def get_scores(self, tokens):
        token_set = set(tokens or [])
        scores = []
        for doc in self.corpus:
            scores.append(float(sum(1 for t in doc if t in token_set)))
        return np.array(scores, dtype=float)


sys.modules.setdefault("rank_bm25", types.SimpleNamespace(BM25Okapi=DummyBM25))
sys.modules.setdefault("dotenv", types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: None))
sys.modules.setdefault("cachetools", types.SimpleNamespace(LRUCache=lambda *args, **kwargs: {}))
sys.modules.setdefault("jsonschema", types.SimpleNamespace(Draft7Validator=lambda *args, **kwargs: None))

import app


@pytest.fixture(autouse=True)
def enable_debug(monkeypatch):
    monkeypatch.setenv("FRAN_DEBUG", "1")
    monkeypatch.setattr(app, "FRAN_DEBUG", True)
    app.LAST_SEARCH_DEBUG.clear()
    app.LAST_FILTER_CATALOG_DEBUG.clear()
    app.LAST_RELEVANCE_DEBUG.clear()
    yield
    app.LAST_SEARCH_DEBUG.clear()
    app.LAST_FILTER_CATALOG_DEBUG.clear()
    app.LAST_RELEVANCE_DEBUG.clear()


@pytest.fixture
def fake_embeddings(monkeypatch):
    def _fake(texts: List[str]):
        vectors = []
        for text in texts:
            norm = app.normalize_search_query(text)
            vectors.append(
                np.array(
                    [
                        float(len(norm)),
                        float(norm.count(" ")),
                        float(sum(ord(c) for c in norm) % 100),
                    ],
                    dtype="float32",
                )
            )
        return vectors

    monkeypatch.setattr(app, "generate_embeddings_with_cache", _fake)
    return _fake


@pytest.fixture
def sample_catalog():
    return [
        {
            "code": "C1",
            "name": "Bujía NGK Honda Wave 110",
            "search_text": "bujia ngk honda wave 110 encendido",
            "category": "bujia",
            "family_name": "encendido",
            "brand": "NGK",
            "model": "wave 110",
            "moto_brand": "honda",
            "moto_model": "wave",
            "displacement": "110",
        },
        {
            "code": "C2",
            "name": "Pastillas freno delanteras Suzuki GN",
            "search_text": "pastillas freno delanteras suzuki gn125",
            "category": "pastillas",
            "family_name": "frenos",
            "brand": "tercom",
            "model": "gn125",
            "moto_brand": "suzuki",
            "moto_model": "gn",
            "displacement": "125",
        },
        {
            "code": "C3",
            "name": "Amortiguador trasero Honda Wave",
            "search_text": "amortiguador trasero honda wave",
            "category": "amortiguadores",
            "family_name": "suspension",
            "brand": "tercom",
            "model": "wave",
            "moto_brand": "honda",
            "moto_model": "wave",
            "displacement": "110",
        },
        {
            "code": "1179/00035-038",
            "name": "Kit transmisión cadena piñon corona YBR 125",
            "search_text": "kit transmision cadena pinon corona ybr 125",
            "category": "transmision",
            "family_name": "transmision",
            "brand": "did",
            "model": "ybr",
            "moto_brand": "yamaha",
            "moto_model": "ybr 125",
            "displacement": "125",
            "final_category": "transmision",
        },
    ]


def prepare_indexes(monkeypatch, catalog, include_bm25=True, include_faiss=True):
    bm25_index, corpus = (None, [])
    if include_bm25:
        bm25_index, corpus = app._build_bm25_index_from_catalog(catalog)

    index = None
    if include_faiss:
        index, _ = app._build_faiss_index_from_catalog(catalog)

    monkeypatch.setattr(
        app,
        "_catalog_and_index_cache",
        {
            "catalog": catalog,
            "index": index,
            "bm25": bm25_index,
            "bm25_corpus": corpus,
            "built_at": None,
        },
    )


@pytest.mark.usefixtures("fake_embeddings")
def test_bm25_individual(monkeypatch, sample_catalog, caplog):
    prepare_indexes(monkeypatch, sample_catalog, include_bm25=True, include_faiss=False)

    with caplog.at_level(logging.INFO):
        results = app.hybrid_search("bujia honda wave", top_k=5)

    assert results
    assert results[0][0]["code"] == "C1"
    assert "[DEBUG][BM25]" in caplog.text


@pytest.mark.usefixtures("fake_embeddings")
def test_faiss_individual(monkeypatch, sample_catalog, caplog):
    prepare_indexes(monkeypatch, sample_catalog, include_bm25=False, include_faiss=True)

    with caplog.at_level(logging.INFO):
        results = app.hybrid_search("pastillas suzuki gn", top_k=5)

    assert results
    assert any("[DEBUG][FAISS]" in record.message for record in caplog.records)


@pytest.mark.usefixtures("fake_embeddings")
def test_rrf_individual(monkeypatch, sample_catalog, caplog):
    prepare_indexes(monkeypatch, sample_catalog, include_bm25=True, include_faiss=True)

    with caplog.at_level(logging.INFO):
        results = app.hybrid_search("amortiguador wave", top_k=5)

    assert results
    assert app.LAST_SEARCH_DEBUG.get("rrf_count", 0) >= len(results)
    assert "[DEBUG][RRF]" in caplog.text


@pytest.mark.usefixtures("fake_embeddings")
def test_code_lookup(monkeypatch, sample_catalog, caplog):
    prepare_indexes(monkeypatch, sample_catalog, include_bm25=True, include_faiss=True)
    monkeypatch.setattr(app, "RELEVANCE_MIN_SCORE", 0)

    with caplog.at_level(logging.INFO):
        filtered = app.run_allowed_products_search("1179/00035-038")

    assert isinstance(filtered, list)
    assert "1179/00035-038" in {p.get("code") for p in filtered}
    assert "[DEBUG][Resumen]" in caplog.text


def test_moto_filter(sample_catalog, caplog):
    parsed = {
        "brands": [],
        "models": [],
        "categories": [],
        "families": [],
        "moto_brands": ["honda"],
        "moto_models": ["wave"],
        "motos_detectadas": [],
        "displacement": None,
        "final_category": None,
    }

    with caplog.at_level(logging.INFO):
        filtered = app.filter_catalog(sample_catalog, parsed)

    assert filtered
    assert all("honda" in app.normalize_search_query(p.get("moto_brand", "")) for p in filtered)
    assert "[DEBUG][Filtro] Rechazos" in caplog.text


def test_familia_filter(sample_catalog, caplog):
    parsed = {
        "brands": [],
        "models": [],
        "categories": [],
        "families": ["transmision"],
        "moto_brands": [],
        "moto_models": [],
        "motos_detectadas": [],
        "displacement": None,
        "final_category": None,
    }

    with caplog.at_level(logging.INFO):
        filtered = app.filter_catalog(sample_catalog, parsed)

    assert filtered == [sample_catalog[3]]
    assert "family_mismatch" in caplog.text
