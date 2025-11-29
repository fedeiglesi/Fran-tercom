import numpy as np
from sentence_transformers import SentenceTransformer

# Se carga un modelo pequeño para enrutamiento, totalmente dinámico
router_model = SentenceTransformer("all-MiniLM-L6-v2")

def build_catalog_centroid(catalog_texts):
    """
    Construye el vector centroide del catálogo → define el "tema" del dominio.
    """
    embeddings = router_model.encode(catalog_texts, convert_to_numpy=True)
    centroid = np.mean(embeddings, axis=0)
    return centroid / np.linalg.norm(centroid)

def compute_similarity(text, centroid):
    emb = router_model.encode([text], convert_to_numpy=True)[0]
    emb = emb / np.linalg.norm(emb)
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

    sim = compute_similarity(text, catalog_centroid)
    entropy = estimate_entropy(text)

    score = 0.7 * sim + 0.3 * entropy

    if score >= threshold:
        return {"route": "technical", "score": score}
    return {"route": "social", "score": score}
