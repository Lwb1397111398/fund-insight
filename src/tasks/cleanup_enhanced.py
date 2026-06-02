"""
清理功能增强模块
包含：软删除机制、清理日志、规则配置、异步执行、失败重试、熔断机制
"""
import os
import uuid
import json
import time
import logging
import threading
from typing import Dict, List, Optional, Any, Callable
from datetime import datetime, date, timedelta
from dataclasses import dataclass, field, asdict
from enum import Enum
from functools import wraps
from contextlib import contextmanager

if __name__ == "__main__":
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if project_root not in os.sys.path:
        os.sys.path.insert(0, project_root)

from sqlalchemy.orm import Session
from sqlalchemy import and_, or_, func

from src.models.database import (
    SessionLocal, Prediction, Viewpoint, Post,
    AnalysisLog, CrawlerArticleRecord, FundInfo
)

logger = logging.getLogger(__name__)


class CleanupAction(Enum):
    SOFT_DELETE = "soft_delete"
    HARD_DELETE = "hard_delete"
    SKIP = "skip"
    RESTORE = "restore"


class CleanupTrigger(Enum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    API = "api"


class CleanupStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"


@dataclass
class CleanupRule:
    data_type: str
    source: str = "all"
    importance: str = "normal"
    retention_days: int = 7
    soft_delete_days: int = 7
    enabled: bool = True
    
    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class CleanupResult:
    success: bool
    action: str
    data_type: str
    data_id: int
    reason: str = ""
    can_restore: bool = True
    restore_before: date = None


DEFAULT_CLEANUP_RULES = {
    "prediction_manual": CleanupRule("prediction", "manual", "high", 30, 7),
    "prediction_auto": CleanupRule("prediction", "auto", "normal", 14, 7),
    "prediction_verified_success": CleanupRule("prediction", "verified_success", "high", 60, 7),
    "prediction_verified_failed": CleanupRule("prediction", "verified_failed", "low", 7, 3),
    "viewpoint_manual": CleanupRule("viewpoint", "manual", "high", 30, 7),
    "viewpoint_vip": CleanupRule("viewpoint", "vip", "high", 14, 7),
    "viewpoint_crawler": CleanupRule("viewpoint", "crawler", "normal", 7, 3),
    "viewpoint_high_credibility": CleanupRule("viewpoint", "high_credibility", "high", 21, 7),
    "trend_core": CleanupRule("trend_analysis", "core", "high", 365, 14),
    "trend_normal": CleanupRule("trend_analysis", "normal", "normal", 180, 7),
    "analysis_log": CleanupRule("analysis_log", "all", "low", 90, 3),
    "crawler_record": CleanupRule("crawler_record", "all", "low", 30, 3),
}


class CleanupRuleManager:
    """清理规则管理器"""
    
    def __init__(self, db: Session = None):
        self.db = db or SessionLocal()
        self._rules: Dict[str, CleanupRule] = {}
        self._load_rules()
    
    def _load_rules(self):
        self._rules = DEFAULT_CLEANUP_RULES.copy()
    
    def get_rule(self, data_type: str, source: str = "all", importance: str = "normal") -> CleanupRule:
        key = f"{data_type}_{source}"
        if key in self._rules:
            return self._rules[key]
        
        key = f"{data_type}_normal"
        if key in self._rules:
            return self._rules[key]
        
        return CleanupRule(data_type, source, importance, 7, 7)
    
    def get_retention_days(self, data_type: str, source: str = "all", 
                           importance: str = "normal", status: str = None) -> int:
        rule = self.get_rule(data_type, source, importance)
        retention = rule.retention_days
        
        if data_type == "prediction" and status:
            if status == "success":
                retention = int(retention * 2.0)
            elif status == "failed":
                retention = int(retention * 0.5)
            elif status == "expired":
                retention = int(retention * 0.3)
        
        return retention
    
    def update_rule(self, key: str, rule: CleanupRule):
        self._rules[key] = rule
    
    def get_all_rules(self) -> Dict[str, CleanupRule]:
        return self._rules.copy()


class CircuitBreaker:
    """熔断器"""
    
    def __init__(self, failure_threshold: int = 3, recovery_timeout: int = 1800):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.last_failure_time = 0
        self.state = "closed"
        self._lock = threading.Lock()
    
    def record_failure(self):
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.failure_count >= self.failure_threshold:
                self.state = "open"
                logger.warning(f"[CircuitBreaker] 熔断器打开，连续失败 {self.failure_count} 次")
    
    def record_success(self):
        with self._lock:
            self.failure_count = 0
            self.state = "closed"
    
    def can_execute(self) -> bool:
        with self._lock:
            if self.state == "closed":
                return True
            if self.state == "open":
                if time.time() - self.last_failure_time >= self.recovery_timeout:
                    self.state = "half_open"
                    logger.info("[CircuitBreaker] 熔断器进入半开状态")
                    return True
                return False
            return True


class RetryPolicy:
    """重试策略"""
    
    def __init__(self, max_retries: int = 3, base_delay: float = 1.0):
        self.max_retries = max_retries
        self.base_delay = base_delay
    
    def get_delay(self, attempt: int) -> float:
        return self.base_delay * (2 ** attempt)
    
    def should_retry(self, attempt: int, error: Exception) -> bool:
        if attempt >= self.max_retries:
            return False
        
        non_retryable_errors = (ValueError, PermissionError)
        if isinstance(error, non_retryable_errors):
            return False
        
        return True


class BatchExecutor:
    """批次执行器"""
    
    def __init__(self, batch_size: int = 50, batch_delay: float = 0.1, 
                 timeout: float = 30.0, retry_policy: RetryPolicy = None):
        self.batch_size = batch_size
        self.batch_delay = batch_delay
        self.timeout = timeout
        self.retry_policy = retry_policy or RetryPolicy()
        self.circuit_breaker = CircuitBreaker()
    
    def execute_batch(self, items: List[Any], processor: Callable, 
                      on_progress: Callable = None) -> Dict:
        results = {
            'total': len(items),
            'success': 0,
            'failed': 0,
            'skipped': 0,
            'errors': []
        }
        
        for i in range(0, len(items), self.batch_size):
            if not self.circuit_breaker.can_execute():
                logger.warning("[BatchExecutor] 熔断器开启，暂停执行")
                break
            
            batch = items[i:i + self.batch_size]
            
            for item in batch:
                for attempt in range(self.retry_policy.max_retries + 1):
                    try:
                        result = processor(item)
                        if result:
                            results['success'] += 1
                        else:
                            results['skipped'] += 1
                        self.circuit_breaker.record_success()
                        break
                    except Exception as e:
                        if self.retry_policy.should_retry(attempt, e):
                            delay = self.retry_policy.get_delay(attempt)
                            logger.warning(f"[BatchExecutor] 重试 {attempt + 1}/{self.retry_policy.max_retries}: {e}")
                            time.sleep(delay)
                        else:
                            results['failed'] += 1
                            results['errors'].append({
                                'item': str(item),
                                'error': str(e)
                            })
                            self.circuit_breaker.record_failure()
                            break
            
            if on_progress:
                on_progress(i + len(batch), len(items))
            
            if i + self.batch_size < len(items):
                time.sleep(self.batch_delay)
        
        return results


class SoftDeleteManager:
    """软删除管理器"""
    
    TRASH_RETENTION_DAYS = 7
    
    def __init__(self, db: Session = None):
        self.db = db or SessionLocal()
    
    def soft_delete(self, model_class, item_id: int, reason: str = "", 
                    deleted_by: str = "system") -> bool:
        try:
            item = self.db.query(model_class).filter(model_class.id == item_id).first()
            if not item:
                return False
            
            if hasattr(item, 'is_deleted') and item.is_deleted:
                return True
            
            if hasattr(item, 'is_deleted'):
                item.is_deleted = True
            if hasattr(item, 'deleted_at'):
                item.deleted_at = datetime.now()
            if hasattr(item, 'deleted_by'):
                item.deleted_by = deleted_by
            if hasattr(item, 'delete_reason'):
                item.delete_reason = reason
            if hasattr(item, 'restore_before'):
                item.restore_before = date.today() + timedelta(days=self.TRASH_RETENTION_DAYS)
            
            self.db.commit()
            return True
        except Exception as e:
            self.db.rollback()
            logger.error(f"[SoftDelete] 软删除失败: {e}")
            raise
    
    def restore(self, model_class, item_id: int) -> bool:
        try:
            item = self.db.query(model_class).filter(model_class.id == item_id).first()
            if not item:
                return False
            
            if hasattr(item, 'is_deleted'):
                item.is_deleted = False
            if hasattr(item, 'deleted_at'):
                item.deleted_at = None
            if hasattr(item, 'deleted_by'):
                item.deleted_by = None
            if hasattr(item, 'delete_reason'):
                item.delete_reason = None
            if hasattr(item, 'restore_before'):
                item.restore_before = None
            
            self.db.commit()
            return True
        except Exception as e:
            self.db.rollback()
            logger.error(f"[SoftDelete] 恢复失败: {e}")
            raise
    
    def hard_delete(self, model_class, item_id: int) -> bool:
        try:
            item = self.db.query(model_class).filter(model_class.id == item_id).first()
            if not item:
                return False
            
            self.db.delete(item)
            self.db.commit()
            return True
        except Exception as e:
            self.db.rollback()
            logger.error(f"[SoftDelete] 硬删除失败: {e}")
            raise
    
    def get_trash_items(self, model_class, limit: int = 100) -> List[Any]:
        query = self.db.query(model_class)
        
        if hasattr(model_class, 'is_deleted'):
            query = query.filter(model_class.is_deleted == True)
        
        if hasattr(model_class, 'restore_before'):
            query = query.filter(model_class.restore_before >= date.today())
        
        return query.order_by(model_class.deleted_at.desc()).limit(limit).all()
    
    def cleanup_expired_trash(self, model_class) -> int:
        if not hasattr(model_class, 'restore_before'):
            return 0
        
        cutoff_date = date.today()
        expired_items = self.db.query(model_class).filter(
            model_class.is_deleted == True,
            model_class.restore_before < cutoff_date
        ).all()
        
        count = 0
        for item in expired_items:
            try:
                self.db.delete(item)
                count += 1
            except Exception as e:
                logger.error(f"[SoftDelete] 清理回收站失败: {e}")
        
        self.db.commit()
        return count


class CleanupProgress:
    """清理进度跟踪"""
    
    def __init__(self, task_id: str, total: int = 0):
        self.task_id = task_id
        self.total = total
        self.current = 0
        self.status = CleanupStatus.PENDING.value
        self.start_time = None
        self.end_time = None
        self.errors: List[str] = []
        self._lock = threading.Lock()
    
    def start(self):
        with self._lock:
            self.status = CleanupStatus.RUNNING.value
            self.start_time = datetime.now()
    
    def update(self, current: int):
        with self._lock:
            self.current = current
    
    def complete(self, success: bool = True):
        with self._lock:
            self.status = CleanupStatus.COMPLETED.value if success else CleanupStatus.FAILED.value
            self.end_time = datetime.now()
    
    def add_error(self, error: str):
        with self._lock:
            self.errors.append(error)
    
    def to_dict(self) -> Dict:
        with self._lock:
            progress = (self.current / self.total * 100) if self.total > 0 else 0
            return {
                'task_id': self.task_id,
                'status': self.status,
                'progress': round(progress, 1),
                'current': self.current,
                'total': self.total,
                'start_time': self.start_time.isoformat() if self.start_time else None,
                'end_time': self.end_time.isoformat() if self.end_time else None,
                'errors': self.errors[-10:] if self.errors else [],
                'duration': str(self.end_time - self.start_time) if self.end_time and self.start_time else None
            }


_cleanup_progress_store: Dict[str, CleanupProgress] = {}


def get_progress(task_id: str) -> Optional[CleanupProgress]:
    return _cleanup_progress_store.get(task_id)


def create_progress(total: int = 0) -> CleanupProgress:
    task_id = str(uuid.uuid4())[:8]
    progress = CleanupProgress(task_id, total)
    _cleanup_progress_store[task_id] = progress

    # 防止内存泄漏：超过 100 条时删除最旧的条目
    if len(_cleanup_progress_store) > 100:
        sorted_items = sorted(
            _cleanup_progress_store.items(),
            key=lambda x: x[1].end_time or x[1].start_time or datetime.min
        )
        for old_task_id, _ in sorted_items[:len(_cleanup_progress_store) - 100]:
            del _cleanup_progress_store[old_task_id]

    return progress


def cleanup_old_progress(max_age_hours: int = 24):
    cutoff = datetime.now() - timedelta(hours=max_age_hours)
    to_remove = []
    for task_id, progress in _cleanup_progress_store.items():
        if progress.end_time and progress.end_time < cutoff:
            to_remove.append(task_id)
    for task_id in to_remove:
        del _cleanup_progress_store[task_id]


get_rule_manager = lambda db=None: CleanupRuleManager(db)
get_soft_delete_manager = lambda db=None: SoftDeleteManager(db)
