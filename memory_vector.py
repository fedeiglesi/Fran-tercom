import os
import csv
import io
import requests
import numpy as np
import faiss
from functools import lru_cache
from openai import OpenAI

# -----------------------------------
# CONFIGURACIÓN
# -----------------------------------
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
CATALOG_URL = os.getenv(
    "CATALOG_URL",
    "https://raw.githubusercontent.com/fedeiglesi/Fran-tercom/main/LISTA_TERCOM_LIMPIA.csv"
).strip()


# -----------------------------------
# EMBEDDINGS
# -----------------------------------
def generar_embeddings(textos):
    """Genera embeddings con limpieza de texto y manejo de errores."""
    try:
        textos = [str(t).strip() for t in textos if t and str(t).strip()]
        if not textos:
            print("⚠️ Lista vacía para embeddings.")
            return []
        resp = client.embeddings.create(input=textos, model="text-embedding-3-small")
        return [d.embedding for d in resp.data]
    except Exception as e:
        print(f"⚠️ Error generando embeddings: {e}")
        return []


# -----------------------------------
# CARGA CATÁLOGO + ÍNDICE FAISS (ATÓMICO)
# -----------------------------------
@lru_cache(maxsize=1)
def get_catalog_and_index():
    """
    Carga el catálogo y construye el índice FAISS en una única operación.
    - Solo se ejecuta una vez por ciclo de vida del proceso.
    - Thread-safe y atómico: nunca hay estado parcial.
    """
    try:
        r = requests.get(CATALOG_URL, timeout=20)
        r.raise_for_status()
        reader = csv.reader(io.StringIO(r.text))
        rows = list(reader)
        if not rows:
            print("⚠️ Catálogo vacío.")
            return [], None

        header = [h.lower() for h in rows[0]]
        idx_code = header.index("codigo") if "codigo" in header else 0
        idx_name = header.index("producto") if "producto" in header else 1
        idx_ars = header.index("ars") if "ars" in header else 2

        catalogo = []
        for line in rows[1:]:
            if len(line) < 3:
                continue
            catalogo.append({
                "code": line[idx_code].strip(),
                "name": line[idx_name].strip(),
                "price_ars": float(line[idx_ars] or 0)
            })

        print(f"✅ Catálogo cargado: {len(catalogo)} productos")

        textos = [p["name"] for p in catalogo]
        embeddings = generar_embeddings(textos)
        if not embeddings:
            print("⚠️ Fallo generación de embeddings.")
            return catalogo, None

        vecs = np.array(embeddings).astype("float32")
        index = faiss.IndexFlatL2(vecs.shape[1])
        index.add(vecs)
        print("✅ Índice FAISS construido y cacheado.")
        return catalogo, index

    except Exception as e:
        print(f"⚠️ Error construyendo índice FAISS: {e}")
        return [], None


# -----------------------------------
# BÚSQUEDA SEMÁNTICA
# -----------------------------------
def buscar_semantico(query, top_k=8):
    """Busca productos similares al query en el catálogo cacheado."""
    catalogo, index = get_catalog_and_index()
    if not index or not catalogo or not query:
        return []

    query_emb = generar_embeddings([query])
    if not query_emb:
        return []

    emb_np = np.array([query_emb[0]]).astype("float32")
    D, I = index.search(emb_np, top_k)
    return [catalogo[i] for i in I[0] if 0 <= i < len(catalogo)]


# -----------------------------------
# RECARGA MANUAL
# -----------------------------------
def recargar_todo():
    """Invalida la caché y reconstruye el catálogo + índice FAISS."""
    get_catalog_and_index.cache_clear()
    print("♻️ Caché invalidada. Reconstruyendo índice...")
    return get_catalog_and_index()


# -----------------------------------
# TEST LOCAL
# -----------------------------------
if __name__ == "__main__":
    catalogo, index = get_catalog_and_index()
    resultados = buscar_semantico("acrilico tablero honda biz", top_k=5)
    for r in resultados:
        print(f"🔹 {r['name']} - ${r['price_ars']}")
