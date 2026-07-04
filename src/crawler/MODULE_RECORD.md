# 模块记录 - Crawler

## 模块定位

`src/crawler/` 负责外部内容和板块资金数据采集，包括天天基金吧、东方财富博客/导读、新浪财经/博客、微信公众号文章、板块资金流和内容筛选。

## 当前职责

- 抓取公开财经文章或帖子。
- 做质量过滤、AI 过滤和情绪/板块识别。
- 为观点采纳和板块资金流服务提供原始或半结构化数据。
- 控制请求频率和超时。

## 主要文件

| 文件 | 职责 |
| --- | --- |
| `base.py` | 爬虫基类和基础请求能力 |
| `tiantian_crawler.py` | 天天基金吧帖子 |
| `eastmoney_blog_crawler.py` | 东方财富博客 |
| `eastmoney_guide_crawler.py` | 东方财富导读 |
| `sina_finance_crawler.py` | 新浪财经 |
| `sina_blog_crawler.py` | 新浪博客 |
| `wechat_fetcher.py` | 微信文章获取 |
| `article_crawler.py` | 通用文章抓取 |
| `enhanced_crawler.py` | 增强抓取流程 |
| `ai_analyzer.py` | 旧 AI 筛选入口，已有迁移提示 |
| `filters/quality_filter.py` | 质量过滤 |
| `filters/ai_filter.py` | AI 过滤 |
| `sentiment_analyzer.py`、`sentiment.py` | 情绪分析 |
| `sector_flow_crawler.py` | 东方财富板块资金流抓取 |
| `sector_flow_fetcher.py` | 板块资金流抓取辅助 |

## 数据流

文章/帖子：

```text
外部页面
  -> crawler
  -> filter/analyzer
  -> API 采纳
  -> Viewpoint / CrawlerArticleRecord
```

板块资金流：

```text
东方财富 API
  -> SectorFlowCrawler
  -> SectorFlowService
  -> SectorFundFlow / SectorFlowFetchRun
```

## 配置

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `CRAWLER_ENABLED` | `false` | 本地默认关闭，Render 当前开启 |
| `CRAWLER_REQUEST_DELAY` | `2.0` | 请求间隔 |
| `MAX_POSTS_PER_FUND` | `10` | 每基金最大帖子数 |
| `CRAWLER_TIMEOUT` | `10` | 请求超时秒数 |

## 高风险点

- 外部网站结构变化会导致解析失败。
- 不要做高频抓取；遵守请求间隔。
- `ai_analyzer.py` 有迁移提示，新代码优先使用 `src/analyzer/post_analyzer.py`。
- 爬虫失败应返回可解释错误，不能影响核心手动录入流程。

## 推荐验证

```bash
pytest tests/unit/test_crawler/test_crawler.py -v
pytest tests/unit/test_sector_flow_service.py -v
pytest tests/unit/test_sector_flow_routes.py -v
```
