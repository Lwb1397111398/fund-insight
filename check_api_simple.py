import requests

r = requests.get('http://localhost:8002/api/posts/71')
data = r.json()

if data.get('success'):
    post = data.get('data', {})
    print('帖子ID:', post.get('id'))
    print('标题:', post.get('title'))
    print()
    print('预测列表:')
    for p in post.get('predictions', []):
        print(f"  - {p.get('sector')}: 周期={p.get('prediction_period')}, 目标={p.get('target_date')}")
