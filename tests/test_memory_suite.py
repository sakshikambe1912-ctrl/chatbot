import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests
import time
import json
import uuid

BASE_URL = "http://127.0.0.1:8000"
STORE_KEY = "Lref8GVYiVxxZ4wehiudiZRlAcI5myX-QG48E4K33V0"

def send_chat(session_id, conversation_id, message):
    res = requests.post(
        f"{BASE_URL}/api/chat/",
        headers={"Content-Type": "application/json", "X-Store-Key": STORE_KEY},
        json={
            "session_id": session_id,
            "conversation_id": conversation_id,
            "message": message,
            "request_id": f"req_{uuid.uuid4().hex[:12]}"
        },
        timeout=120
    )
    if res.status_code != 200:
        raise Exception(f"Chat failed with status {res.status_code}: {res.text}")
    return res.json()

def get_history(session_id, conversation_id):
    res = requests.get(
        f"{BASE_URL}/api/history/",
        headers={"X-Store-Key": STORE_KEY},
        params={"session_id": session_id, "conversation_id": conversation_id},
        timeout=30
    )
    return res.json()

def reset_search(session_id, conversation_id):
    res = requests.post(
        f"{BASE_URL}/api/conversations/reset/",
        headers={"Content-Type": "application/json", "X-Store-Key": STORE_KEY},
        json={"session_id": session_id, "conversation_id": conversation_id},
        timeout=30
    )
    return res.json()

def new_chat(session_id):
    res = requests.post(
        f"{BASE_URL}/api/conversations/new/",
        headers={"Content-Type": "application/json", "X-Store-Key": STORE_KEY},
        json={"session_id": session_id},
        timeout=30
    )
    return res.json()

def run_tests():
    print("=" * 70)
    print("RUNNING CONVERSATIONAL MEMORY & ANTI-LEAKAGE VERIFICATION SUITE")
    print("=" * 70)

    client_id = f"test_client_{uuid.uuid4().hex[:8]}"

    # Start a fresh conversation
    nchat = new_chat(client_id)
    cid = nchat["conversation_id"]
    print(f"Created conversation: {cid} for client: {client_id}\n")

    # -------------------------------------------------------------
    # CASE 1: Initial Search ("Find Nike running shoes under 3000")
    # -------------------------------------------------------------
    print("[1] Test Initial Search: 'Find Nike running shoes under 3000'")
    r1 = send_chat(client_id, cid, "Find Nike running shoes under 3000")
    prods1 = r1.get("products", [])
    print(f"    Reply: {r1.get('reply')[:120]}...")
    print(f"    Products found: {len(prods1)}")
    for p in prods1[:3]:
        print(f"      - {p['name']} (₹{p['price']})")
    assert len(prods1) > 0, "Expected at least 1 Nike shoe under 3000"
    for p in prods1:
        assert "nike" in p["name"].lower(), f"Expected Nike brand in product {p['name']}"
        assert p["price"] <= 3000, f"Expected price <= 3000, got {p['price']}"
    print("    PASSED!\n")

    # -------------------------------------------------------------
    # CASE 2: Follow-up Filter Addition ("Show me black ones")
    # -------------------------------------------------------------
    print("[2] Test Follow-up Filter: 'Show me black ones'")
    r2 = send_chat(client_id, cid, "Show me black ones")
    prods2 = r2.get("products", [])
    print(f"    Reply: {r2.get('reply')[:120]}...")
    print(f"    Products found: {len(prods2)}")
    for p in prods2[:3]:
        print(f"      - {p['name']} (₹{p['price']})")
    assert len(prods2) > 0, "Expected at least 1 black Nike shoe under 3000"
    for p in prods2:
        assert "nike" in p["name"].lower(), f"Expected Nike brand to be preserved, got {p['name']}"
        assert "black" in p["name"].lower(), f"Expected black color in {p['name']}"
        assert p["price"] <= 3000, f"Expected budget <= 3000 to be preserved, got {p['price']}"
    print("    PASSED!\n")

    # -------------------------------------------------------------
    # CASE 3: Superlative / Product Reference ("Which one is the cheapest?")
    # -------------------------------------------------------------
    print("[3] Test Superlative Query: 'Which one is the cheapest?'")
    r3 = send_chat(client_id, cid, "Which one is the cheapest?")
    prods3 = r3.get("products", [])
    reply3 = r3.get("reply", "")
    print(f"    Reply: {reply3}")
    expected_cheapest = min(prods2, key=lambda p: p["price"])
    print(f"    Expected cheapest: {expected_cheapest['name']} (₹{expected_cheapest['price']})")
    assert expected_cheapest["name"] in reply3 or str(int(expected_cheapest["price"])) in reply3 or str(expected_cheapest["price"]) in reply3, "Expected lowest priced product to be identified in reply"
    print("    PASSED!\n")

    # -------------------------------------------------------------
    # CASE 4: Product Availability Reference ("Is it available?")
    # -------------------------------------------------------------
    print("[4] Test Availability Check: 'Is it available?'")
    r4 = send_chat(client_id, cid, "Is it available?")
    reply4 = r4.get("reply", "")
    print(f"    Reply: {reply4}")
    assert "available" in reply4.lower() or "in stock" in reply4.lower(), "Expected availability confirmation"
    print("    PASSED!\n")

    # -------------------------------------------------------------
    # CASE 5: Modifying Existing Requirements - Brand Replacement ("What about Adidas?")
    # -------------------------------------------------------------
    print("[5] Test Brand Replacement: 'What about Adidas?'")
    r5 = send_chat(client_id, cid, "What about Adidas?")
    prods5 = r5.get("products", [])
    print(f"    Reply: {r5.get('reply')[:120]}...")
    print(f"    Products found: {len(prods5)}")
    for p in prods5[:3]:
        print(f"      - {p['name']} (₹{p['price']})")
    assert len(prods5) > 0, "Expected Adidas shoes"
    for p in prods5:
        assert "adidas" in p["name"].lower(), f"Expected Adidas brand, got {p['name']}"
        assert "nike" not in p["name"].lower(), f"Old brand Nike leaked into {p['name']}"
    print("    PASSED!\n")

    # -------------------------------------------------------------
    # CASE 6: Modifying Existing Requirements - Budget Increase ("Increase my budget to 5000")
    # -------------------------------------------------------------
    print("[6] Test Budget Update: 'Increase my budget to 5000'")
    r6 = send_chat(client_id, cid, "Increase my budget to 5000")
    prods6 = r6.get("products", [])
    print(f"    Reply: {r6.get('reply')[:120]}...")
    print(f"    Products found: {len(prods6)}")
    for p in prods6[:3]:
        print(f"      - {p['name']} (₹{p['price']})")
    for p in prods6:
        assert "adidas" in p["name"].lower(), f"Expected Adidas preserved, got {p['name']}"
        assert p["price"] <= 5000, f"Expected price <= 5000, got {p['price']}"
    print("    PASSED!\n")

    # -------------------------------------------------------------
    # CASE 7: Context Switching & Anti-Leakage ("Now show me laptops under 50000")
    # -------------------------------------------------------------
    print("[7] Test Context Switching: 'Now show me laptops under 50000'")
    r7 = send_chat(client_id, cid, "Now show me laptops under 50000")
    prods7 = r7.get("products", [])
    print(f"    Reply: {r7.get('reply')[:120]}...")
    print(f"    Products found: {len(prods7)}")
    for p in prods7[:3]:
        print(f"      - {p['name']} (₹{p['price']})")
    assert len(prods7) > 0, "Expected laptops"
    for p in prods7:
        assert any(w in p["name"].lower() for w in ["laptop", "notebook", "ideapad", "thinkpad", "vivobook", "pavilion", "inspiron", "core", "ryzen", "gaming"]), f"Non-laptop returned: {p['name']}"
        assert "shoe" not in p["name"].lower(), f"Shoe context leaked: {p['name']}"
        assert "adidas" not in p["name"].lower(), f"Shoe brand leaked: {p['name']}"
        assert p["price"] <= 50000, f"Expected price <= 50000, got {p['price']}"
    print("    PASSED! (Zero leakage from shoe context)\n")

    # -------------------------------------------------------------
    # CASE 8: Return to Earlier Search ("Go back to the shoes and show me cheaper ones")
    # -------------------------------------------------------------
    print("[8] Test Return to Earlier Search: 'Go back to the shoes and show me cheaper ones'")
    r8 = send_chat(client_id, cid, "Go back to the shoes and show me cheaper ones")
    prods8 = r8.get("products", [])
    print(f"    Reply: {r8.get('reply')[:120]}...")
    print(f"    Products found: {len(prods8)}")
    for p in prods8[:3]:
        print(f"      - {p['name']} (₹{p['price']})")
    assert len(prods8) > 0, "Expected shoes restored"
    for p in prods8:
        assert any(w in p["name"].lower() for w in ["shoe", "running", "sneaker", "footwear"]), f"Expected shoes returned: {p['name']}"
        assert "laptop" not in p["name"].lower(), f"Laptop context leaked into shoes: {p['name']}"
    print("    PASSED! (Prior search successfully restored)\n")

    # -------------------------------------------------------------
    # CASE 9: Small Talk / Greeting Preservation ("Thank you!")
    # -------------------------------------------------------------
    print("[9] Test Smalltalk Preservation: 'Thank you!'")
    r9 = send_chat(client_id, cid, "Thank you!")
    print(f"    Reply: {r9.get('reply')}")
    assert "welcome" in r9.get("reply", "").lower() or "help" in r9.get("reply", "").lower(), "Expected polite smalltalk response"
    # Immediately ask follow-up to test that context was preserved!
    r9_follow = send_chat(client_id, cid, "Which one is the cheapest?")
    print(f"    Follow-up after thanks ('Which one is the cheapest?'): {r9_follow.get('reply')[:100]}...")
    assert len(r9_follow.get("products", [])) > 0 or "cheapest" in r9_follow.get("reply", "").lower(), "Expected context preserved across thanks"
    print("    PASSED! (Context preserved across smalltalk)\n")

    # -------------------------------------------------------------
    # CASE 10: Out of Scope Guardrail ("Explain quantum physics")
    # -------------------------------------------------------------
    print("[10] Test Out of Scope: 'Explain quantum physics'")
    r10 = send_chat(client_id, cid, "Explain quantum physics")
    reply10 = r10.get("reply", "")
    print(f"    Reply: {reply10}")
    assert any(w in reply10.lower() for w in ["not seem to be related", "shopping", "flipkart", "assist", "catalogue", "can't help"]), f"Expected polite out of scope response, got: {reply10}"
    print("    PASSED!\n")

    # -------------------------------------------------------------
    # CASE 11: Reset Search (Clear Filters, Preserve History)
    # -------------------------------------------------------------
    print("[11] Test Reset Search Endpoint & History Preservation")
    reset_res = reset_search(client_id, cid)
    print(f"    Reset response: {reset_res}")
    assert reset_res.get("ok") is True, "Expected reset ok: True"

    # Verify visible history is intact in PostgreSQL
    hist = get_history(client_id, cid)
    msgs = hist.get("messages", [])
    print(f"    Messages preserved in PostgreSQL: {len(msgs)}")
    assert len(msgs) >= 10, f"Expected full conversation history preserved, found {len(msgs)}"

    # When user asks for "cheaper ones" after reset, assistant asks which product rather than hallucinating
    r11_post = send_chat(client_id, cid, "Show me cheaper ones")
    print(f"    Post-reset 'Show me cheaper ones' reply: {r11_post.get('reply')}")
    assert "which product" in r11_post.get("reply", "").lower() or "what product" in r11_post.get("reply", "").lower() or "tell me" in r11_post.get("reply", "").lower(), "Expected clarification question rather than hallucinated context"
    print("    PASSED!\n")

    # -------------------------------------------------------------
    # CASE 12: New Chat (Complete State & History Isolation)
    # -------------------------------------------------------------
    print("[12] Test New Chat Isolation: + New Chat")
    nchat2 = new_chat(client_id)
    cid2 = nchat2["conversation_id"]
    assert cid2 != cid, "Expected new conversation ID"
    hist2 = get_history(client_id, cid2)
    assert len(hist2.get("messages", [])) == 0, "Expected empty message list for brand new chat"
    print(f"    New chat {cid2} is completely isolated with 0 initial messages.")
    print("    PASSED!\n")

    print("=" * 70)
    print("ALL 12 TEST CASES PASSED SUCCESSFULLY!")
    print("=" * 70)

if __name__ == "__main__":
    run_tests()
