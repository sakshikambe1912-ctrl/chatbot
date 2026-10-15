import re
import time
import difflib
from django.db.models import Q
from catalog.models import Product

STOPWORDS = {
    "show", "me", "give", "find", "get", "want", "need", "looking", "look", "for",
    "products", "product", "items", "item", "with", "price", "prices", "range",
    "free", "cheap", "cheaper", "cheapest", "expensive", "costly", "costliest",
    "most", "best", "top", "good", "under", "below", "above", "over", "within",
    "upto", "up", "to", "the", "a", "an", "any", "have", "you", "do", "does",
    "what", "which", "is", "are", "i", "my", "of", "in", "your", "some", "please",
    "available", "sell", "buy", "rs", "inr", "usd", "and", "or", "ones", "one",
    "than", "less", "more", "lowest", "highest", "budget", "can", "could", "will",
    "search", "searching", "how", "r", "u", "tell", "about", "how", "much", "does",
    "cost", "there", "this", "that", "it", "its", "was", "were", "has", "had",
    "gift", "gifts", "present", "presents", "surprise", "suggest", "recommend",
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
    "ninth", "tenth", "last", "1st", "2nd", "3rd", "4th", "5th", "initial",
    "previous", "earlier", "prior", "former", "latter", "now", "then", "next",
    "also", "instead", "another", "options", "option",
}

SYNONYMS = {
    "perfume": "fragrance", "perfumes": "fragrance", "scent": "fragrance",
    "mobile": "phone", "mobiles": "phone", "smartphone": "phone",
    "smartphones": "phone", "sneakers": "shoes", "trainers": "shoes",
    "earphones": "headphones", "earbuds": "headphones",
}

_CACHE = {}


def get_vocab(store):
    """Words used in this store's product names and categories (cached 5 minutes)."""
    entry = _CACHE.get(store.id)
    if entry and time.time() - entry[0] < 300:
        return entry[1]
    words = set()
    for name, cat in Product.objects.filter(store=store).values_list("name", "category"):
        words.update(re.findall(r"[a-z][a-z'-]{2,}", f"{name} {cat}".lower()))
    _CACHE[store.id] = (time.time(), words)
    return words


def get_categories(store, limit=15):
    cats = (Product.objects.filter(store=store).order_by("category")
            .values_list("category", flat=True).distinct()[:limit])
    return [c for c in cats if c]


def correct_word(word, vocab):
    """Fix spelling mistakes by matching against the store's own words."""
    if word in vocab or len(word) < 4:
        return word
    match = difflib.get_close_matches(word, list(vocab), n=1, cutoff=0.78)
    return match[0] if match else word


def is_catalog_word(store, word):
    """True if the word (or its synonym / plural form) is used in this store's catalog."""
    vocab = get_vocab(store)
    w = SYNONYMS.get(word, word)
    return w in vocab or (w.endswith("s") and w[:-1] in vocab) or (w + "s") in vocab


def _variants(word):
    out = [word]
    if word.endswith("s") and len(word) > 3:
        out.append(word[:-1])
    return out

def _match(word):
    q = Q()
    for v in _variants(word):
        q |= Q(name__icontains=v) | Q(category__icontains=v) | Q(description__icontains=v)
    return q

def _to_card(store, p):
    return {
        "id": p.id,
        "name": p.name,
        "price": float(p.price),
        "currency": store.currency,
        "category": p.category,
        "in_stock": p.in_stock,
        "url": p.url,
        "image": p.image_url,
        "updated": p.updated_at.isoformat(),
    }


def search_products(store, query=None, category=None, min_price=None,
                    max_price=None, in_stock_only=True, sort="relevance", limit=5,
                    brand=None, color=None):
    qs = Product.objects.filter(store=store)
    vocab = get_vocab(store)

    if brand:
        qs = qs.filter(Q(name__icontains=brand) | Q(description__icontains=brand))
    if color:
        qs = qs.filter(Q(name__icontains=color) | Q(description__icontains=color))

    if query:
        for word in re.findall(r"[\w'-]+", query.lower()):
            if word in STOPWORDS or word.isdigit():
                continue
            word = correct_word(SYNONYMS.get(word, word), vocab)
            qs = qs.filter(_match(word))
    if category:
        store_cats = get_categories(store, 100)
        c_low = category.strip().lower()
        matched_cat = next((c for c in store_cats if c.lower() == c_low or c.lower().replace("-", " ") == c_low.replace("-", " ")), None)
        if matched_cat:
            qs = qs.filter(category__iexact=matched_cat)
        else:
            cat = correct_word(SYNONYMS.get(c_low, c_low), vocab)
            qs = qs.filter(Q(category__icontains=cat) | Q(category__icontains=cat.rstrip("s")) | Q(category__icontains=c_low))
    if min_price is not None:
        qs = qs.filter(price__gte=min_price)
    if max_price is not None:
        qs = qs.filter(price__lte=max_price)
    if in_stock_only:
        qs = qs.filter(in_stock=True)

    if sort == "price_low":
        qs = qs.order_by("price")
    elif sort == "price_high":
        qs = qs.order_by("-price")

    return [_to_card(store, p) for p in qs[:limit]]


def search_loose(store, query, min_price=None, max_price=None, sort="relevance",
                 in_stock_only=True, limit=5, brand=None, color=None):
    """Fallback for wordy queries: score products by how many keywords match name/category/description."""
    vocab = get_vocab(store)
    words = []
    for w in re.findall(r"[a-z][a-z'-]{2,}", (query or "").lower()):
        if w in STOPWORDS:
            continue
        words.append(correct_word(SYNONYMS.get(w, w), vocab))
    if not words and not brand and not color:
        return []

    q = Q()
    for w in words:
        for v in _variants(w):
            q |= Q(name__icontains=v) | Q(category__icontains=v) | Q(description__icontains=v)
    qs = Product.objects.filter(store=store)
    if words:
        qs = qs.filter(q)
    if brand:
        qs = qs.filter(Q(name__icontains=brand) | Q(description__icontains=brand))
    if color:
        qs = qs.filter(Q(name__icontains=color) | Q(description__icontains=color))
    if in_stock_only:
        qs = qs.filter(in_stock=True)
    if min_price is not None:
        qs = qs.filter(price__gte=min_price)
    if max_price is not None:
        qs = qs.filter(price__lte=max_price)

    need = len(words) if len(words) <= 2 else (len(words) + 1) // 2
    scored = []
    for p in qs[:300]:
        text = f"{p.name} {p.category} {p.description}".lower()
        score = sum(1 for w in words if any(v in text for v in _variants(w)))
        if not words or score >= need:
            scored.append((score, p))

    if sort == "price_low":
        scored.sort(key=lambda x: x[1].price)
    elif sort == "price_high":
        scored.sort(key=lambda x: -x[1].price)
    else:
        scored.sort(key=lambda x: (-x[0], x[1].price))
    return [_to_card(store, p) for _, p in scored[:limit]]

from catalog.embeddings import semantic_scores

MIN_SIM = 0.62   # tune this in Step 8


def search_semantic(store, query, min_price=None, max_price=None, in_stock_only=True,
                    sort="relevance", limit=5, brand=None, color=None):
    try:
        scored = semantic_scores(store, query, MIN_SIM)
    except Exception as exc:
        print("SEMANTIC FAILED:", exc)
        return []
    if not scored:
        return []
    sim = dict(scored)
    qs = Product.objects.filter(store=store, id__in=list(sim.keys()))
    if brand:
        qs = qs.filter(Q(name__icontains=brand) | Q(description__icontains=brand))
    if color:
        qs = qs.filter(Q(name__icontains=color) | Q(description__icontains=color))
    if min_price is not None:
        qs = qs.filter(price__gte=min_price)
    if max_price is not None:
        qs = qs.filter(price__lte=max_price)
    if in_stock_only:
        qs = qs.filter(in_stock=True)
    prods = list(qs)
    if sort == "price_low":
        prods.sort(key=lambda p: p.price)
    elif sort == "price_high":
        prods.sort(key=lambda p: -p.price)
    else:
        prods.sort(key=lambda p: -sim.get(p.id, 0))
    return [_to_card(store, p) for p in prods[:limit]]