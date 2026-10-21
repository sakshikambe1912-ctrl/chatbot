import math

from django.core.management.base import BaseCommand

from catalog.embeddings import embed, semantic_scores
from catalog.models import Product, Store


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


class Command(BaseCommand):
    help = "Check that RAG (embeddings + semantic search) works, step by step."

    def add_arguments(self, parser):
        parser.add_argument("query", nargs="+")
        parser.add_argument("--store", type=int)
        parser.add_argument("--top", type=int, default=10)

    def handle(self, *args, **o):
        store = Store.objects.get(id=o["store"]) if o["store"] else Store.objects.first()
        q = " ".join(o["query"])
        out = self.stdout.write

        # Step 1: are the products embedded?
        prods = list(Product.objects.filter(store=store))
        have = [p for p in prods if p.embedding]
        out(f"STEP 1  Store {store.id} '{store.name}'")
        out(f"        products: {len(prods)} | with vectors: {len(have)} | "
            f"WITHOUT vectors: {len(prods) - len(have)}")
        try:
            from catalog.embeddings import product_text
            stale = sum(1 for p in have if p.embedded_text != product_text(p))
            out(f"        vectors out of date (text changed since embedding): {stale}")
        except Exception:
            pass
        if not have:
            out("        No vectors found. Run: python manage.py sync_store <store id>")
            return
        out(f"        vector size: {len(have[0].embedding)} numbers per product\n")

        # Step 2: the question becomes a vector
        try:
            qv = embed("search_query: " + q)
        except Exception as exc:
            out(f"STEP 2  FAILED to embed the question: {exc}")
            out("        Is Ollama running, and did you run: ollama pull nomic-embed-text ?")
            return
        out(f"STEP 2  Question: '{q}'")
        out(f"        became a vector of {len(qv)} numbers, first 5: "
            f"{[round(x, 4) for x in qv[:5]]}\n")

        # Step 3: compare it with every stored vector (computed here, independently)
        ranked = sorted(((cosine(qv, p.embedding), p) for p in have), key=lambda x: -x[0])
        try:
            from chatbot.tools import MIN_SIM
        except Exception:
            MIN_SIM = 0.62
        out(f"STEP 3  Top {o['top']} by cosine similarity (* = above the chatbot's cutoff {MIN_SIM})")
        for i, (sc, p) in enumerate(ranked[: o["top"]], 1):
            mark = "*" if sc >= MIN_SIM else " "
            stock = "in stock" if p.in_stock else "OUT OF STOCK"
            out(f"        {i:>2}.{mark}[{sc:.3f}] {p.name} | {store.currency} {p.price} | "
                f"{p.category} | {stock}")

        # Step 4: does the project's own function agree with this manual calculation?
        out("\nSTEP 4  Cross-check against catalog.embeddings.semantic_scores")
        theirs = dict(semantic_scores(store, q, 0.0))
        diffs = [abs(theirs.get(p.id, -9) - sc) for sc, p in ranked[: o["top"]]]
        worst = max(diffs) if diffs else 0
        out(f"        biggest difference in top {o['top']}: {worst:.5f}  ->  "
            f"{'MATCH, scoring is correct' if worst < 0.001 else 'MISMATCH, check embeddings.py'}")