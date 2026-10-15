from django.core.management.base import BaseCommand
from catalog.models import Store
from catalog.sync import sync_store


class Command(BaseCommand):
    help = "Sync products for one store"

    def add_arguments(self, parser):
        parser.add_argument("store_id", type=int)

    def handle(self, *args, **options):
        store = Store.objects.get(id=options["store_id"])
        self.stdout.write(self.style.SUCCESS(str(sync_store(store))))