from types import SimpleNamespace

from django.test import SimpleTestCase

from ingestion.products import _attr_text, product_text


def product(**kw):
    base = dict(name="Nike Air", brand="Nike", category="Shoes", description="Light.", attributes={})
    base.update(kw)
    return SimpleNamespace(**base)


class ProductTextTests(SimpleTestCase):
    def test_includes_brand_category_and_attributes(self):
        p = product(attributes={"color": "Black", "tags": ["sport", "run"], "id": 5, "sku": "X1"})
        self.assertEqual(product_text(p),
                         "Nike Air. Brand: Nike. Category: Shoes. Light. Color: Black. Tags: sport, run.")

    def test_missing_brand_is_left_out(self):
        self.assertEqual(product_text(product(brand=None)), "Nike Air. Category: Shoes. Light.")

    def test_long_text_is_cut(self):
        self.assertEqual(len(product_text(product(description="x" * 5000))), 1500)

    def test_attr_text_skips_noise(self):
        self.assertEqual(_attr_text({"url": "http://a", "meta": {"a": 1}, "empty": "", "material_type": "Mesh"}),
                         "Material Type: Mesh")
        self.assertEqual(_attr_text(None), "")
