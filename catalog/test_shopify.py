from decimal import Decimal
from unittest import mock

from django.test import TestCase

from catalog import shopify_connector as sc
from catalog.models import PriceHistory, Product, Store
from catalog.sync import sync_store


def make_product(pid, title, price="999.00", available=True, **extra):
    """Shaped like an entry of a Shopify /products.json response."""
    p = {
        "id": pid, "title": title, "handle": title.lower().replace(" ", "-"),
        "body_html": "<p>Soft &amp; warm <b>cotton</b> hoodie.</p>",
        "vendor": "Acme", "product_type": "Hoodies",
        "variants": [{"price": price, "available": available}],
        "images": [{"src": "https://cdn.shopify.com/s/files/1/img.jpg?v=1"}],
        "image": {"src": "https://cdn.shopify.com/s/files/1/img.jpg?v=1"},
    }
    p.update(extra)
    return p


class ShopifyMappingTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="Shop", website_url="https://www.myshop.com",
            source_type="shopify", source_url="https://myshop.myshopify.com/products.json?limit=250",
            currency="INR")

    def fetch(self, *pages):
        with mock.patch.object(sc, "_get_json", side_effect=[{"products": p} for p in pages]) as m:
            items = sc.ShopifyConnector(self.store).fetch()
        return items, m

    def test_fields_match_the_product_model(self):
        items, _ = self.fetch([make_product(1, "Cotton Hoodie", "1499.50")])
        item = items[0]
        self.assertEqual(set(item), {"external_id", "name", "description", "category",
                                     "price", "in_stock", "url", "image_url"})
        self.assertEqual(item["external_id"], "1")
        self.assertEqual(item["price"], Decimal("1499.50"))
        self.assertEqual(item["category"], "Hoodies")
        self.assertTrue(item["in_stock"])
        self.assertEqual(item["url"], "https://www.myshop.com/products/cotton-hoodie")  # website URL, not myshopify
        self.assertEqual(item["description"], "Brand: Acme. Soft & warm cotton hoodie.")   # html removed
        self.assertTrue(item["image_url"].startswith("https://cdn.shopify.com"))

    def test_stock_and_price_use_available_variants(self):
        p = make_product(2, "Tee")
        p["variants"] = [{"price": "300.00", "available": False},
                         {"price": "500.00", "available": True},
                         {"price": "450.00", "available": True}]
        items, _ = self.fetch([p])
        self.assertTrue(items[0]["in_stock"])
        self.assertEqual(items[0]["price"], Decimal("450.00"))   # cheapest variant you can buy

    def test_all_variants_sold_out(self):
        p = make_product(3, "Cap", "200.00", available=False)
        items, _ = self.fetch([p])
        self.assertFalse(items[0]["in_stock"])
        self.assertEqual(items[0]["price"], Decimal("200.00"))

    def test_missing_type_image_and_junk_products(self):
        p = make_product(4, "Plain", product_type="", images=[], image=None)
        junk = [{"id": 5, "title": "No variants", "variants": []}, {"title": "No id"}]
        items, _ = self.fetch([p] + junk)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["category"], "Other")
        self.assertEqual(items[0]["image_url"], "")

    def test_long_image_url_is_shortened_to_fit_the_database(self):
        p = make_product(6, "Long", image={"src": "https://cdn.shopify.com/" + "a" * 190 + "?v=" + "1" * 50})
        items, _ = self.fetch([p])
        self.assertLessEqual(len(items[0]["image_url"]), 200)

    def test_pagination_reads_every_page(self):
        page1 = [make_product(i, f"Item {i}") for i in range(1, sc.PAGE_SIZE + 1)]
        page2 = [make_product(1000 + i, f"Other {i}") for i in range(10)]
        items, m = self.fetch(page1, page2)
        self.assertEqual(len(items), sc.PAGE_SIZE + 10)
        self.assertEqual(m.call_count, 2)
        self.assertIn("page=2", m.call_args_list[1].args[0])

    def test_shop_that_ignores_paging_does_not_loop(self):
        page = [make_product(i, f"Item {i}") for i in range(1, sc.PAGE_SIZE + 1)]
        items, m = self.fetch(page, page, page)          # the same full page every time
        self.assertEqual(len(items), sc.PAGE_SIZE)
        self.assertEqual(m.call_count, 2)

    def test_bad_source_url_gives_a_clear_error(self):
        self.store.source_url = "not a url"
        with self.assertRaises(sc.ShopifyError):
            sc.ShopifyConnector(self.store).fetch()

    def test_html_instead_of_json_gives_a_clear_error(self):
        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"<html>Password page</html>"
        with mock.patch("urllib.request.urlopen", return_value=Resp()):
            with self.assertRaises(sc.ShopifyError) as ctx:
                sc.ShopifyConnector(self.store).fetch()
        self.assertIn("did not return JSON", str(ctx.exception))

    def test_response_without_products_field_gives_a_clear_error(self):
        with mock.patch.object(sc, "_get_json", return_value={"oops": []}):
            with self.assertRaises(sc.ShopifyError):
                sc.ShopifyConnector(self.store).fetch()


class ShopifySyncTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(
            name="Shop", website_url="https://www.myshop.com", source_type="shopify",
            source_url="https://myshop.myshopify.com/products.json", currency="INR")
        patcher = mock.patch("catalog.embeddings.embed_products")   # Ollama is not needed here
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_sync(self, products):
        with mock.patch.object(sc, "_get_json", return_value={"products": products}):
            return sync_store(self.store)

    def test_sync_store_loads_products_and_tracks_price_changes(self):
        result = self.run_sync([make_product(1, "Cotton Hoodie", "1000"),
                                make_product(2, "Wool Cap", "400", available=False)])
        self.assertEqual(result, {"fetched": 2, "new": 2, "price_changes": 0})
        self.assertEqual(Product.objects.filter(store=self.store).count(), 2)
        hoodie = Product.objects.get(store=self.store, external_id="1")
        self.assertEqual(hoodie.price, Decimal("1000.00"))
        self.assertFalse(Product.objects.get(external_id="2").in_stock)

        # second run: one price changed, nothing duplicated
        result = self.run_sync([make_product(1, "Cotton Hoodie", "899"),
                                make_product(2, "Wool Cap", "400", available=True)])
        self.assertEqual(result, {"fetched": 2, "new": 0, "price_changes": 1})
        self.assertEqual(Product.objects.filter(store=self.store).count(), 2)
        hoodie.refresh_from_db()
        self.assertEqual(hoodie.price, Decimal("899.00"))
        self.assertEqual(PriceHistory.objects.filter(product=hoodie).count(), 2)
        self.assertTrue(Product.objects.get(external_id="2").in_stock)       # stock refreshed

    def test_other_stores_products_are_untouched(self):
        other = Store.objects.create(name="Other", website_url="https://o.test", source_type="dummyjson")
        Product.objects.create(store=other, external_id="1", name="Mine", price=Decimal("5"))
        self.run_sync([make_product(1, "Cotton Hoodie", "1000")])
        self.assertEqual(Product.objects.get(store=other, external_id="1").name, "Mine")