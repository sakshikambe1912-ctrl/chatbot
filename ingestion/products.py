import logging
import hashlib
import threading

from django.db import close_old_connections
from pgvector.django import CosineDistance

from catalog.models import Product

from .models import Chunk
from .pipeline import embed_batch
from .retrieval import embed_query

log = logging.getLogger(__name__)

BATCH = 32
_LOCK = threading.Lock()          # one background indexing job at a time

# attribute keys that add noise (or duplicate other fields) in the embedded text
SKIP_ATTRS = {"brand", "category", "id", "sku", "barcode", "url", "image", "images",
              "thumbnail", "reviews", "meta", "dimensions"}


def _attr_text(attrs):
    """{'color': 'Black', 'tags': ['sport']} -> 'Color: Black. Tags: sport'"""
    parts = []
    for key, value in (attrs or {}).items():
        if str(key).lower() in SKIP_ATTRS or value in (None, "", [], {}):
            continue
        if isinstance(value, dict):
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(x) for x in value if isinstance(x, (str, int, float)))
        if value:
            parts.append(f"{str(key).replace('_', ' ').title()}: {value}")
    return ". ".join(parts)


def product_text(p):
    """The text that gets embedded. Price and stock stay normal database filters."""
    bits = [f"{p.name}."]
    if p.brand:
        bits.append(f"Brand: {p.brand}.")
    if p.category:
        bits.append(f"Category: {p.category}.")
    if p.description:
        bits.append(p.description)
    attrs = _attr_text(p.attributes)
    if attrs:
        bits.append(attrs + ".")
    return " ".join(bits)[:1500]


def index_products(products, force=False, on_batch=None):
    """Create or refresh one Chunk per product. Unchanged products are skipped.
    Returns how many products were embedded. Finished batches stay saved
    even if a later batch fails."""
    products = list(products)
    done = 0
    for start in range(0, len(products), BATCH):
        batch = products[start:start + BATCH]
        texts = {p.id: product_text(p) for p in batch}
        hashes = {pid: hashlib.sha256(t.encode("utf-8")).hexdigest() for pid, t in texts.items()}
        have = dict(Chunk.objects.filter(product_id__in=list(texts))
                    .values_list("product_id", "meta__text_hash"))   # no vectors loaded
        todo = [p for p in batch if force or have.get(p.id) != hashes[p.id]]
        if todo:
            vectors = embed_batch([texts[p.id] for p in todo], "search_document: ")
            Chunk.objects.bulk_create(
                [Chunk(store_id=p.store_id, product=p, text=texts[p.id], embedding=v,
                       meta={"kind": "product", "title": p.name, "text_hash": hashes[p.id]})
                 for p, v in zip(todo, vectors)],
                update_conflicts=True, unique_fields=["product"],
                update_fields=["store", "text", "embedding", "meta"])
            done += len(todo)
        if on_batch:
            on_batch(done, min(start + BATCH, len(products)))
    return done


def index_products_async(products):
    """Index in a background thread so a web request is not slowed down by Ollama.
    Never raises: a failure only prints a message."""
    ids = [p.id for p in products]
    if not ids:
        return

    def work():
        try:
            with _LOCK:
                index_products(Product.objects.filter(id__in=ids))
        except Exception as exc:
            log.warning('PRODUCT INDEXING FAILED: %s', exc)
        finally:
            close_old_connections()

    threading.Thread(target=work, daemon=True).start()


def semantic_scores(store, query, min_sim, limit=300):
    """[(product_id, similarity), ...] best first. Same return shape as the old
    function, but the search now runs inside Postgres."""
    qv = embed_query(query)
    rows = (Chunk.objects.filter(store=store, product__isnull=False)
            .annotate(dist=CosineDistance("embedding", qv))
            .order_by("dist")
            .values_list("product_id", "dist")[:limit])
    scored = [(pid, 1 - dist) for pid, dist in rows]          # cosine similarity = 1 - distance
    return [(pid, s) for pid, s in scored if s >= min_sim]