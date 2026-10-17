from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIRequestFactory

from chatbot.views import chat

# 401 means the request got past the rate limit (it has no store key); 429 means it was blocked.
LIMITS = dict(CHAT_RATE_IP="3/min", CHAT_RATE_SESSION="2/min",
              REST_FRAMEWORK={"DEFAULT_AUTHENTICATION_CLASSES": [], "NUM_PROXIES": 0,
                              "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"]})


@override_settings(**LIMITS)
class ChatThrottleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = APIRequestFactory()

    def post(self, session, ip="1.1.1.1", **headers):
        request = self.factory.post("/api/chat/", {"message": "hi", "session_id": session},
                                    format="json", REMOTE_ADDR=ip, **headers)
        return chat(request).status_code

    def test_one_session_is_limited(self):
        self.assertEqual([self.post("s1") for _ in range(3)], [401, 401, 429])

    def test_other_sessions_are_not_affected(self):
        for _ in range(3):
            self.post("s1", ip="1.1.1.1")
        self.assertEqual(self.post("s2", ip="2.2.2.2"), 401)

    def test_one_address_is_limited_even_with_new_session_ids(self):
        self.assertEqual([self.post(f"s{i}") for i in range(4)], [401, 401, 401, 429])

    def test_forged_forwarded_header_does_not_get_around_the_limit(self):
        codes = [self.post(f"s{i}", HTTP_X_FORWARDED_FOR=f"9.9.9.{i}") for i in range(4)]
        self.assertEqual(codes[-1], 429)

    def test_preflight_requests_are_free(self):
        for _ in range(6):
            chat(self.factory.options("/api/chat/", REMOTE_ADDR="1.1.1.1"))
        self.assertEqual(self.post("s1"), 401)
