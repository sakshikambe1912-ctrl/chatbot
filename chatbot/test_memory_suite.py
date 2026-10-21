import json
from unittest import mock
from django.test import TestCase
from rest_framework.test import APIClient

from catalog.models import Store, Product
from chatbot.models import Conversation, Message
from chatbot import services as S
from chatbot import engine as E
from chatbot.services import (
    get_active_context, save_active_context,
    get_search_state, save_search_state,
    clear_active_memory, detect_product_switch,
    run_chat_full
)


class ConversationalMemoryTestSuite(TestCase):
    def setUp(self):
        self.store = Store.objects.create(name="Flipkart", currency="INR", website_url="https://www.flipkart.com")
        self.client = APIClient()

        # Create products for testing
        self.shoe1 = Product.objects.create(
            store=self.store,
            external_id="test_nike_1",
            name="Nike Air Max Running Shoes",
            price=2999.0,
            category="shoes",
            in_stock=True,
            description="Comfortable Nike running shoes with cushioned air sole. 6 months manufacturer warranty.",
            attributes={"brand": "Nike", "color": "blue", "warranty": "6 Months Manufacturer Warranty"}
        )
        self.shoe2 = Product.objects.create(
            store=self.store,
            external_id="test_campus_2",
            name="Campus Oxyfit Walking Shoes",
            price=1299.0,
            category="shoes",
            in_stock=True,
            description="Affordable Campus walking shoes with breathable mesh.",
            attributes={"brand": "Campus", "color": "black", "warranty": "3 Months Manufacturer Warranty"}
        )
        self.phone1 = Product.objects.create(
            store=self.store,
            external_id="test_samsung_3",
            name="Samsung Galaxy M34 5G",
            price=16999.0,
            category="phones",
            in_stock=True,
            description="Samsung 5G smartphone with 6000mAh battery and Super AMOLED display. 1 year device warranty.",
            attributes={"brand": "Samsung", "warranty": "1 Year Device Warranty"}
        )

    def test_three_types_of_memory_separation(self):
        """Active context, search state, and conversation history are managed separately."""
        convo = S.new_conversation(self.store, "user_browser_1")
        
        # 1. Conversation History
        Message.objects.create(conversation=convo, role="user", content="Show me Nike shoes")
        Message.objects.create(conversation=convo, role="assistant", content="Here are Nike shoes", cards=[self.shoe1.id])
        self.assertEqual(convo.messages.count(), 2)

        # 2. Active Context
        save_active_context(convo, {
            "product_id": self.shoe1.id,
            "product_name": self.shoe1.name,
            "topic": "shoes",
            "task": "browse",
            "comparison_ids": [self.shoe1.id]
        })
        act = get_active_context(convo)
        self.assertEqual(act["product_id"], self.shoe1.id)
        self.assertEqual(act["topic"], "shoes")

        # 3. Search State
        save_search_state(convo, {
            "keywords": "running shoes",
            "categories": ["shoes"],
            "brand": "Nike",
            "color": "blue",
            "min_price": 1000.0,
            "max_price": 5000.0,
            "sort": "relevance"
        }, ids=[self.shoe1.id])
        sst = get_search_state(convo)
        self.assertEqual(sst["brand"], "Nike")
        self.assertEqual(convo.last_shown_ids, [self.shoe1.id])

        # 4. Reset preserves conversation history while clearing active context & search state
        clear_active_memory(convo)
        self.assertEqual(convo.messages.count(), 2)  # History preserved!
        self.assertEqual(convo.last_shown_ids, [])   # Search results cleared!
        self.assertEqual(get_active_context(convo).get("product_id"), None)  # Focus cleared!
        self.assertFalse(get_search_state(convo).get("brand"))               # Filters cleared!

    def test_product_followup_warranty_resolves_product(self):
        """'What about its warranty?' resolves the active product and returns ONLY that product card."""
        convo = S.new_conversation(self.store, "user_browser_2")
        S.save_state(convo, {"focus": self.shoe1.id, "category": "shoes"}, [self.shoe1.id])
        S.save_active_context(convo, {"product_id": self.shoe1.id, "product_name": self.shoe1.name, "topic": "shoes", "task": "browse"})

        reply, cards = E.core(self.store, convo, "What about its warranty?")
        self.assertIn("Nike Air Max Running Shoes", reply)
        self.assertIn("warranty", reply.lower())
        # ONLY the referenced product is returned (no unnecessary products)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["id"], self.shoe1.id)

    def test_general_policy_vs_product_specific_return(self):
        """Distinguishes 'What is your return policy?' (general, no cards) from 'Can I return these shoes?' (product-specific, only that card)."""
        convo = S.new_conversation(self.store, "user_browser_3")
        S.save_state(convo, {"focus": self.shoe1.id, "category": "shoes"}, [self.shoe1.id])
        S.save_active_context(convo, {"product_id": self.shoe1.id, "product_name": self.shoe1.name, "topic": "shoes", "task": "browse"})

        # General policy inquiry
        gen_reply, gen_cards = E.core(self.store, convo, "What is your return policy?")
        self.assertIn("return", gen_reply.lower())
        self.assertEqual(gen_cards, [])  # No product cards for general store policy!

        # Product-specific inquiry
        prod_reply, prod_cards = E.core(self.store, convo, "Can I return these shoes?")
        self.assertIn("return", prod_reply.lower())
        self.assertIn("Nike Air Max Running Shoes", prod_reply)
        self.assertEqual(len(prod_cards), 1)
        self.assertEqual(prod_cards[0]["id"], self.shoe1.id)

    def test_which_one_is_cheaper(self):
        """'Which one is cheaper?' compares appropriate products and returns ONLY the cheaper product."""
        convo = S.new_conversation(self.store, "user_browser_4")
        shown = [self.shoe1, self.shoe2]  # Nike (2999) and Campus (1299)
        S.save_state(convo, {"category": "shoes"}, [p.id for p in shown])

        u = E.Understanding(intent="superlative", superlative_target="cheapest")
        reply, cards = E.handle_superlative(self.store, convo, S.get_state(convo), shown, "Which one is cheaper?", u)

        self.assertIn("Campus Oxyfit Walking Shoes", reply)
        self.assertIn("cheaper", reply.lower())
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["id"], self.shoe2.id)

    def test_correction_replaces_conflicting_brand(self):
        """'No, I meant Campus shoes' corrects the active brand from Nike to Campus while keeping category shoes."""
        convo = S.new_conversation(self.store, "user_browser_5")
        st = {"category": "shoes", "brand": "Nike", "keywords": "running shoes"}
        S.save_state(convo, st, [self.shoe1.id])

        reply, cards = S.handle_correction(self.store, convo, st, "No, I meant Campus shoes", [self.shoe1])
        self.assertTrue(len(cards) >= 1)
        # All returned cards must be Campus
        for card in cards:
            self.assertEqual(card.get("brand"), "Campus")

    def test_product_switch_starts_new_conversation(self):
        """When user switches from shoes to Samsung phones, detect_product_switch triggers a new conversation/tab."""
        convo = S.new_conversation(self.store, "user_browser_6")
        Message.objects.create(conversation=convo, role="user", content="Show me Nike shoes")
        Message.objects.create(conversation=convo, role="assistant", content="Here are Nike shoes", cards=[self.shoe1.id])
        S.save_state(convo, {"category": "shoes", "brand": "Nike", "query": "shoes"}, [self.shoe1.id])
        S.save_active_context(convo, {"product_id": self.shoe1.id, "product_name": self.shoe1.name, "topic": "shoes", "task": "browse"})

        # Follow-ups should NOT switch
        self.assertFalse(detect_product_switch(self.store, convo, "What about its warranty?"))
        self.assertFalse(detect_product_switch(self.store, convo, "Show me cheaper ones"))
        self.assertFalse(detect_product_switch(self.store, convo, "Can I return these shoes?"))
        self.assertFalse(detect_product_switch(self.store, convo, "No, I meant Campus shoes"))
        self.assertFalse(detect_product_switch(self.store, convo, "What is your return policy?"))

        # Switching to a different product family (shoes -> phones) DOES switch!
        self.assertTrue(detect_product_switch(self.store, convo, "Show me Samsung phones"))

    def test_chat_api_product_switch_returns_new_conversation_flag(self):
        """POST /api/chat/ automatically forks to a new conversation when user talks about other product."""
        # 1. Start initial conversation on Nike shoes
        r1 = self.client.post("/api/chat/", {
            "message": "Show me Nike shoes",
            "session_id": "client_session_xyz"
        }, format="json", HTTP_X_STORE_KEY=self.store.api_key)
        self.assertEqual(r1.status_code, 200)
        c1_id = r1.data["conversation_id"]

        # 2. Follow-up query continues in the SAME conversation on one page
        r2 = self.client.post("/api/chat/", {
            "message": "What about its warranty?",
            "session_id": "client_session_xyz",
            "conversation_id": c1_id
        }, format="json", HTTP_X_STORE_KEY=self.store.api_key)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.data["conversation_id"], c1_id)
        self.assertFalse(r2.data.get("new_conversation", False))

        # 3. Switching to a different product (phones) creates a NEW conversation
        r3 = self.client.post("/api/chat/", {
            "message": "Show me Samsung phones",
            "session_id": "client_session_xyz",
            "conversation_id": c1_id
        }, format="json", HTTP_X_STORE_KEY=self.store.api_key)
        self.assertEqual(r3.status_code, 200)
        c3_id = r3.data["conversation_id"]
        self.assertNotEqual(c3_id, c1_id)
        self.assertTrue(r3.data.get("new_conversation", False))
        self.assertEqual(r3.data.get("previous_conversation_id"), c1_id)

        # 4. Earlier conversation is safely preserved in history and listed in sidebar
        hist_r = self.client.get("/api/conversations/", {
            "session_id": "client_session_xyz"
        }, HTTP_X_STORE_KEY=self.store.api_key)
        self.assertEqual(hist_r.status_code, 200)
        conv_ids = [c["id"] for c in hist_r.data["conversations"]]
        self.assertIn(c1_id, conv_ids)
        self.assertIn(c3_id, conv_ids)

    def test_store_isolation(self):
        """Conversations and state do not leak between different stores."""
        store2 = Store.objects.create(name="OtherStore", currency="USD", website_url="https://other.test")
        convo1 = S.new_conversation(self.store, "shared_client_id")
        convo2 = S.new_conversation(store2, "shared_client_id")

        S.save_state(convo1, {"brand": "Nike"}, [self.shoe1.id])
        S.save_state(convo2, {"brand": "Samsung"}, [self.phone1.id])

        self.assertEqual(S.get_state(convo1)["brand"], "Nike")
        self.assertEqual(S.get_state(convo2)["brand"], "Samsung")
        self.assertEqual(convo1.last_shown_ids, [self.shoe1.id])
        self.assertEqual(convo2.last_shown_ids, [self.phone1.id])
