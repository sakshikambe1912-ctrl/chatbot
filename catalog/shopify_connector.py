"""
Reads a Shopify store's public product list (<shop>/products.json) and returns
dictionaries whose keys are the Product model's fields, which is exactly what
catalog/sync.py expects from every connector's fetch().
"""
import html
import json
import re
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

PAGE_SIZE = 250      # Shopify's maximum per page
MAX_PAGES = 40       # safety stop (10,000 products)
TIMEOUT = 30         # seconds per request


class ShopifyError(Exception):
    """A readable message for the terminal. Never contains secrets."""


def _get_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; StoreChatbotSync/1.0)",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise ShopifyError(
            f"Shopify answered HTTP {exc.code} for {url}. The shop may have its public "
            "product list turned off or be password protected.")
    except urllib.error.URLError as exc:
        raise ShopifyError(f"Could not reach {url}: {exc.reason}")
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ShopifyError(
            f"{url} did not return JSON. The shop may be password protected, or "
            "products.json may be turned off.")


def _clean_text(raw):
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    return re.sub(r"\s+", " ", text).strip()


def _short_url(url, limit=200):
    """Product.url / image_url are limited to 200 characters in the database."""
    url = url or ""
    if len(url) > limit:
        url = url.split("?")[0]
    return url if len(url) <= limit else ""


def _to_item(p, base):
    """One Shopify product -> one Product-shaped dict (or None if it has no usable data)."""
    if not p.get("id") or not p.get("title"):
        return None

    options = []                       # (price, available) for every variant
    for v in p.get("variants") or []:
        try:
            options.append((Decimal(str(v.get("price"))), bool(v.get("available", True))))
        except (InvalidOperation, TypeError):
            continue
    if not options:
        return None

    in_stock = any(avail for _, avail in options)
    pool = [price for price, avail in options if avail] or [price for price, _ in options]
    price = min(pool)

    vendor = _clean_text(p.get("vendor"))
    body = _clean_text(p.get("body_html"))
    description = (f"Brand: {vendor}. " if vendor else "") + body

    image = (p.get("image") or {}).get("src") or ""
    if not image and p.get("images"):
        image = (p["images"][0] or {}).get("src") or ""

    handle = p.get("handle") or ""
    return {
        "external_id": str(p["id"]),
        "name": _clean_text(p["title"])[:300],
        "description": description[:2000],
        "category": (_clean_text(p.get("product_type")) or "Other")[:100],
        "price": price,
        "in_stock": in_stock,
        "url": _short_url(f"{base}/products/{handle}") if handle else "",
        "image_url": _short_url(image),
    }


class ShopifyConnector:
    def __init__(self, store):
        self.store = store

    def _endpoint(self):
        src = urlparse(self.store.source_url or "")
        if src.scheme not in ("http", "https") or not src.netloc:
            raise ShopifyError(
                "Source URL must look like https://your-shop.myshopify.com/products.json")
        path = src.path if src.path.endswith("/products.json") else "/products.json"
        return f"{src.scheme}://{src.netloc}{path}"

    def _site_base(self):
        """Customer-facing address used for product links (website URL, else the source host)."""
        site = urlparse(self.store.website_url or "")
        if site.scheme in ("http", "https") and site.netloc:
            return f"{site.scheme}://{site.netloc}"
        src = urlparse(self.store.source_url)
        return f"{src.scheme}://{src.netloc}"

    def fetch(self):
        endpoint, base = self._endpoint(), self._site_base()
        items, seen = [], set()
        for page in range(1, MAX_PAGES + 1):
            data = _get_json(f"{endpoint}?limit={PAGE_SIZE}&page={page}")
            products = data.get("products") if isinstance(data, dict) else None
            if not isinstance(products, list):
                raise ShopifyError("The response has no product list (expected a 'products' field).")
            added = 0
            for p in products:
                item = _to_item(p, base)
                if item and item["external_id"] not in seen:
                    seen.add(item["external_id"])
                    items.append(item)
                    added += 1
            if len(products) < PAGE_SIZE or added == 0:   # last page, or the shop ignores paging
                break
        return items