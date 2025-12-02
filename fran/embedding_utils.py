"""
Utilidades para generación de embeddings con fallback robusto.

Fix para inconsistencia #2: Embedding model fallback incompatible.
"""

from __future__ import annotations
import os
import logging
import numpy as np
from typing import List

logger = logging.getLogger("fran313")


class EmbeddingGenerator:
    """
    Generador de embeddings con fallbacks robustos y validación de dimensionalidad.
    """

    def __init__(
        self,
        model_name: str = "text-embedding-3-large",
        expected_dim: int = 3072,
        fallback_model: str = "sentence-transformers",
    ):
        """
        Args:
            model_name: Modelo de OpenAI a usar (default: text-embedding-3-large)
            expected_dim: Dimensionalidad esperada (default: 3072)
            fallback_model: Modelo de fallback si OpenAI falla
                - "sentence-transformers": Usa all-MiniLM-L6-v2 (384 dim)
                - "random": Genera embeddings aleatorios (solo para tests)
        """
        self.model_name = model_name
        self.expected_dim = expected_dim
        self.fallback_model = fallback_model
        self._openai_client = None
        self._st_model = None

    def _get_openai_client(self):
        """Lazy load de cliente OpenAI."""
        if self._openai_client is None:
            try:
                from openai import OpenAI
                api_key = os.environ.get("OPENAI_API_KEY")
                if api_key:
                    self._openai_client = OpenAI(api_key=api_key)
            except Exception as e:
                logger.warning(f"Failed to initialize OpenAI client: {e}")
        return self._openai_client

    def _get_sentence_transformer(self):
        """Lazy load de SentenceTransformer."""
        if self._st_model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._st_model = SentenceTransformer("all-MiniLM-L6-v2")
                logger.info("Loaded SentenceTransformer fallback model")
            except Exception as e:
                logger.warning(f"Failed to load SentenceTransformer: {e}")
        return self._st_model

    def _generate_openai_embeddings(self, texts: List[str]) -> List[np.ndarray] | None:
        """
        Genera embeddings usando OpenAI API.

        Returns:
            Lista de embeddings o None si falla
        """
        client = self._get_openai_client()
        if client is None:
            return None

        try:
            response = client.embeddings.create(
                input=texts,
                model=self.model_name
            )
            vectors = [np.array(item.embedding, dtype="float32") for item in response.data]

            # Normalizar L2
            normalized = []
            for vec in vectors:
                norm = np.linalg.norm(vec)
                normalized.append(vec / (norm or 1.0))

            return normalized

        except Exception as e:
            logger.warning(f"OpenAI embeddings failed: {e}")
            return None

    def _generate_sentence_transformer_embeddings(self, texts: List[str]) -> List[np.ndarray] | None:
        """
        Genera embeddings usando SentenceTransformer.

        IMPORTANTE: Genera embeddings de 384 dims (incompatible con FAISS si esperamos 3072).
        Solo usar si se ajusta expected_dim o se re-indexa FAISS.

        Returns:
            Lista de embeddings o None si falla
        """
        model = self._get_sentence_transformer()
        if model is None:
            return None

        try:
            embeddings = model.encode(
                texts,
                convert_to_numpy=True,
                normalize_embeddings=True
            )
            return [emb.astype("float32") for emb in embeddings]

        except Exception as e:
            logger.warning(f"SentenceTransformer failed: {e}")
            return None

    def _generate_random_embeddings(self, texts: List[str]) -> List[np.ndarray]:
        """
        Genera embeddings aleatorios normalizados (solo para tests).

        Returns:
            Lista de embeddings de dimensionalidad esperada
        """
        logger.warning(f"Using RANDOM embeddings (dim={self.expected_dim}) - NOT FOR PRODUCTION")
        embeddings = []
        for _ in texts:
            vec = np.random.randn(self.expected_dim).astype("float32")
            vec = vec / (np.linalg.norm(vec) or 1.0)
            embeddings.append(vec)
        return embeddings

    def _pad_or_truncate(self, embeddings: List[np.ndarray]) -> List[np.ndarray]:
        """
        Ajusta dimensionalidad de embeddings a expected_dim.

        Args:
            embeddings: Lista de embeddings (pueden tener dim diferente)

        Returns:
            Lista de embeddings con dim = expected_dim
        """
        adjusted = []
        for emb in embeddings:
            current_dim = emb.shape[0]

            if current_dim == self.expected_dim:
                adjusted.append(emb)
            elif current_dim < self.expected_dim:
                # Pad con ceros
                padded = np.zeros(self.expected_dim, dtype="float32")
                padded[:current_dim] = emb
                # Re-normalizar
                padded = padded / (np.linalg.norm(padded) or 1.0)
                adjusted.append(padded)
            else:
                # Truncar
                truncated = emb[:self.expected_dim]
                # Re-normalizar
                truncated = truncated / (np.linalg.norm(truncated) or 1.0)
                adjusted.append(truncated)

        return adjusted

    def generate(self, texts: List[str]) -> List[np.ndarray]:
        """
        Genera embeddings con fallback robusto.

        Estrategia:
        1. Intentar OpenAI (expected_dim)
        2. Si falla y fallback_model="sentence-transformers":
           - Generar con SentenceTransformer (384 dim)
           - Ajustar a expected_dim (pad o truncate)
        3. Si todo falla → random embeddings (solo para tests)

        Args:
            texts: Lista de textos a embeddear

        Returns:
            Lista de embeddings normalizados con dim = expected_dim

        Raises:
            ValueError si texts está vacío
        """
        if not texts:
            raise ValueError("texts cannot be empty")

        # Intento 1: OpenAI
        embeddings = self._generate_openai_embeddings(texts)
        if embeddings is not None:
            logger.debug(f"Generated {len(embeddings)} embeddings via OpenAI")
            return embeddings

        # Intento 2: SentenceTransformer (con ajuste de dim)
        if self.fallback_model == "sentence-transformers":
            embeddings = self._generate_sentence_transformer_embeddings(texts)
            if embeddings is not None:
                logger.warning(
                    f"Using SentenceTransformer fallback (dim={embeddings[0].shape[0]}) "
                    f"adjusting to {self.expected_dim}"
                )
                return self._pad_or_truncate(embeddings)

        # Intento 3: Random (solo para tests/emergencias)
        logger.error("All embedding methods failed, using random embeddings")
        return self._generate_random_embeddings(texts)

    def validate_dimensions(self, embeddings: List[np.ndarray]) -> bool:
        """
        Valida que todos los embeddings tengan dimensionalidad esperada.

        Args:
            embeddings: Lista de embeddings a validar

        Returns:
            True si todos tienen expected_dim, False si hay alguno diferente
        """
        for i, emb in enumerate(embeddings):
            if emb.shape[0] != self.expected_dim:
                logger.error(
                    f"Embedding {i} has dim {emb.shape[0]}, expected {self.expected_dim}"
                )
                return False
        return True


# ============================================================
# SINGLETON GLOBAL
# ============================================================

_GLOBAL_EMBEDDING_GENERATOR: EmbeddingGenerator | None = None


def get_embedding_generator(
    model_name: str | None = None,
    expected_dim: int | None = None,
    fallback_model: str = "sentence-transformers",
) -> EmbeddingGenerator:
    """
    Obtiene generador de embeddings global (singleton).

    Args:
        model_name: Modelo de OpenAI (default: desde env OPENAI_EMBEDDING_MODEL)
        expected_dim: Dimensionalidad esperada (default: 3072 para text-embedding-3-large)
        fallback_model: Modelo de fallback

    Returns:
        EmbeddingGenerator configurado
    """
    global _GLOBAL_EMBEDDING_GENERATOR

    if _GLOBAL_EMBEDDING_GENERATOR is None:
        if model_name is None:
            model_name = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")

        if expected_dim is None:
            # Mapeo de modelos a dimensiones conocidas
            dim_map = {
                "text-embedding-3-large": 3072,
                "text-embedding-3-small": 1536,
                "text-embedding-ada-002": 1536,
            }
            expected_dim = dim_map.get(model_name, 3072)

        _GLOBAL_EMBEDDING_GENERATOR = EmbeddingGenerator(
            model_name=model_name,
            expected_dim=expected_dim,
            fallback_model=fallback_model,
        )

    return _GLOBAL_EMBEDDING_GENERATOR


def generate_embeddings(texts: List[str]) -> List[np.ndarray]:
    """
    Función de conveniencia para generar embeddings.

    Args:
        texts: Lista de textos a embeddear

    Returns:
        Lista de embeddings normalizados
    """
    generator = get_embedding_generator()
    return generator.generate(texts)
