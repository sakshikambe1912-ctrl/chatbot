from django.test import TestCase

# Create your tests here.
from django.test import TestCase
from rest_framework.test import APIClient
from catalog.models import Store


class StoreKeyAuthTests(TestCase):
    def setUp(self):
        self.store = Store.objects.create(name="T", website_url="https://t.test")
        self.client = APIClient()

    def test_missing_key_is_401(self):
        r = self.client.post("/api/chat/", {"message": "hi", "session_id": "s"}, format="json")
        self.assertEqual(r.status_code, 401)

    def test_wrong_key_is_401(self):
        r = self.client.get("/api/history/", {"session_id": "s"}, HTTP_X_STORE_KEY="bad")
        self.assertEqual(r.status_code, 401)

    def test_valid_key_is_200(self):
        r = self.client.get("/api/history/", {"session_id": "s"},
                            HTTP_X_STORE_KEY=self.store.api_key)
        self.assertEqual(r.status_code, 200)