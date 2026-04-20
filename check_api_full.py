import requests
import json

# 检查帖子ID 71的API返回
r = requests.get('http://localhost:8002/api/posts/71')
data = r.json()

print("=== API 返回的完整数据 ===")
print(json.dumps(data, indent=2, ensure_ascii=False))
