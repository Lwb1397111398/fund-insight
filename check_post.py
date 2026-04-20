import requests
r = requests.get('http://localhost:8002/api/posts/65')
data = r.json()
if data.get('success'):
    post = data.get('data', {})
    print('帖子ID:', post.get('id'))
    print('标题:', post.get('title'))
    for p in post.get('predictions', []):
        print(f"  预测ID {p.get('id')}: 周期={p.get('prediction_period')}, 目标日期={p.get('target_date')}")
