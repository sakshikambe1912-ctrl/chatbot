import json
import logging
import difflib
import re
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from catalog.models import Product
from . import services as S
from .tools import get_categories, get_vocab, correct_word

log = logging.getLogger(__name__)


# ---------------------------------------------------------------
# 1. What the model must return (the "structured understanding")
# ---------------------------------------------------------------

class Understanding(BaseModel):
    intent: Literal[
        "greeting", "smalltalk", "product_search", "product_followup",
        "product_details", "availability", "comparison", "recommendation",
        "superlative", "clarification", "dislike", "frustrated", "reset",
        "order_help", "cart", "reviews", "discounts", "store_info",
        "human_support", "out_of_scope", "go_back",
    ] = "product_search"
    operation: Literal["new_search", "refine"] = "new_search"
    categories: List[str] = Field(default_factory=list)
    keywords: List[str] = Field(default_factory=list)
    brand: Optional[str] = None
    color: Optional[str] = None
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    attributes: dict = Field(default_factory=dict)
    sort: Literal["relevance", "price_low", "price_high"] = "relevance"
    price_direction: Literal["none", "cheaper", "pricier"] = "none"
    superlative_target: Literal["none", "cheapest", "costliest", "best_rated", "best_value", "top_pick"] = "none"
    remove: List[Literal["categories", "keywords", "brand", "color", "price", "sort", "all"]] = Field(default_factory=list)
    refs: List[int] = Field(default_factory=list)
    ref_query: Optional[str] = None
    go_back_target: Optional[str] = None
    show_more: bool = False
    question: Optional[str] = None


SYSTEM = """You are the language-understanding intelligence of an online shopping assistant for Flipkart. Read the customer's message and return ONE JSON object following the schema. Never answer the customer directly.

Intent classification:
- product_search: the customer is searching for or expressing a need for any physical item, electronic, apparel, gadget, appliance, or household product (e.g. 'find gaming mouse', 'sofa sets', 'cotton t-shirts under 500', 'need something for headache').
- product_followup: the customer is refining, modifying, or continuing the current search (e.g. adding a brand, changing color, specifying size or specs, asking for cheaper/pricier, requesting more options).
- superlative: asking which is the cheapest, costliest, highest rated, best value, or top pick among the shown products (e.g. 'which one is the cheapest', 'show me the most expensive one', 'which has best rating'). Set superlative_target accordingly.
- availability: asking if a product or shown item is available or in stock (e.g. 'is it available', 'is the 2nd one in stock', 'can I buy this').
- product_details: asking about specifications, details, or features of a specific shown product (e.g. 'tell me about the 2nd one', 'what are the specs of the blue one', 'does this have warranty').
- comparison: asking to compare two or more shown products, brands, or models (e.g. 'compare the first two', 'what is the difference between them').
- recommendation: asking for general advice or which product is best to choose.
- go_back: asking to return to an earlier product or search topic (e.g. 'go back to shoes', 'take me back to earlier search', 'return to TVs'). Set go_back_target to the target category or 'previous'.
- clarification: the request is too vague to search directly (e.g. 'suggest a gift', 'surprise me'); provide ONE concise question in "question".
- greeting, smalltalk: friendly chat, pleasantries, compliments, thank you, goodbye.
- out_of_scope: non-shopping topics (coding, homework, general knowledge, weather, trivia, mathematics, politics).
- reset: customer explicitly asks to reset, start over, or clear the search context.
- store_info, order_help, cart, reviews, discounts, human_support: standard e-commerce customer support topics.

Operation:
- new_search: when the customer switches to a DIFFERENT product type, or when there is no active search.
- refine: when the customer stays on the current product type and modifies or adds requirements (brand, color, size, specs, budget).

Parameters:
- categories: category names matching the store catalog.
- keywords: product-type and descriptive words with typos corrected.
- brand: specific brand requested (replaces old brand).
- color: color requested (replaces old color).
- min_price, max_price: numeric limits explicitly stated in the message.
- remove: list of filters the customer wants dropped ('brand', 'color', 'price', 'sort', 'all').
- refs: 1-indexed numbers of shown products referenced ([1], [2], [1, 2]).
- ref_query: descriptive phrase when referring to a product by attribute (e.g. 'the blue one', 'the 16GB one', 'the Puma one')."""


NUM_RE = re.compile(r"\d|\b(one|two|three|four|five|six|seven|eight|nine|ten|hundred|thousand|lakh|free|zero)\b")
FIELDS = ("keywords", "categories", "brand", "color", "min_price", "max_price", "sort")


def recent_messages(convo, n=4):
    qs = convo.messages.all()
    reset_at = (convo.preferences or {}).get("reset_at")
    if reset_at:
        try:
            from django.utils.dateparse import parse_datetime
            dt = parse_datetime(reset_at)
            if dt:
                qs = qs.filter(created_at__gt=dt)
        except Exception:
            pass
    rows = list(qs.order_by("-id")[:n + 1])[::-1]
    if rows and rows[-1].role == "user":
        rows = rows[:-1]                       # drop the message being processed
    return "\n".join(f"{m.role}: {m.content[:200]}" for m in rows[-n:]) or "none"


def prev_state(st):
    p = {k: st.get(k) for k in FIELDS}
    p["keywords"] = p["keywords"] or st.get("query") or ""
    p["categories"] = p["categories"] or ([st["category"]] if st.get("category") else [])
    return p


# ---------------------------------------------------------------
# 2. Ask the model, then VALIDATE everything against the catalogue
# ---------------------------------------------------------------

def match_category(name, cats):
    low = {c.lower(): c for c in cats}
    n = (name or "").strip().lower()
    if n in low:
        return low[n]
    m = difflib.get_close_matches(n, list(low), n=1, cutoff=0.8)
    return low[m[0]] if m else None


def archive_context(convo, old_state, shown):
    """Save the old search context into convo.preferences['history_contexts'] before switching topics."""
    if not old_state:
        return
    has_meaningful = bool(old_state.get("keywords") or old_state.get("categories") or old_state.get("brand"))
    if not has_meaningful:
        return
    prefs = dict(convo.preferences or {})
    hist = list(prefs.get("history_contexts", []))
    topic_parts = [old_state.get("brand"), old_state.get("keywords"), " ".join(old_state.get("categories") or [])]
    topic_label = " ".join(p for p in topic_parts if p).strip() or "previous search"
    entry = {
        "topic_label": topic_label,
        "keywords": old_state.get("keywords", ""),
        "categories": list(old_state.get("categories") or []),
        "brand": old_state.get("brand"),
        "color": old_state.get("color"),
        "min_price": old_state.get("min_price"),
        "max_price": old_state.get("max_price"),
        "lowest": old_state.get("lowest"),
        "highest": old_state.get("highest"),
        "sort": old_state.get("sort"),
        "shown_ids": [p.id for p in shown] if shown else list(convo.last_shown_ids or []),
    }
    # Avoid duplicate top entry
    if hist and hist[-1].get("keywords") == entry["keywords"] and hist[-1].get("categories") == entry["categories"] and hist[-1].get("brand") == entry["brand"]:
        return
    hist.append(entry)
    prefs["history_contexts"] = hist[-10:]
    convo.preferences = prefs
    convo.save(update_fields=["preferences"])


def restore_context(convo, topic=""):
    """Retrieve and return the matching previous context from history_contexts."""
    prefs = dict(convo.preferences or {})
    hist = list(prefs.get("history_contexts", []))
    if not hist:
        return None
    matched_idx = -1
    t = (topic or "").lower().strip()
    if t and t not in ("previous", "earlier", "last", "first"):
        for idx in range(len(hist) - 1, -1, -1):
            h = hist[idx]
            blob = f"{h.get('topic_label', '')} {h.get('keywords', '')} {' '.join(h.get('categories', []))} {h.get('brand', '')}".lower()
            if any(w in blob for w in t.split() if len(w) > 2):
                matched_idx = idx
                break
    if matched_idx == -1:
        if t in ("first", "initial", "1st"):
            matched_idx = 0
        else:
            matched_idx = len(hist) - 1
    if matched_idx != -1:
        entry = hist.pop(matched_idx)
        prefs["history_contexts"] = hist
        convo.preferences = prefs
        convo.save(update_fields=["preferences"])
        return entry
    return None


COMMON_COLORS = ["black", "white", "blue", "red", "green", "grey", "gray", "yellow", "pink", "purple", "orange", "brown", "gold", "silver"]

CHEAPEST_RE = re.compile(
    r"\bwhich\s+(?:one\s+)?(?:is\s+)?(?:the\s+)?cheapest\b|"
    r"\bwhich\s+(?:is\s+)?(?:the\s+)?cheapest\s+one\b|"
    r"\bcheapest\s+one\b|\blowest\s+price\s+one\b|\bwhich\s+is\s+cheapest\b",
    re.I
)

AVAIL_RE = re.compile(
    r"\b(is\s+it|is\s+this|are\s+they|is\s+that)\s+(?:in\s+stock|available)\b|"
    r"^\s*is\s+it\s+available\??\s*$|^\s*is\s+it\s+in\s+stock\??\s*$",
    re.I
)

GO_BACK_RE = re.compile(
    r"\b(go\s+back|take\s+me\s+back|previous\s+search|switch\s+back|back\s+to\s+(?:the\s+)?|return\s+to\s+(?:the\s+)?)\b",
    re.I
)


ORDINAL_STOPWORDS = {
    "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth",
    "ninth", "tenth", "last", "1st", "2nd", "3rd", "4th", "5th", "initial",
    "previous", "earlier", "prior", "former", "latter", "one", "ones"
}

OUT_OF_SCOPE_RE = re.compile(
    r"\b(capital\s+of|weather\s+in|who\s+is\s+the\s+president|quantum\s+physics|"
    r"explain\s+(?:theory|relativity|calculus|photosynthesis|gravity|physics|chemistry|history)|"
    r"solve\s+\d|meaning\s+of\s+life|write\s+(?:a\s+)?(?:poem|essay|code|script))\b",
    re.I
)


def validate(store, u, text, shown=()):
    t = S.normalize_numbers(text.lower())
    vocab = get_vocab(store)
    all_cats = get_categories(store, 100)

    cats, extra = [], []
    for c in u.categories:
        real = match_category(c, all_cats)
        if real and real not in cats:
            cats.append(real)
        elif not real and c:
            extra += c.split()

    words = []
    for w in S.content_words(" ".join(list(u.keywords) + extra)):
        w = correct_word(w, vocab)
        if w not in words and w.lower() not in ORDINAL_STOPWORDS:
            words.append(w)
    if not words:
        raw_words = [w for w in S.content_words(text) if w != (u.brand or "").lower() and w != (u.color or "").lower() and w.lower() not in ORDINAL_STOPWORDS]
        if raw_words:
            words = raw_words
    u.categories, u.keywords = cats, words

    # Brand detection fallback
    if not u.brand:
        for b in S.KNOWN_BRANDS:
            if re.search(rf"\b{re.escape(b)}\b", text.lower()):
                u.brand = b.title()
                break

    # Color detection fallback
    if not u.color:
        for c in COMMON_COLORS:
            if re.search(rf"\b{c}\b", text.lower()):
                u.color = c
                break

    # Filter removal detection
    if re.search(r"\b(remove|clear|drop|delete|no|without|any)\s+(?:the\s+)?colou?r\b|\bany\s+colou?r\b", t):
        if "color" not in u.remove:
            u.remove.append("color")
        u.color = None
    if re.search(r"\b(remove|clear|drop|delete|no|without|any)\s+(?:the\s+)?brand\b|\bany\s+brand\b", t):
        if "brand" not in u.remove:
            u.remove.append("brand")
        u.brand = None
    if re.search(r"\b(remove|clear|drop|delete|no|without)\s+(?:the\s+)?(?:price|budget|limit)\b|\bno\s+budget\b|\bany\s+price\b", t):
        if "price" not in u.remove:
            u.remove.append("price")
        u.min_price = u.max_price = None

    # Ordinal extraction
    ord_refs = S.ordinal_refs(t, len(shown) if shown else 10)
    if ord_refs and not u.refs:
        u.refs = ord_refs

    is_details_inquiry = bool(re.search(
        r"\b(tell\s+me\s+about|what\s+about|how\s+about|details?\s+of|specs?\s+of|specifications?\s+of|"
        r"info\s+(?:on|about)|features?\s+of|tell\s+me\s+more|explain|describe|how\s+much\s+is\s+the|"
        r"price\s+of\s+the|show\s+me\s+the)\b|"
        r"^\s*(?:show\s+me\s+)?(?:the\s+)?(?:first|second|third|fourth|fifth|last|1st|2nd|3rd|4th|5th)\s*(?:one|option|product|item)?\s*\??\s*$",
        t
    ))

    # Intent overrides based on deterministic expressions
    if AVAIL_RE.search(t):
        u.intent = "availability"
    elif CHEAPEST_RE.search(t):
        u.intent = "superlative"
        u.superlative_target = "cheapest"
        u.sort = "price_low"
    elif re.search(r"\b(which\s+(?:is\s+)?(?:the\s+)?(?:most\s+expensive|costliest|priciest))\b", t):
        u.intent = "superlative"
        u.superlative_target = "costliest"
        u.sort = "price_high"
    elif GO_BACK_RE.search(t):
        u.intent = "go_back"
    elif re.search(r"\b(compare|comparison|difference\s+between|vs\.?|versus)\b", t):
        u.intent = "comparison"
    elif OUT_OF_SCOPE_RE.search(t):
        u.intent = "out_of_scope"
    elif shown and (ord_refs or u.refs or is_details_inquiry) and not u.brand and not u.categories:
        u.intent = "product_details"

    u.brand = (u.brand or "").strip().title() or None
    u.color = (u.color or "").strip().lower() or None

    p_price = S.parse_price(t)
    if "min_price" in p_price:
        u.min_price = p_price["min_price"]
    if "max_price" in p_price:
        u.max_price = p_price["max_price"]

    if not NUM_RE.search(t):
        u.min_price = u.max_price = None
    for attr in ("min_price", "max_price"):
        v = getattr(u, attr)
        if v is not None and (v != v or v < 0 or v > 1e9):
            setattr(u, attr, None)
    if u.min_price is not None and u.max_price is not None and u.min_price > u.max_price:
        u.min_price, u.max_price = u.max_price, u.min_price
    if u.max_price == 0 and not re.search(r"free|zero|\b0\b", t):
        u.max_price = None

    u.refs = [r for r in u.refs if isinstance(r, int) and r >= 1][:5]

    # Guardrails against intent leakage from previous conversation turns:
    is_superlative_text = bool(
        CHEAPEST_RE.search(t)
        or re.search(r"\b(which\s+(?:is\s+)?(?:the\s+)?(?:most\s+expensive|costliest|priciest|best\s+rated|highest\s+price))\b|\b(cheapest|costliest|priciest|lowest\s+price)\b", t)
    )
    if u.intent == "superlative" and not is_superlative_text:
        u.intent = "product_followup" if (u.brand or u.color or u.categories) else "product_search"
        u.operation = "refine"

    if u.intent == "availability" and not AVAIL_RE.search(t) and not re.search(r"\b(stock|available|in\s+stock)\b", t):
        u.intent = "product_followup" if (u.brand or u.color or u.categories) else "product_search"
        u.operation = "refine"

    if u.intent == "go_back" and not GO_BACK_RE.search(t):
        u.intent = "product_followup" if (u.brand or u.color or u.categories) else "product_search"
        u.operation = "refine"

    # An actionable filter change (brand, color, category, price, keywords) should never be intercepted by clarification
    if u.intent == "clarification" and (u.brand or u.color or u.categories or u.keywords or u.min_price is not None or u.max_price is not None):
        u.intent = "product_followup"
        u.operation = "refine"
    return u


def understand(store, convo, st, shown, text):
    cur = prev_state(st)
    cur_view = {k: v for k, v in cur.items() if v not in (None, "", [])}
    listing = "\n".join(f"{i + 1}. {p.name}" for i, p in enumerate(shown)) or "none"
    prompt = (f"Store categories: {', '.join(get_categories(store, 60)) or 'none'}\n"
              f"Current search: {cur_view or 'none'}\n"
              f"Products shown last:\n{listing}\n"
              f"Recent messages:\n{recent_messages(convo)}\n\n"
              f"Latest customer message: {text}\n\nJSON:")
    try:
        resp = S.llm(messages=[{"role": "system", "content": SYSTEM},
                               {"role": "user", "content": prompt}],
                     format=Understanding.model_json_schema(),
                     options={"temperature": 0})
        u = Understanding.model_validate_json(resp.message.content)
    except S.ModelDown:
        raise
    except Exception as exc:
        log.warning('UNDERSTAND FAILED: %s', exc)
        u = Understanding(intent="product_search", keywords=S.content_words(text))
    return validate(store, u, text, shown)


# ---------------------------------------------------------------
# 3. Fixed, honest replies
# ---------------------------------------------------------------

def fixed_reply(store, intent, text=""):
    if intent == "store_info":
        return S.policy_reply(store, text)
    if intent == "order_help":
        return S.order_reply(store)
    if intent == "discounts":
        return S.discount_reply(store)
    if intent == "human_support":
        return S.human_support_reply(store)
    if intent == "cart":
        return ("To purchase an item, please click the 'View' or product link on any product card above, "
                "select your options, and add it to your cart for secure checkout.")
    if intent == "reviews":
        return ("Customer reviews and ratings are available on individual product detail pages. "
                "I can also compare prices, specifications, and availability across our catalogue for you.")
    if intent == "frustrated":
        return ("Sorry about that! Let me help you find exactly what you need. Could you tell me the most "
                "important feature, brand, or budget you have in mind?")
    return None


# ---------------------------------------------------------------
# 4. Search: apply add / replace / remove to the saved state
# ---------------------------------------------------------------

def merge_words(old, new):
    out = []
    for w in (old or "").split() + list(new):
        if S.norm(w) not in [S.norm(x) for x in out]:
            out.append(w)
    return " ".join(out)


def params(n, cat, sort=None):
    p = {
        "query": n.get("keywords") or "",
        "category": cat,
        "brand": n.get("brand"),
        "color": n.get("color"),
        "sort": sort or n.get("sort") or "relevance",
    }
    if n.get("min_price") is not None:
        p["min_price"] = n["min_price"]
    if n.get("max_price") is not None:
        p["max_price"] = n["max_price"]
    return p


def search_flow(store, convo, st, u, text, shown, reco=False):
    old = prev_state(st)
    has_prev = bool(old["keywords"] or old["categories"] or old["brand"] or old["color"])
    named = bool(u.categories or u.keywords or u.brand or u.color)
    tweak = bool(u.min_price is not None or u.max_price is not None or u.price_direction != "none"
                 or u.show_more or u.sort != "relevance" or u.remove)

    is_gift = bool(S.GIFT_RE.search(text))
    if is_gift and not u.categories and not u.keywords:
        gift_cats = ["fragrances", "womens-jewellery", "mens-watches", "womens-watches", "beauty", "sunglasses", "accessories"]
        store_cats = get_categories(store, 100)
        matched_gift = [c for c in gift_cats if c in store_cats]
        if matched_gift:
            u.categories = matched_gift[:3]
            named = True
            reco = True

    op = u.operation

    # Category switch detection: prevent old shoe filters leaking into laptops/phones etc.
    old_nouns = {noun for noun in S.CATEGORY_NOUNS if any(v in f"{old['keywords']} {' '.join(old['categories'])}".lower() for v in S.CATEGORY_NOUNS[noun])}
    new_nouns = {noun for noun in S.CATEGORY_NOUNS if any(re.search(rf"\b{re.escape(v)}\b", text.lower()) for v in S.CATEGORY_NOUNS[noun])}

    if not has_prev:
        op = "new_search"
    elif old_nouns and new_nouns and not (old_nouns & new_nouns):
        op = "new_search"                       # distinct category noun detected: complete context switch
    elif u.categories and set(u.categories) != set(old["categories"]):
        op = "new_search"                       # a different product type: nothing is inherited
    elif op == "new_search" and not named and tweak:
        op = "refine"

    # If context was reset and user sends bare tweak (e.g. "show me cheaper ones" after reset), don't hallucinate
    if not has_prev and not named:
        return ("What product would you like to search for?", [])

    if op == "new_search" and not named and not tweak:
        return S.which_product_reply(store), []

    if op == "refine" and st.get("multi") and not u.categories and (u.price_direction != "none" or u.show_more):
        return S.clarify_reply(st), []

    if op == "new_search":
        # Archive previous shopping context so user can go back later!
        if has_prev:
            archive_context(convo, old, shown)
        new = {
            "keywords": " ".join(u.keywords),
            "categories": list(u.categories),
            "brand": u.brand,
            "color": u.color,
            "min_price": u.min_price,
            "max_price": u.max_price,
            "sort": u.sort if u.sort != "relevance" else None
        }
        exclude = set()
    else:
        new = dict(old)
        for key in u.remove:
            if key == "price":
                new["min_price"] = new["max_price"] = None
            elif key == "categories":
                new["categories"] = []
            elif key == "keywords":
                new["keywords"] = ""
            elif key in ("brand", "color", "sort"):
                new[key] = None
        if u.categories:
            new["categories"] = list(u.categories)
        if u.keywords:
            new["keywords"] = merge_words(new["keywords"], u.keywords)
        if u.brand:
            # When brand is replaced, remove old brand from keywords so it doesn't conflict
            if old.get("brand") and u.brand.lower() != old["brand"].lower():
                old_b = old["brand"].lower()
                new["keywords"] = " ".join(w for w in (new.get("keywords") or "").split() if w.lower() != old_b)
            new["brand"] = u.brand
        if u.color:
            new["color"] = u.color
        if u.min_price is not None:
            new["min_price"] = u.min_price
        if u.max_price is not None:
            new["max_price"] = u.max_price
        if u.sort != "relevance":
            new["sort"] = u.sort
        exclude = set(st.get("rejected", []))

    eff = dict(new)
    if u.price_direction == "cheaper":
        if st.get("lowest") is not None:
            cap = st["lowest"] - 0.01
            eff["max_price"] = min(eff["max_price"], cap) if eff.get("max_price") is not None else cap
            eff["sort"] = "price_high"
        else:
            eff["sort"] = "price_low"
    elif u.price_direction == "pricier":
        if st.get("highest") is not None:
            floor = st["highest"] + 0.01
            eff["min_price"] = max(eff["min_price"], floor) if eff.get("min_price") is not None else floor
            eff["sort"] = "price_low"
        else:
            eff["sort"] = "price_high"
    if u.show_more:
        exclude |= set(st.get("seen", []))

    cats = new.get("categories") or []
    if len(cats) >= 2:
        results, ids = [], set()
        for c in cats:
            for r in S.run_search(store, params(eff, c), exclude=exclude, limit=3):
                if r["id"] not in ids:
                    ids.add(r["id"])
                    results.append(r)
        terms = cats
        sq = params(eff, None)
    else:
        sq = params(eff, cats[0] if cats else None)
        results = S.run_search(store, sq, exclude=exclude, relax=(op == "new_search"))
        terms = None
    log.info('SEARCH: %s %s %s %s', op, sq, "exclude:", len(exclude))

    if results:
        keep = dict(new)
        keep["rejected"] = st.get("rejected", []) if op == "refine" else []
        keep["seen"] = st.get("seen", []) if u.show_more else []
        return S.present(store, convo, keep, results, text, keep_seen=u.show_more,
                         reco=reco, terms=terms)

    # Nothing found: say exactly why.
    if u.price_direction != "none":
        cheaper = u.price_direction == "cheaper"
        edge = S.run_search(store, {"query": sq.get("query"), "category": sq.get("category"),
                                    "brand": sq.get("brand"), "color": sq.get("color"),
                                    "sort": "price_low" if cheaper else "price_high"}, limit=1)
        if edge:
            p = edge[0]
            word = "least expensive" if cheaper else "most expensive"
            brand_phrase = f"{sq.get('brand')} " if sq.get('brand') else ""
            return (f"These are the {word} matching {brand_phrase}products currently available in our catalogue "
                    f"({p['name']} at {S.fmt_price(p['price'], p.get('currency', store.currency))}). Would you like to change your "
                    "budget slightly or explore a different category?"), edge
    if u.show_more:
        return ("That's everything matching in our catalogue right now. Would you like to change "
                "your budget or look at a different category?"), []

    S.save_state(convo, {**new, "seen": []})
    label = sq["query"] or ", ".join(cats) or text
    return S.finish_none(store, convo, sq, label)


# ---------------------------------------------------------------
# 5. Questions about products already shown
# ---------------------------------------------------------------

# ---------------------------------------------------------------
# 5. Questions about products already shown & generalized reference resolution
# ---------------------------------------------------------------

def resolve_referenced_product(store, st, text, shown, refs=None, ref_query=None):
    """
    Generalized product resolver. Identifies which product from `shown` (or catalog)
    is being referred to by:
    - Ordinal position (1st, 2nd, 3rd, last, previous)
    - Superlative expression (cheapest, most expensive, best)
    - Distinctive attribute / keyword in name / brand / color / specs (e.g. "the blue one", "the 16GB one", "the Puma one")
    - Active conversation focus product
    """
    if not shown:
        fid = st.get("focus")
        if fid:
            return Product.objects.filter(store=store, id=fid).first()
        return None
    t = (text or "").lower()

    # 1. By explicit ordinal refs
    if refs and isinstance(refs[0], int) and 1 <= refs[0] <= len(shown):
        return shown[refs[0] - 1]
    ord_refs = S.ordinal_refs(t, len(shown))
    if ord_refs and 1 <= ord_refs[0] <= len(shown):
        return shown[ord_refs[0] - 1]
    if re.search(r"\b(last\s+one|the\s+last)\b", t):
        return shown[-1]

    # 2. By superlative reference
    if re.search(r"\b(cheapest|lowest\s+price|least\s+expensive|most\s+affordable|sasta)\b", t):
        return min(shown, key=lambda p: p.price)
    if re.search(r"\b(most\s+expensive|costliest|priciest|highest\s+price|premium)\b", t):
        return max(shown, key=lambda p: p.price)

    # 3. By descriptive ref_query or distinguishing feature
    query_hint = (ref_query or "").strip().lower()
    if not query_hint:
        m = re.search(r"\b(?:the|about\s+the|is\s+the)\s+([a-zA-Z0-9\s-]{2,25})\s+one\b", t)
        if m:
            query_hint = m.group(1).strip()

    if query_hint:
        for p in shown:
            blob = f"{p.name} {p.brand} {p.description} {json.dumps(getattr(p, 'attributes', {}))}".lower()
            if query_hint in blob or all(w in blob for w in query_hint.split() if len(w) > 2):
                return p

    # 4. By any distinctive content words in message matching exactly one shown product
    content_matches = []
    c_words = [w for w in S.content_words(t) if w not in ("tell", "about", "product", "item", "available", "stock", "price", "cost")]
    if c_words:
        for p in shown:
            p_blob = f"{p.name} {p.brand}".lower()
            if any(w in p_blob for w in c_words):
                content_matches.append(p)
        if len(content_matches) == 1:
            return content_matches[0]

    # 5. Check active focus product in state
    fid = st.get("focus")
    if fid:
        for p in shown:
            if p.id == fid:
                return p
        p = Product.objects.filter(store=store, id=fid).first()
        if p:
            return p

    # 6. Single product fallback
    if len(shown) == 1:
        return shown[0]

    return None


def handle_superlative(store, convo, st, shown, text, u):
    t = text.lower()
    is_cheapest = bool(getattr(u, "superlative_target", "none") in ("cheapest", "best_value") or re.search(r"\b(cheapest|lowest\s+price|least\s+expensive|most\s+affordable|sasta)\b", t) or re.search(r"\bwhich\s+(?:one\s+)?is\s+cheaper\b", t))
    is_priciest = bool(getattr(u, "superlative_target", "none") == "costliest" or re.search(r"\b(most\s+expensive|costliest|priciest|highest\s+price|premium)\b", t))

    if not shown:
        if prev_state(st)["keywords"] or prev_state(st)["categories"]:
            sort_dir = "price_low" if is_cheapest else ("price_high" if is_priciest else "relevance")
            res = S.run_search(store, {**prev_state(st), "sort": sort_dir}, limit=1)
            if res:
                word = "least expensive" if is_cheapest else ("most premium" if is_priciest else "best matching")
                return (f"The {word} option matching your search in our catalogue is {res[0]['name']} for {S.fmt_price(res[0]['price'], store.currency)}.", res)
        return ("Which product category are you looking for the best or cheapest option in? Tell me what you need and your budget.", [])

    if is_cheapest:
        cheapest = min(shown, key=lambda p: p.price)
        st["focus"] = cheapest.id
        st["focus_name"] = cheapest.name
        S.save_state(convo, st)
        S.save_active_context(convo, {"product_id": cheapest.id, "product_name": cheapest.name, "topic": getattr(cheapest, "category", "") or "product", "task": "compare"})
        if len(shown) >= 2:
            others = [p for p in shown if p.id != cheapest.id]
            comp_bits = ", ".join(f"{p.name} ({S.fmt_price(p.price, store.currency)})" for p in others[:2])
            reply = f"{cheapest.name} is the cheaper option at {S.fmt_price(cheapest.price, store.currency)} (compared to {comp_bits})."
        else:
            reply = f"The cheapest option among these results is {cheapest.name} for {S.fmt_price(cheapest.price, store.currency)}."
        return (reply, [S.item(store, cheapest)])

    if is_priciest:
        priciest = max(shown, key=lambda p: p.price)
        st["focus"] = priciest.id
        st["focus_name"] = priciest.name
        S.save_state(convo, st)
        S.save_active_context(convo, {"product_id": priciest.id, "product_name": priciest.name, "topic": getattr(priciest, "category", "") or "product", "task": "compare"})
        return (f"The most premium option among these results is {priciest.name} for {S.fmt_price(priciest.price, store.currency)}.", [S.item(store, priciest)])

    # General recommendation or best value among shown
    return S.recommend(store, convo, st, shown, text)


def handle_availability(store, convo, st, shown, text, u):
    p = resolve_referenced_product(store, st, text, shown, u.refs, getattr(u, "ref_query", None))
    if p:
        st["focus"] = p.id
        S.save_state(convo, st)
        if p.in_stock:
            reply = f"Yes, {p.name} is in stock and available for {S.fmt_price(p.price, store.currency)}."
        else:
            reply = f"I'm sorry, {p.name} is currently out of stock."
        return (reply, [S.item(store, p)])

    if shown:
        in_stock = [p for p in shown if p.in_stock]
        if in_stock:
            items_summary = "; ".join(f"{p.name} ({S.fmt_price(p.price, store.currency)})" for p in in_stock[:3])
            return (f"Yes, the following matching products are currently in stock: {items_summary}.", [S.item(store, p) for p in in_stock[:3]])
        else:
            return ("None of the displayed products are currently in stock.", [])

    return ("Which product would you like me to check the availability for?", [])


def handle_go_back(store, convo, st, shown, text, u):
    target_topic = getattr(u, "go_back_target", None) or ""
    if not target_topic:
        m = re.search(r"\b(?:back\s+to|return\s+to|switch\s+to|previous\s+search\s+for)\s+(?:the\s+)?([a-zA-Z0-9\s-]{2,25})\b", text.lower())
        if m:
            target_topic = m.group(1).replace("search", "").strip()

    restored = restore_context(convo, target_topic)
    if not restored:
        return ("Which earlier product search would you like to return to? Tell me the category or product name.", [])

    old_curr = prev_state(st)
    archive_context(convo, old_curr, shown)

    st = dict(restored)
    u.operation = "refine"
    u.keywords = [w for w in (restored.get("keywords") or "").split()]
    u.categories = list(restored.get("categories") or [])
    u.brand = restored.get("brand")
    u.color = restored.get("color")

    p_price = S.parse_price(text)
    if "max_price" in p_price:
        u.max_price = p_price["max_price"]
    elif restored.get("max_price"):
        u.max_price = restored.get("max_price")
    if "min_price" in p_price:
        u.min_price = p_price["min_price"]
    elif restored.get("min_price"):
        u.min_price = restored.get("min_price")

    if S.CHEAPER_RE.search(text):
        u.price_direction = "cheaper"
    elif S.PRICIER_RE.search(text):
        u.price_direction = "pricier"

    return search_flow(store, convo, st, u, text, shown)


def product_flow(store, convo, st, u, text, shown):
    p = resolve_referenced_product(store, st, text, shown, u.refs, getattr(u, "ref_query", None))
    if not p:
        clean_name = re.sub(r"\b(tell me about|what about|how about|details of|specifications of|specs of|info on|is|are|the)\b", " ", text, flags=re.I).strip()
        if clean_name:
            found = S.run_search(store, {"query": clean_name}, limit=1)
            if found:
                p = Product.objects.filter(store=store, id=found[0]["id"]).first()
    if not p:
        if shown:
            return ("Which product are you referring to? You can tell me its name, position (first, second, last), or a feature like its brand or color."), []
        return S.which_product_reply(store), []
    return S.answer_about(store, convo, st, p, text)


# ---------------------------------------------------------------
# 6. The dispatcher
# ---------------------------------------------------------------

def core(store, convo, text):
    st = S.get_state(convo)
    t = text.lower().replace("’", "'").strip()
    shown = S.get_shown(store, convo)

    # 1. Reset / Start new conversation: clear active memory, keep messages
    if S.RESET_RE.search(t):
        S.clear_active_memory(convo)
        return "Okay, I've cleared the active product context. What would you like to search for now?", []

    # 2. Out of scope / code requests: politely explain chatbot scope
    if OUT_OF_SCOPE_RE.search(t) or S.is_code_request(t):
        return S.not_related_reply(store), []

    # 3. Friendly greetings and pleasantries
    canned = S.smalltalk_reply(store, t)
    if canned:
        return canned, []

    # 4. Customer corrections (e.g. "No, I meant Campus shoes", "Actually Campus")
    if S.is_correction(t) and (st.get("category") or st.get("keywords") or st.get("brand") or shown):
        return S.handle_correction(store, convo, st, text, shown)

    # 5. Policy & Warranty Inquiries: distinguish product-specific questions from general store policies
    is_warranty = bool(re.search(r"\b(warranty|guarantee)\b", t))
    is_return = bool(re.search(r"\b(return|returns|refund|refunds|exchange|can i return|returnable)\b", t))
    is_shipping = bool(re.search(r"\b(shipping|delivery charges?|delivery date|when will it arrive|free shipping)\b", t))
    is_policy_q = is_warranty or is_return or is_shipping or bool(S.POLICY_RE.search(t))

    if is_policy_q:
        is_explicit_general = bool(re.search(r"\b(what is (?:your|the) (?:return|refund|warranty|shipping) policy|store policy|your policies|return policy|warranty policy)\b", t) and not any(w in t for w in ("these", "this", "it", "its")))
        ord_refs = S.ordinal_refs(t, len(shown) if shown else 10)
        has_pronoun = bool(S.PRONOUN_REF_RE.search(t) or any(w in t for w in ("these", "this", "it", "its", "the product", "this product", "these shoes", "this phone", "this laptop")))

        if not is_explicit_general and (has_pronoun or ord_refs or st.get("focus") or len(shown) == 1):
            p = resolve_referenced_product(store, st, text, shown, refs=ord_refs)
            if p:
                policy_kind = "warranty" if is_warranty else ("return" if is_return else ("shipping" if is_shipping else "general"))
                return S.handle_product_policy_inquiry(store, convo, p, text, policy_type=policy_kind)
            elif len(shown) > 1 and (has_pronoun or ord_refs):
                names = [f"'{p.name}'" for p in shown[:3]]
                return f"Which product are you asking about: {', '.join(names[:-1])} or {names[-1]}?", []

        return S.handle_general_policy_inquiry(store, text)

    # 6. Customer support, discounts, help, catalog discovery
    if S.ORDER_RE.search(t):
        return S.order_reply(store), []
    if S.HUMAN_RE.search(t):
        return S.human_support_reply(store), []
    if S.DISCOUNT_RE.search(t):
        return S.discount_reply(store), []
    if S.HELP_RE.search(t):
        return S.help_reply(store, t), []
    if S.DISCOVERY_RE.search(t):
        return S.discovery_reply(store), []

    # 7. Availability check: returns ONLY the asked product
    if AVAIL_RE.search(t):
        u_temp = Understanding(intent="availability", refs=S.ordinal_refs(t, len(shown) if shown else 10))
        return handle_availability(store, convo, st, shown, text, u_temp)

    # 8. Superlatives & "Which one is cheaper?"
    if CHEAPEST_RE.search(t) or re.search(r"\b(which\s+(?:is\s+)?(?:the\s+)?(?:most\s+expensive|costliest|priciest))\b", t) or re.search(r"\bwhich\s+(?:one\s+)?is\s+cheaper\b", t):
        u_temp = Understanding(intent="superlative", superlative_target="cheapest" if (CHEAPEST_RE.search(t) or "cheaper" in t) else "costliest")
        return handle_superlative(store, convo, st, shown, text, u_temp)

    # 9. Go back
    if GO_BACK_RE.search(t):
        u_temp = Understanding(intent="go_back")
        return handle_go_back(store, convo, st, shown, text, u_temp)

    ord_refs = S.ordinal_refs(t, len(shown) if shown else 10)
    details_phrase = bool(re.search(
        r"\b(tell\s+me\s+about|what\s+about|how\s+about|details?\s+of|specs?\s+of|specifications?\s+of|"
        r"info\s+(?:on|about)|features?\s+of|tell\s+me\s+more|explain|describe|how\s+much\s+is\s+the|"
        r"price\s+of\s+the|show\s+me\s+the)\b|"
        r"^\s*(?:show\s+me\s+)?(?:the\s+)?(?:first|second|third|fourth|fifth|last|1st|2nd|3rd|4th|5th)\s*(?:one|option|product|item)?\s*\??\s*$",
        t
    ))
    has_brand = any(re.search(rf"\b{re.escape(b)}\b", t) for b in S.KNOWN_BRANDS)
    if shown and ord_refs and details_phrase and not has_brand:
        u_temp = Understanding(intent="product_details", refs=ord_refs)
        return product_flow(store, convo, st, u_temp, text, shown)

    u = understand(store, convo, st, shown, text)
    log.info('UNDERSTOOD: %s', u.model_dump(exclude_defaults=True))
    intent = u.intent

    # Superlatives: "Which one is the cheapest?", "Which is the costliest?", "Which has the best rating?"
    if intent == "superlative" or CHEAPEST_RE.search(t) or re.search(r"\b(which\s+(?:is\s+)?(?:the\s+)?(?:most\s+expensive|costliest|priciest))\b", t):
        return handle_superlative(store, convo, st, shown, text, u)

    # Availability check: "Is it available?", "Is the 2nd one in stock?", "Are they in stock?"
    if intent == "availability" or AVAIL_RE.search(t):
        return handle_availability(store, convo, st, shown, text, u)

    # Return to earlier search: "Go back to the shoes", "Take me back to previous search"
    if intent == "go_back" or GO_BACK_RE.search(t):
        return handle_go_back(store, convo, st, shown, text, u)

    fixed = fixed_reply(store, intent, text)
    if fixed:
        return fixed, []
    if intent in ("greeting", "smalltalk"):
        return S.smalltalk_model(store, text)
    if intent == "out_of_scope":
        return S.not_related_reply(store), []
    if intent == "reset":
        S.save_state(convo, {}, [])
        return "Okay, I've cleared our shopping context. What would you like to look for now?", []
    if intent == "clarification":
        q = (u.question or "").strip()
        if not q or S.CURRENCY_RE.search(q):
            q = "Could you tell me a bit more about what you're looking for, and your budget?"
        return q, []
    if intent == "dislike":
        if shown and prev_state(st)["keywords"] or prev_state(st)["categories"]:
            st["rejected"] = list(set(st.get("rejected", []) + st.get("seen", [])))
            S.save_state(convo, st)
            return ("No problem! What would you like to change: the brand, design, colour, style "
                    "or price? I can also show you different options in the same category."), []
        return S.which_product_reply(store), []

    if intent == "comparison":
        refs = [r for r in dict.fromkeys(u.refs) if 1 <= r <= len(shown)]
        if st.get("multi") and len(refs) < 2:
            return S.clarify_reply(st), []
        picks = [shown[r - 1] for r in refs] if len(refs) >= 2 else []
        if not picks:
            picks = S.find_products_to_compare(store, text, shown)
        if not picks and len(shown) >= 2:
            picks = shown[:3]
        if len(picks) < 2:
            return ("I'd be happy to compare products for you! Tell me the names of the two products "
                    "you'd like to compare, or search for a product category first."), []
        return S.compare(store, convo, st, picks, text)

    if intent == "recommendation":
        named = bool(u.categories or u.keywords or u.brand or u.color)
        if named or u.max_price is not None or u.min_price is not None:
            return search_flow(store, convo, st, u, text, shown, reco=True)
        if shown:
            if st.get("multi"):
                return S.clarify_reply(st), []
            return S.recommend(store, convo, st, shown, text)
        return ("Happy to help! What kind of product are you looking for, and what is your budget?"), []


    if intent in ("product_details", "availability"):
        named = bool(u.categories or u.keywords or u.brand or u.color)
        if not shown and named:
            return search_flow(store, convo, st, u, text, shown)
        return product_flow(store, convo, st, u, text, shown)

    return search_flow(store, convo, st, u, text, shown)     # product_search / product_followup