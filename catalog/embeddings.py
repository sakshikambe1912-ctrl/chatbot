import math
import time
import ollama
from .models import Product

EMBED_MODEL = "nomic-embed-text"
_CACHE = {}


def embed(text):
    return ollama.embed(model=EMBED_MODEL, input=text)["embeddings"][0]


def _unit(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def product_text(p):
    # Only name, category and description. Price and stock stay as normal filters.
    return f"{p.name}. Category: {p.category}. {p.description}"[:1500]


def embed_products(store=None):
    qs = Product.objects.all()
    if store:
        qs = qs.filter(store=store)
    done = 0
    for p in qs.iterator():
        text = product_text(p)
        if p.embedding and p.embedded_text == text:
            continue  # unchanged, skip
        p.embedding = embed("search_document: " + text)
        p.embedded_text = text
        p.save(update_fields=["embedding", "embedded_text"])
        done += 1
    _CACHE.clear()
    return done


def _load(store):
    entry = _CACHE.get(store.id)
    if entry and time.time() - entry[0] < 60:
        return entry[1]
    rows = [(pid, _unit(v)) for pid, v in
            Product.objects.filter(store=store, embedding__isnull=False)
            .values_list("id", "embedding")]
    _CACHE[store.id] = (time.time(), rows)
    return rows


def semantic_scores(store, query, min_sim):
    q = _unit(embed("search_query: " + query))
    out = []
    for pid, v in _load(store):
        s = sum(a * b for a, b in zip(q, v))
        if s >= min_sim:
            out.append((pid, s))
    out.sort(key=lambda x: -x[1])
    return out