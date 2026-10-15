from django.contrib import admin
from .models import Store, Product, PriceHistory

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "price", "in_stock", "store")
    list_filter = ("category", "in_stock", "store")
    search_fields = ("name", "description")

admin.site.register(Store)
admin.site.register(PriceHistory)