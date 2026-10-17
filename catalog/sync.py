from decimal import Decimal
from .models import Product, PriceHistory
from .connectors import CONNECTORS
from .shopify_connector import ShopifyConnector

CONNECTORS["shopify"] = ShopifyConnector


def sync_store(store):
    items = CONNECTORS[store.source_type](store).fetch()
    created_count = price_changes = 0

    for item in items:
        item["price"] = Decimal(str(item["price"]))
        product, created = Product.objects.get_or_create(
            store=store, external_id=item["external_id"], defaults=item)

        if created:
            created_count += 1
            PriceHistory.objects.create(product=product, price=product.price)
            continue

        price_changed = product.price != item["price"]
        for field, value in item.items():
            setattr(product, field, value)
        product.save()

        if price_changed:
            price_changes += 1
            PriceHistory.objects.create(product=product, price=product.price)
    try:
        from ingestion.products import index_products
        index_products(Product.objects.filter(store=store))
    except Exception as exc:
        print("EMBEDDING SKIPPED:", exc)

    return {"fetched": len(items), "new": created_count, "price_changes": price_changes}