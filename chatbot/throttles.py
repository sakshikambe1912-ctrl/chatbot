"""Rate limits for the chat endpoint. The limits come from settings.py (CHAT_RATE_IP,
CHAT_RATE_SESSION, like "30/min") and are read on every request."""
from django.conf import settings
from rest_framework.throttling import SimpleRateThrottle


class ChatIPThrottle(SimpleRateThrottle):
    """Limits one network address, however many session ids it makes up."""
    scope = "chat_ip"

    def get_rate(self):
        return getattr(settings, "CHAT_RATE_IP", "30/min")

    def get_cache_key(self, request, view):
        if request.method == "OPTIONS":                 # browser preflight: free
            return None
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class ChatSessionThrottle(SimpleRateThrottle):
    """Limits one chat session (one browser)."""
    scope = "chat_session"

    def get_rate(self):
        return getattr(settings, "CHAT_RATE_SESSION", "12/min")

    def get_cache_key(self, request, view):
        if request.method == "OPTIONS":
            return None
        session_id = str(request.data.get("session_id") or "").strip()[:64]
        if not session_id:
            return None                                 # the IP limit still applies
        return self.cache_format % {"scope": self.scope, "ident": session_id}
