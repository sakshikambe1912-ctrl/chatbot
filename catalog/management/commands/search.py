from django.core.management.base import BaseCommand

from catalog.embeddings import semantic_scores
from catalog.models import Product, Store


class Command(BaseCommand):
    help = "Search the store's products from the terminal (keyword + semantic, like the chatbot)."

    def add_arguments(self, parser):
        parser.add_argument("query", nargs="+", help="what to search for")
        parser.add_argument("--store", type=int, help="store id (default: first store)")
        parser.add_argument("--limit", type=int, default=50)
        parser.add_argument("--min-price", type=float)
        parser.add_argument("--max-price", type=float)
        parser.add_argument("--sort", choices=["relevance", "price_low", "price_high"],
                            default="relevance")
        parser.add_argument("--semantic-only", action="store_true",
                            help="raw meaning-search results with scores, no cutoff")

    def handle(self, *args, **o):
        store = Store.objects.get(id=o["store"]) if o["store"] else Store.objects.first()
        if not store:
            self.stderr.write("No store found. Create one first.")
            return
        q = " ".join(o["query"])
        self.stdout.write(f"Store: {store.id} {store.name}")
        self.stdout.write(f"Query: {q}\n")

        try:
            scores = dict(semantic_scores(store, q, 0.0))
        except Exception as exc:
            scores = {}
            self.stderr.write(f"Semantic scores unavailable: {exc}")

        rows = []  # (score, name, currency, price, in_stock)
        if o["semantic_only"]:
            products = {p.id: p for p in Product.objects.filter(store=store)}
            for pid, sc in sorted(scores.items(), key=lambda x: -x[1]):
                p = products.get(pid)
                if not p:
                    continue
                price = float(p.price)
                if o["max_price"] is not None and price > o["max_price"]:
                    continue
                if o["min_price"] is not None and price < o["min_price"]:
                    continue
                rows.append((sc, p.name, store.currency, p.price, p.in_stock))
            rows = rows[: o["limit"]]
        else:
            from chatbot.services import run_search
            st = {"query": q, "sort": o["sort"]}
            if o["min_price"] is not None:
                st["min_price"] = o["min_price"]
            if o["max_price"] is not None:
                st["max_price"] = o["max_price"]
            for r in run_search(store, st, limit=o["limit"]):
                rows.append((scores.get(r["id"], 0.0), r["name"],
                             r.get("currency", store.currency), r["price"], r["in_stock"]))

        if not rows:
            self.stdout.write("No products found.")
            return
        self.stdout.write(f"{len(rows)} result(s):\n")
        for i, (sc, name, cur, price, in_stock) in enumerate(rows, 1):
            stock = "in stock" if in_stock else "OUT OF STOCK"
            self.stdout.write(f"{i:>2}. [{sc:.2f}] {name} | {cur} {price} | {stock}")