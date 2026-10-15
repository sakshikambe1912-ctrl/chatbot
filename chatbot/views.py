import traceback
from urllib.parse import urlparse
from django.views.decorators.csrf import csrf_exempt
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import AllowAny
from catalog.models import Store
from .services import (get_conversation, run_chat_full, resolve_conversation, new_conversation,
                       list_conversations, history_payload, cached_response)


def _store_from_request(request):
    """Same store lookup as before (with the same fallback)."""
    raw_key = request.headers.get("X-Store-Key", "").strip()
    store = Store.objects.filter(api_key=raw_key).first()
    if not store:
        # Fallback to Flipkart store if key not provided or invalid
        store = Store.objects.filter(source_type="flipkart").first() or Store.objects.first()
    return store


def _clip(value, n=64):
    return str(value or "").strip()[:n]


@csrf_exempt
@api_view(["POST", "OPTIONS"])
@authentication_classes([])
@permission_classes([AllowAny])
def chat(request):
    if request.method == "OPTIONS":
        return Response({})

    store = _store_from_request(request)
    if not store:
        return Response({"error": "No store configured"}, status=500)

    message = str(request.data.get("message", "")).strip()[:500]
    session_id = _clip(request.data.get("session_id"))
    conversation_id = _clip(request.data.get("conversation_id")) or None
    request_id = _clip(request.data.get("request_id")) or None
    print(f"[CHAT API] Received: '{message}' (session: {session_id}, "
          f"conversation: {conversation_id}, store: {store.name})")
    if not message or not session_id:
        return Response({"error": "message and session_id are required"}, status=400)

    # session_id identifies the browser. conversation_id picks which chat of that browser.
    convo = resolve_conversation(store, session_id, conversation_id)
    if convo is None and conversation_id:
        convo = resolve_conversation(store, session_id)      # unknown id: use the latest chat
    if convo is None:
        convo = get_conversation(store, session_id)          # old behaviour as a last resort

    # A retried request (same request_id) gets the saved answer instead of a second one.
    cached = cached_response(convo, request_id)
    if cached:
        return Response(cached)

    try:
        return Response(run_chat_full(store, convo, message, request_id=request_id))
    except Exception:
        traceback.print_exc()
        return Response({"error": "The assistant is unavailable. Please try again."},
                        status=503)


@csrf_exempt
@api_view(["GET", "OPTIONS"])
@authentication_classes([])
@permission_classes([AllowAny])
def history(request):
    """Messages (with product cards) of one conversation, so a refresh restores the chat."""
    if request.method == "OPTIONS":
        return Response({})
    store = _store_from_request(request)
    if not store:
        return Response({"error": "No store configured"}, status=500)

    client_id = _clip(request.GET.get("session_id"))
    conversation_id = _clip(request.GET.get("conversation_id")) or None
    if not client_id:
        return Response({"error": "session_id is required"}, status=400)

    convo = resolve_conversation(store, client_id, conversation_id, create=False)
    if convo is None and conversation_id:
        convo = resolve_conversation(store, client_id, None, create=False)
    if convo is None:
        return Response({"conversation_id": None, "messages": []})
    return Response({"conversation_id": convo.session_id,
                     "messages": history_payload(store, convo)})


@csrf_exempt
@api_view(["GET", "OPTIONS"])
@authentication_classes([])
@permission_classes([AllowAny])
def conversations(request):
    """The list of this browser's past chats, newest first (for the History panel)."""
    if request.method == "OPTIONS":
        return Response({})
    store = _store_from_request(request)
    if not store:
        return Response({"error": "No store configured"}, status=500)
    client_id = _clip(request.GET.get("session_id"))
    if not client_id:
        return Response({"error": "session_id is required"}, status=400)
    return Response({"conversations": list_conversations(store, client_id)})


@csrf_exempt
@api_view(["POST", "OPTIONS"])
@authentication_classes([])
@permission_classes([AllowAny])
def conversation_new(request):
    """Start a separate conversation (the New chat button)."""
    if request.method == "OPTIONS":
        return Response({})
    store = _store_from_request(request)
    if not store:
        return Response({"error": "No store configured"}, status=500)
    client_id = _clip(request.data.get("session_id"))
    if not client_id:
        return Response({"error": "session_id is required"}, status=400)
    convo = new_conversation(store, client_id)
    return Response({"conversation_id": convo.session_id})


@csrf_exempt
@api_view(["POST", "OPTIONS"])
@authentication_classes([])
@permission_classes([AllowAny])
def conversation_reset(request):
    """Reset the active shopping context (filters, seen products) while preserving visible chat messages."""
    if request.method == "OPTIONS":
        return Response({})
    store = _store_from_request(request)
    if not store:
        return Response({"error": "No store configured"}, status=500)
    client_id = _clip(request.data.get("session_id"))
    conversation_id = _clip(request.data.get("conversation_id")) or None
    if not client_id:
        return Response({"error": "session_id is required"}, status=400)
    convo = resolve_conversation(store, client_id, conversation_id, create=False)
    if not convo and conversation_id:
        convo = resolve_conversation(store, client_id, None, create=False)
    if not convo:
        return Response({"error": "Conversation not found"}, status=404)
    from .services import save_state
    save_state(convo, {}, [])
    return Response({
        "ok": True,
        "conversation_id": convo.session_id,
        "message": "Shopping search context cleared. Message history preserved."
    })



@csrf_exempt
@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def live_search(request):
    """Real-time product search endpoint that queries live Flipkart with strict relevance filtering."""
    from catalog.connectors import FlipkartConnector
    from .services import is_product_relevant
    query = request.GET.get("q", "").strip()
    if not query:
        return Response({"products": []})
    try:
        items = FlipkartConnector.search_live(query, limit=25)
        filtered = [
            it for it in items
            if is_product_relevant(it["name"], it["price"], q=query)
        ]
        return Response({"products": filtered or items[:12], "query": query})
    except Exception as e:
        print("[LIVE SEARCH ERROR]:", e)
        return Response({"products": [], "error": str(e)}, status=500)


def flipkart_demo(request):
    import os
    from django.http import HttpResponse
    from django.conf import settings
    path = os.path.join(settings.BASE_DIR, "..", "test-site", "index.html")
    if not os.path.exists(path):
        path = r"C:\Users\kambe\test-site\index.html"
    try:
        with open(path, "r", encoding="utf-8") as f:
            html = f.read()
        return HttpResponse(html, content_type="text/html; charset=utf-8")
    except Exception as e:
        return HttpResponse(f"<h1>Error loading Flipkart test page</h1><p>{e}</p>", status=500)