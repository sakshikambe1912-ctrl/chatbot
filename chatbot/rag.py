import re

from ingestion.retrieval import retrieve

NO_INFO = ("I don't have that information in our store documents. "
           "Please check the 'Contact Us' page or ask our support team.")

# "what about returns for it?", "is this covered by warranty?"
FOLLOWUP_RE = re.compile(r"\b(it|this|that|these|those|them|this one|that one|the first|the second)\b", re.I)

CITE_RE = re.compile(r"\[(\d+)\]")
NUM_RE = re.compile(r"\d+(?:[.,]\d+)*")

CHUNK_CHARS = 1200      # per chunk sent to the model (keeps the CPU model fast)
EXCERPT_CHARS = 450     # length of the plain-text fallback answer


def standalone_question(question, product=None):
    """Make a follow-up question self-contained, without calling the (small) LLM."""
    q = (question or "").strip()
    if product is not None and FOLLOWUP_RE.search(q):
        return f"{q} (about: {product.name})"
    return q


def chunk_title(c):
    """Title of a retrieved chunk: from meta, else from its Document."""
    title = (c.meta or {}).get("title")
    if not title and getattr(c, "document_id", None):
        try:
            title = c.document.title
        except Exception:
            title = None
    return title or "Store information"


def _numbers(text):
    """Every number in a text, normalised ('1,500' -> '1500'), citation markers excluded."""
    text = CITE_RE.sub(" ", text or "")
    return {m.replace(",", "").rstrip(".") for m in NUM_RE.findall(text)}


def citations_ok(reply, n_chunks):
    """Every [n] in the reply must point at a real context chunk."""
    return all(1 <= int(n) <= n_chunks for n in CITE_RE.findall(reply))


def numbers_ok(reply, context):
    """Every number in the reply must appear in the context (like prices_ok for prices)."""
    return _numbers(reply) <= _numbers(context)


def excerpt(chunk, limit=EXCERPT_CHARS):
    """Plain-text fallback: the start of the chunk, cut at a sentence end."""
    text = re.sub(r"(?m)^\s*#+\s*", "", chunk.text or "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        cut = text[:limit]
        end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
        text = (cut[:end + 1] if end > 150 else cut.rstrip()) + ("" if end > 150 else "...")
    return f"From our {chunk_title(chunk)}: {text}"


def _generate(store, system_extra, question):
    """The only place that calls the model (so tests can replace it)."""
    from chatbot import services as S            # imported here to avoid a circular import
    resp = S.llm(messages=[{"role": "system", "content": S.chat_system(store, system_extra)},
                           {"role": "user", "content": question}],
                 options={"temperature": 0.2, "num_predict": 250})
    return S.clean_reply(resp.message.content or "")


def _model_down_errors():
    from chatbot import services as S
    return (S.ModelDown,)


def answer_with_sources(store, question, fallback=None, product=None, k=3):
    """Grounded answer. Returns (reply, sources) where sources is a list of {"title": ...}.
    fallback: reply used only when nothing relevant is found (instead of NO_INFO)."""
    miss = fallback or NO_INFO
    query = standalone_question(question, product)
    try:
        chunks = retrieve(store, query, k=k, only_docs=True)
    except Exception as exc:
        print("RAG RETRIEVE FAILED:", exc)
        return miss, []
    if not chunks:
        return miss, []                           # nothing relevant: do not guess

    context = "\n\n".join(f"[{i + 1}] {chunk_title(c)}: {(c.text or '')[:CHUNK_CHARS]}"
                          for i, c in enumerate(chunks))
    extra = ("Answer ONLY from the numbered context below. Cite the source like [1] after each "
             "fact. If the context does not contain the answer, say you don't have that "
             "information and suggest contacting the store's support team. Do not invent days, "
             "amounts or conditions.\n\nCONTEXT:\n" + context)
    if product is not None:
        extra += f"\n\nThe customer is asking about this product: {product.name}."

    top = [{"title": chunk_title(chunks[0])}]
    try:
        reply = _generate(store, extra, query)
    except _model_down_errors():
        return excerpt(chunks[0]), top            # retrieval worked, the model did not
    if not reply:
        return excerpt(chunks[0]), top

    allowed = context + " " + (product.name if product is not None else "")
    if not citations_ok(reply, len(chunks)) or not numbers_ok(reply, allowed):
        print("RAG ANSWER REJECTED (bad citation or number):", reply)
        return excerpt(chunks[0]), top

    cited = sorted({int(n) for n in CITE_RE.findall(reply)})
    sources = [{"title": chunk_title(chunks[i - 1])} for i in cited]
    return reply, sources


def answer_with_rag(store, question, fallback=None, product=None, k=3):
    """Same as answer_with_sources, but returns ONE string for the chat reply:
    the answer, followed by a 'Sources:' line when it cited something."""
    reply, sources = answer_with_sources(store, question, fallback=fallback, product=product, k=k)
    if sources:
        titles = ", ".join(dict.fromkeys(s["title"] for s in sources))
        reply = f"{reply}\n\nSources: {titles}"
    return reply