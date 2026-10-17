import difflib
import json
import logging
import os
import re
import uuid
from collections import Counter


import ollama
from django.db import transaction
from django.db.models import Q

from catalog.models import Product
from ingestion.products import index_products_async, semantic_scores
from .models import Conversation, Message
from .tools import (search_products, search_loose, get_categories, get_vocab,
                    correct_word, STOPWORDS, SYNONYMS, is_catalog_word)
from ingestion.retrieval import retrieve_products
from .rag import answer_with_rag, FOLLOWUP_RE
# MIN_SIM lives in tools.py; fall back safely if it is missing there.
try:
    from .tools import MIN_SIM
except ImportError:
    MIN_SIM = 0.64

MODEL = "qwen2.5:3b"

CURRENCY_RE = re.compile(r"(?:[\$₹]|USD|INR|Rs\.?)\s?(\d[\d,]*(?:\.\d+)?)", re.I)

FILLER = {
    "only", "just", "also", "same", "plz", "dikhao", "dikha", "mujhe", "chahiye", "kuch",
    "aur", "other", "another", "options", "option", "something", "else", "buy", "purchase",
    "like", "want", "wanted", "suggest", "recommend", "better", "new", "these", "those",
    "them", "kam", "sasta", "sasti", "thoda", "bit", "little", "between", "around",
    "about", "approximately", "rupees", "rupee", "dollars", "dollar", "thousand", "lakh",
    "hundred", "costlier", "pricier", "affordable", "lower", "higher", "low", "high",
    "least", "maximum", "max", "minimum", "min", "upto", "within", "kindly", "hey",
    "hello", "its", "got", "stock", "make", "sure", "availability", "cost", "much",
    "gimme", "let", "see", "check", "pair", "pairs", "kind", "type", "nice", "great",
    "really", "very", "too", "get", "find", "list", "display", "anything", "all",
    "everything", "whatever", "any", "thing", "things", "wanna", "gonna", "would",
}


log = logging.getLogger(__name__)

# Seconds to wait for Ollama before giving up (override with the OLLAMA_TIMEOUT env var).
OLLAMA_TIMEOUT = float(os.environ.get("OLLAMA_TIMEOUT", "180"))


class ModelDown(Exception):
    pass


class AssistantUnavailable(Exception):
    """Nothing useful could be produced; the request is rolled back and nothing is saved."""


def llm(**kwargs):
    """The ONLY place that talks to the model. Swap this to change models."""
    client = ollama.Client(timeout=OLLAMA_TIMEOUT)
    opts = dict(kwargs.get("options") or {})
    if os.environ.get("OLLAMA_NUM_GPU") is not None:      # force CPU with OLLAMA_NUM_GPU=0
        opts.setdefault("num_gpu", int(os.environ["OLLAMA_NUM_GPU"]))
    kwargs["options"] = opts
    kwargs.setdefault("keep_alive", "30m")                # keep the model loaded between chats
    try:
        return client.chat(model=MODEL, **kwargs)
    except Exception as exc:
        log.exception("Ollama chat failed: %s", exc)
        raise ModelDown()


# =====================================================================
# Conversations: identity, ownership, history (kept apart from active memory)
# =====================================================================

def get_conversation(store, session_id):
    """Old helper, kept so shell tests still work."""
    convo, _ = Conversation.objects.get_or_create(store=store, session_id=session_id)
    return convo


def new_conversation(store, client_id):
    """Start a separate conversation. Re-uses an empty one so repeated resets don't pile up."""
    last = latest_conversation(store, client_id)
    if last and not last.messages.exists():
        save_state(last, {}, [])
        return last
    return Conversation.objects.create(
        store=store, session_id="c" + uuid.uuid4().hex[:24],
        preferences={"client": client_id, "state": {}, "title": ""})


def get_owned(store, client_id, conversation_id):
    """Only returns a conversation that belongs to this store AND this browser."""
    if not client_id or not conversation_id:
        return None
    return Conversation.objects.filter(
        store=store, session_id=str(conversation_id)[:64],
        preferences__client=client_id).first()


def latest_conversation(store, client_id):
    if not client_id:
        return None
    return (Conversation.objects.filter(store=store, preferences__client=client_id)
            .order_by("-updated_at", "-id").first())


def resolve_conversation(store, client_id, conversation_id=None, create=True):
    """
    The one place that decides which conversation a request belongs to.
    - conversation_id given: it must belong to this store AND this client, else None.
    - otherwise: the client's latest conversation; an old-style conversation
      (created before conversation ids existed, session_id == client id) is adopted
      so existing history is kept; failing that a new one is created (if create).
    """
    if not client_id:
        return None
    if conversation_id:
        return get_owned(store, client_id, conversation_id)
    convo = latest_conversation(store, client_id)
    if convo:
        return convo
    legacy = Conversation.objects.filter(store=store, session_id=client_id).first()
    if legacy and (legacy.preferences or {}).get("client") in (None, "", client_id):
        prefs = dict(legacy.preferences or {})
        prefs["client"] = client_id
        prefs.setdefault("state", {})
        legacy.preferences = prefs
        legacy.save(update_fields=["preferences"])
        return legacy
    return new_conversation(store, client_id) if create else None


def cached_response(convo, request_id):
    """If this exact request was already answered (a retry), return the saved answer."""
    if not request_id:
        return None
    last = (convo.preferences or {}).get("last_request") or {}
    if last.get("id") == request_id:
        return last.get("response")
    return None


def list_conversations(store, client_id, limit=30):
    if not client_id:
        return []
    qs = (Conversation.objects.filter(store=store, preferences__client=client_id,
                                      messages__isnull=False)
          .distinct().order_by("-updated_at", "-id")[:limit])
    return [{"id": c.session_id, "title": c.title or (c.preferences or {}).get("title") or "New chat",
             "updated": c.updated_at.isoformat()} for c in qs]


def history_payload(store, convo, limit=200):
    prefs = convo.preferences or {}
    cards_map = prefs.get("cards", {})
    msgs = list(convo.messages.order_by("-id")[:limit])[::-1]
    all_ids = set()
    for m in msgs:
        for c in (m.cards or []):
            if isinstance(c, int):
                all_ids.add(c)
            elif isinstance(c, dict) and "id" in c:
                all_ids.add(c["id"])
        for i in cards_map.get(str(m.id), []):
            all_ids.add(i)

    by_id = {p.id: p for p in Product.objects.filter(store=store, id__in=all_ids)}
    out = []
    for m in msgs:
        p_ids = []
        if m.cards:
            p_ids = [c if isinstance(c, int) else c.get("id") for c in m.cards if (isinstance(c, int) or (isinstance(c, dict) and "id" in c))]
        if not p_ids:
            p_ids = cards_map.get(str(m.id), [])
        out.append({
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "created": m.created_at.isoformat(),
            "products": [item(store, by_id[i]) for i in p_ids if i in by_id]
        })
    return out


# =====================================================================
# Active shopping state (belongs to ONE conversation only)
# =====================================================================

def get_state(convo):
    st = (convo.preferences or {}).get("state")
    return dict(st) if isinstance(st, dict) else {}


def save_state(convo, st, ids=None):
    prefs = dict(convo.preferences or {})
    prefs["state"] = st
    convo.preferences = prefs
    fields = ["preferences"]
    if ids is not None:
        convo.last_shown_ids = ids
        fields.append("last_shown_ids")
    convo.save(update_fields=fields)


def get_shown(store, convo):
    by_id = {p.id: p for p in Product.objects.filter(store=store, id__in=convo.last_shown_ids)}
    return [by_id[i] for i in convo.last_shown_ids if i in by_id]


def item(store, p, full=False):
    prod_url = (p.url or "").strip()
    if not prod_url or not prod_url.startswith("http") or ("/p/" in prod_url and "pid=" not in prod_url):
        import urllib.parse
        prod_url = f"https://www.flipkart.com/search?q={urllib.parse.quote_plus(p.name)}"
    d = {"id": p.id, "name": p.name, "price": float(p.price), "currency": store.currency,
         "category": p.category, "in_stock": p.in_stock, "url": prod_url,
         "image": p.image_url, "updated": p.updated_at.isoformat()}
    if getattr(p, "brand", None):
        d["brand"] = p.brand
    if full:
        d["description"] = (p.description or "")[:500]
        if getattr(p, "attributes", None):
            d["attributes"] = p.attributes
    return d


# =====================================================================
# Text helpers
# =====================================================================

NUMWORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
            "eight": 8, "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20}


def normalize_numbers(t):
    def rep(m):
        mult = {"hundred": 100, "thousand": 1000, "lakh": 100000}[m.group(2)]
        return str(NUMWORDS[m.group(1)] * mult)
    return re.sub(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty)\s+"
                  r"(hundred|thousand|lakh)\b", rep, t)


def content_words(text):
    return [w for w in re.findall(r"[a-z][a-z'-]{2,}", text.lower())
            if w not in STOPWORDS and w not in FILLER]


def norm(w):
    w = SYNONYMS.get(w, w)
    return w[:-1] if w.endswith("s") and len(w) > 3 else w


def fuzzy_catalog(store, w):
    """True if the word is, or is a typo of, a word used in this store's catalog."""
    if is_catalog_word(store, w):
        return True
    w2 = SYNONYMS.get(w, w)
    if len(w2) < 4:
        return False
    vocab = get_vocab(store)
    for v in difflib.get_close_matches(w2, list(vocab), n=3, cutoff=0.8):
        if v[0] == w2[0]:
            return True
    return False


AMT = r"(?:rs\.?|inr|usd|₹|\$)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k\b|thousand|lakh)?"


def amount(num, suf):
    v = float(num.replace(",", ""))
    if suf:
        v *= 1000 if suf.lower() in ("k", "thousand") else 100000
    return v


def parse_price(t):
    out = {}
    m = re.search(r"\b(?:between|from)\s*" + AMT + r"\s*(?:and|to|-|–)\s*" + AMT, t)
    if m:
        a, b = amount(m.group(1), m.group(2)), amount(m.group(3), m.group(4))
        return {"min_price": min(a, b), "max_price": max(a, b)}
    m = re.search(r"\b(?:under|below|within|upto|up to|less than|lesser than|maximum|max|"
                  r"not more than|cheaper than|budget|range)\b[^\d]{0,18}?" + AMT, t)
    if m:
        out["max_price"] = amount(m.group(1), m.group(2))
    m = re.search(r"\b(?:above|over|more than|at least|minimum|min|greater than|"
                  r"costlier than)\b[^\d]{0,12}?" + AMT, t)
    if m:
        out["min_price"] = amount(m.group(1), m.group(2))
    if not out:
        m = re.search(r"\b(?:around|about|approximately|approx|near|roughly)\b[^\d]{0,6}?" + AMT, t)
        if m:
            v = amount(m.group(1), m.group(2))
            if v >= 20:
                out["min_price"], out["max_price"] = v * 0.8, v * 1.2
    if not out and re.search(r"\b(budget|rupees|rs|inr|usd|dollars?)\b|₹|\$", t):
        m = re.search(AMT, t)
        if m:
            out["max_price"] = amount(m.group(1), m.group(2))
    if not out:
        m = re.fullmatch(r"\W*(?:rs\.?|inr|usd|₹|\$)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakh)?"
                         r"\s*(?:rs|rupees|inr|usd)?\W*", t)
        if m:
            out["max_price"] = amount(m.group(1), m.group(2))
    if re.search(r"\bfree (products?|items?|stuff)\b|\bprice (of )?0\b|\bzero price\b|\bfor free\b", t):
        out["max_price"] = 0.0
    return out


def parse_sort(t):
    if re.search(r"most expensive|highest|costliest|premium|top[- ]end|flagship", t):
        return "price_high"
    if re.search(r"cheapest|lowest|least expensive|sasta|sasti|\bcheap\b|affordable|budget friendly", t):
        return "price_low"
    return None


CHEAPER_RE = re.compile(r"\bche+a?pe?r\b|lower[- ]priced|less expensive|lesser price", re.I)
PRICIER_RE = re.compile(r"more expensive|costlier|pricier|higher[- ]priced|expensive ones|premium ones", re.I)
MORE_RE = re.compile(
    r"^\W*(?:show\s+(?:me\s+)?)?(?:some\s+)?more(?:\s+(?:options|ones|products|items))?\W*$"
    r"|more options|any other|other options|another option|anything else|any more|show more"
    r"|something else|other ones|different ones", re.I)
SIMILAR_RE = re.compile(r"similar|alternatives?|like (?:this|it|that)", re.I)
REFINE_RE = re.compile(r"\b(only|just|also|same|in|with|but|it|this|that|these|those|them|ones|one)\b|instead|make it", re.I)
PRONOUN_REF_RE = re.compile(r"\b(it|them|these|those|same)\b", re.I)

ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3,
            "fourth": 4, "4th": 4, "fifth": 5, "5th": 5}


def ordinal_refs(t, n):
    if re.search(r"first (two|2)\b", t) and n >= 2:
        return [1, 2]
    if re.search(r"first (three|3)\b", t) and n >= 3:
        return [1, 2, 3]
    refs = [i for w, i in ORDINALS.items() if re.search(rf"\b{w}\b", t) and i <= n]
    if n and re.search(r"\blast one\b|\bthe last\b", t):
        refs.append(n)
    return sorted(set(refs))


def clean_reply(text):
    text = text.replace("*", "").replace("`", "")
    text = re.sub(r"(?m)^\s*#+\s*", "", text)
    text = re.sub(r"(?m)^\s*[-•]\s+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def prices_ok(reply, results):
    allowed = {round(r["price"], 2) for r in results}
    for m in CURRENCY_RE.findall(reply):
        try:
            if round(float(m.replace(",", "")), 2) not in allowed:
                return False
        except ValueError:
            continue
    return True


def fmt_price(val, currency="INR"):
    cur_sym = "₹" if currency in ("INR", "₹") else ("$" if currency in ("USD", "$") else f"{currency} ")
    try:
        fval = float(val)
        if fval.is_integer():
            return f"{cur_sym}{int(fval):,}"
        return f"{cur_sym}{fval:,.2f}"
    except (ValueError, TypeError):
        return f"{cur_sym}{val}"


def fallback_text(results):
    parts = [f"{r['name']} for {fmt_price(r['price'], r.get('currency', 'INR'))}" for r in results[:3]]
    return "Here's what I found: " + "; ".join(parts) + "."


def describe_filters(store, st):
    cur_sym = "₹" if store.currency in ("INR", "₹") else store.currency
    parts = []
    lo, hi = st.get("min_price"), st.get("max_price")
    if lo is not None and hi is not None:
        parts.append(f"between {fmt_price(lo, store.currency)} and {fmt_price(hi, store.currency)}")
    elif hi is not None:
        parts.append(f"under {fmt_price(hi, store.currency)}")
    elif lo is not None:
        parts.append(f"above {fmt_price(lo, store.currency)}")
    if st.get("sort") == "price_low":
        parts.append("cheapest first")
    elif st.get("sort") == "price_high":
        parts.append("most expensive first")
    return ", ".join(parts) or "none"


# =====================================================================
# Detectors (done in code, so they never depend on the model)
# =====================================================================

SMALL = {"hi", "hii", "hiii", "hello", "hey", "heyy", "hola", "namaste", "thanks", "thank",
         "you", "thx", "ok", "okay", "bye", "good", "morning", "evening", "afternoon",
         "night", "how", "are", "r", "u", "is", "it", "going", "whats", "what's", "up",
         "sup", "there", "doing", "bro", "dear", "sir", "madam", "hlo", "helo", "da",
         "hru", "wassup", "yo", "nice", "great", "cool", "fine", "well", "welcome",
         "please", "pls", "and", "your", "name", "who", "i", "am", "im", "i'm", "me",
         "my", "see", "later", "take", "care", "have", "a", "day", "much", "so", "very",
         "ji", "kaise", "ho", "kya", "haal", "hai", "aap", "tum", "theek", "accha",
         "alright", "goodbye", "all", "right", "the", "this"}
ACK_WORDS = {"ok", "okay", "fine", "cool", "nice", "great", "good", "alright", "right", "all"}
HELP_RE = re.compile(r"what can you do|who are you|what do you do|your name|what are you|"
                     r"how can you help|can you help me\W*$|^\W*help( me)?\W*$", re.I)
YES_RE = re.compile(r"^\W*(yes|yeah|yep|sure|ok(ay)?|please do|show( me)?( them)?|go ahead|haan|ha)\W*$", re.I)
RESET_RE = re.compile(r"\b(reset|start (a )?(new|over|fresh)|new chat|new conversation|"
                      r"clear (the )?(chat|conversation|context)|forget (it|everything|that|all))\b", re.I)

CODE_RE = re.compile(
    r"\b(html|css|javascript|python|java|php|sql|react|django|angular|nodejs|"
    r"program|programming|coding|source code|script|algorithm|compile|debug|leetcode|regex|"
    r"homework|assignment|essay|thesis|equation|integral|derivative|"
    r"write (?:a |an |the )?(?:code|function|class|query|essay|poem|story|article))\b", re.I)

FRUSTRATED_RE = re.compile(r"not understanding|don'?t understand|you('re| are) (not|useless|stupid|wrong)|"
                           r"this is (useless|wrong)|wrong answer|not what i (asked|said|want)|useless", re.I)
DISLIKE_RE = re.compile(r"(don'?t|do not|didn'?t) like (these|this|them|it|any)|not interested|"
                        r"show (me )?(something )?different|i hate these|not these", re.I)
HUMAN_RE = re.compile(r"\b(human|real person|customer (care|support|service)|talk to (a |an )?(person|agent)|"
                      r"contact (the )?store|phone number|contact number|store address|email address|"
                      r"opening hours|timings)\b", re.I)
ORDER_RE = re.compile(r"\b(my order|track(ing)?|order status|cancel (my |the )?order|refund|"
                      r"return (this|it|my|the)|replacement|damaged|delivery date|when will it arrive|exchange)\b", re.I)
CART_RE = re.compile(r"\b(cart|checkout|check out|add (this|it|these|them|two|one|\d+)|buy (this|it|these)|"
                     r"place (an |the |my )?order|remove (the|this|it)|buy now)\b", re.I)
REVIEW_RE = re.compile(r"\b(reviews?|ratings?|rated|customers? (think|say)|feedback)\b", re.I)
DISCOUNT_RE = re.compile(r"\b(discounts?|offers?|sale|deals?|coupons?|promo|cashback)\b", re.I)
POLICY_RE = re.compile(r"\b(shipping|delivery charges?|return policy|refund policy|warranty|"
                       r"payment (method|option)s?|cod|cash on delivery|emi|gst|invoice)\b", re.I)

BEST_RE = re.compile(r"which (?:one )?(?:is |would be |should i )?(?:the )?(?:best|better)|best one|"
                     r"which should i (?:buy|choose|get|pick)|your (?:pick|choice|recommendation)|"
                     r"show (?:me )?the best|which one (?:do you )?(?:recommend|suggest)|"
                     r"what do you recommend|worth buying", re.I)
RECO_RE = re.compile(r"recommend|suggest|\bbest\b|good one|worth", re.I)
COMPARE_RE = re.compile(r"\bcompar(e|ison|ing)\b|difference between|differences|\bvs\.?\b|versus", re.I)
WHICH_RE = re.compile(r"\bwhich\b", re.I)
PRONOUN_RE = re.compile(r"\b(it|its|this|that|this one|that one|the product)\b", re.I)
ABOUT_RE = re.compile(r"stock|available|availability|price|cost|detail|tell me|about|describe|info|spec|"
                      r"worth|good|better|colou?r|size|material|made of|battery|\bram\b|storage|camera|"
                      r"weight|processor|display|screen|feature|advantage|disadvantage|pros|cons|"
                      r"quality|suitable|beginner|gaming|waterproof", re.I)
WANT_RE = re.compile(r"\b(i want|i need|looking for|want to buy|wanna buy|need to buy|i'd like|"
                     r"i would like|help me (find|choose|buy))\b", re.I)
GIFT_RE = re.compile(r"\b(gift|present|surprise)\b", re.I)
VAGUE_RE = re.compile(r"\b(suggest|recommend|something good|anything good|surprise me|what should i buy|"
                      r"best products?)\b", re.I)
THING_RE = re.compile(r"\b(that|this) (thing|one|product|item)\b|\bsomething\b", re.I)
TOPIC_SPLIT_RE = re.compile(r"\band\b|,|&|\bor\b|\bplus\b")
DISCOVERY_RE = re.compile(
    r"\b(what (do you|can i) (sell|have|buy)|what products|what categories|what items|"
    r"show (me )?(all )?(categories|collection|catalogue|catalog)|what do you offer|"
    r"available categories|what's available|whats available|all categories)\b", re.I
)


def is_smalltalk(t):
    words = re.findall(r"[a-z']+", t.lower())
    return bool(words) and all(w in SMALL for w in words)


def is_code_request(t):
    t = re.sub(r"(promo|coupon|discount|offer|gift|voucher|zip|pin|postal|referral)\s+code"
               r"|(loyalty|rewards|membership)\s+program", "", t.lower())
    return bool(CODE_RE.search(t))


def split_greeting(text):
    m = re.match(r"^\s*(hi+|hello+|hey+|hlo|namaste|good\s+(?:morning|afternoon|evening))\b[\s,!.:;-]*(.*)$",
                 text, re.I | re.S)
    if not m:
        return text, None
    rest = m.group(2).strip()
    if not rest or is_smalltalk(rest) or HELP_RE.search(rest):
        return text, None
    return rest, m.group(1).strip().capitalize() + "!"


# =====================================================================
# Fixed replies (written in code, so they are always correct)
# =====================================================================

def smalltalk_reply(store, t):
    if not is_smalltalk(t):
        return None
    if re.search(r"\b(bye|goodbye|see you|take care)\b", t):
        return "Goodbye! Thanks for shopping with us. Have a great day!"
    if re.search(r"\b(thanks|thank|thx)\b", t):
        return "You're welcome! Let me know if you need help finding anything else."
    if re.search(r"\bhow\b.*\b(you|u)\b|\bhru\b|what'?s up|\bsup\b|wassup|kaise ho|kya haal|aap kaise", t):
        return ("I'm doing great, thank you! I'm here to help you find the right products. "
                "What are you shopping for?")
    m = re.search(r"good\s+(morning|afternoon|evening|night)", t)
    if m:
        if m.group(1) == "night":
            return "Good night! Come back anytime you need help shopping."
        return f"Good {m.group(1)}! What can I help you find today?"
    words = set(re.findall(r"[a-z']+", t))
    if words and words <= ACK_WORDS:
        return "Great! Let me know what you'd like to find."
    if re.search(r"\b(hello|hlo|helo)\b", t):
        return "Hello! What are you looking for today?"
    return f"Hi! Welcome to {store.name}. How can I help you shop today?"


def help_reply(store, t):
    if re.search(r"who are you|your name|what are you", t):
        return ("I'm your AI shopping assistant. I can help you find products, compare prices, "
                "check availability, and choose suitable items.")
    return ("I can search products, filter by price, recommend products, compare items, and answer "
            "questions about the products in our catalogue. Let me know what you'd like to find!")


def discovery_reply(store):
    cats = get_categories(store, 15)
    cat_str = ", ".join(c.replace("-", " ").title() for c in cats[:7]) if cats else "a wide variety of products"
    return (f"Welcome to {store.name}! We offer a wide selection of products across categories including: "
            f"{cat_str}, and more. What would you like to find today? You can also specify a brand, color, or your budget!")


def policy_reply(store, text=""):
    t = (text or "").lower()
    n = store.name
    if re.search(r"\b(return|returns|refund|refunds|exchange|exchanges|damaged|replace|replacement)\b", t):
        return (f"We have an easy 7-day return and exchange policy on {n} for all eligible items in original condition. "
                f"You can request a return or replacement directly from your account's 'Orders' section.")
    if re.search(r"\b(cod|cash on delivery|payment|pay|upi|card|cards|credit|debit|emi|net banking|wallet)\b", t):
        return (f"At {n}, we accept all major Credit/Debit Cards, UPI, Net Banking, and secure wallets. "
                f"Cash on Delivery (COD) is also available on eligible orders and delivery locations at checkout.")
    if re.search(r"\b(shipping|delivery|deliver|dispatch|courier|how long|charges?|cost|free shipping)\b", t):
        return (f"Standard delivery across {n} typically takes 3 to 5 business days. Free shipping is available "
                f"on qualifying orders, and you'll receive real-time courier tracking updates once your parcel ships.")
    if re.search(r"\b(warranty|guarantee)\b", t):
        return (f"Products on {n} come with standard manufacturer warranty where applicable. "
                f"Check individual product pages for specific warranty terms and coverage details.")
    return (f"At {n}, we provide secure payments (including COD), fast 3-5 day shipping with tracking, "
            f"and an easy 7-day return policy. What can I help you find today?")


def discount_reply(store):
    return (f"We regularly feature sales, seasonal promotions, and discount deals on our {store.name} homepage. "
            f"If you have a promo coupon code, you can apply it at checkout for an instant discount!")


def order_reply(store):
    return (f"You can track your order status anytime by visiting the 'Orders' section of your {store.name} account. "
            f"There you'll find real-time delivery estimates, courier tracking links, and cancellation or return options.")


def human_support_reply(store):
    return (f"You can contact our support team through the 'Contact Us' page on the {store.name} website, "
            f"or reach out to customer care via email. We're happy to assist with any questions about orders or products!")


def find_products_to_compare(store, text, shown=()):
    t = re.sub(r"\b(compare|comparison|difference between|differences between|vs\.?|versus)\b", " ", text, flags=re.I).strip()
    parts = [p.strip() for p in re.split(r"\b(?:and|with|to)\b", t, flags=re.I) if p.strip()]
    prods = []
    seen_ids = set()
    if len(parts) >= 2:
        for part in parts:
            part_clean = re.sub(r"[^\w\s-]", "", part).strip()
            if not part_clean:
                continue
            p = Product.objects.filter(store=store, name__icontains=part_clean).first()
            if not p:
                words = content_words(part_clean)
                found = run_search(store, {"query": " ".join(words) if words else part_clean}, limit=1)
                if found:
                    p = Product.objects.filter(store=store, id=found[0]["id"]).first()
            if p and p.id not in seen_ids:
                seen_ids.add(p.id)
                prods.append(p)
    return prods


def not_related_reply(store):
    return (f"I'm sorry, but that doesn't seem to be related to shopping on {store.name}, so I can't "
            "help with it here. If you're looking for a product, just tell me what you need and "
            "I'll check our catalogue for you.")


def not_available_reply(store, asked, cats):
    tail = f" You could try something else, for example: {cats}." if cats else ""
    if asked:
        return (f"I'm sorry, but \"{asked}\" isn't available on {store.name} right now. "
                f"I couldn't find it in our catalogue.{tail}")
    return (f"I couldn't find any products matching that on {store.name}. "
            f"Try a different keyword or price range.{tail}")


def which_product_reply(store):
    return ("I'd be happy to help! Which product are you referring to? You can tell me its name "
            "or describe it.")


def clarify_reply(st):
    terms = st.get("terms") or []
    if len(terms) >= 2:
        return (f"Your last search covered more than one kind of product ({', '.join(terms)}). "
                "Which of these do you want me to use: " + " or ".join(terms) + "?")
    return "Do you want me to refine your previous search, or start a new one? Tell me the product."


# =====================================================================
# Model: classify unclear messages, write natural replies
# =====================================================================

def classify(store, st, text):
    """One simple question for the model: PRODUCT, CHAT or OTHER."""
    cats = ", ".join(get_categories(store, 20)) or "various products"
    prev = st.get("query") or st.get("category") or "none"
    prompt = (
        f"An online store sells these kinds of products: {cats}.\n"
        f"The customer's previous product search was: {prev}.\n"
        f"Customer message: \"{text}\"\n\n"
        "Classify the message with ONE word:\n"
        "PRODUCT = the customer wants to find, buy, compare, or know the price or availability "
        "of a physical item a shop could sell (even if this store may not stock it), or describes "
        "a need or use case that products could solve.\n"
        "CHAT = friendly conversation: compliments, feelings, jokes about the assistant.\n"
        "OTHER = general knowledge, news, weather, math, coding, homework, health advice, "
        "opinions, or anything else unrelated to shopping.\n"
        "Answer with only PRODUCT, CHAT or OTHER.")
    resp = llm(messages=[{"role": "user", "content": prompt}],
               options={"temperature": 0, "num_predict": 4})
    ans = (resp.message.content or "").strip().upper()
    if ans.startswith("PRO"):
        return "product"
    if ans.startswith("CHA"):
        return "chat"
    return "other"


def followup_check(store, st, text):
    """Is this message about the current search, or something new? FOLLOWUP, NEW or UNCLEAR."""
    prev = " ".join(x for x in [st.get("query"), st.get("category")] if x) or ", ".join(st.get("terms") or []) or "none"
    prompt = (
        f"A customer is shopping. Their current product search is: {prev}.\n"
        f"Their new message: \"{text}\"\n\n"
        "Decide with ONE word:\n"
        "FOLLOWUP = the message refines or asks about the current search or the products already "
        "shown (a feature, colour, size, price, brand, 'only X').\n"
        "NEW = the message is about a different product or a different topic.\n"
        "UNCLEAR = it could be either.\n"
        "Answer with only FOLLOWUP, NEW or UNCLEAR.")
    resp = llm(messages=[{"role": "user", "content": prompt}],
               options={"temperature": 0, "num_predict": 4})
    ans = (resp.message.content or "").strip().upper()
    if ans.startswith("FOL"):
        return "followup"
    if ans.startswith("UNC"):
        return "unclear"
    return "new"


def suggest_categories(store, text):
    """Map a need ('something for the gym', 'gift for my sister') to this store's categories."""
    cats = get_categories(store, 40)
    if not cats:
        return []
    prompt = (f"A shop sells these categories: {', '.join(cats)}.\n"
              f"Customer says: \"{text}\"\n"
              "Which of these categories suit the customer's need? Reply with up to 3 category "
              "names copied exactly from the list, separated by commas, or NONE if none fit.")
    resp = llm(messages=[{"role": "user", "content": prompt}],
               options={"temperature": 0, "num_predict": 30})
    out = []
    for part in re.split(r"[,\n]", resp.message.content or ""):
        p = part.strip().strip(".").lower()
        for c in cats:
            if p == c.lower() and c not in out:
                out.append(c)
    return out[:3]


def chat_system(store, extra):
    return (
        f"You are the friendly shopping assistant of the online store {store.name}. "
        f"Prices are in {store.currency}. "
        "Talk like a warm, knowledgeable shop assistant messaging a customer. "
        "Reply in the same language the customer writes in. "
        "RULES: use only the store data given below. Never invent products, prices, stock, "
        "specifications, reviews, discounts, shipping or return policies. If a detail is not in "
        "the data, say that information is not available. "
        "STYLE: plain conversational text. No markdown at all: no asterisks, no bold, no bullet "
        "points, no headings, no numbered lists. Keep it concise: a few short sentences. Product "
        "cards with images are shown separately, so don't list everything.\n\n" + extra)


def write_reply(system, user_text):
    resp = llm(messages=[{"role": "system", "content": system},
                         {"role": "user", "content": user_text}],
               options={"temperature": 0.4, "num_predict": 90})
    return clean_reply(resp.message.content or "")


def smalltalk_model(store, text):
    print("SMALL TALK (model)")
    extra = ("The customer is making friendly conversation (a compliment, a feeling, a joke). "
             "Reply warmly in one or two short sentences and gently ask what they would like to "
             "find today. Do not mention any product or price.")
    reply = write_reply(chat_system(store, extra), text)
    if not reply or CURRENCY_RE.search(reply) or "outside" in reply.lower():
        reply = "Thank you! What can I help you find today?"
    return reply, []


# =====================================================================
# Searching & Strict Relevance Filter
# =====================================================================

KNOWN_BRANDS = {
    'nike', 'adidas', 'puma', 'reebok', 'sparx', 'campus', 'asian', 'bata', 'woodland',
    'skechers', 'asics', 'apple', 'samsung', 'sony', 'oneplus', 'xiaomi', 'redmi',
    'realme', 'vivo', 'oppo', 'motorola', 'google', 'dell', 'hp', 'lenovo', 'asus',
    'acer', 'boat', 'noise', 'fastrack', 'titan', 'casio', 'lg', 'whirlpool', 'philips',
    'havells', 'bajaj', 'crocs', 'fossil', 'nothing', 'poco', 'infinix'
}

ACCESSORY_WORDS = [
    'back cover', 'cases & covers', 'case cover', 'phone case', 'tempered glass',
    'screen protector', 'screen guard', 'laptop bag', 'laptop sleeve', 'laptop skin',
    'watch strap', 'remote control', 'wall mount', 'cable', 'adapter', 'charger',
    'shoe polish', 'socks pack', 'pack of socks', 'cotton socks'
]

CATEGORY_NOUNS = {
    "shoes": ["shoe", "shoes", "sneaker", "sneakers", "running", "walking", "sports", "footwear", "clog", "sandals", "slides", "loafers", "boots"],
    "shoe": ["shoe", "shoes", "sneaker", "sneakers", "running", "walking", "sports", "footwear", "clog", "sandals", "slides", "loafers", "boots"],
    "sneakers": ["shoe", "shoes", "sneaker", "sneakers"],
    "laptops": ["laptop", "laptops", "notebook", "vivobook", "zenbook", "thinkpad", "ideapad", "macbook", "inspiron", "pavilion", "yoga"],
    "laptop": ["laptop", "laptops", "notebook", "vivobook", "zenbook", "thinkpad", "ideapad", "macbook", "inspiron", "pavilion", "yoga"],
    "tv": ["tv", "television", "bravia", "oled", "qled", "smart tv", "led tv"],
    "tvs": ["tv", "television", "bravia", "oled", "qled", "smart tv", "led tv"],
    "television": ["tv", "television", "bravia", "oled", "qled", "smart tv", "led tv"],
    "phone": ["phone", "smartphone", "mobile", "iphone", "5g", "4g", "galaxy"],
    "phones": ["phone", "smartphone", "mobile", "iphone", "5g", "4g", "galaxy"],
    "mobile": ["phone", "smartphone", "mobile", "iphone", "5g", "4g", "galaxy"],
    "mobiles": ["phone", "smartphone", "mobile", "iphone", "5g", "4g", "galaxy"],
    "smartwatch": ["watch", "smartwatch"],
    "watch": ["watch", "smartwatch"],
    "watches": ["watch", "smartwatch"],
    "headphone": ["headphone", "headphones", "earphone", "earphones", "earbuds", "airpods", "neckband", "tws", "buds"],
    "headphones": ["headphone", "headphones", "earphone", "earphones", "earbuds", "airpods", "neckband", "tws", "buds"],
    "earphones": ["headphone", "headphones", "earphone", "earphones", "earbuds", "airpods", "neckband", "tws", "buds"],
    "earphone": ["headphone", "headphones", "earphone", "earphones", "earbuds", "airpods", "neckband", "tws", "buds"],
    "earbuds": ["headphone", "headphones", "earphone", "earphones", "earbuds", "airpods", "neckband", "tws", "buds"],
    "refrigerator": ["refrigerator", "fridge"],
    "fridge": ["refrigerator", "fridge"],
    "refrigerators": ["refrigerator", "fridge"],
    "washing machine": ["washing machine", "washer"],
    "microwave": ["microwave", "oven"],
    "air conditioner": ["air conditioner", "ac", "split ac", "inverter ac"],
    "t-shirt": ["t-shirt", "tshirt", "tee"],
    "t-shirts": ["t-shirt", "tshirt", "tees", "tee"],
    "shirt": ["shirt", "shirts"],
    "shirts": ["shirt", "shirts"],
    "jeans": ["jean", "jeans", "denim"],
    "sofa": ["sofa", "couch", "recliner"],
    "camera": ["camera", "dslr", "mirrorless"],
    "perfume": ["perfume", "fragrance", "deodorant", "cologne", "body spray"],
    "sunglasses": ["sunglasses", "shades", "aviator", "wayfarer"],
    "speaker": ["speaker", "soundbar", "bluetooth speaker"],
    "soundbar": ["soundbar", "speaker", "audio"],
}


def is_product_relevant(name, price, q="", brand=None, color=None, min_price=None, max_price=None, category=None):
    n = name.lower()
    full_text = f"{q or ''} {category or ''}".lower()

    # 1. Price check
    if min_price is not None and float(price) < float(min_price):
        return False
    if max_price is not None and float(price) > float(max_price):
        return False

    # 2. Accessory exclusion (unless user explicitly searched for an accessory)
    is_acc_query = any(w in full_text for w in ['cover', 'case', 'glass', 'bag', 'sleeve', 'skin', 'strap', 'remote', 'mount', 'charger', 'cable', 'sock', 'polish'])
    if not is_acc_query:
        for acc in ACCESSORY_WORDS:
            if acc in n:
                return False

    # 3. Brand filter (strictly enforced!)
    b = (brand or "").lower().strip()
    if not b:
        for kb in KNOWN_BRANDS:
            if re.search(rf"\b{kb}\b", full_text):
                b = kb
                break
    if b:
        if not re.search(rf"\b{re.escape(b)}\b", n):
            return False

    # 4. Color filter (strictly enforced if requested)
    if color:
        c = color.lower().strip()
        if c and not re.search(rf"\b{re.escape(c)}\b", n):
            return False

    # 5. Core category noun check
    for noun, valid_matches in CATEGORY_NOUNS.items():
        if re.search(rf"\b{noun}\b", full_text):
            if not any(re.search(rf"\b{re.escape(v)}\b", n) for v in valid_matches):
                return False
            break

    return True


def run_search(store, st, exclude=(), in_stock=True, limit=5, relax=True):
    q = (st.get("query") or "").strip()
    cat = st.get("category") or None
    brand = st.get("brand") or None
    color = st.get("color") or None
    kw = {k: st[k] for k in ("min_price", "max_price") if st.get(k) is not None}
    if not (q or cat or brand or color or kw):
        return []
    sort = st.get("sort") or "relevance"

    # 0) For Flipkart store: LIVE SEARCH ALWAYS TAKES PRIORITY!
    if getattr(store, "source_type", "") == "flipkart":
        try:
            from catalog.connectors import FlipkartConnector
            terms = [x for x in (brand, q, color, cat if (cat and cat.lower() != "flipkart live") else None) if x]
            query_str = " ".join(terms) if terms else (q or brand or cat or "")
            if query_str:
                live_items = FlipkartConnector.search_live(query_str, limit=25)
                # Apply strict relevance filtering!
                filtered = [
                    li for li in live_items
                    if is_product_relevant(li["name"], li["price"], q=q, brand=brand, color=color,
                                           min_price=kw.get("min_price"), max_price=kw.get("max_price"), category=cat)
                ]
                added = []
                upserted = []
                for li in filtered:
                    prod, _ = Product.objects.update_or_create(
                        store=store, external_id=li["external_id"],
                        defaults={
                            "name": li["name"],
                            "description": li["description"],
                            "category": cat or "Flipkart Live",
                            "price": li["price"],
                            "in_stock": li["in_stock"],
                            "url": li["url"],
                            "image_url": li["image_url"],
                        }
                    )
                    upserted.append(prod)
                    if prod.id not in exclude:
                        added.append(item(store, prod))
                index_products_async(upserted)

                if added:
                    if sort == "price_low":
                        added.sort(key=lambda r: r["price"])
                    elif sort == "price_high":
                        added.sort(key=lambda r: -r["price"])
                    print(f"LIVE FLIPKART STRICT MATCH: {len(added)} items for '{query_str}'")
                    return added[:limit]
        except Exception as err:
            print("LIVE FLIPKART SCRAPE EXCEPTION:", err)

    # 1) hybrid retrieval: meaning (vectors) + exact words (full-text), merged with RRF.
    #    Price, brand, category, colour and stock are applied in SQL.
    results = []
    ranks = {}
    search_text = " ".join(x for x in (q, brand, color) if x) or (cat or "")
    if search_text:
        try:
            ids = retrieve_products(store, search_text, k=40, brand=brand, category=cat,
                                    color=color, in_stock=in_stock, **kw)
            by_id = Product.objects.in_bulk(ids)
            results = [item(store, by_id[i]) for i in ids if i in by_id and i not in exclude]
            ranks = {pid: n for n, pid in enumerate(ids)}
            if results:
                print("HYBRID RETRIEVAL matched")
        except Exception as exc:
            print("HYBRID RETRIEVAL FAILED:", exc)

    # 2) safety net: the old keyword search (also used when there is no text, e.g. only a price)
    if not results:
        attempts = [q]
        if relax:
            cw = " ".join(w for w in q.split() if fuzzy_catalog(store, w))
            if cw and cw != q:
                attempts.append(cw)
        for a in attempts:
            res = search_products(store, query=a or None, category=cat, sort=sort,
                                  in_stock_only=in_stock, limit=40, brand=brand, color=color, **kw)
            res = [r for r in res if r["id"] not in exclude]
            if res:
                results = res
                break

    if not results and (q or brand or color):
        res = search_loose(store, q, sort=sort, in_stock_only=in_stock, limit=40,
                           brand=brand, color=color, **kw)
        results = [r for r in res if r["id"] not in exclude]

    # Strictly filter any fallback database results for relevance as well
    results = [
        r for r in results
        if is_product_relevant(r["name"], r["price"], q=q, brand=brand, color=color,
                               min_price=kw.get("min_price"), max_price=kw.get("max_price"), category=cat)
    ]

    # 4) order the results
    if sort == "price_low":
        results.sort(key=lambda r: r["price"])
    elif sort == "price_high":
        results.sort(key=lambda r: -r["price"])
    else:
        results.sort(key=lambda r: ranks.get(r["id"], len(ranks)))   # keep retrieval order
    print("SEARCH RESULTS:", [(r["name"], ranks.get(r["id"])) for r in results[:5]])
    return results[:limit]


def run_categories(store, cats, price, sort, exclude=()):
    out = []
    for c in cats:
        out += run_search(store, {"category": c, "sort": sort, **price}, exclude=exclude, limit=3)
    if sort == "price_low":
        out.sort(key=lambda r: r["price"])
    elif sort == "price_high":
        out.sort(key=lambda r: -r["price"])
    seen, uniq = set(), []
    for r in out:
        if r["id"] not in seen:
            seen.add(r["id"])
            uniq.append(r)
    return uniq[:5]


def subtype_options(store, word):
    """Real product types in this store for a bare category (e.g. running, sports, formal)."""
    vocab = get_vocab(store)
    w = norm(correct_word(SYNONYMS.get(word, word), vocab))
    prods = list(Product.objects.filter(store=store)
                 .filter(Q(name__icontains=w) | Q(category__icontains=w))[:200])
    if len(prods) < 4:
        return []
    c = Counter()
    for p in prods:
        for x in set(re.findall(r"[a-z]{3,}", p.name.lower())):
            if norm(x) != w and x not in STOPWORDS and x not in FILLER:
                c[x] += 1
    return [x for x, n in c.most_common(6) if n >= 2][:4]


def split_topics(store, t, price, sort):
    """'shoes and phones' -> [(shoes, results), (phones, results)] when they are different categories."""
    segs = [s for s in TOPIC_SPLIT_RE.split(t) if s.strip()]
    if len(segs) < 2:
        return None
    groups = []
    for s in segs:
        ws = [w for w in content_words(s) if fuzzy_catalog(store, w)]
        if not ws:
            continue
        res = run_search(store, {"query": " ".join(ws), "sort": sort or "relevance", **price}, limit=3)
        if res:
            groups.append((" ".join(ws), res))
    cats = {g[1][0]["category"] for g in groups}
    return groups if len(groups) >= 2 and len(cats) >= 2 else None


def present(store, convo, st, results, text, keep_seen=False, reco=False, terms=None):
    ids = [r["id"] for r in results]
    st = dict(st)
    st["seen"] = (st.get("seen", []) if keep_seen else []) + ids
    prices = [r["price"] for r in results]
    st["lowest"], st["highest"] = min(prices), max(prices)
    st["focus"] = ids[0] if len(ids) == 1 else None
    if terms:
        st["multi"], st["terms"] = True, terms
    else:
        st.pop("multi", None)
        st.pop("terms", None)
    st.pop("pending", None)
    st.pop("offer", None)
    save_state(convo, st, ids)

    slim = [{k: r[k] for k in ("name", "price", "currency", "category", "in_stock")} for r in results]
    goal = ("Recommend one or two of these products and explain the criteria you used (price, "
            "stock and details in the data). Say when information is missing. "
            if reco else
            "Mention at most three products with their exact prices, then end with one short "
            "helpful follow-up offer (for example offering to compare them). ")
    extra = ("Search results for the customer's latest message (exact store data): "
             + json.dumps(slim) + f". Filters applied: {describe_filters(store, st)}. " + goal)
    reply = write_reply(chat_system(store, extra), text)
    if not reply or not prices_ok(reply, results):
        reply = fallback_text(results)
    return reply, results


def explain_none(store, st, label):
    """Nothing matched: say exactly why. Returns (reply, cards, offer_word)."""
    cats = ", ".join(get_categories(store, 5))
    terms = [st.get("color"), st.get("brand"), label or st.get("query") or st.get("category") or "items"]
    label = " ".join(x for x in terms if x)
    base = {k: v for k, v in st.items() if k in ("query", "category", "brand", "color", "sort")}

    if st.get("min_price") is not None or st.get("max_price") is not None:
        near_st = dict(base)
        near_st["sort"] = "price_low" if st.get("max_price") is not None else "price_high"
        near = run_search(store, near_st)
        if near:
            p = near[0]
            return (f"I couldn't find any {label} within that budget on {store.name}. The closest "
                    f"option is {p['name']} at {fmt_price(p['price'], p.get('currency', store.currency))}. Would you like to "
                    "increase your budget or see other options?"), [p], None

    oos = run_search(store, base, in_stock=False)
    if oos:
        p = oos[0]
        return (f"{p['name']} is listed in our catalogue, but it is currently out of stock. I can "
                "show you similar products that are available if you like."), [p], None

    for w in reversed(content_words(label)):
        if fuzzy_catalog(store, w):
            part = run_search(store, {"query": w})
            if part:
                return (f"I couldn't find {label} on {store.name}, but I do have other {w} "
                        "products. Would you like to see them?"), [], w

    return not_available_reply(store, label, cats), [], None


def finish_none(store, convo, st, label):
    reply, cards, offer = explain_none(store, st, label)
    s = get_state(convo)
    if offer:
        s["offer"] = offer
    ids = None
    if cards:
        ids = [c["id"] for c in cards]
        s["focus"] = ids[0] if len(ids) == 1 else None
    s.pop("pending", None)
    save_state(convo, s, ids)
    return reply, cards


# =====================================================================
# Product questions, compare, recommend
# =====================================================================

def pick_focus(store, st, t, shown):
    refs = ordinal_refs(t, len(shown))
    if refs:
        return shown[refs[0] - 1]
    fid = st.get("focus")
    if fid:
        for p in shown:
            if p.id == fid:
                return p
        p = Product.objects.filter(store=store, id=fid).first()
        if p:
            return p
    if len(shown) == 1:
        return shown[0]
    return None


def answer_about(store, convo, st, p, text):
    d = item(store, p, True)
    extra = ("The customer asks about this product. Store data (exact): " + json.dumps(d) +
             ". Answer only from this data. If the customer asks for a detail that is not in the "
             "data (such as colour, size, material, warranty, reviews or ratings), say that this "
             "information is not available in the catalogue. Do not assume anything. Mention the "
             "price and stock status when relevant.")
    reply = write_reply(chat_system(store, extra), text)
    if not reply or not prices_ok(reply, [d]):
        status = "in stock" if p.in_stock else "currently out of stock"
        reply = f"{p.name} costs {fmt_price(d['price'], d.get('currency', store.currency))} and is {status}."
    st = dict(st)
    st["focus"] = p.id
    save_state(convo, st)
    return reply, [item(store, p)]


def compare(store, convo, st, picks, text):
    data = [item(store, p, True) for p in picks]
    extra = ("Compare these products using only this exact store data: " + json.dumps(data) +
             ". Compare the same attributes for each one (price, category, stock status and any "
             "details in the description). Where a detail is missing, say 'information not "
             "available'. Finish with a short recommendation explaining which suits which need.")
    reply = write_reply(chat_system(store, extra), text)
    if not reply or not prices_ok(reply, data):
        bits = [f"{d['name']} costs {fmt_price(d['price'], d.get('currency', store.currency))} "
                f"({'in stock' if d['in_stock'] else 'out of stock'})" for d in data]
        reply = ("Here is how they compare: " + "; ".join(bits) +
                 ". Other specifications are not available in our catalogue.")
    return reply, [item(store, p) for p in picks]


def recommend(store, convo, st, shown, text):
    data = [item(store, p, True) for p in shown[:5]]
    extra = ("Products shown to the customer (exact store data): " + json.dumps(data) +
             f". The customer's filters: {describe_filters(store, st)}. Recommend ONE product and "
             "mention one alternative if useful. Explain the criteria you used (price, stock, and "
             "details in the description). Do not claim anything that is not in the data, and "
             "say when information such as ratings or specifications is missing.")
    reply = write_reply(chat_system(store, extra), text)
    if not reply or not prices_ok(reply, data):
        in_stock = [d for d in data if d["in_stock"]] or data
        best = min(in_stock, key=lambda d: d["price"])
        reply = (f"Based on price and availability, {best['name']} at {best['currency']} "
                 f"{best['price']:.2f} looks like the best-value option here. I don't have ratings "
                 "or detailed specifications to compare further.")
    return reply, [item(store, p) for p in shown[:5]]


def use_case_search(store, convo, need_text, price, sort, text):
    cats = suggest_categories(store, need_text)
    print("USE-CASE CATEGORIES:", cats)
    if not cats:
        c = ", ".join(get_categories(store, 6))
        return (f"I couldn't match that to a specific product yet. Are you looking for something "
                f"like {c}? Tell me the type of product and I'll check our catalogue." if c
                else which_product_reply(store)), []
    results = run_categories(store, cats, price, sort)
    st = {"query": "", "category": cats[0], "sort": sort, **price}
    if results:
        return present(store, convo, st, results, text, reco=True)
    return finish_none(store, convo, st, " or ".join(cats))


# =====================================================================
# Main flow
# =====================================================================

def _core(store, convo, text):
    st = get_state(convo)
    t = normalize_numbers(text.lower().replace("’", "'")).strip()
    shown = get_shown(store, convo)

    # The customer said "yes" to an offer ("I do have other shoes, want to see them?").
    if st.get("offer") and YES_RE.match(t):
        new = {"query": st["offer"]}
        res = run_search(store, new)
        if res:
            return present(store, convo, new, res, text)

    # 1) Friendly conversation and store discovery
    if DISCOVERY_RE.search(t):
        return discovery_reply(store), []
    if HELP_RE.search(t):
        return help_reply(store, t), []
    canned = smalltalk_reply(store, t)
    if canned:
        return canned, []

    # 1b) Reset typed in chat: clear the shopping context, keep the messages.
    if RESET_RE.search(t):
        save_state(convo, {}, [])
        return ("Okay, I've cleared our shopping context. What would you like to look for now?"), []

    # 2) Code / homework: not related to the store.
    if is_code_request(t):
        return not_related_reply(store), []

    # 3) Store policies, FAQs and customer assistance (answered from the ingested documents)
    focus = None
    if FOLLOWUP_RE.search(t) and (shown or st.get("focus")):
        focus = pick_focus(store, st, t, shown)       # the product the customer is talking about
    if POLICY_RE.search(t):
        return answer_with_rag(store, t, product=focus), []
    if ORDER_RE.search(t):
        if re.search(r"\b(return|refund|exchange|replacement|damaged|cancel)", t):
            return answer_with_rag(store, t, fallback=order_reply(store), product=focus), []
        return order_reply(store), []                 # order tracking needs account data, not documents
    if DISCOUNT_RE.search(t):
        return answer_with_rag(store, t, fallback=discount_reply(store)), []
    if HUMAN_RE.search(t):
        return human_support_reply(store), []
    if FRUSTRATED_RE.search(t):
        return ("Sorry about that! Let me try again. Could you tell me the most important thing "
                "you're looking for? I'll use that to refine the results."), []
    if CART_RE.search(t):
        p = pick_focus(store, st, t, shown) if shown else None
        reply = ("To purchase this item, please open the product page using the link on the card and "
                 "use the Add to Cart and secure checkout options!")
        return (reply, [item(store, p)]) if p else (reply, [])
    if REVIEW_RE.search(t):
        return ("Customer reviews and ratings are available on individual product pages. "
                "I can also compare prices, stock, and descriptions across our catalogue for you."), []

    # 4) The customer rejects the shown products.
    if DISLIKE_RE.search(t):
        if shown and (st.get("query") or st.get("category")):
            st["rejected"] = list(set(st.get("rejected", []) + st.get("seen", [])))
            st["pending"] = "refine"
            save_state(convo, st)
            return ("No problem! What would you like to change: the brand, design, colour, style "
                    "or price? I can also show you different options in the same category."), []
        return which_product_reply(store), []

    # 5) Understand the message.
    price = parse_price(t)
    sort = parse_sort(t)
    words = content_words(t)
    cat_new = [w for w in words if fuzzy_catalog(store, w)]
    rel = "cheaper" if CHEAPER_RE.search(t) else ("pricier" if PRICIER_RE.search(t) else None)
    more = bool(MORE_RE.search(t))
    similar = bool(SIMILAR_RE.search(t))
    signals = bool(price or sort or rel or more or similar)

    terms = st.get("terms") or []
    has_state = bool(st.get("query") or st.get("category") or terms)
    state_norm = {norm(w) for w in content_words(st.get("query", ""))}
    state_norm |= {norm(w) for term in terms for w in term.split()}
    if st.get("category"):
        state_norm.add(norm(str(st["category"]).lower()))
    overlap = any(norm(w) in state_norm for w in words)
    fresh = bool(cat_new) and not overlap and not PRONOUN_REF_RE.search(t)
    print("SIGNALS:", {"price": price, "sort": sort, "words": words, "catalog": cat_new,
                       "rel": rel, "more": more, "similar": similar,
                       "has_state": has_state, "fresh": fresh})

    refs = ordinal_refs(t, len(shown))

    # 5b) Ambiguity: the last search mixed several kinds of products.
    if (st.get("multi") and not fresh and not refs
            and (rel or more or similar or BEST_RE.search(t) or COMPARE_RE.search(t)
                 or WHICH_RE.search(t))):
        return clarify_reply(st), []

    # 6) Compare / recommend / questions about shown products.
    if COMPARE_RE.search(t):
        if len(shown) < 2:
            return ("I need at least two products to compare. Search for a product first and "
                    "I'll compare the results for you."), []
        picks = [shown[i - 1] for i in refs] if len(refs) >= 2 else shown[:3]
        return compare(store, convo, st, picks, text)

    if BEST_RE.search(t) and shown and not fresh:
        return recommend(store, convo, st, shown, text)

    if shown and not fresh and (refs or (PRONOUN_RE.search(t) and ABOUT_RE.search(t))):
        p = pick_focus(store, st, t, shown)
        if not p:
            return ("Which one do you mean? You can say the first, second or third product, or "
                    "tell me its name."), []
        return answer_about(store, convo, st, p, text)

    # 7) Continue the earlier search (cheaper, more, only X, under N, similar ...).
    refine = has_state and not fresh and (
        st.get("pending") in ("type", "refine")
        or overlap
        or (not cat_new and not words and signals)
        or (not cat_new and bool(words) and bool(REFINE_RE.search(t))))

    # Unclear wording: let the model decide whether it continues the current search.
    if (not refine and has_state and not fresh and not cat_new and words and not signals):
        kind = followup_check(store, st, text)
        print("FOLLOWUP CHECK:", kind)
        if kind == "followup":
            refine = True
        elif kind == "unclear":
            return clarify_reply(st), []

    if refine:
        new = {k: v for k, v in st.items() if k in
               ("query", "category", "min_price", "max_price", "sort", "seen", "rejected")}
        merged = []
        for w in content_words(st.get("query", "")) + words:
            if norm(w) not in [norm(x) for x in merged]:
                merged.append(w)
        new["query"] = " ".join(merged)
        new.update(price)
        if sort:
            new["sort"] = sort

        eff = dict(new)
        exclude = set(st.get("rejected", []))
        if rel == "cheaper" and st.get("lowest") is not None:
            cap = st["lowest"] - 0.01
            eff["max_price"] = min(eff.get("max_price", cap), cap)
            eff["sort"] = "price_high"
        elif rel == "pricier" and st.get("highest") is not None:
            floor = st["highest"] + 0.01
            eff["min_price"] = max(eff.get("min_price", floor), floor)
            eff["sort"] = "price_low"
        if more or similar:
            exclude |= set(st.get("seen", []))
        if similar and shown:
            eff["query"] = ""
            eff["category"] = (pick_focus(store, st, t, shown) or shown[0]).category
        print("REFINE SEARCH:", eff, "exclude:", len(exclude))

        results = run_search(store, eff, exclude=exclude, relax=False)
        if results:
            return present(store, convo, new, results, text,
                           keep_seen=bool(more or similar), reco=bool(RECO_RE.search(t)))

        if rel in ("cheaper", "pricier"):
            edge = run_search(store, {"query": new["query"], "category": new.get("category"),
                                      "sort": "price_low" if rel == "cheaper" else "price_high"},
                              limit=1)
            word = "least expensive" if rel == "cheaper" else "most expensive"
            if edge:
                p = edge[0]
                return (f"These are the {word} matching products currently available in our catalogue "
                        f"({p['name']} at {p['currency']} {p['price']:.2f}). Would you like to change "
                        "your budget slightly or explore a different category?"), edge
        if more or similar:
            return ("That's everything matching in our catalogue right now. Would you like to change "
                    "your budget or look at a different category?"), []
        return finish_none(store, convo, eff, new["query"])

    # 8) A new request. From here on NOTHING from the old search is reused.
    if st.get("pending") == "gift":
        st.pop("pending", None)
        save_state(convo, st)
        return use_case_search(store, convo, "gift " + t, price, sort or "relevance", text)

    if not cat_new:
        if GIFT_RE.search(t):
            if price:
                return use_case_search(store, convo, t, price, sort or "relevance", text)
            st["pending"] = "gift"
            save_state(convo, st)
            return ("I'd be happy to help! Who is the gift for, and approximately how much would "
                    "you like to spend?"), []
        if VAGUE_RE.search(t) or THING_RE.search(t):
            cats = ", ".join(get_categories(store, 5))
            if VAGUE_RE.search(t) and cats:
                return (f"Of course! Are you looking for {cats}, or something else? You can also "
                        "tell me your budget."), []
            return which_product_reply(store), []
        if not words:
            return which_product_reply(store), []

        # Words outside the catalog (e.g. "cellphone"): try meaning search first,
        # so they reach the right products before the model classifies them.
        sem_new = {"query": " ".join(words), "category": None, **price,
                   "sort": sort or "relevance"}
        sem_results = run_search(store, sem_new)
        if sem_results:
            print("NEW SEARCH (meaning):", sem_new)
            return present(store, convo, sem_new, sem_results, text, reco=bool(RECO_RE.search(t)))

        kind = classify(store, st, text)
        print("CLASSIFIED:", kind)
        if kind == "chat":
            return smalltalk_model(store, text)
        if kind == "other":
            return not_related_reply(store), []
        return use_case_search(store, convo, text, price, sort or "relevance", text)

    # Several different products in one message ("shoes and phones").
    groups = split_topics(store, t, price, sort)
    if groups:
        print("MULTI TOPIC:", [g[0] for g in groups])
        combined, seen_ids = [], set()
        for _, res in groups:
            for r in res:
                if r["id"] not in seen_ids:
                    seen_ids.add(r["id"])
                    combined.append(r)
        new = {"query": "", "category": None, "sort": sort or "relevance", **price}
        return present(store, convo, new, combined, text, terms=[g[0] for g in groups])

    # Bare category with "I want / I need": ask the type if the store really has several.
    bare = (len({norm(w) for w in words}) == 1 and not price and not sort
            and WANT_RE.search(t) and not rel)
    if bare:
        opts = subtype_options(store, words[0])
        if len(opts) >= 2:
            st = {"query": words[0], "pending": "type"}
            save_state(convo, st)
            return (f"Sure! What kind of {words[0]} are you looking for? For example: "
                    f"{', '.join(opts)}. You can also tell me your budget."), []

    new = {"query": " ".join(words), "category": None, **price,
           "sort": sort or ("price_low" if rel == "cheaper" else
                            "price_high" if rel == "pricier" else "relevance")}
    print("NEW SEARCH:", new)
    results = run_search(store, new)
    if results:
        return present(store, convo, new, results, text, reco=bool(RECO_RE.search(t)))
    return finish_none(store, convo, new, " ".join(words))


def _answer(store, convo, user_text):
    from .engine import core
    text, greet = split_greeting(user_text)
    reply, cards = core(store, convo, text)
    if greet and reply and not reply.lower().startswith(("hi", "hello", "hey", "good")):
        reply = f"{greet} {reply}"
    return reply, cards


def run_chat_full(store, convo, user_text, request_id=None):
    """
    Answer one message. Everything is saved in ONE transaction: if anything fails,
    neither the user message nor the shopping state is kept, so a failed request
    never leaves a half-saved chat or a misleading assistant reply.
    """
    with transaction.atomic():
        Message.objects.create(conversation=convo, role="user", content=user_text)
        try:
            reply, cards = _answer(store, convo, user_text)
        except ModelDown:
            t = user_text.lower()
            canned = smalltalk_reply(store, t)
            if canned:
                cards, reply = [], canned
            else:
                words = " ".join(content_words(t))
                cards = run_search(store, {"query": words, **parse_price(t)}, limit=3) if words else []
                if not cards:
                    # The engine is down and the database cannot answer either:
                    # save nothing and let the caller show a clean error.
                    raise AssistantUnavailable()
                reply = fallback_text(cards)
        card_ids = [c["id"] for c in cards] if cards else []
        amsg = Message.objects.create(conversation=convo, role="assistant", content=reply, cards=card_ids)

        # Persist product cards (as ids) and a title, so a refresh restores the chat.
        prefs = dict(convo.preferences or {})
        if cards:
            cm = dict(prefs.get("cards", {}))
            cm[str(amsg.id)] = card_ids
            for k in sorted(cm, key=int)[:-60]:
                cm.pop(k)
            prefs["cards"] = cm
        if not prefs.get("title") and not is_smalltalk(user_text):
            title_text = user_text.strip()[:50]
            prefs["title"] = title_text
            convo.title = title_text
        result = {"reply": reply, "products": cards, "conversation_id": convo.session_id}
        if request_id:
            prefs["last_request"] = {"id": request_id, "response": result}
        convo.preferences = prefs
        convo.save()
    return result


def run_chat(store, convo, user_text):
    return run_chat_full(store, convo, user_text)["reply"]