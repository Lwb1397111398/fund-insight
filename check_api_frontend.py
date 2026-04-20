import requests

# 检查帖子ID 71的API返回
r = requests.get('http://localhost:8002/api/posts/71')
data = r.json()

if data.get('success'):
    post = data.get('data', {})
    print(f"帖子ID: {post.get('id')}")
    print(f"标题: {post.get('title')}")
    print(f"\n预测列表:")
    for p in post.get('predictions', []):
        print(f"  - {p.get('sector')}: 周期={p.get('prediction_period')}, 目标日期={p.get('target_date')}")
else:
    print("API返回错误:", data)
