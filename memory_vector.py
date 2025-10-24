import os
import numpy as np
import faiss
from functools import lru_cache
from openai import OpenAI

# -----------------------------------
# CONFIGURACIÓN DEL CLIENTE OPENAI
# -----------------------------------
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# -----------------------------------
# GENERACIÓN DE EMBEDDINGS
# -----------------------------------
def generar_embeddings(textos):
    """
    Genera embeddings para una lista de textos usando el modelo text-embedding-3-small.
    Se asegura de que todas las entradas sean cadenas válidas.
    """
    try:
        # 🔧 Limpieza de datos: forzamos a string y filtramos vacíos
        textos = [str(t).strip() for t in textos if t and str(t).strip()]

        if not textos:
            print("⚠️ Lista de textos vacía o inválida para generar embeddings.")
            return []

        response = client.embeddings.create(
            input=textos,
            model="text-embedding-3-small"
        )
        return [d.embedding for d in response.data]

    except Exception as e:
        print(f"⚠️ Error generando embeddings: {e}")
        return []


# -----------------------------------
# CREACIÓN DEL ÍNDICE FAISS
# -----------------------------------
@lru_cache(maxsize=1)
def construir_indice_faiss(lista_productos):
    """
    Crea un índice FAISS a partir de una lista de productos con campo 'name'.
    Se cachea para evitar recomputar embeddings en cada consulta.
    """
    try:
        textos = [p.get("name", "") for p in lista_productos]
        embeddings = generar_embeddings(textos)

        if not embeddings:
            print("⚠️ No se generaron embeddings. Verificá los datos del catálogo.")
            return None, None

        vecs = np.array(embeddings).astype("float32")
        index = faiss.IndexFlatL2(vecs.shape[1])
        index.add(vecs)
        print(f"✅ Índice FAISS creado con {len(lista_productos)} productos.")
        return index, lista_productos

    except Exception as e:
        print(f"⚠️ Error creando índice FAISS: {e}")
        return None, None


# -----------------------------------
# BÚSQUEDA SEMÁNTICA
# -----------------------------------
def buscar_semantico(query, lista_productos, top_k=8):
    """
    Busca productos similares en el catálogo usando FAISS y embeddings semánticos.
    """
    try:
        if not query or not lista_productos:
            print("⚠️ Parámetros vacíos en búsqueda semántica.")
            return []

        index, catalogo = construir_indice_faiss(tuple(lista_productos))  # lru_cache requiere hashable
        if index is None:
            return []

        query_emb = generar_embeddings([query])
        if not query_emb:
            return []

        emb_np = np.array([query_emb[0]]).astype("float32")
        D, I = index.search(emb_np, top_k)
        resultados = [catalogo[i] for i in I[0] if i < len(catalogo)]
        return resultados

    except Exception as e:
        print(f"⚠️ Error en búsqueda semántica: {e}")
        return []


# -----------------------------------
# TEST LOCAL (opcional)
# -----------------------------------
if __name__ == "__main__":
    productos = [
        {"code": "001", "name": "Filtro de aire Honda Biz 125", "price_ars": 2500},
        {"code": "002", "name": "Manubrio Yamaha YBR", "price_ars": 6000},
        {"code": "003", "name": "Acrílico tablero Honda Biz 125 Revolution", "price_ars": 280000},
    ]

    resultados = buscar_semantico("acrilico tablero honda biz", productos)
    for r in resultados:
        print(f"🔹 {r['name']} - ${r['price_ars']}")
