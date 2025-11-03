# coding: utf-8
"""
Módulo: catalog.py
Responsable de:
- Cargar catálogo CSV (desde GitHub o local)
- Generar embeddings y FAISS index
- Cachear resultados para performance
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
    EMBEDDING_MAX_RETRIES,
    REQUEST_HEADERS,
    REQUESTS_TIMEOUT,
    logger,
)
from fran.utils import strip_accents, normalize_search_query
from openai import OpenAI

# =========================================================
# CLIENTE OPENAI (para embeddings)
# =========================================================

client = OpenAI()

# =========================================================
# DESCARGA Y CARGA DEL CATÁLOGO CSV
# =========================================================

def _load_raw_csv() -> List[Dict[str, str]]:
    """Descarga y parsea el catálogo CSV desde la URL configurada."""
    logger.info(f"📦 Cargando catálogo desde {CATALOG_URL}")
    try:
        response = requests.get(
            CATALOG_URL,
            headers=REQUEST_HEADERS,
            timeout=REQUESTS_TIMEOUT,
        )
        response.raise_for_status()

        # 🔹 Compatibilidad con GitHub RAW (a veces devuelve charset incorrecto)
        content = response.content.decode("utf-8", errors="ignore")
        rows = list(csv.DictReader(io.StringIO(content)))

        if not rows:
            logger.warning("⚠️ Catálogo CSV descargado pero vacío o sin encabezados.")
        else:
            logger.info(f"✅ Catálogo cargado con {len(rows)} productos.")
        return rows
    except Exception as e:
        logger.error(f"❌ Error cargando catálogo: {e}")
        return []

def load_catalog_enriched() -> List[Dict[str, str]]:
    """Normaliza columnas y agrega campos auxiliares para búsquedas."""
    rows = _load_raw_csv()
    if not rows:
        logger.warning("⚠️ No se pudieron cargar filas del catálogo.")
        return []

    catalog = []
    for r in rows:
        name = strip_accents((r.get("name") or r.get("producto") or "").strip())
        code = (r.get("code") or r.get("codigo") or "").strip().upper()
        price_raw = (r.get("price_usd") or r.get("price") or "0").strip()

        # 🔹 Limpieza de valor numérico de precio
        try:
            price = float(price_raw.replace(",", "."))
        except ValueError:
            price = 0.0

        catalog.append({
            "code": code,
            "name": name,
            "price": price,
            "full_text": f"{code} {name}".lower(),
        })
    logger.info(f"🧾 Catálogo enriquecido con {len(catalog)} ítems procesados.")
    return catalog

# =========================================================
# GENERACIÓN DE EMBEDDINGS
# =========================================================

def generate_embeddings_with_cache(
    catalog: List[Dict[str, str]]
) -> Tuple[np.ndarray, List[str]]:
    """Genera embeddings con cache local para evitar recomputar todo el catálogo."""
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
        except Exception as e:
            logger.warning(f"⚠️ No se pudo guardar embeddings_cache: {e}")

    all_embeddings = [cache[t] for t in texts if t in cache]
    embeddings = np.array(all_embeddings).astype("float32")
    logger.info(f"✅ Total embeddings en memoria: {len(embeddings)}")
    return embeddings, texts

# =========================================================
# FAISS INDEX
# =========================================================

def _build_faiss_index_from_catalog(
    catalog: List[Dict[str, str]], embeddings: np.ndarray
) -> faiss.IndexFlatL2:
    """Crea índice FAISS en memoria a partir de embeddings."""
    if embeddings.size == 0:
        raise ValueError("❌ No hay embeddings para construir el índice.")
    index = faiss.IndexFlatL2(embeddings.shape[1])
    index.add(embeddings)
    logger.info(f"📈 FAISS index construido ({index.ntotal} items)")
    return index

def save_faiss_index(index, mapping):
    """Guarda índice FAISS y mapping (posición → producto)."""
    try:
        faiss.write_index(index, FAISS_INDEX_PATH)
        with open(FAISS_MAPPING_PATH, "wb") as f:
            pickle.dump(mapping, f)
        logger.info("💾 FAISS index guardado en disco.")
    except Exception as e:
        logger.error(f"❌ Error guardando FAISS index: {e}")

def load_faiss_index() -> Tuple[Optional[faiss.IndexFlatL2], Optional[List[Dict[str, str]]]]:
    """Carga el índice FAISS y su mapping, si existen."""
    if not (os.path.exists(FAISS_INDEX_PATH) and os.path.exists(FAISS_MAPPING_PATH)):
        logger.warning("⚠️ No se encontró FAISS index. Se generará uno nuevo.")
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
# CARGA COMPLETA DEL CATÁLOGO + ÍNDICE
# =========================================================

@lru_cache(maxsize=1)
def get_catalog_and_index() -> Tuple[List[Dict[str, str]], faiss.IndexFlatL2, List[str]]:
    """Carga todo el stack de catálogo + embeddings + índice FAISS (cacheado)."""
    catalog = load_catalog_enriched()
    if not catalog:
        raise RuntimeError("❌ Catálogo vacío o no disponible.")

    index, mapping = load_faiss_index()
    if index and mapping:
        logger.info("🟢 FAISS index cargado desde disco (cache).")
        return catalog, index, [m["full_text"] for m in mapping]

    embeddings, texts = generate_embeddings_with_cache(catalog)
    index = _build_faiss_index_from_catalog(catalog, embeddings)

    mapping = [{"code": p["code"], "name": p["name"], "full_text": p["full_text"]} for p in catalog]
    save_faiss_index(index, mapping)

    return catalog, index, texts

# =========================================================
# BÚSQUEDA EN FAISS
# =========================================================

def search_catalog(query: str, top_k: int = 10) -> List[Dict[str, str]]:
    """Realiza búsqueda semántica con FAISS."""
    catalog, index, texts = get_catalog_and_index()
    query_norm = normalize_search_query(query)

    try:
        emb_query = client.embeddings.create(model=EMBEDDING_MODEL, input=[query_norm])
        vector = np.array(emb_query.data[0].embedding, dtype="float32").reshape(1, -1)
        distances, indices = index.search(vector, top_k)
        results = []
        for idx, dist in zip(indices[0], distances[0]):
            if idx < len(catalog):
                item = dict(catalog[idx])
                item["score"] = float(1 - dist / (dist + 1e-5))
                results.append(item)
        logger.info(f"🔍 Búsqueda completada: {len(results)} resultados para '{query}'.")
        return results
    except Exception as e:
        logger.error(f"❌ Error en búsqueda FAISS: {e}")
        return []
