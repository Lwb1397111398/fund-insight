"""
并发分析工具
提供线程安全的并发分析能力
"""
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Callable, Any
from dataclasses import dataclass, field
import time
import logging

logger = logging.getLogger(__name__)


@dataclass
class AnalysisProgress:
    """分析进度（线程安全）"""
    total: int = 0
    completed: int = 0
    failed: int = 0
    in_progress: int = 0
    results: List[Dict] = field(default_factory=list)
    errors: List[Dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def increment_completed(self):
        with self._lock:
            self.completed += 1
            self.in_progress -= 1

    def increment_failed(self):
        with self._lock:
            self.failed += 1
            self.in_progress -= 1

    def increment_in_progress(self):
        with self._lock:
            self.in_progress += 1

    def add_result(self, result: Dict):
        with self._lock:
            self.results.append(result)

    def add_error(self, error: Dict):
        with self._lock:
            self.errors.append(error)


class ConcurrentAnalyzer:
    """
    并发分析器

    特点：
    1. 线程安全的进度跟踪
    2. 可控制的并发数
    3. 独立的错误处理
    4. 实时进度反馈
    5. 线程安全的单例模式
    6. 安全的数据库会话管理
    """

    _instance = None
    _instance_lock = threading.Lock()
    _init_lock = threading.Lock()

    def __new__(cls, max_workers: int = 3):
        # 使用类级别的锁保护单例创建
        if cls._instance is None:
            with cls._instance_lock:
                # 双重检查锁定模式
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._initialized = False
                    cls._instance = instance
        return cls._instance

    def __init__(self, max_workers: int = 3):
        # 防止重复初始化
        with self._init_lock:
            if self._initialized:
                return
            self._initialized = True
            self.max_workers = min(max_workers, 5)
            self._task_locks = {}
            self._active_tasks = set()
    
    def analyze_batch(
        self,
        items: List[Any],
        analyze_func: Callable[[Any], Dict],
        on_progress: Callable[[AnalysisProgress], None] = None,
        delay: float = 0.3
    ) -> AnalysisProgress:
        """
        并发批量分析
        
        Args:
            items: 待分析的项目列表
            analyze_func: 分析函数，接收一个项目，返回分析结果
            on_progress: 进度回调函数
            delay: 每个任务之间的延迟（秒），用于避免API限流
            
        Returns:
            分析进度对象
        """
        progress = AnalysisProgress(total=len(items))

        if not items:
            return progress

        def process_item(item: Any, index: int) -> Dict:
            """处理单个项目"""
            progress.increment_in_progress()

            try:
                time.sleep(delay * index % self.max_workers)

                result = analyze_func(item)

                progress.increment_completed()
                if result:
                    progress.add_result(result)

                if on_progress:
                    on_progress(progress)

                return result

            except Exception as e:
                progress.increment_failed()
                progress.add_error({
                    "item": str(item)[:100] if hasattr(item, '__str__') else f"item_{index}",
                    "error": str(e)
                })

                if on_progress:
                    on_progress(progress)

                # 记录异常但不吞掉，让调用者知道
                logger.error(f"处理项目 {index} 失败: {e}", exc_info=True)
                return None

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(process_item, item, i): i
                for i, item in enumerate(items)
            }

            # 正确处理所有 futures 的异常
            for future in as_completed(futures):
                index = futures[future]
                try:
                    future.result()
                except Exception as e:
                    # 记录未捕获的异常
                    logger.error(f"任务 {index} 发生未预期的错误: {e}", exc_info=True)
                    # 确保失败计数准确
                    progress.increment_failed()

        return progress
    
    def analyze_posts_concurrent(
        self,
        posts: List[Dict],
        db_session_factory: Callable,
        delay: float = 0.5
    ) -> AnalysisProgress:
        """
        并发分析帖子
        
        Args:
            posts: 帖子列表
            db_session_factory: 数据库会话工厂函数
            delay: 延迟时间
            
        Returns:
            分析进度
        """
        from src.analyzer.llm_analyzer import get_analyzer
        from src.constants import get_fund_for_sector
        from src.models.database import Prediction, Post
        
        def analyze_single_post(post_data: Dict) -> Dict:
            """分析单个帖子（线程安全）"""
            post_id = post_data.get("id")
            db = db_session_factory()
            try:
                llm_analyzer = get_analyzer()

                result = llm_analyzer.analyze_post(
                    title=post_data.get("title", ""),
                    content=post_data.get("content", ""),
                    post_date=post_data.get("post_date")
                )

                blogger_id = post_data.get("blogger_id")

                # 使用 with_for_update() 锁定行，防止并发写入冲突
                db_post = db.query(Post).filter(Post.id == post_id).with_for_update().first()
                if db_post:
                    # 检查是否已被其他线程分析过（避免重复分析）
                    if db_post.analyzed:
                        logger.info(f"帖子 {post_id} 已被其他线程分析，跳过")
                        db.rollback()
                        return {
                            "post_id": post_id,
                            "success": True,
                            "predictions_count": 0,
                            "skipped": True,
                            "reason": "already_analyzed"
                        }

                    db_post.analyzed = True
                    db_post.analysis_result = result

                    for pred in result.get("predictions", []):
                        sector = pred.get("sector", "")

                        fund_code, fund_name = None, None
                        if sector:
                            try:
                                fund_info = get_fund_for_sector(sector)
                                if fund_info:
                                    fund_code = fund_info.get("fund_code")
                                    fund_name = fund_info.get("fund_name")
                            except Exception:
                                pass

                        prediction = Prediction(
                            post_id=post_id,
                            blogger_id=blogger_id,
                            fund_code=fund_code,
                            fund_name=fund_name,
                            sector=sector,
                            sector_type=pred.get("sector_type", "其他"),
                            prediction_type=pred.get("prediction_type"),
                            prediction_content=pred.get("prediction_content"),
                            confidence=pred.get("confidence", 50),
                            prediction_date=db_post.post_date,
                            prediction_period=pred.get("prediction_period", "1周"),
                            target_date=llm_analyzer.calculate_target_date(
                                db_post.post_date,
                                pred.get("prediction_period", "1周")
                            ),
                            next_verify_date=llm_analyzer.calculate_next_verify_date(
                                db_post.post_date,
                                llm_analyzer.calculate_target_date(
                                    db_post.post_date,
                                    pred.get("prediction_period", "1周")
                                )
                            )
                        )
                        db.add(prediction)

                    db.commit()
                    logger.info(f"帖子 {post_id} 分析完成，生成 {len(result.get('predictions', []))} 个预测")

                return {
                    "post_id": post_id,
                    "success": True,
                    "predictions_count": len(result.get("predictions", []))
                }

            except Exception as e:
                db.rollback()
                logger.error(f"分析帖子 {post_id} 失败: {e}", exc_info=True)
                return {
                    "post_id": post_id,
                    "success": False,
                    "error": str(e)
                }
            finally:
                db.close()
        
        return self.analyze_batch(posts, analyze_single_post, delay=delay)
    
    def analyze_viewpoints_concurrent(
        self,
        viewpoints: List[Dict],
        db_session_factory: Callable,
        delay: float = 0.5
    ) -> AnalysisProgress:
        """
        并发分析观点
        
        Args:
            viewpoints: 观点列表
            db_session_factory: 数据库会话工厂函数
            delay: 延迟时间
            
        Returns:
            分析进度
        """
        from src.analyzer.viewpoint_analyzer import get_viewpoint_analyzer
        from src.analyzer.llm_analyzer import get_analyzer as get_llm_analyzer
        from datetime import date
        
        def analyze_single_viewpoint(vp_data: Dict) -> Dict:
            """分析单个观点（线程安全）"""
            viewpoint_id = vp_data.get("id")
            db = db_session_factory()
            try:
                analyzer = get_viewpoint_analyzer()
                llm_analyzer = get_llm_analyzer()

                result = analyzer.analyze_viewpoint(
                    title=vp_data.get("content", "")[:100],
                    content=vp_data.get("content", ""),
                    author=vp_data.get("author", ""),
                    source=vp_data.get("source", "")
                )

                time_horizon = result.get('time_horizon', 'medium')
                validity_map = {
                    'short': '1周',
                    'medium': '1个月',
                    'long': '3个月'
                }
                validity_period = validity_map.get(time_horizon, '1个月')

                valid_until = llm_analyzer.calculate_target_date(
                    date.today(),
                    validity_period
                )

                from src.services.viewpoint_service import ViewpointService
                service = ViewpointService(db)

                reasoning = f"【AI深度分析】{result.get('analysis', '')}\n\n【判断理由】{result.get('reasoning', '')}"

                service.update_viewpoint_analysis(
                    viewpoint_id=viewpoint_id,
                    market_direction=result.get('market_direction', 'neutral'),
                    confidence=result.get('confidence', 50),
                    sectors_bullish=result.get('sectors_bullish', []),
                    sectors_bearish=result.get('sectors_bearish', []),
                    reasoning=reasoning,
                    time_horizon=time_horizon,
                    validity_period=validity_period,
                    valid_until=valid_until,
                    summary=result.get('summary', ''),
                    credibility=result.get('credibility', 50),
                    key_points=result.get('key_points', []),
                    action_suggestion=result.get('action_suggestion', '观望'),
                    risk_level=result.get('risk_level', 'medium'),
                    sentiment_score=result.get('sentiment_score', 0.5)
                )

                logger.info(f"观点 {viewpoint_id} 分析完成")
                return {
                    "viewpoint_id": viewpoint_id,
                    "success": True
                }

            except Exception as e:
                db.rollback()
                logger.error(f"分析观点 {viewpoint_id} 失败: {e}", exc_info=True)
                return {
                    "viewpoint_id": viewpoint_id,
                    "success": False,
                    "error": str(e)
                }
            finally:
                db.close()
        
        return self.analyze_batch(viewpoints, analyze_single_viewpoint, delay=delay)


_concurrent_analyzer = None
_concurrent_analyzer_lock = threading.Lock()


def get_concurrent_analyzer(max_workers: int = 3) -> ConcurrentAnalyzer:
    """获取并发分析器单例（线程安全）"""
    global _concurrent_analyzer
    if _concurrent_analyzer is None:
        with _concurrent_analyzer_lock:
            if _concurrent_analyzer is None:
                _concurrent_analyzer = ConcurrentAnalyzer(max_workers)
    return _concurrent_analyzer
