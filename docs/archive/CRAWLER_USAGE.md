# 天天基金吧爬虫模块 - 使用说明

## 📋 功能概述

爬虫模块用于抓取天天基金吧的热门帖子，并自动分析博主的观点倾向（看多/看空/中性）。

### 核心特性

- ✅ **完全隔离** - 不影响现有基金预测功能
- ✅ **手动触发** - 默认关闭，需要时手动调用
- ✅ **频率控制** - 自动限制请求频率，避免被封
- ✅ **情绪分析** - 自动识别博主观点倾向
- ✅ **板块识别** - 提取帖子提到的行业板块

---

## 🚀 快速开始

### 1️⃣ 启用爬虫模块

编辑 `.env` 文件，设置：

```bash
CRAWLER_ENABLED=true
```

### 2️⃣ 重启服务器

```bash
python start.py
```

### 3️⃣ 调用 API 抓取

**抓取所有活跃基金的帖子：**

```bash
curl -X POST http://localhost:8002/api/crawler/fetch \
  -H "Content-Type: application/json" \
  -d '{"fund_codes": null}'
```

**抓取指定基金的帖子：**

```bash
curl -X POST http://localhost:8002/api/crawler/fetch \
  -H "Content-Type: application/json" \
  -d '{"fund_codes": ["000001", "160221"]}'
```

---

## 📡 API 接口

### 1. POST /api/crawler/fetch

手动触发抓取天天基金吧帖子

**请求参数：**

```json
{
  "fund_codes": ["000001", "160221"]  // 不传或 null 则抓取所有活跃基金
}
```

**返回示例：**

```json
{
  "success": true,
  "message": "成功抓取 20 条帖子",
  "data": {
    "total_posts": 20,
    "total_funds": 2,
    "posts": [
      {
        "post_id": "1234567890",
        "fund_code": "000001",
        "title": "今天半导体暴涨，我重仓吃到大肉",
        "author": "投资达人张三",
        "sentiment": "bullish",
        "sentiment_score": 0.85,
        "confidence": 75,
        "sectors": ["半导体"],
        "keywords": ["暴涨", "重仓", "吃肉"],
        "url": "https://guba.eastmoney.com/news,000001,1234567890.html",
        "crawl_time": "2025-03-06 10:30:00"
      }
    ]
  }
}
```

**字段说明：**

| 字段 | 类型 | 说明 |
|------|------|------|
| post_id | string | 帖子 ID |
| fund_code | string | 基金代码 |
| title | string | 帖子标题 |
| author | string | 博主名称 |
| sentiment | string | 情绪：bullish（看多）/ bearish（看空）/ neutral（中性） |
| sentiment_score | float | 情绪评分：-1.0 ~ 1.0，正数看多，负数看空 |
| confidence | int | 置信度：0 ~ 100 |
| sectors | array | 提到的板块列表 |
| keywords | array | 命中的关键词 |
| url | string | 帖子链接 |
| crawl_time | string | 抓取时间 |

---

### 2. GET /api/crawler/status

获取爬虫模块状态

**请求示例：**

```bash
curl http://localhost:8002/api/crawler/status
```

**返回示例：**

```json
{
  "success": true,
  "data": {
    "enabled": true,
    "request_delay": 2.0,
    "max_posts_per_fund": 10,
    "timeout": 10
  }
}
```

**字段说明：**

| 字段 | 类型 | 说明 |
|------|------|------|
| enabled | bool | 是否启用 |
| request_delay | float | 请求间隔（秒） |
| max_posts_per_fund | int | 每基金最多抓取帖子数 |
| timeout | int | 请求超时（秒） |

---

### 3. GET /api/crawler/stats

获取爬虫统计信息

**请求示例：**

```bash
curl http://localhost:8002/api/crawler/stats
```

---

## 🔧 配置说明

在 `.env` 文件中配置：

```bash
# 是否启用爬虫（true/false）
CRAWLER_ENABLED=false

# 请求间隔（秒），避免被封
CRAWLER_REQUEST_DELAY=2.0

# 每基金最多抓取帖子数
MAX_POSTS_PER_FUND=10

# 请求超时（秒）
CRAWLER_TIMEOUT=10
```

---

## 📊 情绪分析说明

### 情绪评分规则

| 评分范围 | 情绪倾向 | 说明 |
|----------|----------|------|
| score > 0.15 | bullish（看多） | 博主看好后市 |
| score < -0.15 | bearish（看空） | 博主看空后市 |
| -0.15 ≤ score ≤ 0.15 | neutral（中性） | 观望或震荡 |

### 关键词匹配

**看多关键词示例：**
- 强烈：暴涨、涨停、抄底、买入、加仓、重仓、看好
- 温和：上涨、红盘、盈利、赚钱、价值投资、长期持有

**看空关键词示例：**
- 强烈：暴跌、跌停、崩盘、割肉、卖出、清仓、跑路
- 温和：下跌、回调、调整、绿盘、亏损、高估、风险大

### 板块识别

支持识别以下板块：
- 半导体、新能源、医药、消费、科技、金融、地产、有色、化工、军工、电力、汽车、互联网、农业、基建、卫星、存储、电力设备

---

## 💡 使用场景

### 1️⃣ 市场情绪监控

抓取所有活跃基金的帖子，了解市场整体情绪：

```python
import requests

response = requests.post("http://localhost:8002/api/crawler/fetch")
data = response.json()

# 统计看多/看空比例
bullish_count = sum(1 for p in data['data']['posts'] if p['sentiment'] == 'bullish')
bearish_count = sum(1 for p in data['data']['posts'] if p['sentiment'] == 'bearish')

print(f"看多：{bullish_count}, 看空：{bearish_count}")
```

### 2️⃣ 博主观点追踪

对特定基金，追踪博主观点变化：

```python
# 抓取某基金吧
response = requests.post(
    "http://localhost:8002/api/crawler/fetch",
    json={"fund_codes": ["160221"]}
)

posts = response.json()['data']['posts']
for post in posts:
    print(f"{post['title']} - {post['sentiment']} ({post['sentiment_score']})")
```

### 3️⃣ 热点板块发现

识别大家都在讨论哪些板块：

```python
from collections import Counter

response = requests.post("http://localhost:8002/api/crawler/fetch")
posts = response.json()['data']['posts']

all_sectors = []
for post in posts:
    all_sectors.extend(post['sectors'])

sector_counts = Counter(all_sectors)
print("热门板块 TOP10:", sector_counts.most_common(10))
```

---

## ⚠️ 注意事项

### 1. 频率控制

- 默认请求间隔 2 秒
- 建议不要频繁调用（每天 1-2 次即可）
- 如需批量抓取，建议分批进行

### 2. 合规使用

- 仅抓取公开数据
- 不要用于商业用途
- 遵守网站 robots.txt 协议

### 3. 数据保存

当前版本抓取的数据不会保存到数据库，仅在 API 响应中返回。

**如需保存，可以扩展：**

1. 在数据库中添加 `CrawledPost` 表
2. 在 `fetch_fund_posts` API 中保存到数据库
3. 添加查询历史帖子的 API

---

## 🛠️ 故障排查

### 问题 1：爬虫未启用

**现象：** 返回 `"爬虫模块未启用"`

**解决：** 检查 `.env` 文件中 `CRAWLER_ENABLED=true`

---

### 问题 2：抓取失败

**现象：** 返回 `"抓取失败：..."`

**可能原因：**
- 网络问题
- 天天基金网反爬
- BeautifulSoup 未安装

**解决：**
```bash
# 安装依赖
pip install beautifulsoup4

# 检查网络连接
ping guba.eastmoney.com
```

---

### 问题 3：现有功能受影响

**现象：** 基金预测功能无法使用

**解决：** 爬虫模块完全隔离，理论上不影响现有功能。如遇到问题：
1. 设置 `CRAWLER_ENABLED=false` 关闭爬虫
2. 重启服务器
3. 检查日志

---

## 📝 扩展建议

### 1. 保存到数据库

```python
# models/database.py
class CrawledPost(Base):
    __tablename__ = 'crawled_posts'
    
    id = Column(Integer, primary_key=True)
    post_id = Column(String, unique=True)
    fund_code = Column(String)
    title = Column(String)
    content = Column(Text)
    author = Column(String)
    sentiment = Column(String)
    sentiment_score = Column(Float)
    sectors = Column(JSON)
    url = Column(String)
    crawl_time = Column(DateTime)
```

### 2. 定时任务

```python
# 每天上午 9 点自动抓取
@app.post("/api/crawler/schedule")
def schedule_daily_fetch():
    # 添加到定时任务
    pass
```

### 3. 情绪趋势图

```python
# 统计每天的情绪评分，绘制趋势图
@app.get("/api/crawler/sentiment-trend")
def get_sentiment_trend(fund_code: str, days: int = 7):
    # 返回最近 N 天的情绪趋势
    pass
```

---

## ✅ 验证清单

使用前确认：

- [ ] `.env` 中设置 `CRAWLER_ENABLED=true`
- [ ] 已安装 `beautifulsoup4`
- [ ] 服务器已重启
- [ ] 网络正常
- [ ] 现有基金预测功能正常

---

## 📞 技术支持

如有问题，查看日志输出：

```bash
# 查看控制台日志
# 爬虫相关日志以 [Crawler] 或 [Crawler API] 开头
```

---

**最后更新：** 2025-03-06
**版本：** v1.0.0
