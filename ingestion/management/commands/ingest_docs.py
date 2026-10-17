from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from catalog.models import Store
from ingestion.pipeline import ingest_document

DEFAULT_DIR = Path(settings.BASE_DIR) / "catalog" / "data" / "knowledge"


class Command(BaseCommand):
    help = "Chunk, embed and store the .md/.txt knowledge files for one store."

    def add_arguments(self, parser):
        parser.add_argument("--store", help="Store id or name (needed if there are several stores)")
        parser.add_argument("--dir", default=str(DEFAULT_DIR), help="Folder with .md/.txt files")
        parser.add_argument("--force", action="store_true", help="Re-embed even if unchanged")

    def handle(self, *args, **opts):
        store = self._pick_store(opts["store"])
        folder = Path(opts["dir"])
        if not folder.is_dir():
            raise CommandError(f"Folder not found: {folder}")
        files = sorted(p for p in folder.iterdir() if p.suffix.lower() in (".md", ".txt"))
        if not files:
            raise CommandError(f"No .md or .txt files in {folder}")

        self.stdout.write(f"Store: {store.name} (id={store.id})")
        total, failed = 0, 0
        for path in files:
            text = path.read_text(encoding="utf-8-sig").strip()   # utf-8-sig also handles a BOM
            if not text:
                self.stdout.write(self.style.WARNING(f"  skipped (empty): {path.name}"))
                continue
            doc_type = "faq" if "faq" in path.stem.lower() else "policy"
            try:
                n = ingest_document(store, self._title(path, text), text,
                                    doc_type=doc_type, source=path.name, force=opts["force"])
            except Exception as exc:
                failed += 1
                self.stderr.write(self.style.ERROR(f"  FAILED {path.name}: {exc}"))
                continue
            if n:
                total += n
                self.stdout.write(self.style.SUCCESS(f"  {path.name}: {n} chunk(s) saved"))
            else:
                self.stdout.write(f"  {path.name}: unchanged, skipped")

        self.stdout.write(f"Done. {total} chunk(s) written.")
        if failed:
            raise CommandError(f"{failed} file(s) failed. Is Ollama running and 'nomic-embed-text' pulled?")

    @staticmethod
    def _title(path, text):
        first = text.splitlines()[0].strip()
        if first.startswith("#"):
            return first.lstrip("#").strip()[:200]
        return path.stem.replace("_", " ").replace("-", " ").title()

    @staticmethod
    def _pick_store(value):
        stores = Store.objects.order_by("id")
        if value:
            store = (stores.filter(id=int(value)) if value.isdigit()
                     else stores.filter(name__iexact=value)).first()
            if not store:
                raise CommandError(f"No store matches '{value}'.")
            return store
        if not stores.exists():
            raise CommandError("No store exists yet. Run import_products first.")
        if stores.count() == 1:
            return stores.first()
        listing = ", ".join(f"{s.id}={s.name}" for s in stores)
        raise CommandError(f"Several stores exist ({listing}). Pass --store <id or name>.")