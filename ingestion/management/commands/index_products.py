from django.core.management.base import BaseCommand, CommandError

from catalog.models import Product
from ingestion.products import index_products


class Command(BaseCommand):
    help = "Create or refresh the embedding chunk of every product (unchanged ones are skipped)."

    def add_arguments(self, parser):
        parser.add_argument("--store", help="Only this store (id or name). Default: all stores.")
        parser.add_argument("--force", action="store_true", help="Re-embed even if unchanged")

    def handle(self, *args, **opts):
        qs = Product.objects.order_by("id")
        if opts["store"]:
            v = opts["store"]
            qs = qs.filter(store_id=int(v)) if v.isdigit() else qs.filter(store__name__iexact=v)
        total = qs.count()
        if not total:
            raise CommandError("No products found. Run import_products first (or check --store).")

        self.stdout.write(f"Checking {total} product(s)...")
        last = {"done": 0}

        def progress(done, seen):
            if done != last["done"]:
                last["done"] = done
                self.stdout.write(f"  checked {seen}/{total}, embedded so far: {done}")

        try:
            done = index_products(qs, force=opts["force"], on_batch=progress)
        except Exception as exc:
            raise CommandError(f"Stopped: {exc}. Is Ollama running and 'nomic-embed-text' pulled? "
                               "Run the command again; finished batches are kept.")
        self.stdout.write(self.style.SUCCESS(f"Done. {done} embedded, {total - done} unchanged."))