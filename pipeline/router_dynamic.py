import numpy as np

try:
    from sentence_transformers import SentenceTransformer

    # Se carga un modelo pequeño para enrutamiento, totalmente dinámico
    router_model = SentenceTransformer("all-MiniLM-L6-v2")
except Exception:
    class _FallbackRouter:
        def encode(self, texts, convert_to_numpy=True):
            vectors = []
            for text in texts:
                norm = text.lower().strip()
                vec = np.array(
                    [float(len(norm)), float(norm.count(" ")), float(sum(ord(c) for c in norm) % 101)],
                    dtype="float32",
                )
                vectors.append(vec)
            return vectors

    router_model = _FallbackRouter()

def build_catalog_centroid(catalog_texts):
    """
    Construye el vector centroide del catálogo → define el "tema" del dominio.
    """
    if not catalog_texts:
        return None

    embeddings = router_model.encode(catalog_texts, convert_to_numpy=True)
    if embeddings is None or len(embeddings) == 0:
        return None

    centroid = np.mean(embeddings, axis=0)
    norm = float(np.linalg.norm(centroid)) or 0.0
    if norm == 0.0 or np.isnan(norm):
        return None

    return centroid / norm

def compute_similarity(text, centroid):
    if centroid is None:
        return 0.0

    emb = router_model.encode([text], convert_to_numpy=True)[0]
    norm = float(np.linalg.norm(emb)) or 0.0
    if norm == 0.0 or np.isnan(norm):
        return 0.0

    emb = emb / norm
    return float(np.dot(emb, centroid))

def estimate_entropy(text):
    """
    Aproximación suave: mensajes sociales suelen tener baja densidad semántica.
    """
    toks = text.split()
    if len(toks) <= 2:
        return 0.1
    return min(1.0, np.log(len(toks)) / 5.0)

def router_fase0_dynamic(message, catalog_centroid, threshold=0.35):
    """
    Decide si el mensaje pertenece al dominio técnico sin hardcodear palabras.
    """
    text = message.lower().strip()

    entropy = estimate_entropy(text)
    vowel_rich = sum(ch in "aeiouáéíóú" for ch in text)
    if len(text.split()) == 1 and len(text) <= 4 and vowel_rich >= 2 and entropy < 0.25:
        return {"route": "social", "score": entropy, "reason": "short_vowel_token"}

    # Si no tenemos centroide (por ejemplo, si el catálogo todavía no cargó),
    # no podemos medir similitud. En ese caso priorizamos técnico salvo que el
    # mensaje sea muy corto y con baja entropía (saludos genéricos).
    if catalog_centroid is None:
        if entropy < 0.2 and len(text.split()) <= 4:
            return {"route": "social", "score": entropy, "reason": "low_entropy_no_centroid"}
        return {"route": "technical", "score": 1.0, "reason": "no_centroid"}

    sim = compute_similarity(text, catalog_centroid)

    if entropy < 0.2 and len(text.split()) <= 2 and sim < 0.85:
        return {"route": "social", "score": sim, "reason": "low_entropy_low_similarity"}

    score = 0.7 * sim + 0.3 * entropy

    # Para mensajes de una sola palabra, permitimos enrutar a técnico si el
    # score es lo suficientemente alto (ej: "cdi", "corona", "piñon").
    # Solo los marcamos como sociales cuando la señal semántica es baja.
    if len(text.split()) <= 1 and score < (threshold * 0.8):
        return {"route": "social", "score": score}

    if len(text.split()) <= 2 and len(text) <= 5 and score < (threshold * 1.2):
        return {"route": "social", "score": score}

    if score >= threshold:
        return {"route": "technical", "score": score}
    return {"route": "social", "score": score}
