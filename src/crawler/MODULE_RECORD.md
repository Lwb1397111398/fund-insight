# 模块记录文档 - Crawler 模块

## 基本信息

| 属性 | 值 |
|------|-----|
| **模块名称** | Crawler (爬虫模块) |
| **模块路径** | src/crawler/ |
| **负责人** | Fund Insight Team |
| **创建日期** | 2024-01-01 |
| **最后更新** | 2026-03-07 |
| **版本** | 1.0.0 |

## 一、模块概述

### 1.1 职责定义

从多个财经平台抓取文章、帖子和观点数据，支持 AI 筛选和内容分析，为系统提供外部数据来源。

### 1.2 功能范围

| 功能 | 描述 | 状态 |
|------|------|------|
| 天天基金吧爬虫 | 抓取基金吧热门帖子 | 已实现 |
| 东方财富博客爬虫 | 抓取热门博主文章 | 已实现 |
| 东方财富导读爬虫 | 抓取博客导读文章 | 已实现 |
| 新浪财经爬虫 | 抓取新浪财经新闻 | 已实现 |
| 新浪博文爬虫 | 抓取新浪博文列表 | 已实现 |
| AI 筛选 | 使用 LLM 筛选高质量内容 | 已实现 |
| 情感分析 | 分析帖子情感倾向 | 已实现 |
| 观点采纳 | 将抓取内容采纳为观点 | 已实现 |

### 1.3 边界定义

**包含：**
- 数据抓取逻辑
- 页面解析
- 质量筛选
- AI 筛选（调用 analyzer）
- 情感分析

**不包含：**
- 数据存储（由 services 负责）
- 深度分析（由 analyzer 负责）
- HTTP 接口（由 api 负责）

## 二、文件清单

| 文件名 | 职责 | 代码行数 | 关键类/函数 |
|--------|------|----------|-------------|
| tiantian_crawler.py | 天天基金吧爬虫 | ~300 | TiantianCrawler, crawler |
| eastmoney_blog_crawler.py | 东方财富博客爬虫 | ~200 | EastmoneyBlogCrawler |
| eastmoney_guide_crawler.py | 博客导读爬虫 | ~150 | EastmoneyGuideCrawler |
| sina_finance_crawler.py | 新浪财经爬虫 | ~150 | SinaFinanceCrawler |
| sina_blog_crawler.py | 新浪博文爬虫 | ~150 | SinaBlogCrawler |
| ai_analyzer.py | AI 筛选分析器 | ~200 | AIPostAnalyzer, ai_analyzer |
| sentiment_analyzer.py | 情感分析器 | ~100 | SentimentAnalyzer, analyzer |
| __init__.py | 模块导出 | 23 | - |

## 三、依赖关系

### 3.1 上游依赖（本模块依赖的其他模块）

| 模块 | 依赖方式 | 依赖内容 | 耦合度 |
|------|----------|----------|--------|
| core | 直接导入 | config.CRAWLER_* | 低 |
| analyzer | 直接导入 | get_analyzer | 中 |

### 3.2 下游依赖（依赖本模块的其他模块）

| 模块 | 依赖方式 | 依赖内容 |
|------|----------|----------|
| api | 直接导入 | 各爬虫类 |

### 3.3 外部依赖

| 包名 | 版本 | 用途 |
|------|------|------|
| requests | ^2.28.0 | HTTP 请求 |
| beautifulsoup4 | ^4.12.0 | HTML 解析 |

## 四、核心接口

### 4.1 公开接口

```python
from src.crawler import (
    TiantianCrawler, crawler,
    EastMoneyArticleCrawler, article_crawler,
    SentimentAnalyzer, analyzer,
    AIPostAnalyzer, ai_analyzer
)
```

### 4.2 接口说明

#### TiantianCrawler (天天基金吧爬虫)

```python
class TiantianCrawler:
    """天天基金吧爬虫"""
    
    def fetch_fund_posts(self, fund_code: str, max_posts: int = 10) -> List[Dict]:
        """抓取指定基金的帖子列表"""
        
    def fetch_hot_posts(self, max_posts: int = 20) -> List[Dict]:
        """抓取热门帖子"""
        
    def _is_quality_post(self, item: Dict) -> tuple:
        """判断是否为高质量帖子"""
```

#### EastmoneyBlogCrawler (东方财富博客爬虫)

```python
class EastmoneyBlogCrawler:
    """东方财富博客爬虫"""
    
    def fetch_hot_articles(self, max_articles: int = 20) -> List[Dict]:
        """抓取热门博主文章"""
```

#### AIPostAnalyzer (AI 筛选分析器)

```python
class AIPostAnalyzer:
    """AI 帖子分析器"""
    
    def analyze_post(self, title: str, content: str) -> Dict:
        """分析帖子内容，返回情感和板块信息"""
        
    def filter_posts(self, posts: List[Dict], min_confidence: int = 60) -> List[Dict]:
        """筛选高质量帖子"""
```

## 五、数据模型

### 5.1 使用的数据库表

| 表名 | 用途 | 访问模式 |
|------|------|----------|
| viewpoints | 存储采纳的观点 | 写 |

### 5.2 数据流向

```
外部网站 → 爬虫抓取 → 解析处理 → AI 筛选 → 返回数据 → API → 数据库
```

## 六、配置项

| 配置项 | 环境变量 | 默认值 | 说明 |
|--------|----------|--------|------|
| CRAWLER_ENABLED | CRAWLER_ENABLED | false | 爬虫开关 |
| CRAWLER_REQUEST_DELAY | CRAWLER_REQUEST_DELAY | 2.0 | 请求间隔(秒) |
| MAX_POSTS_PER_FUND | MAX_POSTS_PER_FUND | 10 | 每基金最大帖子数 |
| CRAWLER_TIMEOUT | CRAWLER_TIMEOUT | 10 | 请求超时(秒) |

## 七、使用示例

### 7.1 基本使用

```python
from src.crawler import crawler, article_crawler

# 抓取天天基金吧帖子
posts = crawler.fetch_fund_posts('000001', max_posts=10)

# 抓取东方财富博客文章
articles = article_crawler.fetch_home_articles(max_articles=10)
```

### 7.2 AI 筛选

```python
from src.crawler import ai_analyzer

# 分析帖子
result = ai_analyzer.analyze_post(
    title="看好白酒板块反弹",
    content="白酒板块调整充分..."
)

print(result["sentiment"])  # bullish/bearish/neutral
print(result["confidence"])  # 0-100
```

### 7.3 情感分析

```python
from src.crawler import analyzer

# 分析情感
sentiment = analyzer.analyze("看好白酒板块反弹")
print(sentiment)  # positive/negative/neutral
```

## 八、测试指南

### 8.1 单元测试

```bash
# 运行单元测试
pytest tests/unit/test_crawler.py -v
```

### 8.2 测试覆盖

| 文件 | 覆盖率 | 目标 |
|------|--------|------|
| tiantian_crawler.py | 待测试 | 80% |
| eastmoney_blog_crawler.py | 待测试 | 80% |
| ai_analyzer.py | 待测试 | 80% |

## 九、变更记录

| 日期 | 版本 | 变更内容 | 变更人 |
|------|------|----------|--------|
| 2024-01-01 | 1.0.0 | 初始版本 | Team |
| 2024-06-01 | 1.1.0 | 添加 AI 筛选功能 | Team |
| 2024-06-01 | 1.2.0 | 添加新浪爬虫 | Team |
| 2026-03-07 | 1.0.0 | 模块化文档创建 | Agent |

## 十、已知问题与改进计划

### 10.1 已知问题

| 问题 | 严重程度 | 计划解决时间 |
|------|----------|--------------|
| 网站结构变化可能导致爬虫失效 | 中 | 待定 |
| 缺少统一的爬虫基类 | 低 | 待定 |
| AI 筛选逻辑与 analyzer 模块重复 | 中 | 待定 |

### 10.2 改进计划

| 改进项 | 优先级 | 预计工作量 |
|--------|--------|------------|
| 创建爬虫基类 | 中 | 1人天 |
| 分离筛选逻辑到 filters 子模块 | 中 | 1人天 |
| 添加异步支持 | 低 | 2人天 |
| 添加代理支持 | 低 | 1人天 |
