import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests
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
        timeout=180
    )
    if res.status_code != 200:
        raise Exception(f"Chat failed with status {res.status_code}: {res.text}")
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
    print("=" * 75)
    print("TESTING DIVERSE REAL-WORLD USER INQUIRIES (BEYOND FIXED EXAMPLES)")
    print("=" * 75)

    client_id = f"user_{uuid.uuid4().hex[:8]}"
    nchat = new_chat(client_id)
    cid = nchat["conversation_id"]
    print(f"Session: {client_id} | Conversation: {cid}\n")

    # Query 1: Electronics / Audio Category ("Find Sony Bluetooth headphones under 2000")
    print("[1] User Query: 'Find Sony Bluetooth headphones under 2000'")
    r1 = send_chat(client_id, cid, "Find Sony Bluetooth headphones under 2000")
    p1 = r1.get("products", [])
    print(f"    Reply: {r1.get('reply')[:120]}...")
    print(f"    Products: {len(p1)}")
    for it in p1[:2]:
        print(f"      - {it['name']} (₹{it['price']})")
    assert len(p1) > 0, "Expected headphones"
    print("    PASSED!\n")

    # Query 2: Relative Ordinal Reference ("Tell me about the first one")
    print("[2] User Query: 'Tell me about the first one'")
    r2 = send_chat(client_id, cid, "Tell me about the first one")
    print(f"    Reply: {r2.get('reply')[:120]}...")
    p2 = r2.get("products", [])
    assert len(p2) > 0, "Expected referenced product"
    assert p2[0]["id"] == p1[0]["id"], "Expected first product to be resolved"
    print(f"    Correctly resolved 1st item: {p2[0]['name']}")
    print("    PASSED!\n")

    # Query 3: Availability check on focus product ("Is it in stock?")
    print("[3] User Query: 'Is it in stock?'")
    r3 = send_chat(client_id, cid, "Is it in stock?")
    print(f"    Reply: {r3.get('reply')}")
    assert any(w in r3.get('reply', '').lower() for w in ["in stock", "available", "out of stock"]), "Expected stock status"
    print("    PASSED!\n")

    # Query 4: Superlative ("Which one is the most expensive?")
    print("[4] User Query: 'Which one is the most expensive?'")
    r4 = send_chat(client_id, cid, "Which one is the most expensive?")
    print(f"    Reply: {r4.get('reply')}")
    p4 = r4.get("products", [])
    expected_priciest = max(p1, key=lambda p: p["price"])
    assert len(p4) > 0, "Expected product card"
    assert p4[0]["id"] == expected_priciest["id"], f"Expected {expected_priciest['name']} as priciest"
    print(f"    Correctly identified most expensive: {p4[0]['name']} (₹{p4[0]['price']})")
    print("    PASSED!\n")

    # Query 5: Clean Context Switch ("Now show me smartwatches under 3000")
    print("[5] User Query: 'Now show me smartwatches under 3000'")
    r5 = send_chat(client_id, cid, "Now show me smartwatches under 3000")
    p5 = r5.get("products", [])
    print(f"    Reply: {r5.get('reply')[:120]}...")
    print(f"    Products: {len(p5)}")
    for it in p5[:2]:
        print(f"      - {it['name']} (₹{it['price']})")
    assert len(p5) > 0, "Expected smartwatches"
    for it in p5:
        assert any(w in it["name"].lower() for w in ["watch", "smartwatch", "band", "dial"]), f"Non-watch returned: {it['name']}"
        assert "headphone" not in it["name"].lower(), f"Headphone context leaked: {it['name']}"
        assert it["price"] <= 3000, f"Expected price <= 3000, got {it['price']}"
    print("    PASSED! (Zero leakage from headphones)\n")

    # Query 6: Modifying brand in new context ("What about Boat?")
    print("[6] User Query: 'What about Boat?'")
    r6 = send_chat(client_id, cid, "What about Boat?")
    p6 = r6.get("products", [])
    print(f"    Reply: {r6.get('reply')[:120]}...")
    print(f"    Products: {len(p6)}")
    for it in p6[:2]:
        print(f"      - {it['name']} (₹{it['price']})")
    assert len(p6) > 0, "Expected Boat smartwatches"
    for it in p6:
        assert "boat" in it["name"].lower(), f"Expected Boat brand, got {it['name']}"
    print("    PASSED!\n")

    # Query 7: Returning to previous topic from stack ("Go back to the headphones search and show me cheaper ones")
    print("[7] User Query: 'Go back to the headphones search and show me cheaper ones'")
    r7 = send_chat(client_id, cid, "Go back to the headphones search and show me cheaper ones")
    p7 = r7.get("products", [])
    print(f"    Reply: {r7.get('reply')[:120]}...")
    print(f"    Products: {len(p7)}")
    for it in p7[:2]:
        print(f"      - {it['name']} (₹{it['price']})")
    assert len(p7) > 0, "Expected headphones restored"
    for it in p7:
        assert any(w in it["name"].lower() for w in ["headphone", "earphone", "earbuds", "audio", "wireless", "tws", "sound"]), f"Expected headphones: {it['name']}"
        assert "watch" not in it["name"].lower(), f"Watch context leaked into headphones: {it['name']}"
    print("    PASSED! (Prior headphone search restored from context stack)\n")

    # Query 8: General Knowledge Out-of-Scope ("What is the capital of France?")
    print("[8] User Query: 'What is the capital of France?'")
    r8 = send_chat(client_id, cid, "What is the capital of France?")
    print(f"    Reply: {r8.get('reply')}")
    assert any(w in r8.get('reply', '').lower() for w in ["not seem to be related", "shopping", "flipkart", "assist", "can't help"]), "Expected store guardrail"
    print("    PASSED!\n")

    # Query 9: Conversational politeness ("Thanks a lot!")
    print("[9] User Query: 'Thanks a lot!'")
    r9 = send_chat(client_id, cid, "Thanks a lot!")
    print(f"    Reply: {r9.get('reply')}")
    assert "welcome" in r9.get('reply', '').lower() or "help" in r9.get('reply', '').lower(), "Expected polite smalltalk reply"
    print("    PASSED!\n")

    print("=" * 75)
    print("ALL DIVERSE REAL-WORLD INQUIRIES PASSED SUCCESSFULLY!")
    print("=" * 75)

if __name__ == "__main__":
    run_tests()
