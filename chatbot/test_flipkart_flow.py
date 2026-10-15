import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django
django.setup()

from catalog.models import Store
from chatbot.models import Conversation
from chatbot.services import run_chat_full

s = Store.objects.get(name="Flipkart")
convo = Conversation.objects.create(store=s, session_id="flipkart-live-test-suite-3")

turns = [
    "I'm looking for running shoes under 2000",
    "Show me only Nike shoes",
    "What about black ones?",
    "Is this product available?",
    "Explain the history of the French Revolution",
]

for t in turns:
    print(f"\n>>> USER: {t}")
    res = run_chat_full(s, convo, t)
    print(f"<<< BOT: {res['reply']}")
    if res.get("products"):
        print(f"    PRODUCTS: {[(p['name'], p['price'], p['currency']) for p in res['products']]}")
