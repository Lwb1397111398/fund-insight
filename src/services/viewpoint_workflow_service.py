"""观点抓取、深度分析和每日汇总的持久化工作流。"""
from __future__ import annotations

import hashlib
import logging
import traceback
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from sqlalchemy.orm import Session

from src.models.database import BatchAnalysisTask, CrawlerArticleRecord, SessionLocal, Viewpoint
from src.services.viewpoint_service import get_source_authority


logger = logging.getLogger(__name__)

# 三个数据源的发布时间均为北京时间；Render 服务器默认 UTC，
# 直接用 date.today() 会在北京时间 00:00-08:00 之间把"昨天"误当"今天"，
# 导致"只抓当天"在北京时间凌晨失效。统一以北京日期为准。
BEIJING_TZ = timezone(timedelta(hours=8))


def beijing_today() -> date:
    """当前北京日期（观点同日发布判断的唯一基准）。"""
    return datetime.now(BEIJING_TZ).date()


DEFAULT_SOURCES = ("sina_blog", "stock_guba", "fund_guba")
ALLOWED_SOURCES = frozenset(DEFAULT_SOURCES)

# 任务心跳超时：后台任务跑在 Web 进程内，Render 休眠/重启会直接杀死它，
# 在 DB 里留下 status='running' 的僵尸任务。轮询端点发现超时任务即自愈为
# failed（前端随即显示重试按钮），避免按钮永久卡在"抓取分析中"。
# 阈值与 create_fetch_task 的中断恢复保持一致；活任务逐项 commit 刷新
# updated_at，正常心跳间隔远小于该值。
STALE_TASK_AFTER = timedelta(minutes=20)

# 核心观点词：命中任一即视为有实质市场态度（方向/板块/操作/行情动作）。
_CORE_VIEWPOINT_KEYWORDS = (
    # 方向判断（核心）
    '看多', '看空', '看涨', '看跌', '牛市', '熊市', '上涨', '下跌',
    # 板块/行业（核心）
    '板块', '科技', '医药', '消费', '新能源', '半导体', '芯片', '军工',
    '金融', '地产', '银行', '券商', '白酒', '光伏', '锂电', 'AI',
    # 操作建议（核心）
    '加仓', '减仓', '建仓', '清仓', '调仓', '持仓', '仓位',
    '买入', '卖出', '观望', '抄底', '止盈', '止损',
    # 行情判断（核心）
    '突破', '跌破', '反弹', '回调', '震荡', '调整',
    '压力位', '支撑位', '目标位', '阻力位',
)

# 泛化观点词：只命中这些（核心词零命中）的内容模棱两可——可能是新闻转述、
# 资讯点评而非博主自己的观点，交给轻量辅助AI裁决，其余不调AI、注重效率。
_GENERAL_VIEWPOINT_KEYWORDS = (
    # 分析逻辑
    '观点', '预测', '判断', '分析', '逻辑', '策略', '建议',
    '机会', '风险', '利好', '利空',
)

# 观点内容门禁：内容必须包含至少一个，才视为"博主市场观点"。
# 并集 == 原门禁关键词集合（拆分不能漏词，测试有断言）。
_VIEWPOINT_KEYWORDS = _CORE_VIEWPOINT_KEYWORDS + _GENERAL_VIEWPOINT_KEYWORDS

# 命中即丢弃的垃圾关键词（广告、导流、非法荐股）。
_SPAM_KEYWORDS = (
    '加群', '加微信', '加QQ', '私聊', '代客理财', '荐股', '牛股', '黑马',
    '内幕', '跟单', '带盘', '分成', '保本', '稳赚', '包赚', '合作', '咨询',
    '联系我', '扫码', '关注公众号', '添加好友', '免费领取', '限时',
)

# 低于此字数的内容视为无分析价值。
# 股吧/基金吧很多有效短帖在 40~80 字，过严会把两源几乎全过滤。
_MIN_CONTENT_LENGTH = 40


class ViewpointWorkflowService:
    """使用现有表实现可恢复的观点流水线。"""

    @staticmethod
    def _stable_article_id(source: str, article: Dict[str, Any]) -> str:
        stable_value = next(
            (
                str(article.get(key)).strip()
                for key in ("article_id", "id", "newsid", "url", "link")
                if article.get(key)
            ),
            "|".join(
                str(article.get(key) or "").strip()
                for key in ("title", "author", "publish_time", "date")
            ),
        )
        digest = hashlib.md5(stable_value.encode("utf-8")).hexdigest()
        return f"{source}:{digest}"

    @staticmethod
    def _content_hash(article: Dict[str, Any]) -> str:
        content = str(article.get("content") or article.get("title") or "").strip()
        return hashlib.md5(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _article_date(article: Dict[str, Any]) -> date:
        value = (
            article.get("publish_time")
            or article.get("post_time")
            or article.get("publish_date")
            or article.get("date")
        )
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if value:
            text = str(value).strip()
            # 新浪列表常见：2026年03月07日 01:00
            for fmt in (
                "%Y年%m月%d日 %H:%M:%S",
                "%Y年%m月%d日 %H:%M",
                "%Y年%m月%d日",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
                "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d %H:%M",
                "%Y-%m-%d",
                "%Y/%m/%d",
            ):
                try:
                    return datetime.strptime(text, fmt).date()
                except ValueError:
                    continue
            # ISO / 带时区
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00").replace("/", "-")).date()
            except ValueError:
                pass
            # 兜底：截前 10 位当 YYYY-MM-DD
            try:
                return datetime.strptime(text.replace("/", "-")[:10], "%Y-%m-%d").date()
            except ValueError:
                pass
        # 解析失败时按当天处理：宁可放行，也不要因为时间源异常把整源观点全部误杀
        return beijing_today()

    @staticmethod
    def _normalize_sources(sources: Optional[Iterable[str]]) -> List[str]:
        selected = list(dict.fromkeys(sources or DEFAULT_SOURCES))
        invalid = [source for source in selected if source not in ALLOWED_SOURCES]
        if invalid:
            raise ValueError(f"不支持的观点来源: {', '.join(invalid)}")
        return selected

    @classmethod
    def create_fetch_task(
        cls,
        db: Session,
        *,
        sources: Optional[Iterable[str]] = None,
        limit_per_source: int = 20,
        mode: str = "fetch",
    ) -> Tuple[BatchAnalysisTask, bool]:
        if mode != "fetch":
            raise ValueError("观点抓取仅支持 fetch 模式")
        selected = cls._normalize_sources(sources)
        limit_per_source = max(1, min(int(limit_per_source or 20), 50))
        active = db.query(BatchAnalysisTask).filter(
            BatchAnalysisTask.task_type == "viewpoint_fetch",
            BatchAnalysisTask.status.in_(("pending", "running")),
        ).order_by(BatchAnalysisTask.created_at.desc()).first()
        if active:
            if active.status == "running" and active.updated_at:
                if datetime.now() - active.updated_at > timedelta(minutes=20):
                    active.status = "pending"
                    active.error_message = "检测到 Render 重启中断，任务已恢复等待执行"
                    db.commit()
            return active, False

        source_stats = {
            source: {"fetched": 0, "adopted": 0, "duplicates": 0, "skipped": 0, "failed": 0}
            for source in selected
        }
        task = BatchAnalysisTask(
            task_type="viewpoint_fetch",
            status="pending",
            total_count=0,
            processed_count=0,
            success_count=0,
            failed_count=0,
            processed_ids=[],
            failed_ids=[],
            task_params={"sources": selected, "limit_per_source": limit_per_source, "mode": mode},
            result_summary={"sources": source_stats, "adopted": 0, "duplicates": 0, "skipped": 0},
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        return task, True

    @staticmethod
    def _default_fetchers() -> Dict[str, Callable[[int], List[Dict[str, Any]]]]:
        from src.crawler.sina_blog_crawler import get_blog_crawler
        from src.crawler.stock_guba_crawler import StockGubaCrawler
        from src.crawler.tiantian_crawler import TiantianCrawler

        stock_guba = StockGubaCrawler()
        fund_guba = TiantianCrawler()

        # 热门指数和基金代码
        HOT_STOCKS = ['000001', '399001', '399006']  # 上证、深证、创业板
        # 覆盖主要板块的热门基金（场外混合 + 场内行业ETF，这些基金吧发帖活跃）。
        # 覆盖面越广，当天样本越能代表"整个市场"而不是个别赛道。
        HOT_FUNDS = [
            '000001',  # 华夏成长（综合/大盘）
            '110022',  # 易方达消费（消费）
            '161725',  # 招商中证白酒（白酒）
            '159915',  # 易方达创业板ETF（科技成长）
            '512880',  # 国泰证券ETF（券商）
            '512480',  # 国联安半导体ETF（芯片）
            '512010',  # 易方达医药ETF（医药）
            '512660',  # 国泰军工ETF（军工）
            '515030',  # 华夏新能源车ETF（新能源）
            '515790',  # 华泰柏瑞光伏ETF（光伏）
        ]

        def _cap(limit: int, default: int) -> int:
            try:
                value = int(limit or default)
            except (TypeError, ValueError):
                value = default
            return max(1, min(value, 50))

        def fetch_stock_guba(limit: int) -> List[Dict[str, Any]]:
            """抓取热门股吧。最多 15 条，会补详情正文。"""
            # 前端统一传 limit_per_source=20（给新浪）；股吧压到 15，
            # 保证过滤后当天样本不至于太少（市场观点汇总至少需要 ~10 条）。
            cap = min(_cap(limit, 15), 15)
            # 每个吧先抓一点列表，再统一截断，避免只打一个吧。
            per_board = max(3, (cap + len(HOT_STOCKS) - 1) // len(HOT_STOCKS))
            stock_guba.max_posts = per_board
            results = stock_guba.fetch_hot_stocks(HOT_STOCKS)
            all_posts: List[Dict[str, Any]] = []
            for posts in results.values():
                all_posts.extend(posts)
            return all_posts[:cap]

        def fetch_fund_guba(limit: int) -> List[Dict[str, Any]]:
            """抓取热门基金吧。最多 15 条，会补详情正文。"""
            cap = min(_cap(limit, 15), 15)
            fund_guba.max_posts = max(3, (cap + len(HOT_FUNDS) - 1) // len(HOT_FUNDS))
            all_posts: List[Dict[str, Any]] = []
            for fund_code in HOT_FUNDS:
                posts = fund_guba.fetch_fund_posts(fund_code)
                all_posts.extend(posts)
                if len(all_posts) >= cap:
                    break
            return all_posts[:cap]

        return {
            "sina_blog": lambda limit: get_blog_crawler().fetch_blog_posts(max_posts=_cap(limit, 20)),
            "stock_guba": fetch_stock_guba,
            "fund_guba": fetch_fund_guba,
        }

    @staticmethod
    def _default_deep_analyzer(article: Dict[str, Any], source: str) -> Dict[str, Any]:
        from src.analyzer.viewpoint_analyzer import get_viewpoint_analyzer

        return get_viewpoint_analyzer().analyze_viewpoint(
            title=str(article.get("title") or ""),
            content=str(article.get("content") or article.get("title") or ""),
            author=str(article.get("author") or ""),
            source=source,
        )

    @staticmethod
    def _default_assistant_judge(article: Dict[str, Any], source: str) -> Optional[bool]:
        """辅助AI（轻量模型）裁决边界内容。返回 True/False；None=拿不准，调用方放行。"""
        from src.analyzer.viewpoint_analyzer import quick_judge_viewpoint

        result = quick_judge_viewpoint(
            str(article.get("title") or ""),
            str(article.get("content") or article.get("title") or ""),
        )
        return None if result is None else bool(result.get("keep"))

    @staticmethod
    def _needs_assistant_judge(article: Dict[str, Any]) -> bool:
        """过了关键词门禁、但核心观点词零命中（只命中"分析/观点/风险"等泛化词）。

        这类内容规则拿不准（新闻转述/资讯点评也会命中泛化词），才值得花一次
        轻量AI调用；命中任一核心词的内容直接采纳，不调AI，保证抓取效率。
        """
        title = str(article.get("title") or "").strip()
        content = str(article.get("content") or "").strip()
        text = f"{title}\n{content}" if content and content != title else (content or title)
        return not any(keyword in text for keyword in _CORE_VIEWPOINT_KEYWORDS)

    # 深度分析判定为这些类型的内容不属于"理性市场分析"，
    # 不进入观点列表和每日市场汇总（软删除后可在回收站查看、恢复）。
    _REJECT_VIEWPOINT_TYPES = ("情绪表达", "新闻转述", "广告引流", "无关内容")

    @classmethod
    def _apply_deep_analysis(cls, viewpoint: Viewpoint, analysis: Dict[str, Any]) -> None:
        horizon = analysis.get("time_horizon") or "medium"
        valid_days = {"short": 7, "medium": 30, "long": 90}.get(horizon, 30)
        viewpoint.market_direction = analysis.get("market_direction") or "neutral"
        viewpoint.confidence = int(analysis.get("confidence") or 50)
        viewpoint.sectors_bullish = analysis.get("sectors_bullish") or []
        viewpoint.sectors_bearish = analysis.get("sectors_bearish") or []
        analysis_text = analysis.get("analysis") or ""
        reasoning = analysis.get("reasoning") or ""
        viewpoint.reasoning = f"【AI深度分析】{analysis_text}\n\n【判断理由】{reasoning}".strip()
        viewpoint.summary = analysis.get("summary") or viewpoint.content[:80]
        viewpoint.time_horizon = horizon
        viewpoint.validity_period = f"{valid_days}天"
        viewpoint.valid_until = viewpoint.viewpoint_date + timedelta(days=valid_days)
        viewpoint.credibility_score = int(analysis.get("credibility") or 50)
        viewpoint.tags = analysis.get("key_points") or []
        viewpoint.action_suggestion = analysis.get("action_suggestion") or "观望"
        viewpoint.risk_level = analysis.get("risk_level") or "medium"
        viewpoint.source_authority = get_source_authority(viewpoint.source)
        viewpoint.viewpoint_type = analysis.get("viewpoint_type") or "深度分析"
        viewpoint.calculate_weight()

        viewpoint_type = str(analysis.get("viewpoint_type") or "").strip()
        if viewpoint_type in cls._REJECT_VIEWPOINT_TYPES:
            # 方向/板块等字段仍照常写入，回收站里可查看 AI 的完整判断
            viewpoint.is_deleted = True
            viewpoint.analysis_summary = f"rejected:{viewpoint_type}"
            viewpoint.reassessment_reason = (
                f"AI 判定为「{viewpoint_type}」，不属于理性市场分析，已自动排除出市场观点汇总"
            )
            logger.info(
                "[观点分析] 观点 %s 被判定为「%s」，已软删除", viewpoint.id, viewpoint_type,
            )
        else:
            viewpoint.analysis_summary = "succeeded"

    @classmethod
    def run_fetch_task(
        cls,
        task_id: int,
        *,
        session_factory: Callable[[], Session] = SessionLocal,
        fetchers: Optional[Dict[str, Callable[[int], List[Dict[str, Any]]]]] = None,
        deep_analyzer: Optional[Callable[[Dict[str, Any], str], Dict[str, Any]]] = None,
        assistant_judge: Optional[Callable[[Dict[str, Any], str], Optional[bool]]] = None,
    ) -> None:
        """抓取观点并直接入库：不调用主力AI，只对规则拿不准的边界内容
        调用轻量辅助AI裁决（每次任务最多 15 次，超限或失败一律放行）。"""
        fetchers = fetchers or cls._default_fetchers()
        deep_analyzer = deep_analyzer or cls._default_deep_analyzer
        # 辅助AI裁决上下文：judge=裁决函数，calls=已调用次数，cap=本次任务上限。
        assistant_ctx = {
            "judge": assistant_judge or cls._default_assistant_judge,
            "calls": 0,
            "cap": 15,
        }
        db = session_factory()
        try:
            task = db.query(BatchAnalysisTask).filter(
                BatchAnalysisTask.id == task_id,
                BatchAnalysisTask.task_type == "viewpoint_fetch",
            ).with_for_update().first()
            if not task or task.status in ("succeeded", "cancelled"):
                return
            task.status = "running"
            task.started_at = task.started_at or datetime.now()
            task.completed_at = None
            task.error_message = None
            db.commit()

            params = dict(task.task_params or {})
            retry_ids = list(params.get("retry_viewpoint_ids") or [])
            if retry_ids:
                cls._run_deep_retries(db, task, retry_ids, deep_analyzer)
                return

            sources = cls._normalize_sources(params.get("sources"))
            limit = max(1, min(int(params.get("limit_per_source") or 20), 50))

            # ===== 阶段 1: 抓取 =====
            # 先从各源拉取文章到内存, 记录抓取进度(阶段耗时在进度栏显示)
            fetch_started = datetime.now()
            source_articles: Dict[str, List[Dict[str, Any]]] = {}
            total_to_fetch = 0
            for source in sources:
                task = db.get(BatchAnalysisTask, task_id)
                if not task or task.status in ("cancelled", "failed"):
                    return
                summary = dict(task.result_summary or {})
                source_stats = dict((summary.get("sources") or {}).get(source) or {})
                # 抓取前心跳：fetcher 是慢 HTTP，期间没有 commit，
                # 不写这次心跳会让轮询端点的超时自愈误判任务已死。
                summary["phase"] = f"fetching:{source}"
                task.result_summary = summary
                db.commit()
                try:
                    articles = list(fetchers[source](limit) or [])
                    source_articles[source] = articles
                    source_stats["fetched"] = len(articles)
                    total_to_fetch += len(articles)
                    task.total_count = total_to_fetch
                    cls._save_source_stats(task, summary, source, source_stats)
                    db.commit()
                except Exception as exc:
                    source_articles[source] = []
                    source_stats["fetched"] = 0
                    source_stats["failed"] = (source_stats.get("failed") or 0) + 1
                    source_stats["error"] = str(exc)
                    failures = list(task.failed_ids or [])
                    failures.append({"source": source, "error": str(exc)})
                    task.failed_ids = failures
                    task.failed_count = len(failures)
                    cls._save_source_stats(task, summary, source, source_stats)
                    db.commit()

            # 记录抓取阶段耗时与总数
            task = db.get(BatchAnalysisTask, task_id)
            if task:
                summary = dict(task.result_summary or {})
                summary["fetch_duration_seconds"] = round((datetime.now() - fetch_started).total_seconds(), 1)
                summary["fetched_total"] = total_to_fetch
                task.result_summary = summary
                db.commit()

            for source, articles in source_articles.items():
                task = db.get(BatchAnalysisTask, task_id)
                # failed：任务在运行中被轮询端点自愈（进程中断）→ 停止推进，
                # 否则活过来的慢任务会把自己写成 succeeded，自愈白做。
                if not task or task.status in ("cancelled", "failed"):
                    return
                for article in articles:
                    try:
                        cls._process_article(db, task_id, source, article, assistant_ctx)
                    except Exception as exc:
                        db.rollback()
                        task = db.get(BatchAnalysisTask, task_id)
                        summary = dict(task.result_summary or {})
                        source_stats = dict((summary.get("sources") or {}).get(source) or {})
                        source_stats["failed"] = (source_stats.get("failed") or 0) + 1
                        failures = list(task.failed_ids or [])
                        failures.append({"article_id": cls._stable_article_id(source, article), "source": source, "error": str(exc)})
                        task.failed_ids = failures
                        task.failed_count = len(failures)
                        task.processed_count = (task.processed_count or 0) + 1
                        cls._save_source_stats(task, summary, source, source_stats)
                        db.commit()

            task = db.get(BatchAnalysisTask, task_id)
            if task and task.status != "cancelled":
                # 当天样本过少时给出诚实预警：不放松"仅当天"规则（用前一天补量
                # 会与前一天已有的汇总冲突、破坏汇总幂等），只提示汇总可能偏差。
                summary = dict(task.result_summary or {})
                summary["assistant_calls"] = assistant_ctx["calls"]
                adopted_total = summary.get("adopted") or 0
                if not task.failed_count and adopted_total < 10:
                    summary["low_volume"] = True
                    summary["message"] = (
                        f"当天仅采纳 {adopted_total} 条观点（不足10条），市场样本偏少，"
                        f"汇总结果可能有偏差，可在发帖更活跃的时段重新抓取。"
                    )
                    logger.warning("[观点抓取] 当天样本不足：仅采纳 %d 条", adopted_total)
                task.result_summary = summary
                task.status = "failed" if task.failed_count else "succeeded"
                task.completed_at = datetime.now()
                db.commit()
        except Exception as exc:
            db.rollback()
            task = db.get(BatchAnalysisTask, task_id)
            if task and task.status != "cancelled":
                task.status = "failed"
                task.error_message = str(exc)
                task.error_stack = traceback.format_exc()
                task.completed_at = datetime.now()
                db.commit()
            logger.exception("观点抓取任务 %s 失败", task_id)
        finally:
            db.close()

    @staticmethod
    def _save_source_stats(task, summary, source, source_stats):
        sources = dict(summary.get("sources") or {})
        sources[source] = source_stats
        summary["sources"] = sources
        task.result_summary = summary

    @classmethod
    def _is_quality_viewpoint(cls, article: Dict[str, Any]) -> Tuple[bool, str]:
        """判断文章是否是值得入库的博主观点。返回 (是否通过, 原因)。"""
        title = str(article.get("title") or "").strip()
        content = str(article.get("content") or "").strip()
        # 列表页偶发 content 空/等于标题；用 title+content 合成后再判长度与关键词。
        text = f"{title}\n{content}".strip() if content and content != title else (content or title)
        text = str(text or "").strip()

        if len(text) < _MIN_CONTENT_LENGTH:
            return False, f"内容过短（{len(text)}字 < {_MIN_CONTENT_LENGTH}）"

        # content==title 通常表示详情抓取失败；已有长度与关键词门槛兜底，不再额外硬拒。
        for spam in _SPAM_KEYWORDS:
            if spam in text:
                return False, f"命中垃圾关键词：{spam}"

        if not any(kw in text for kw in _VIEWPOINT_KEYWORDS):
            return False, "未包含任何市场/观点关键词，疑似非观点内容"

        return True, "ok"

    @classmethod
    def _record_skipped(cls, db, task, summary, source, source_stats, reason, article_id):
        """记录一篇被跳过的文章（统计 + 心跳 commit），供各过滤环节共用。"""
        source_stats["skipped"] = (source_stats.get("skipped") or 0) + 1
        source_stats["skipped_reasons"] = source_stats.get("skipped_reasons") or {}
        source_stats["skipped_reasons"][reason] = (
            source_stats["skipped_reasons"].get(reason, 0) + 1
        )
        summary["skipped"] = (summary.get("skipped") or 0) + 1
        task.processed_count = (task.processed_count or 0) + 1
        processed = list(task.processed_ids or [])
        processed.append(article_id)
        task.processed_ids = processed
        cls._save_source_stats(task, summary, source, source_stats)
        db.commit()

    @classmethod
    def _process_article(cls, db, task_id, source, article, assistant_ctx=None):
        task = db.get(BatchAnalysisTask, task_id)
        article_id = cls._stable_article_id(source, article)
        summary = dict(task.result_summary or {})
        source_stats = dict((summary.get("sources") or {}).get(source) or {})
        existing = db.query(CrawlerArticleRecord).filter(
            CrawlerArticleRecord.article_id == article_id
        ).first()
        if existing:
            source_stats["duplicates"] = (source_stats.get("duplicates") or 0) + 1
            summary["duplicates"] = (summary.get("duplicates") or 0) + 1
            task.processed_count = (task.processed_count or 0) + 1
            cls._save_source_stats(task, summary, source, source_stats)
            db.commit()
            return

        # 只采纳当天发布的观点（北京时间）。三个源的列表页均按时间倒序，
        # 往日内容混进来会污染"当日市场观点"汇总，直接跳过并记原因。
        # 容忍轻度未来时间（<= 明天）：服务器时钟偏差时不要把整源误杀。
        article_day = cls._article_date(article)
        today = beijing_today()
        if article_day < today:
            logger.info(
                "[观点抓取] 跳过非当天观点 %s（发布于 %s）: %s",
                source, article_day.isoformat(), str(article.get("title") or "")[:40],
            )
            cls._record_skipped(
                db, task, summary, source, source_stats,
                "非当天观点", article_id,
            )
            return

        quality_ok, quality_reason = cls._is_quality_viewpoint(article)
        if not quality_ok:
            cls._record_skipped(
                db, task, summary, source, source_stats,
                quality_reason, article_id,
            )
            return

        # 辅助AI裁决（仅边界内容）：核心观点词零命中时才调用轻量模型。
        # 三重保险——调用上限、异常容错、拿不准放行：宁可放进列表（深度分析
        # 阶段还会二次剔除），也不能因为AI抖动误杀正常观点。
        if assistant_ctx and cls._needs_assistant_judge(article):
            keep = None
            if assistant_ctx["calls"] < assistant_ctx["cap"]:
                assistant_ctx["calls"] += 1
                try:
                    keep = assistant_ctx["judge"](article, source)
                except Exception as exc:
                    logger.warning("[观点抓取] 辅助AI裁决出错，按放行处理: %s", exc)
            else:
                logger.info("[观点抓取] 辅助AI调用达上限(%s次)，边界内容按放行处理", assistant_ctx["cap"])
            if keep is False:
                cls._record_skipped(
                    db, task, summary, source, source_stats,
                    "辅助AI判定非有效观点", article_id,
                )
                return

        record = CrawlerArticleRecord(
            article_id=article_id,
            source=source,
            title=str(article.get("title") or "")[:500],
            content_hash=cls._content_hash(article),
            url=article.get("url") or article.get("link"),
            author=article.get("author"),
            is_adopted=False,
            capture_score=0.0,
        )
        db.add(record)

        content_text = str(article.get("content") or article.get("title") or "").strip()
        title_text = str(article.get("title") or "").strip()
        viewpoint = Viewpoint(
            viewpoint_date=cls._article_date(article),
            content=content_text,
            # 未分析前也给列表页可用摘要，避免一直显示“待生成摘要”
            summary=(title_text or content_text)[:160] or None,
            author=str(article.get("author") or "未知"),
            source=source,
            article_id=article_id,
            article_url=record.url,
            content_hash=record.content_hash,
            source_authority=get_source_authority(source),
            is_deleted=False,
            is_summary=False,
            analysis_summary="pending",
        )
        db.add(viewpoint)
        db.flush()
        record.is_adopted = True
        record.viewpoint_id = viewpoint.id
        db.commit()

        source_stats["adopted"] = source_stats.get("adopted", 0) + 1
        summary["adopted"] = summary.get("adopted", 0) + 1
        task.success_count = (task.success_count or 0) + 1
        processed = list(task.processed_ids or [])
        processed.append(article_id)
        task.processed_ids = processed
        task.processed_count = (task.processed_count or 0) + 1
        cls._save_source_stats(task, summary, source, source_stats)
        db.commit()

    @classmethod
    def _run_deep_retries(
        cls,
        db: Session,
        task: BatchAnalysisTask,
        viewpoint_ids: List[int],
        deep_analyzer: Callable[[Dict[str, Any], str], Dict[str, Any]],
    ) -> None:
        """对已有 viewpoint 逐个补深度分析并写回，更新任务进度与状态。

        被 batch-analyze 后台任务和 retry_task(带 retry_viewpoint_ids) 共用。
        """
        ids = list(dict.fromkeys(int(vid) for vid in viewpoint_ids if vid))
        task.status = "running"
        task.started_at = task.started_at or datetime.now()
        if not task.total_count:
            task.total_count = len(ids)
        task.completed_at = None
        task.error_message = None
        task.failed_ids = list(task.failed_ids or [])
        db.commit()

        for vid in ids:
            task = db.get(BatchAnalysisTask, task.id)
            if not task or task.status in ("cancelled", "failed"):
                return
            viewpoint = db.get(Viewpoint, vid)
            if not viewpoint or viewpoint.is_deleted or viewpoint.is_summary:
                task.processed_count = (task.processed_count or 0) + 1
                db.commit()
                continue
            article = {
                "title": (viewpoint.content or "")[:200],
                "content": viewpoint.content or "",
                "author": viewpoint.author or "",
            }
            try:
                analysis = deep_analyzer(article, viewpoint.source or "manual")
                cls._apply_deep_analysis(viewpoint, analysis)
                task.success_count = (task.success_count or 0) + 1
                processed = list(task.processed_ids or [])
                processed.append(vid)
                task.processed_ids = processed
            except Exception as exc:
                db.rollback()
                task = db.get(BatchAnalysisTask, task.id)
                viewpoint = db.get(Viewpoint, vid)
                if viewpoint:
                    viewpoint.analysis_summary = f"failed:{str(exc)[:180]}"
                failures = list(task.failed_ids or [])
                failures.append({"viewpoint_id": vid, "source": viewpoint.source if viewpoint else None, "error": str(exc)})
                task.failed_ids = failures
                task.failed_count = len(failures)
            task.processed_count = (task.processed_count or 0) + 1
            db.commit()

        task = db.get(BatchAnalysisTask, task.id)
        if task and task.status != "cancelled":
            task.status = "failed" if task.failed_count else "succeeded"
            task.completed_at = datetime.now()
            db.commit()

    @staticmethod
    def retry_task(db: Session, task_id: int) -> BatchAnalysisTask:
        task = db.query(BatchAnalysisTask).filter(
            BatchAnalysisTask.id == task_id,
            BatchAnalysisTask.task_type.in_(("viewpoint_fetch", "viewpoint_summary", "viewpoint_batch")),
        ).with_for_update().first()
        if not task:
            raise ValueError("观点任务不存在")
        if task.status not in ("failed", "cancelled"):
            raise ValueError("只有失败或已取消的任务可以重试")
        params = dict(task.task_params or {})
        retry_ids = [item.get("viewpoint_id") for item in (task.failed_ids or []) if item.get("viewpoint_id")]
        if retry_ids:
            params["retry_viewpoint_ids"] = list(dict.fromkeys(retry_ids))
        task.task_params = params
        task.status = "pending"
        task.failed_ids = []
        task.failed_count = 0
        task.error_message = None
        task.error_stack = None
        task.completed_at = None
        task.processed_count = 0
        if not retry_ids:
            task.total_count = 0
        db.commit()
        db.refresh(task)
        return task

    @staticmethod
    def heal_stale_task(
        db: Session,
        task: Optional[BatchAnalysisTask],
        stale_after: timedelta = STALE_TASK_AFTER,
    ) -> bool:
        """把心跳超时的活跃任务自愈为 failed。返回是否发生了自愈。

        参考时间戳取 updated_at（每次进度 commit 都会经 onupdate 刷新），
        尊重运行中任务的心跳；绝不能误杀仍在推进的慢任务。
        自愈失败不得让高频轮询的 GET 端点 500，因此整段吞异常。
        """
        if task is None or task.status not in ("pending", "running"):
            return False
        reference = task.updated_at or task.started_at or task.created_at
        if not reference or datetime.now() - reference <= stale_after:
            return False
        try:
            task.status = "failed"
            task.error_message = "检测到进程中断（Render 重启/休眠），任务已自动标记失败，可点击重试"
            task.completed_at = datetime.now()
            db.commit()
            logger.warning(
                "观点任务 %s 心跳超时已自愈为 failed（最后更新 %s）", task.id, reference
            )
            return True
        except Exception:
            db.rollback()
            logger.exception("观点任务 %s 自愈失败", getattr(task, "id", None))
            return False

    @staticmethod
    def serialize_task(task: BatchAnalysisTask) -> Dict[str, Any]:
        total = task.total_count or 0
        processed = task.processed_count or 0
        summary = dict(task.result_summary or {})

        # 耗时与 ETA
        now = datetime.now()
        started = task.started_at or task.created_at or now
        completed = task.completed_at
        end_time = completed or now
        elapsed_seconds = round((end_time - started).total_seconds(), 1)
        eta_seconds = None
        if not completed and processed > 0 and total > 0:
            rate = processed / max(elapsed_seconds, 0.1)  # 项/秒
            remaining = max(total - processed, 0)
            eta_seconds = round(remaining / rate, 1)

        # 抓取阶段耗时(抓取模式/混合模式均记录)
        fetch_duration = summary.get("fetch_duration_seconds")

        return {
            "task_id": task.id,
            "task_type": task.task_type,
            "status": "succeeded" if task.status == "completed" else task.status,
            "total_count": total,
            "processed_count": processed,
            "success_count": task.success_count or 0,
            "failed_count": task.failed_count or 0,
            "progress": round(processed / total * 100, 1) if total else 0,
            "elapsed_seconds": elapsed_seconds,
            "eta_seconds": eta_seconds,
            "fetch_duration_seconds": fetch_duration,
            "result_summary": summary,
            "error_message": task.error_message,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "started_at": task.started_at.isoformat() if task.started_at else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        }

    @staticmethod
    def summarize_date(
        db: Session,
        target_date: date,
        *,
        summarizer: Optional[Callable[[List[Dict[str, Any]], str], Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        from src.analyzer.llm_analyzer import summarize_viewpoints_by_date

        summarizer = summarizer or summarize_viewpoints_by_date
        try:
            existing = db.query(Viewpoint).filter(
                Viewpoint.viewpoint_date == target_date,
                Viewpoint.is_summary.is_(True),
                Viewpoint.is_deleted.is_(False),
            ).with_for_update().first()
            if existing:
                return {
                    "success": True,
                    "summary_id": existing.id,
                    "deleted_originals": 0,
                    "already_summarized": True,
                }

            originals = db.query(Viewpoint).filter(
                Viewpoint.viewpoint_date == target_date,
                Viewpoint.is_summary.is_(False),
                Viewpoint.is_deleted.is_(False),
            ).with_for_update().all()
            if not originals:
                raise ValueError("该日期没有可汇总观点")
            incomplete = [row.id for row in originals if not row.market_direction or not row.summary or not row.reasoning]
            if incomplete:
                raise ValueError(f"仍有 {len(incomplete)} 条观点尚未完成深度分析")

            payload = [
                {
                    "id": row.id,
                    "summary": row.summary,
                    "market_direction": row.market_direction,
                    "confidence": row.confidence,
                    "sectors_bullish": row.sectors_bullish or [],
                    "sectors_bearish": row.sectors_bearish or [],
                    "source": row.source,
                }
                for row in originals
            ]
            result = summarizer(payload, target_date.isoformat())
            if not result or result.get("success") is False:
                raise RuntimeError((result or {}).get("error") or "观点汇总失败")

            original_ids = [row.id for row in originals]
            summary = Viewpoint(
                viewpoint_date=target_date,
                source="daily_summary",
                author="系统汇总",
                content=result.get("content") or "",
                summary=(result.get("content") or "")[:200],
                market_direction=result.get("market_direction") or "neutral",
                confidence=int(result.get("confidence") or 50),
                topics=result.get("topics") or [],
                sectors_bullish=result.get("sectors_bullish") or [],
                sectors_bearish=result.get("sectors_bearish") or [],
                reasoning=result.get("reasoning") or "",
                is_summary=True,
                original_count=len(originals),
                original_ids=original_ids,
                credibility_score=75,
                weight=1.0,
                source_authority=1.0,
                time_horizon="short",
                validity_period="7天",
                valid_until=target_date + timedelta(days=7),
                is_deleted=False,
            )
            db.add(summary)
            db.flush()
            db.query(CrawlerArticleRecord).filter(
                CrawlerArticleRecord.viewpoint_id.in_(original_ids)
            ).update({CrawlerArticleRecord.viewpoint_id: summary.id}, synchronize_session=False)
            deleted = db.query(Viewpoint).filter(Viewpoint.id.in_(original_ids)).delete(synchronize_session=False)
            db.commit()
            db.refresh(summary)
            return {
                "success": True,
                "summary_id": summary.id,
                "deleted_originals": deleted,
                "crawler_records_relinked": db.query(CrawlerArticleRecord).filter(
                    CrawlerArticleRecord.viewpoint_id == summary.id
                ).count(),
                "already_summarized": False,
            }
        except Exception:
            db.rollback()
            raise

    @classmethod
    def summarize_pending_dates(cls, db: Session) -> Dict[str, Any]:
        dates = [
            row[0]
            for row in db.query(Viewpoint.viewpoint_date).filter(
                # 按北京日期界定"往日"：与抓取的同日过滤同一基准，
                # 避免 UTC 凌晨把北京时间的当天观点提前送去汇总。
                Viewpoint.viewpoint_date < beijing_today(),
                Viewpoint.is_summary.is_(False),
                Viewpoint.is_deleted.is_(False),
            ).distinct().order_by(Viewpoint.viewpoint_date.asc()).all()
        ]
        completed = []
        skipped = []
        for target in dates:
            try:
                completed.append(cls.summarize_date(db, target))
            except ValueError as exc:
                skipped.append({"date": target.isoformat(), "reason": str(exc)})
        return {"success": True, "completed": completed, "skipped": skipped}

    @classmethod
    def run_daily_summary_task(
        cls,
        *,
        session_factory: Callable[[], Session] = SessionLocal,
    ) -> Dict[str, Any]:
        """创建可审计的每日汇总任务，并同步执行一次。"""
        db = session_factory()
        task = None
        try:
            today = date.today()
            latest = db.query(BatchAnalysisTask).filter(
                BatchAnalysisTask.task_type == "viewpoint_summary",
            ).order_by(BatchAnalysisTask.created_at.desc()).first()
            if latest and latest.created_at and latest.created_at.date() == today and latest.status == "succeeded":
                # 仅当上次实际汇总过(completed 非空)才视为"已完成"; 全被跳过则允许重试
                prev_completed = (latest.result_summary or {}).get("completed") or []
                if prev_completed:
                    return {"success": True, "already_completed": True, "task_id": latest.id, **(latest.result_summary or {})}
                task = latest
            else:
                task = latest if latest and latest.created_at and latest.created_at.date() == today else None
            if task is None:
                task = BatchAnalysisTask(
                    task_type="viewpoint_summary",
                    status="pending",
                    total_count=0,
                    processed_count=0,
                    success_count=0,
                    failed_count=0,
                    processed_ids=[],
                    failed_ids=[],
                    task_params={"run_date": today.isoformat()},
                    result_summary={},
                )
                db.add(task)
                db.commit()
                db.refresh(task)
            task.status = "running"
            task.started_at = task.started_at or datetime.now()
            task.completed_at = None
            db.commit()

            result = cls.summarize_pending_dates(db)
            task = db.get(BatchAnalysisTask, task.id)
            completed = list(result.get("completed") or [])
            skipped = list(result.get("skipped") or [])
            task.total_count = len(completed) + len(skipped)
            task.processed_count = task.total_count
            task.success_count = len(completed)
            task.failed_count = 0
            task.status = "succeeded"
            task.result_summary = result
            task.completed_at = datetime.now()
            db.commit()
            return {"success": True, "task_id": task.id, **result}
        except Exception as exc:
            db.rollback()
            if task is not None:
                task = db.get(BatchAnalysisTask, task.id)
                if task:
                    task.status = "failed"
                    task.failed_count = 1
                    task.error_message = str(exc)
                    task.error_stack = traceback.format_exc()
                    task.completed_at = datetime.now()
                    db.commit()
            return {"success": False, "task_id": task.id if task else None, "error": str(exc)}
        finally:
            db.close()
