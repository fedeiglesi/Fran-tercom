"""
Tests para validar fixes de coherencia entre Fran 3.16 y 3.17.

Relacionado: ANALISIS_COHERENCIA_FRAN_3.6.md
"""

import pytest
import time
import numpy as np
from datetime import datetime, timedelta


# ============================================================
# TEST FIX #3: Normalización Unificada
# ============================================================

class TestNormalization:
    """Validar que normalización sea consistente entre versiones."""

    def test_normalize_text_basic(self):
        from fran.search_utils import normalize_text

        assert normalize_text("Batería") == "bateria"
        assert normalize_text("HONDA  CG  150") == "honda cg 150"
        assert normalize_text("filtro@aceite#yamaha") == "filtro aceite yamaha"

    def test_normalize_query_noise_with_dedup(self):
        from fran.search_utils import normalize_query_noise

        # Deduplicación activa (v3.17 default)
        result = normalize_query_noise("batería batería honda honda", deduplicate=True)
        assert result == "bateria honda"

        # Sin duplicados → no cambio
        result = normalize_query_noise("bateria honda cg", deduplicate=True)
        assert result == "bateria honda cg"

    def test_normalize_query_noise_without_dedup(self):
        from fran.search_utils import normalize_query_noise

        # Sin deduplicación (v3.16 compatible)
        result = normalize_query_noise("batería batería honda", deduplicate=False)
        assert result == "bateria bateria honda"

    def test_tokenize_text(self):
        from fran.search_utils import tokenize_text

        tokens = tokenize_text("Batería Honda CG 150", deduplicate=True)
        assert tokens == ["bateria", "honda", "cg", "150"]

        tokens = tokenize_text("filtro filtro aceite", deduplicate=True)
        assert tokens == ["filtro", "aceite"]

    def test_normalize_accents_comprehensive(self):
        from fran.search_utils import normalize_text

        assert normalize_text("ñandú") == "nandu"
        assert normalize_text("amortiguador") == "amortiguador"
        assert normalize_text("bujía") == "bujia"
        assert normalize_text("piñón") == "pinon"


# ============================================================
# TEST FIX #1: RRF Fusion Unificado
# ============================================================

class TestRRFFusion:
    """Validar que RRF fusion sea coherente entre v3.16 y v3.17."""

    def test_rrf_simple_v316_mode(self):
        from fran.search_utils import rrf_fusion, RRFConfig

        # Configuración v3.16 (sin consensus)
        config = RRFConfig(
            k_rrf=60,
            bm25_weight=1.0,
            faiss_weight=1.2,
            use_consensus=False,
            use_calibration=False,
        )

        bm25_ranks = {
            0: (1, 0.8),  # idx=0, rank=1, score=0.8
            1: (2, 0.6),
            2: (3, 0.4),
        }

        faiss_ranks = {
            0: (1, 0.9),  # idx=0, rank=1, score=0.9
            2: (2, 0.7),
            3: (3, 0.5),
        }

        results = rrf_fusion(bm25_ranks, faiss_ranks, config=config)

        # idx=0 debe estar primero (presente en ambos con rank=1)
        assert results[0][0] == 0
        assert len(results) == 4  # Total de índices únicos

    def test_rrf_v317_consensus_boost(self):
        from fran.search_utils import rrf_fusion, RRFConfig

        # Configuración v3.17 (con consensus boost)
        config = RRFConfig(
            k_rrf=60,
            bm25_weight=1.0,
            faiss_weight=1.2,
            consensus_boost=0.15,
            use_consensus=True,
            use_calibration=True,
        )

        bm25_ranks = {0: (1, 0.8), 1: (2, 0.6)}
        faiss_ranks = {0: (1, 0.9), 2: (2, 0.7)}

        results = rrf_fusion(bm25_ranks, faiss_ranks, config=config)

        # Verificar que idx=0 tiene consensus
        assert results[0][2]["consensus"] is True

        # Score de idx=0 debe ser mayor con consensus boost
        score_with_consensus = results[0][1]

        # Comparar con versión sin consensus
        config_no_consensus = RRFConfig(use_consensus=False, use_calibration=False)
        results_no_boost = rrf_fusion(bm25_ranks, faiss_ranks, config=config_no_consensus)
        score_no_consensus = results_no_boost[0][1]

        assert score_with_consensus > score_no_consensus

    def test_rrf_fuzzy_integration(self):
        from fran.search_utils import rrf_fusion, RRFConfig

        config = RRFConfig(fuzzy_weight=0.8, use_consensus=True)

        bm25_ranks = {0: (1, 0.8)}
        faiss_ranks = {0: (1, 0.9)}
        fuzzy_ranks = {0: (2, 0.7), 1: (1, 0.85)}

        results = rrf_fusion(bm25_ranks, faiss_ranks, fuzzy_ranks, config=config)

        # idx=0 tiene consenso total (BM25 + FAISS)
        assert results[0][2]["consensus"] is True

    def test_rrf_v316_v317_coherence(self):
        """
        Validar que Top-5 entre v3.16 y v3.17 coincida en >= 80%.
        """
        from fran.search_utils import rrf_fusion, RRFConfig

        # Mismo input
        bm25 = {i: (i + 1, 1.0 - i * 0.1) for i in range(10)}
        faiss = {i: (i + 1, 0.9 - i * 0.1) for i in range(10)}

        # v3.16 config
        config_v316 = RRFConfig(use_consensus=False, use_calibration=False)
        results_v316 = rrf_fusion(bm25, faiss, config=config_v316)

        # v3.17 config
        config_v317 = RRFConfig(use_consensus=True, use_calibration=True)
        results_v317 = rrf_fusion(bm25, faiss, config=config_v317)

        # Top-5 de cada versión
        top5_v316 = {r[0] for r in results_v316[:5]}
        top5_v317 = {r[0] for r in results_v317[:5]}

        # Overlap debe ser >= 80% (4 de 5)
        overlap = len(top5_v316 & top5_v317)
        assert overlap >= 4, f"Coherence fail: overlap {overlap}/5 < 80%"


# ============================================================
# TEST FIX #4: Hydratación de Contexto
# ============================================================

class TestTimestampParsing:
    """Validar parsing robusto de timestamps."""

    def test_parse_unix_timestamp(self):
        from fran.context_utils import parse_timestamp

        ts = parse_timestamp(1701518400.0)
        assert ts == 1701518400.0

        ts = parse_timestamp(1701518400)  # int
        assert ts == 1701518400.0

    def test_parse_iso_string(self):
        from fran.context_utils import parse_timestamp

        ts = parse_timestamp("2025-12-02T10:30:00")
        assert ts is not None
        assert isinstance(ts, float)

        # Validar que está cerca de la fecha esperada
        dt = datetime.fromisoformat("2025-12-02T10:30:00")
        assert abs(ts - dt.timestamp()) < 1.0

    def test_parse_iso_variants(self):
        from fran.context_utils import parse_timestamp

        # Con milisegundos
        ts1 = parse_timestamp("2025-12-02T10:30:00.123456")
        assert ts1 is not None

        # Formato fecha-hora con espacio
        ts2 = parse_timestamp("2025-12-02 10:30:00")
        assert ts2 is not None

        # Solo fecha
        ts3 = parse_timestamp("2025-12-02")
        assert ts3 is not None

    def test_parse_age_minutes(self):
        from fran.context_utils import parse_timestamp

        # 5 minutos atrás
        ts = parse_timestamp(5)
        expected = time.time() - (5 * 60)
        assert abs(ts - expected) < 10  # Margen de 10 segundos

    def test_parse_invalid(self):
        from fran.context_utils import parse_timestamp

        assert parse_timestamp(None) is None
        assert parse_timestamp("invalid-date") is None
        assert parse_timestamp({}) is None

    def test_is_context_expired(self):
        from fran.context_utils import is_context_expired

        # Contexto reciente (no expirado)
        recent = time.time() - 300  # 5 min atrás
        assert not is_context_expired(recent, None, None, None, ttl_seconds=1800)

        # Contexto viejo (expirado)
        old = time.time() - 2000  # 33 min atrás
        assert is_context_expired(old, None, None, None, ttl_seconds=1800)

        # Sin contexto (expirado)
        assert is_context_expired(None, None, None, None)

    def test_build_context_from_search_history(self):
        from fran.context_utils import build_context_from_search_history

        history = {
            "query": "batería honda",
            "products": [{"code": "1234"}],
            "metadata": {"timestamp": "2025-12-02T10:30:00"},
        }

        context = build_context_from_search_history(history)

        assert "last_search" in context
        assert context["last_search"]["query"] == "batería honda"
        assert len(context["last_search"]["results"]) == 1
        assert context["last_search"]["timestamp"] is not None


# ============================================================
# TEST FIX #2: Embeddings con Fallback
# ============================================================

class TestEmbeddings:
    """Validar generación de embeddings con fallback robusto."""

    def test_embedding_generator_basic(self):
        from fran.embedding_utils import EmbeddingGenerator

        gen = EmbeddingGenerator(
            model_name="text-embedding-3-large",
            expected_dim=3072,
        )

        embeddings = gen.generate(["test", "batería honda"])

        assert len(embeddings) == 2
        assert all(emb.shape[0] == 3072 for emb in embeddings)
        assert gen.validate_dimensions(embeddings)

    def test_embedding_normalization(self):
        from fran.embedding_utils import EmbeddingGenerator

        gen = EmbeddingGenerator(expected_dim=3072)
        embeddings = gen.generate(["test"])

        # Verificar que está normalizado (norm ≈ 1.0)
        norm = np.linalg.norm(embeddings[0])
        assert abs(norm - 1.0) < 0.01

    def test_embedding_dimension_validation(self):
        from fran.embedding_utils import EmbeddingGenerator

        gen = EmbeddingGenerator(expected_dim=3072)

        # Embeddings correctos
        correct = [np.random.randn(3072).astype("float32")]
        assert gen.validate_dimensions(correct)

        # Embeddings incorrects
        incorrect = [np.random.randn(384).astype("float32")]
        assert not gen.validate_dimensions(incorrect)

    def test_embedding_padding(self):
        from fran.embedding_utils import EmbeddingGenerator

        gen = EmbeddingGenerator(expected_dim=3072)

        # Embedding de 384 dims (SentenceTransformer fallback)
        small_emb = [np.random.randn(384).astype("float32")]

        # Debe pad a 3072
        padded = gen._pad_or_truncate(small_emb)
        assert padded[0].shape[0] == 3072

    def test_embedding_truncation(self):
        from fran.embedding_utils import EmbeddingGenerator

        gen = EmbeddingGenerator(expected_dim=1536)

        # Embedding de 3072 dims
        large_emb = [np.random.randn(3072).astype("float32")]

        # Debe truncar a 1536
        truncated = gen._pad_or_truncate(large_emb)
        assert truncated[0].shape[0] == 1536

    def test_get_embedding_generator_singleton(self):
        from fran.embedding_utils import get_embedding_generator

        gen1 = get_embedding_generator()
        gen2 = get_embedding_generator()

        # Debe retornar mismo singleton
        assert gen1 is gen2

    def test_generate_embeddings_convenience(self):
        from fran.embedding_utils import generate_embeddings

        embeddings = generate_embeddings(["test"])

        assert len(embeddings) == 1
        assert embeddings[0].shape[0] > 0


# ============================================================
# TEST FIX #5: Pending Actions Queue
# ============================================================

class TestPendingActionsQueue:
    """Validar queue de pending actions sin race conditions."""

    def test_add_and_get_next(self):
        from fran.pending_actions import PendingActionsQueue

        queue = PendingActionsQueue(db_path=":memory:")

        action_id = queue.add(
            phone="test_phone",
            action_type="add_each",
            action_data={"qty": 5},
        )

        assert action_id > 0

        action = queue.get_next("test_phone")
        assert action is not None
        assert action["action_type"] == "add_each"
        assert action["action_data"]["qty"] == 5

    def test_fifo_order(self):
        from fran.pending_actions import PendingActionsQueue

        queue = PendingActionsQueue(db_path=":memory:")

        queue.add("phone1", "action1", {"order": 1})
        queue.add("phone1", "action2", {"order": 2})
        queue.add("phone1", "action3", {"order": 3})

        # Debe retornar en orden FIFO
        action1 = queue.get_next("phone1")
        assert action1["action_data"]["order"] == 1

        queue.mark_processed(action1["id"])

        action2 = queue.get_next("phone1")
        assert action2["action_data"]["order"] == 2

        queue.mark_processed(action2["id"])

        action3 = queue.get_next("phone1")
        assert action3["action_data"]["order"] == 3

    def test_multiple_actions_no_overwrite(self):
        from fran.pending_actions import PendingActionsQueue

        queue = PendingActionsQueue(db_path=":memory:")

        # Agregar 3 acciones para mismo usuario
        queue.add("phone1", "action1", {})
        queue.add("phone1", "action2", {})
        queue.add("phone1", "action3", {})

        # Todas deben estar presentes (no sobrescritura)
        all_actions = queue.get_all("phone1")
        assert len(all_actions) == 3

    def test_ttl_expiration(self):
        from fran.pending_actions import PendingActionsQueue

        queue = PendingActionsQueue(db_path=":memory:")

        # Acción con TTL de 0 minutos (expira inmediatamente)
        queue.add("phone1", "expired", {}, ttl_minutes=0)

        # Esperar 1 segundo
        time.sleep(1)

        # No debe retornar acción expirada
        action = queue.get_next("phone1")
        assert action is None

    def test_mark_processed(self):
        from fran.pending_actions import PendingActionsQueue

        queue = PendingActionsQueue(db_path=":memory:")

        action_id = queue.add("phone1", "test", {})

        action = queue.get_next("phone1")
        assert action["id"] == action_id

        queue.mark_processed(action_id)

        # No debe retornar acción procesada
        next_action = queue.get_next("phone1")
        assert next_action is None

    def test_clear_all(self):
        from fran.pending_actions import PendingActionsQueue

        queue = PendingActionsQueue(db_path=":memory:")

        queue.add("phone1", "action1", {})
        queue.add("phone1", "action2", {})

        assert queue.count_pending("phone1") == 2

        queue.clear("phone1")

        assert queue.count_pending("phone1") == 0

    def test_isolation_between_users(self):
        from fran.pending_actions import PendingActionsQueue

        queue = PendingActionsQueue(db_path=":memory:")

        queue.add("phone1", "action1", {})
        queue.add("phone2", "action2", {})

        # phone1 solo ve su acción
        assert queue.count_pending("phone1") == 1
        assert queue.get_next("phone1")["action_type"] == "action1"

        # phone2 solo ve su acción
        assert queue.count_pending("phone2") == 1
        assert queue.get_next("phone2")["action_type"] == "action2"


# ============================================================
# TEST FIX #7: Multi-Intent Integration
# ============================================================

class TestMultiIntentIntegration:
    """Validar integración opcional de multi-intent."""

    def test_is_multi_intent_enabled(self, monkeypatch):
        from fran.multi_intent_integration import is_multi_intent_enabled

        monkeypatch.setenv("ENABLE_MULTI_INTENT", "false")
        assert not is_multi_intent_enabled()

        monkeypatch.setenv("ENABLE_MULTI_INTENT", "true")
        assert is_multi_intent_enabled()

    def test_should_use_multi_intent_long_message(self, monkeypatch):
        from fran.multi_intent_integration import should_use_multi_intent

        monkeypatch.setenv("ENABLE_MULTI_INTENT", "true")

        # Mensaje largo (>= 20 palabras)
        long_message = " ".join(["palabra"] * 25)
        assert should_use_multi_intent(long_message, {})

    def test_should_use_multi_intent_multiple_clauses(self, monkeypatch):
        from fran.multi_intent_integration import should_use_multi_intent

        monkeypatch.setenv("ENABLE_MULTI_INTENT", "true")

        # Mensaje con múltiples cláusulas
        message = "Hola! Quiero batería. Y también filtro."
        assert should_use_multi_intent(message, {})

    def test_should_use_multi_intent_llm_classification(self, monkeypatch):
        from fran.multi_intent_integration import should_use_multi_intent

        monkeypatch.setenv("ENABLE_MULTI_INTENT", "true")

        classification = {
            "multi_intent": [
                {"type": "social", "confidence": 0.8},
                {"type": "product_search", "confidence": 0.7},
            ]
        }

        assert should_use_multi_intent("Hola quiero batería", classification)


# ============================================================
# TEST DE RELEVANCIA (Bonus)
# ============================================================

class TestRelevanceScoring:
    """Validar cálculo de relevancia."""

    def test_calculate_relevance_score(self):
        from fran.search_utils import calculate_relevance_score

        product = {
            "name": "Batería YTX7L-BS Honda CG 150",
            "categoria_final": "bateria",
            "search_text": "bateria ytx7l bs honda cg 150",
        }

        # Query exacta → score alto
        score = calculate_relevance_score("batería honda cg 150", product)
        assert score >= 70.0

        # Query parcial → score medio
        score = calculate_relevance_score("batería honda", product)
        assert 40.0 <= score <= 80.0

        # Query no relacionada → score bajo
        score = calculate_relevance_score("filtro yamaha", product)
        assert score < 50.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
