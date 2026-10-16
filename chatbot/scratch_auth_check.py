import requests
u = "http://127.0.0.1:8000/api/chat/"
b = {"message": "hi", "session_id": "t1"}
print(requests.post(u, json=b).status_code)
print(requests.post(u, json=b, headers={"X-Store-Key": "bad"}).status_code)
print(requests.post(u, json=b, headers={"X-Store-Key": "Lref8GVYiVxxZ4wehiudiZRlAcI5myX-QG48E4K33V0"}).status_code)