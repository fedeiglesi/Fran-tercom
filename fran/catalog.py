# coding: utf-8

"""
Modulo: catalog.py (VERSION MEJORADA CON ENRIQUECIMIENTO FLEXIBLE)
Responsable de:

- Cargar catalogo CSV con multiples formatos
- Enriquecer con todos los campos disponibles
- Generar embeddings optimizados
- Cachear resultados
"""

import os
import io
import csv
import pickle
import requests
import numpy as np
import faiss
from typing import Optional, List, Dict, Tuple
from functools import lru_cache
from fran.config import (
    CATALOG_URL,
    FAISS_INDEX_PATH,
    FAISS_MAPPING_PATH,
    EMBEDDINGS_CACHE_PATH,
    EMBEDDING_MODEL,
    EMBEDDING_BATCH,
    REQUEST_HEADERS,
    REQUESTS_TIMEOUT,
    OPENAI_API_KEY,
    logger,
)
from fran.utils import strip_accents, normalize_search_query
from openai import OpenAI

# =========================================================
# CLIENTE OPENAI
# =========================================================

if not OPENAI_API_KEY:
    logger.error("❌ OPENAI_API_KEY no configurada. Las busquedas semanticas no funcionaran.")
    client = None
else:
    client = OpenAI()

# =========================================================
# DESCARGA Y CARGA DEL CATALOGO CSV
# =========================================================

def _load_raw_csv() -> List[Dict[str, str]]:
    """Descarga y parsea el catalogo CSV desde la URL configurada."""
    logger.info(f"📦 Cargando catalogo desde {CATALOG_URL}")
    try:
        response = requests.get(
            CATALOG_URL,
            headers=REQUEST_HEADERS,
            timeout=REQUESTS_TIMEOUT,
        )
        response.raise_for_status()

        # Intentar multiples encodings
        try:
            content = response.content.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning("⚠️ Encoding UTF-8 fallo, intentando latin-1")
            content = response.content.decode("latin-1", errors="replace")

        rows = list(csv.DictReader(io.StringIO(content)))

        if not rows:
            logger.warning("⚠️ Catalogo CSV descargado pero vacio o sin encabezados.")
        else:
            logger.info(f"✅ Catalogo cargado con {len(rows)} productos.")
        return rows

    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Error de red cargando catalogo: {e}")
        return []
    except Exception as e:
        logger.error(f"❌ Error inesperado cargando catalogo: {e}")
        return []

def load_catalog_enriched() -> List[Dict[str, str]]:
    """
    Normaliza columnas y agrega campos auxiliares para busquedas.
    Soporta multiples formatos de CSV con nombres de columna flexibles.
    """
    rows = _load_raw_csv()
    if not rows:
        logger.warning("⚠️ No se pudieron cargar filas del catalogo.")
        return []

    catalog = []
    for r in rows:
        name = strip_accents((r.get("name") or r.get("producto") or r.get("nombre") or "").strip())
        code = (r.get("code") or r.get("codigo") or "").strip().upper()

        price_raw = (r.get("price_usd") or r.get("price") or r.get("precio_usd") or "0").strip()
        try:
            price_usd = float(price_raw.replace(",", "."))
        except ValueError:
            price_usd = 0.0

        price_ars_raw = (r.get("price_ars") or r.get("precio_ars") or "0").strip()
        try:
            price_ars = float(price_ars_raw.replace(",", "."))
        except ValueError:
            price_ars = 0.0

        if not (name and code):
            continue

        brand = strip_accents((r.get("brand") or r.get("marca") or "").strip())
        category = strip_accents((r.get("category") or r.get("categoria") or "").strip())

        models = strip_accents((
            r.get("models") or
            r.get("modelos") or
            r.get("compatibilidad") or
            r.get("compatible") or
            ""
        ).strip())

        oem = strip_accents((r.get("oem") or r.get("oem_code") or "").strip())

        keywords = strip_accents((r.get("keywords") or r.get("palabras_clave") or "").strip())

        description = strip_accents((r.get("description") or r.get("descripcion") or "").strip())

        full_text_parts = [
            code, name, brand, category, models, oem, keywords
        ]

        full_text = " ".join(part for part in full_text_parts if part).lower()
        full_text = " ".join(full_text.split())

        catalog.append({
            "code": code,
            "name": name,
            "brand": brand,
            "category": category,
            "price": price_usd,
            "price_ars": price_ars,
            "full_text": full_text,
            "models": models,
            "oem": oem,
        })

    logger.info(f"🧾 Catalogo enriquecido con {len(catalog)} items procesados.")
    return catalog

# =========================================================
# GENERACION DE EMBEDDINGS
# =========================================================

def generate_embeddings_with_cache(
    catalog: List[Dict[str, str]]
) -> Tuple[np.ndarray, List[str]]:
    """Genera embeddings con cache local."""

    if not client:
        logger.error("❌ No se puede generar embeddings sin OpenAI client")
        return np.array([]), []

    cache = {}
    if os.path.exists(EMBEDDINGS_CACHE_PATH):
        try:
            with open(EMBEDDINGS_CACHE_PATH, "rb") as f:
                cache = pickle.load(f)
            logger.info(f"🧠 Cache embeddings cargada ({len(cache)} items)")
        except Exception as e:
            logger.warning(f"⚠️ No se pudo leer cache de embeddings: {e}")

    texts = [p["full_text"] for p in catalog]
    new_texts = [t for t in texts if t not in cache]

    if new_texts:
        logger.info(f"⚙️ Generando {len(new_texts)} embeddings nuevos...")
        for i in range(0, len(new_texts), EMBEDDING_BATCH):
            batch = new_texts[i:i + EMBEDDING_BATCH]
            try:
                emb_response = client.embeddings.create(model=EMBEDDING_MODEL, input=batch)
                for text, emb in zip(batch, emb_response.data):
                    cache[text] = emb.embedding
            except Exception as e:
                logger.error(f"❌ Error generando embeddings: {e}")
                break

        try:
            with open(EMBEDDINGS_CACHE_PATH, "wb") as f:
                pickle.dump(cache, f)
            logger.info("💾 Cache de embeddings guardada")
        except Exception as e:
            logger.warning(f"⚠️ No se pudo guardar embeddings_cache: {e}")

    all_embeddings = [cache[t] for t in texts if t in cache]

    if not all_embeddings:
        logger.error("❌ No se pudieron generar embeddings")
        return np.array([]), texts

    embeddings = np.array(all_embeddings).astype("float32")
    logger.info(f"✅ Total embeddings en memoria: {len(embeddings)}")
    return embeddings, texts

# =========================================================
# FAISS INDEX
# =========================================================

def _build_faiss_index_from_catalog(
    catalog: List[Dict[str, str]], embeddings: np.ndarray
) -> Optional[faiss.IndexFlatIP]:
    """
    Crea indice FAISS en memoria usando Inner Product (similitud por coseno).
    """
    if embeddings.size == 0:
        logger.error("❌ No hay embeddings para construir el indice.")
        return None

    try:
        index = faiss.IndexFlatIP(embeddings.shape[1])
        index.add(embeddings)
        logger.info(f"📈 FAISS index construido con IndexFlatIP ({index.ntotal} items)")
        return index
    except Exception as e:
        logger.error(f"❌ Error construyendo indice FAISS: {e}")
        return None

def save_faiss_index(index, mapping):
    """Guarda indice FAISS y mapping."""
    try:
        faiss.write_index(index, FAISS_INDEX_PATH)
        with open(FAISS_MAPPING_PATH, "wb") as f:
            pickle.dump(mapping, f)
        logger.info("💾 FAISS index guardado en disco.")
    except Exception as e:
        logger.error(f"❌ Error guardando FAISS index: {e}")

def load_faiss_index() -> Tuple[Optional[faiss.IndexFlatIP], Optional[List[Dict[str, str]]]]:
    """Carga el indice FAISS y su mapping."""
    if not (os.path.exists(FAISS_INDEX_PATH) and os.path.exists(FAISS_MAPPING_PATH)):
        logger.warning("⚠️ No se encontro FAISS index. Se generara uno nuevo.")
        return None, None
    try:
        index = faiss.read_index(FAISS_INDEX_PATH)
        with open(FAISS_MAPPING_PATH, "rb") as f:
            mapping = pickle.load(f)
        logger.info(f"📚 FAISS index cargado con {len(mapping)} items.")
        return index, mapping
    except Exception as e:
        logger.error(f"❌ Error cargando FAISS index: {e}")
        return None, None

# =========================================================
# CARGA COMPLETA
# =========================================================

@lru_cache(maxsize=1)
def get_catalog_and_index() -> Tuple[List[Dict[str, str]], Optional[faiss.IndexFlatIP], List[str]]:
    """Carga catalogo + embeddings + indice FAISS (cacheado)."""
    catalog = load_catalog_enriched()
    if not catalog:
        logger.error("❌ Catalogo vacio o no disponible.")
        return [], None, []

    index, mapping = load_faiss_index()
    if index and mapping and len(mapping) == len(catalog):
        logger.info("🟢 FAISS index cargado desde disco (cache).")
        return catalog, index, [m["full_text"] for m in mapping]

    if not client:
        logger.warning("⚠️ No se puede generar FAISS index sin OpenAI client")
        return catalog, None, []

    embeddings, texts = generate_embeddings_with_cache(catalog)

    if embeddings.size == 0:
        logger.warning("⚠️ No se pudieron generar embeddings, FAISS no disponible")
        return catalog, None, []

    index = _build_faiss_index_from_catalog(catalog, embeddings)

    if index:
        mapping = [
            {
                "code": p["code"],
                "name": p["name"],
                "full_text": p["full_text"],
                "brand": p.get("brand", ""),
                "category": p.get("category", ""),
            }
            for p in catalog
        ]
        save_faiss_index(index, mapping)

    return catalog, index, texts

# =========================================================
# BUSQUEDA EN FAISS
# =========================================================

def search_catalog(query: str, top_k: int = 10) -> List[Dict[str, str]]:
    """
    Realiza busqueda semantica en FAISS.
    """
    if not client:
        logger.warning("⚠️ Busqueda semantica no disponible sin OpenAI client")
        return []

    try:
        catalog, index, texts = get_catalog_and_index()

        if not index:
            logger.warning("⚠️ FAISS index no disponible")
            return []

        query_norm = normalize_search_query(query)

        emb_query = client.embeddings.create(model=EMBEDDING_MODEL, input=[query_norm])
        vector = np.array(emb_query.data[0].embedding, dtype="float32").reshape(1, -1)

        distances, indices = index.search(vector, min(top_k, len(catalog)))

        results = []
        for idx, score in zip(indices[0], distances[0]):
            if 0 <= idx < len(catalog):
                item = dict(catalog[idx])
                item["score"] = float(score * 100)
                results.append(item)

        logger.info(f"🔍 Busqueda completada: {len(results)} resultados para '{query}'.")
        if results:
            logger.info(f"   Top resultado: {results[0]['name']} (score: {results[0]['score']:.1f})")

        return results

    except Exception as e:
        logger.error(f"❌ Error en busqueda FAISS: {e}")
        return []

# =========================================================
# WARMUP EAGER
# =========================================================

def eager_warmup():
    """Precarga el catalogo y FAISS index al iniciar."""
    try:
        logger.info("🔥 Iniciando warmup del catalogo...")
        catalog, index, texts = get_catalog_and_index()

        if not catalog:
            logger.error("❌ Warmup fallo: catalogo vacio")
            return False

        if not index:
            logger.warning("⚠️ Warmup completado pero FAISS index no disponible")
            return True

        logger.info(f"✅ Warmup exitoso: {len(catalog)} productos, FAISS con {index.ntotal} vectores")
        return True

    except Exception as e:
        logger.error(f"❌ Error en warmup: {e}")
        return False

# =========================================================
# NUEVO
# =========================================================

def get_relevant_products_for_query(query: str, top_k: int = 15) -> List[Dict[str, str]]:
    """
    Devuelve productos relevantes para una pregunta tecnica.
    Ej: "Que bateria lleva una YBR?"
    """
    return search_catalog(query, top_k=top_k)
