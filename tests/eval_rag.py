r"""RAG evaluation. Run from the project folder (PowerShell):

    python tests\eval_rag.py --docs        show the ingested documents (write questions from these)
    python tests\eval_rag.py               retrieval metrics: recall@5, MRR (fast; needs Ollama for embeddings)
    python tests\eval_rag.py --sweep       the same for several min_sim values, to pick the best one
    python tests\eval_rag.py --answers     also generate answers and grade them (slow; uses the chat model)

Questions live in tests\golden_rag.json:
    {"q": "...", "doc": "Return and Refund Policy", "must": ["7 days|seven days"]}
      doc   title of the document that answers it (a list is allowed); null = the docs cannot answer it
      must  optional key facts; each entry is matched case-insensitively, "a|b" means a or b
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).with_name("golden_rag.json")
K = 5

# phrases that mean "I don't know" in a reply
IDK_RE = re.compile(r"don'?t have|do not have|not available|couldn'?t find|can'?t find|cannot find|"
                    r"no information|not (?:mentioned|specified|provided|listed)|unable to find", re.I)


# ------------------------------------------------------------------ pure scoring (no Django)

def norm(s):
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def expected_titles(case):
    doc = case.get("doc")
    if not doc:
        return set()
    return {norm(d) for d in ([doc] if isinstance(doc, str) else doc)}


def has_must(text, must):
    """Every key fact must appear; 'a|b' accepts either spelling."""
    t = norm(text)
    return all(any(norm(alt) in t for alt in m.split("|")) for m in (must or []))


def is_idk(reply, no_info=""):
    return bool(reply) and (norm(reply) == norm(no_info) or bool(IDK_RE.search(reply)))


def score_retrieval(case, found):
    """found = [(title, text), ...] best first. Returns per-question facts."""
    want = expected_titles(case)
    rank = next((i for i, (title, _) in enumerate(found, 1) if norm(title) in want), None)
    covered = None
    if case.get("must"):
        covered = has_must(" ".join(text for _, text in found), case["must"])
    return {"rank": rank, "hit": rank is not None, "covered": covered,
            "titles": [t for t, _ in found]}


def run_retrieval(cases, search):
    """search(question) -> [(title, text), ...]. Returns (summary, rows)."""
    rows, ans, unans = [], [], []
    for case in cases:
        found = search(case["q"])
        if case.get("doc"):
            r = score_retrieval(case, found)
            r["case"] = case
            ans.append(r)
            rows.append(r)
        else:
            unans.append({"case": case, "empty": not found, "titles": [t for t, _ in found]})
    cov = [r["covered"] for r in ans if r["covered"] is not None]
    summary = {
        "n_answerable": len(ans),
        "recall": sum(r["hit"] for r in ans) / len(ans) if ans else 0.0,
        "mrr": sum(1 / r["rank"] for r in ans if r["rank"]) / len(ans) if ans else 0.0,
        "coverage": sum(cov) / len(cov) if cov else None,
        "n_unanswerable": len(unans),
        "abstained": sum(u["empty"] for u in unans),
        "misses": [r for r in ans if not r["hit"]],
        "false_hits": [u for u in unans if not u["empty"]],
    }
    return summary, rows


def grade_answer(case, reply, raw, context, n_chunks, no_info=""):
    """Grade one generated answer. raw = the model's text before the safety checks."""
    from chatbot import rag
    faithful = None
    if raw:
        faithful = rag.citations_ok(raw, n_chunks) and rag.numbers_ok(raw, context)
    idk = is_idk(reply, no_info)
    if case.get("doc"):
        correct = (not idk) and has_must(reply, case.get("must"))
    else:
        correct = idk                      # unanswerable: the right answer is "I don't know"
    return {"correct": correct, "faithful": faithful, "idk": idk}


def run_answers(cases, answer_fn, no_info=""):
    """answer_fn(question) -> (reply, raw, context, n_chunks)."""
    out = []
    for case in cases:
        reply, raw, context, n = answer_fn(case["q"])
        g = grade_answer(case, reply, raw, context, n, no_info)
        g.update(case=case, reply=reply)
        out.append(g)
    ans = [g for g in out if g["case"].get("doc")]
    unans = [g for g in out if not g["case"].get("doc")]
    faith = [g["faithful"] for g in out if g["faithful"] is not None]
    return {
        "n_answerable": len(ans), "correct": sum(g["correct"] for g in ans),
        "n_unanswerable": len(unans), "idk_ok": sum(g["correct"] for g in unans),
        "faithful": sum(faith), "n_model_answers": len(faith),
        "wrong": [g for g in out if not g["correct"]],
    }


# ------------------------------------------------------------------ reporting

def show_retrieval(s, min_sim):
    print(f"\nRETRIEVAL   (top {K}, min_sim={min_sim}, {s['n_answerable']} answerable questions)")
    print(f"  recall@{K}   {s['recall']:.2f}   the right document is in the top {K}")
    print(f"  MRR        {s['mrr']:.2f}   1.00 = always first")
    if s["coverage"] is not None:
        print(f"  coverage   {s['coverage']:.2f}   the retrieved text contains the key facts")
    if s["n_unanswerable"]:
        print(f"  abstained  {s['abstained']}/{s['n_unanswerable']}   unanswerable questions that returned nothing")
    for r in s["misses"]:
        print(f'  MISS  "{r["case"]["q"]}"  wanted {r["case"]["doc"]}  got {r["titles"] or "nothing"}')
    for u in s["false_hits"]:
        print(f'  (unanswerable but retrieved) "{u["case"]["q"]}"  got {u["titles"]}')


def show_answers(a):
    print(f"\nANSWERS     ({a['n_answerable']} answerable, {a['n_unanswerable']} unanswerable)")
    print(f"  correct answers          {a['correct']}/{a['n_answerable']}")
    print(f"  said \"I don't know\" right {a['idk_ok']}/{a['n_unanswerable']}")
    if a["n_model_answers"]:
        print(f"  model faithful (before the safety check)  {a['faithful']}/{a['n_model_answers']}")
    for g in a["wrong"]:
        kind = "should say I don't know" if not g["case"].get("doc") else "wrong or missing facts"
        print(f'  FAIL  "{g["case"]["q"]}"  ({kind})\n        -> {g["reply"][:160]!r}')


# ------------------------------------------------------------------ Django side

def main():
    ap = argparse.ArgumentParser(description="Evaluate retrieval and answers")
    ap.add_argument("--store", default="Flipkart", help="store name or id (default Flipkart)")
    ap.add_argument("--docs", action="store_true", help="list the ingested documents and exit")
    ap.add_argument("--sweep", action="store_true", help="try several min_sim values")
    ap.add_argument("--answers", action="store_true", help="also generate and grade answers (slow)")
    ap.add_argument("--limit", type=int, help="only the first N questions (useful with --answers)")
    ap.add_argument("--min-sim", type=float, default=0.55)
    ap.add_argument("--min-recall", type=float, help="exit with an error if recall@5 is lower")
    ap.add_argument("--golden", default=str(GOLDEN))
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.path.insert(0, str(ROOT))
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django
    django.setup()

    from catalog.models import Store
    from ingestion.models import Chunk
    import ingestion.retrieval as R

    v = args.store
    store = Store.objects.filter(id=int(v)).first() if v.isdigit() else Store.objects.filter(name__iexact=v).first()
    if not store:
        sys.exit(f"Store '{v}' not found. Stores: {list(Store.objects.values_list('id', 'name'))}")

    chunks = list(Chunk.objects.filter(store=store, document__isnull=False).select_related("document"))
    titles = {norm(c.document.title): c.document.title for c in chunks}

    if args.docs:
        print(f"{len(chunks)} document chunk(s) in store {store.name}:\n")
        for c in chunks:
            print(f"=== {c.document.title}  ({c.document.doc_type}) ===\n{c.text[:900]}\n")
        return
    if not titles:
        sys.exit(f"No documents found for store {store.name}. Run: python manage.py ingest_docs --store {store.id}")

    cases = json.loads(Path(args.golden).read_text(encoding="utf-8"))["cases"]
    bad = [(c["q"], d) for c in cases for d in ([c["doc"]] if isinstance(c.get("doc"), str) else (c.get("doc") or []))
           if norm(d) not in titles]
    if bad:
        print("These questions name a document that does not exist. Existing titles:", list(titles.values()))
        for q, d in bad:
            print(f'  "{q}" -> {d}')
        sys.exit(1)
    if args.limit:
        cases = cases[:args.limit]
    n_un = sum(1 for c in cases if not c.get("doc"))
    print(f"Store: {store.name} | {len(cases)} questions ({len(cases) - n_un} answerable, {n_un} unanswerable)")

    # embed each question only once, even across a sweep
    orig_embed, cache = R.embed_batch, {}

    def cached_embed(texts, prefix=""):
        key = (tuple(texts), prefix)
        if key not in cache:
            cache[key] = orig_embed(texts, prefix)
        return cache[key]

    R.embed_batch = cached_embed

    from chatbot import rag

    def search_with(min_sim):
        def search(q):
            return [(rag.chunk_title(c), c.text)
                    for c in R.retrieve(store, q, k=K, min_sim=min_sim, only_docs=True)]
        return search

    summary = None
    if args.sweep:
        print("\nmin_sim  recall@5   MRR   abstained(unanswerable)")
        for ms in (0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70):
            s, _ = run_retrieval(cases, search_with(ms))
            print(f"  {ms:.2f}     {s['recall']:.2f}    {s['mrr']:.2f}    {s['abstained']}/{s['n_unanswerable']}")
        print("\nPick the highest min_sim that keeps recall@5 about as high as the lowest one.")
    else:
        summary, _ = run_retrieval(cases, search_with(args.min_sim))
        show_retrieval(summary, args.min_sim)

    if args.answers:
        seen = {}
        orig_gen, orig_ret = rag._generate, rag.retrieve

        def spy_gen(st, extra, question):
            seen["raw"] = orig_gen(st, extra, question)
            return seen["raw"]

        def spy_ret(st, query, **kw):
            kw["min_sim"] = args.min_sim
            seen["chunks"] = orig_ret(st, query, **kw)
            return seen["chunks"]

        rag._generate, rag.retrieve = spy_gen, spy_ret

        def answer_fn(q):
            seen.clear()
            reply, _ = rag.answer_with_sources(store, q)
            found = seen.get("chunks") or []
            ctx = "\n\n".join(f"[{i + 1}] {rag.chunk_title(c)}: {(c.text or '')[:rag.CHUNK_CHARS]}"
                              for i, c in enumerate(found))
            return reply, seen.get("raw"), ctx, len(found)

        print("\nGenerating answers (this uses the chat model and can take a while)...")
        show_answers(run_answers(cases, answer_fn, no_info=rag.NO_INFO))

    if args.min_recall is not None and summary and summary["recall"] < args.min_recall:
        sys.exit(f"recall@5 {summary['recall']:.2f} is below {args.min_recall}")


if __name__ == "__main__":
    main()