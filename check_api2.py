import requests

r = requests.get('http://localhost:8002/api/posts/65')
print(f"Status: {r.status_code}")
print(f"Content: {r.text[:500]}")
