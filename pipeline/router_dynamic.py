import logging

import numpy as np

logger = logging.getLogger(__name__)

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

    normalized = []
    for text in catalog_texts:
        if not text:
            continue
        clean = " ".join(str(text).split())
        if clean:
            normalized.append(clean)

    if not normalized:
        return None

    try:
        embeddings = router_model.encode(normalized, convert_to_numpy=True)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[router] Fallo generando embeddings de centroid: %s", exc)
        return None

    if embeddings is None or len(embeddings) == 0:
        return None

    matrix = np.array(embeddings, dtype="float32")
    if matrix.ndim != 2 or matrix.shape[1] == 0:
        return None

    centroid = np.mean(matrix, axis=0)
    norm = float(np.linalg.norm(centroid)) or 0.0
    if norm == 0.0 or np.isnan(norm):
        return None

    validated = centroid / norm
    if np.any(np.isnan(validated)):
        return None

    return validated

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
        result = {"route": "social", "score": entropy, "reason": "short_vowel_token"}
        if catalog_centroid is None:
            result["fallback"] = True
        return result

    # Si no tenemos centroide (por ejemplo, si el catálogo todavía no cargó),
    # usamos un fallback basado en keywords técnicas para evitar clasificar todo
    # como social cuando el mensaje es corto.
    # Fix #6: Mejorado con más keywords y lógica de fallback más robusta
    if catalog_centroid is None:
        # Keywords técnicas expandidas
        technical_keywords = [
            # Repuestos generales
            "filtro", "bujia", "pastilla", "amortiguador", "aceite", "corona",
            "piñon", "cadena", "cdi", "pastillas", "bateria", "llanta", "freno",
            "embrague", "carburador", "llantas", "suspension", "manubrio",
            "escape", "motor", "cilindro", "piston", "biela", "valvula",
            # Sinónimos y variantes
            "repuesto", "repuestos", "pieza", "piezas", "parte", "partes",
            "recambio", "recambios", "accesorio", "accesorios",
            # Acciones técnicas
            "arreglar", "reparar", "cambiar", "instalar", "reemplazar",
            "mantenimiento", "service", "revision",
            # Contexto moto
            "moto", "motocicleta", "ciclomotor", "scooter", "enduro",
            "cuatriciclo", "cuatri",
            # Stock y compra
            "stock", "precio", "cuanto", "cuesta", "sale", "comprar",
            "necesito", "quiero", "busco", "tengo", "vendo",
        ]

        # Marcas expandidas
        technical_brands = [
            "honda", "yamaha", "kawasaki", "suzuki", "bajaj", "zanella",
            "motomel", "corven", "gilera", "keller", "beta", "mondial",
            "benelli", "guerrero", "ktm", "husqvarna", "ducati", "bmw",
            "harley", "triumph", "royal", "enfield", "vespa", "piaggio",
        ]

        # Modelos comunes
        technical_models = [
            "cg150", "cg", "titan", "twister", "wave", "biz", "xr", "tornado",
            "fz", "ybr", "crypton", "mt", "r15", "fazer", "tenere",
            "zb", "rx", "patagonian", "duo", "zr", "sahel",
            "rouser", "pulsar", "dominar", "ns", "discover",
        ]

        # Categorías técnicas
        technical_categories = [
            "bateria", "amortiguador", "aceite", "filtro", "cadena", "bujia",
        ]

        # Indicadores de intención técnica (sin ser keywords exactos)
        technical_intent_patterns = [
            "para mi", "para la", "para el", "de mi", "de la", "de el",
            "cuanto", "precio", "stock", "tengo", "necesito", "quiero",
        ]

        # Prioridad 1: Keywords técnicas directas (alta confianza)
        if any(keyword in text for keyword in technical_keywords):
            return {
                "route": "technical",
                "score": 1.0,
                "reason": "fallback_keywords",
                "fallback": True,
            }

        # Prioridad 2: Marcas (media-alta confianza)
        if any(brand in text for brand in technical_brands):
            return {
                "route": "technical",
                "score": 0.85,
                "reason": "fallback_brand",
                "fallback": True,
            }

        # Prioridad 3: Modelos (media confianza)
        if any(model in text for model in technical_models):
            return {
                "route": "technical",
                "score": 0.75,
                "reason": "fallback_model",
                "fallback": True,
            }

        # Prioridad 4: Categorías (media confianza)
        if any(cat in text for cat in technical_categories):
            return {
                "route": "technical",
                "score": 0.80,
                "reason": "fallback_category",
                "fallback": True,
            }

        # Prioridad 5: Patrones de intención + mensaje largo (baja-media confianza)
        if len(text.split()) >= 5 and any(pattern in text for pattern in technical_intent_patterns):
            return {
                "route": "technical",
                "score": 0.65,
                "reason": "fallback_intent_pattern",
                "fallback": True,
            }

        # Prioridad 6: Mensajes muy cortos con baja entropía → social
        if entropy < 0.2 and len(text.split()) <= 4:
            return {
                "route": "social",
                "score": entropy,
                "reason": "low_entropy_no_centroid",
                "fallback": True,
            }

        # Prioridad 7: Default a technical (cambio importante: antes era "no_centroid")
        # Razón: Es mejor un false positive técnico que perder una venta
        logger.warning(
            f"[router] No centroid available, defaulting to technical for: {text[:50]}"
        )
        return {
            "route": "technical",
            "score": 0.60,
            "reason": "no_centroid_default_technical",
            "fallback": True,
        }

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
