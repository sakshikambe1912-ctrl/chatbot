import json
import os
import re
import urllib.parse
from decimal import Decimal, InvalidOperation
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from catalog.models import Store, Product, PriceHistory

KNOWN_BRANDS = [
    'nike', 'adidas', 'puma', 'reebok', 'sparx', 'campus', 'asian', 'bata', 'woodland',
    'skechers', 'asics', 'apple', 'samsung', 'sony', 'oneplus', 'xiaomi', 'redmi',
    'realme', 'vivo', 'oppo', 'motorola', 'google', 'dell', 'hp', 'lenovo', 'asus',
    'acer', 'boat', 'noise', 'fastrack', 'titan', 'casio', 'lg', 'whirlpool', 'philips',
    'havells', 'bajaj', 'crocs', 'fossil', 'nothing', 'poco', 'infinix'
]


def extract_brand(name, raw_brand=""):
    if raw_brand and str(raw_brand).strip():
        return str(raw_brand).strip().title()
    n = (name or "").lower()
    for b in KNOWN_BRANDS:
        if re.search(rf"\b{re.escape(b)}\b", n):
            return b.title()
    # Fallback to first capitalized word if it looks like a brand
    words = (name or "").split()
    if words and len(words[0]) > 2 and words[0].isalpha():
        return words[0].title()
    return ""


def clean_url(url, name=""):
    u = (url or "").strip()
    if not u or not u.startswith("http"):
        return f"https://www.flipkart.com/search?q={urllib.parse.quote_plus(name)}"
    # If it is a Flipkart URL, preserve canonical path and pid
    if "flipkart.com" in u:
        parsed = urllib.parse.urlparse(u)
        qparams = urllib.parse.parse_qs(parsed.query)
        pid = qparams.get("pid", [None])[0]
        if "/p/" in parsed.path:
            clean_path = parsed.path
            return f"https://www.flipkart.com{clean_path}?pid={pid}" if pid else f"https://www.flipkart.com{clean_path}"
    return u


class Command(BaseCommand):
    help = "Import or update products from a JSON file into PostgreSQL."

    def add_arguments(self, parser):
        parser.add_argument("--file", type=str, required=True, help="Path to the JSON file")
        parser.add_argument("--source", type=str, default="flipkart", help="Source type or store name (default: flipkart)")
        parser.add_argument("--store-id", type=int, help="Optional specific Store ID")

    def handle(self, *args, **options):
        file_path = options["file"]
        source = (options["source"] or "flipkart").strip().lower()
        store_id = options.get("store_id")

        if not os.path.exists(file_path):
            raise CommandError(f"File not found: {file_path}")

        # Resolve or create Store
        store = None
        if store_id:
            store = Store.objects.filter(id=store_id).first()
            if not store:
                raise CommandError(f"Store with id {store_id} not found.")
        else:
            store = (Store.objects.filter(source_type=source).first() or
                     Store.objects.filter(name__iexact=source).first())
            if not store:
                store = Store.objects.create(
                    name=source.title(),
                    website_url=f"https://www.{source}.com" if "." not in source else f"https://{source}",
                    source_type=source,
                    currency="INR",
                    allowed_domain=f"{source}.com" if "." not in source else source
                )
                self.stdout.write(self.style.SUCCESS(f"Created new store '{store.name}' (ID: {store.id})"))

        self.stdout.write(self.style.NOTICE(f"Loading data from '{file_path}' into store '{store.name}' (ID: {store.id})..."))

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
        except Exception as e:
            raise CommandError(f"Failed to read JSON file: {e}")

        # Extract product list
        if isinstance(raw_data, list):
            items = raw_data
        elif isinstance(raw_data, dict):
            items = raw_data.get("products") or raw_data.get("data") or raw_data.get("items") or []
            if not items and "name" in raw_data and "price" in raw_data:
                items = [raw_data]
        else:
            raise CommandError("Invalid JSON structure: expected list or object with 'products' list.")

        total_processed = 0
        created_count = 0
        updated_count = 0
        skipped_count = 0
        rejected_count = 0
        rejections = []

        with transaction.atomic():
            for idx, item in enumerate(items, 1):
                total_processed += 1
                if not isinstance(item, dict):
                    rejected_count += 1
                    rejections.append(f"Row {idx}: item is not an object")
                    continue

                name = str(item.get("name") or "").strip()
                if not name:
                    rejected_count += 1
                    rejections.append(f"Row {idx}: missing required field 'name'")
                    continue

                raw_price = item.get("price")
                if raw_price is None or raw_price == "":
                    rejected_count += 1
                    rejections.append(f"Row {idx} ('{name[:30]}'): missing required field 'price'")
                    continue

                try:
                    price_cleaned = re.sub(r"[^\d.]", "", str(raw_price))
                    price = Decimal(price_cleaned)
                    if price < 0:
                        raise ValueError("Price cannot be negative")
                except (InvalidOperation, ValueError, TypeError) as pe:
                    rejected_count += 1
                    rejections.append(f"Row {idx} ('{name[:30]}'): invalid price '{raw_price}' ({pe})")
                    continue

                ext_id = str(item.get("external_id") or item.get("id") or item.get("pid") or "").strip()
                if not ext_id:
                    # Deterministic hash/slug fallback
                    ext_id = f"gen-{abs(hash((name, str(price)))) % 10000000:07d}"

                category = str(item.get("category") or "General").strip().title()
                brand = extract_brand(name, item.get("brand"))
                description = str(item.get("description") or "").strip()
                in_stock = bool(item.get("in_stock", True))
                url = clean_url(item.get("url"), name)
                image_url = str(item.get("image_url") or item.get("image") or "").strip()

                # Extract extra attributes
                std_keys = {"name", "price", "external_id", "id", "pid", "category",
                            "brand", "description", "in_stock", "url", "image_url", "image"}
                attributes = {k: v for k, v in item.items() if k not in std_keys}
                if brand:
                    attributes.setdefault("brand", brand)

                # Check if product exists in database for this store
                product = Product.objects.filter(store=store, external_id=ext_id).first()
                if product:
                    # Check if any field changed
                    changed = False
                    if product.name != name:
                        product.name = name; changed = True
                    if product.price != price:
                        old_price = product.price
                        product.price = price; changed = True
                        PriceHistory.objects.create(product=product, price=price)
                    if product.category != category:
                        product.category = category; changed = True
                    if product.brand != brand:
                        product.brand = brand; changed = True
                    if product.description != description:
                        product.description = description; changed = True
                    if product.in_stock != in_stock:
                        product.in_stock = in_stock; changed = True
                    if product.url != url:
                        product.url = url; changed = True
                    if product.image_url != image_url:
                        product.image_url = image_url; changed = True
                    if product.attributes != attributes:
                        product.attributes = attributes; changed = True

                    if changed:
                        product.save()
                        updated_count += 1
                    else:
                        skipped_count += 1
                else:
                    new_prod = Product.objects.create(
                        store=store,
                        external_id=ext_id,
                        name=name,
                        brand=brand,
                        category=category,
                        price=price,
                        description=description,
                        in_stock=in_stock,
                        url=url,
                        image_url=image_url,
                        attributes=attributes
                    )
                    PriceHistory.objects.create(product=new_prod, price=price)
                    created_count += 1

        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(self.style.SUCCESS("PRODUCT INGESTION COMPLETED"))
        self.stdout.write(self.style.SUCCESS("=" * 60))
        self.stdout.write(f"Store:          {store.name} (ID: {store.id})")
        self.stdout.write(f"Total rows:     {total_processed}")
        self.stdout.write(self.style.SUCCESS(f"Created:        {created_count}"))
        self.stdout.write(self.style.SUCCESS(f"Updated:        {updated_count}"))
        self.stdout.write(self.style.NOTICE(f"Skipped (same): {skipped_count}"))
        if rejected_count:
            self.stdout.write(self.style.ERROR(f"Rejected:       {rejected_count}"))
            for r in rejections[:5]:
                self.stdout.write(self.style.WARNING(f"  - {r}"))
            if len(rejections) > 5:
                self.stdout.write(self.style.WARNING(f"  ... and {len(rejections) - 5} more."))
        else:
            self.stdout.write(f"Rejected:       0")
        self.stdout.write(self.style.SUCCESS("=" * 60))
