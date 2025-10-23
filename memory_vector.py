import os
import json
import faiss
import numpy as np
import openai

openai.api_key = os.environ.get("OPENAI_API_KEY")
INDEX_PATH = "fran_memory.faiss"
META_PATH = "fran_metadata.json"

DIM = 1536
index = faiss.IndexFlatL2(DIM)
memory = {}

if os.path.exists(INDEX_PATH):
    index = faiss.read_index(INDEX_PATH)
    if os.path.exists(META_PATH):
        with open(META_PATH, "r", encoding="utf-8") as f:
            memory = json.load(f)

def embed(text):
    emb = openai.Embedding.create(model="text-embedding-3-large", input=text)
    return np.array(emb["data"][0]["embedding"], dtype=np.float32)

def add_memory(user, text):
    vec = embed(text)
    index.add(np.array([vec]))
    memory[len(memory)] = {"user": user, "text": text}
    save_index()

def query_memory(user, text, k=5):
    if index.ntotal == 0:
        return []
    vec = embed(text)
    D, I = index.search(np.array([vec]), k)
    results = []
    for i in I[0]:
        if i < len(memory) and memory[i]["user"] == user:
            results.append(memory[i]["text"])
    return results

def save_index():
    faiss.write_index(index, INDEX_PATH)
    with open(META_PATH, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)
