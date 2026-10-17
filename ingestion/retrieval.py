import hashlib
import logging
import re

from django.core.cache import cache
from django.contrib.postgres.search import SearchQuery, SearchRank, SearchVector
from django.db.models import Q
from pgvector.django import CosineDistance

from .models import Chunk
from .pipeline import embed_batch

log = logging.getLogger(__name__)

FTS = "english"                 # must match the config used in the GIN index
EMBED_CACHE_TTL = 60 * 60       # seconds a query embedding is remembered
EMBED_CACHE_VERSION = "v1"      # change this when you switch the embedding model


def embed_query(query):
    """Embedding of a search query, remembered for an hour so a repeated question
    does not call Ollama again. 'Black  Running Shoes' and 'black running shoes' share one entry."""
    text = " ".join((query or "").lower().split())
    key = "qemb:%s:%s" % (EMBED_CACHE_VERSION, hashlib.sha256(text.encode("utf-8")).hexdigest())
    vec = cache.get(key)
    if vec is None:
        vec = embed_batch([query], "search_query: ")[0]
        cache.set(key, vec, EMBED_CACHE_TTL)
    else:
        log.debug("query embedding cache hit")
    return vec


def rrf(lists, k=60):
    """Reciprocal Rank Fusion: merge several ranked id lists into one."""
    scores = {}
    for lst in lists:
        for rank, item in enumerate(lst):
            scores[item] = scores.get(item, 0) + 1 / (k + rank + 1)
    return sorted(scores, key=scores.get, reverse=True)


def _or_query(text):
    """'black running shoes' -> black | running | shoes (any word may match)."""
    q = None
    for w in re.findall(r"\w+", text.lower()):
        if len(w) < 3:
            continue
        part = SearchQuery(w, config=FTS)
        q = part if q is None else q | part
    return q


def retrieve(store, query, k=5, min_sim=0.55, only_docs=False, extra=None):
    """Hybrid search. Returns up to k Chunk objects, best first, or [] if nothing is relevant.
    only_docs=True searches policies/FAQs only; extra is an optional Q() filter."""
    base = Chunk.objects.filter(store=store)
    if only_docs:
        base = base.filter(document__isnull=False)
    if extra is not None:
        base = base.filter(extra)

    qv = embed_query(query)
    vec = [c for c in base.annotate(dist=CosineDistance("embedding", qv)).order_by("dist")[:30]
           if 1 - c.dist >= min_sim]

    kw = []
    q = _or_query(query)
    if q is not None:
        kw = list(base.annotate(rank=SearchRank(SearchVector("text", config=FTS), q))
                      .filter(rank__gt=0).order_by("-rank")[:30])

    if not vec and not kw:
        return []                      # caller should say "I don't know"

    by_id = {c.id: c for c in vec + kw}
    return [by_id[i] for i in rrf([[c.id for c in vec], [c.id for c in kw]])[:k]]


def retrieve_products(store, query, k=20, min_price=None, max_price=None, brand=None,
                      category=None, color=None, in_stock=False):
    """Hybrid product search. Price, brand, category, colour and stock are SQL filters.
    Returns product ids, best first."""
    f = Q(product__isnull=False)
    if min_price is not None:
        f &= Q(product__price__gte=min_price)
    if max_price is not None:
        f &= Q(product__price__lte=max_price)
    if brand:
        f &= Q(product__brand__iexact=brand) | Q(product__name__icontains=brand)
    if category:
        f &= Q(product__category__iexact=category)
    if color:
        f &= Q(product__name__icontains=color) | Q(product__description__icontains=color)
    if in_stock:
        f &= Q(product__in_stock=True)
    return [c.product_id for c in retrieve(store, query, k=k, extra=f)]
