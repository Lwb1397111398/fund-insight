# -*- coding: utf-8 -*-
"""板块 AI 批量匹配的后台任务状态。

为什么要异步：一次真实跑批实测单板块 4-25 秒（LLM + 多只基金抓站验证），169 个板块
同步跑必然超时；而且线上是 Render 免费实例，长请求还会拖死前端。
照 prediction_verify_task 的"进程内状态 + 前端轮询"范式做，并且带上一次性入口
`run_sync` 供脚本/定时任务复用同一套逻辑。
"""
import logging
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# 超过这个秒数没有任何进展就认为线程已被杀（Render 重启/异常退出），
# 否则会出现"永远 running 挡住后续跑批"的老毛病。
STALE_AFTER_SECONDS = 600


class SectorAiMatchTask:
    def __init__(self, sectors: List[str], apply: bool, run_id: str):
        self.sectors = list(sectors)
        self.apply = apply
        self.run_id = run_id
        self.status = 'pending'          # pending|running|completed|failed|cancelled
        self.total = len(self.sectors)
        self.done = 0
        self.counts = {'matched': 0, 'proxy': 0, 'needs_review': 0, 'conflict': 0, 'no_fund': 0}
        self.results: List[dict] = []
        self.current_sector: Optional[str] = None
        self.error: Optional[str] = None
        self.started_at: Optional[datetime] = None
        self.finished_at: Optional[datetime] = None
        self.last_progress_at = time.monotonic()
        self._lock = threading.Lock()

    def touch(self):
        with self._lock:
            self.last_progress_at = time.monotonic()

    def is_stale(self) -> bool:
        return (self.status == 'running'
                and time.monotonic() - self.last_progress_at > STALE_AFTER_SECONDS)

    def to_public_dict(self) -> Dict:
        return {
            'status': 'stale' if self.is_stale() else self.status,
            'run_id': self.run_id,
            'apply': self.apply,
            'total': self.total,
            'done': self.done,
            'counts': dict(self.counts),
            'current_sector': self.current_sector,
            'error': self.error,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'finished_at': self.finished_at.isoformat() if self.finished_at else None,
            'idle_seconds': round(time.monotonic() - self.last_progress_at, 1),
            'results': self.results if self.status != 'running' else self.results[-20:],
        }


class SectorAiMatchManager:
    def __init__(self):
        self._task: Optional[SectorAiMatchTask] = None
        self._thread: Optional[threading.Thread] = None
        # 必须可重入：start() 持锁期间会调用 is_running()，用普通 Lock 会自锁死
        self._lock = threading.RLock()

    @property
    def current(self) -> Optional[SectorAiMatchTask]:
        task = self._task
        if task and task.is_stale():
            task.status = 'failed'
            task.error = '任务超时未推进（进程可能重启过），可重新发起'
        return task

    def is_running(self) -> bool:
        task = self.current
        with self._lock:
            return bool(task and task.status == 'running'
                        and self._thread and self._thread.is_alive())

    def status(self) -> Dict:
        task = self.current
        return task.to_public_dict() if task else {'status': 'idle'}

    def start(self, sectors: List[str], apply: bool = True, run_id: Optional[str] = None) -> Dict:
        if not sectors:
            return {'success': False, 'message': '没有需要处理的板块'}
        with self._lock:
            if self.is_running():
                return {'success': False, 'message': '已有批量匹配在跑，请等它结束',
                        'data': self._task.to_public_dict()}
            run_id = run_id or datetime.now().strftime('%Y%m%d%H%M%S')
            task = SectorAiMatchTask(sectors, apply, run_id)
            self._task = task
            self._thread = threading.Thread(target=self._run, args=(task,), daemon=True)
            self._thread.start()
        return {'success': True, 'data': task.to_public_dict()}

    def _run(self, task: SectorAiMatchTask):
        from src.models.database import SessionLocal
        from src.services.sector_fund_agent import resolve_sector_fund, apply_decision

        task.status = 'running'
        task.started_at = datetime.now()
        task.touch()
        db = SessionLocal()
        try:
            for sector in task.sectors:
                task.current_sector = sector
                try:
                    decision = resolve_sector_fund(sector, budget_ms=45000, db=db)
                    applied = apply_decision(db, decision) if task.apply else {'applied': False}
                    key = decision.status if decision.status in task.counts else 'needs_review'
                    task.counts[key] += 1
                    task.results.append({
                        'sector': sector,
                        'status': decision.status,
                        'confidence': round(decision.confidence, 3),
                        'fund_code': decision.chosen.code if decision.chosen else None,
                        'fund_name': decision.chosen.display_name if decision.chosen else None,
                        'match_kind': (decision.chosen.t3_proxy and 'proxy') or 'direct'
                        if decision.chosen else None,
                        'applied': bool(applied.get('applied')),
                        'note': applied.get('reason') or '',
                        'elapsed_ms': decision.elapsed_ms,
                        'degraded': decision.degraded,
                        'timed_out': decision.timed_out,
                    })
                except Exception as exc:
                    logger.exception('[AI批量匹配] 板块 %s 失败', sector)
                    db.rollback()
                    task.counts['needs_review'] += 1
                    task.results.append({'sector': sector, 'status': 'error',
                                         'note': str(exc)[:200]})
                task.done += 1
                task.touch()
            task.status = 'completed'
        except Exception as exc:
            logger.exception('[AI批量匹配] 任务异常终止')
            task.status = 'failed'
            task.error = str(exc)[:500]
        finally:
            task.current_sector = None
            task.finished_at = datetime.now()
            db.close()


_manager: Optional[SectorAiMatchManager] = None


def get_sector_ai_match_manager() -> SectorAiMatchManager:
    global _manager
    if _manager is None:
        _manager = SectorAiMatchManager()
    return _manager
