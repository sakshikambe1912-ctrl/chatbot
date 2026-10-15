import secrets
from django.db import models

class Store(models.Model):
    name = models.CharField(max_length=200)
    website_url = models.URLField()
    api_key = models.CharField(max_length=64, unique=True, default=secrets.token_urlsafe)
    allowed_domain = models.CharField(max_length=200, blank=True)
    source_type = models.CharField(max_length=20, default="api",
        choices=[("api", "Store API"), ("feed", "CSV/JSON feed"), ("webhook", "Webhook"),
                ("dummyjson", "DummyJSON demo"), ("shopify", "Shopify"), ("flipkart", "Flipkart")])
    source_url = models.URLField(blank=True)
    currency = models.CharField(max_length=5, default="INR")

    def __str__(self):
        return self.name

class Product(models.Model):
    store = models.ForeignKey(Store, on_delete=models.CASCADE)
    external_id = models.CharField(max_length=100)
    name = models.CharField(max_length=300)
    description = models.TextField(blank=True)
    category = models.CharField(max_length=100, blank=True)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    in_stock = models.BooleanField(default=True)
    url = models.TextField(blank=True)
    image_url = models.TextField(blank=True)
    brand = models.CharField(max_length=100, blank=True, db_index=True)
    attributes = models.JSONField(default=dict, blank=True)
    embedding = models.JSONField(null=True, blank=True)
    embedded_text = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("store", "external_id")
        indexes = [
            models.Index(fields=["store", "price"]),
            models.Index(fields=["store", "category"]),
            models.Index(fields=["store", "brand"]),
        ]

    def __str__(self):
        return self.name

class PriceHistory(models.Model):
    product = models.ForeignKey(Product, on_delete=models.CASCADE)
    price = models.DecimalField(max_digits=12, decimal_places=2)
    recorded_at = models.DateTimeField(auto_now_add=True)