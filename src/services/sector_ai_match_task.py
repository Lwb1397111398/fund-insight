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
PACE_SECONDS = 2.0            # 板块之间的限速间隔，避免把 LLM 侧打到 429 熔断
RETRY_ROUNDS = 2              # "LLM 不可用"的板块最多再重试两轮
RETRY_BACKOFF_SECONDS = 120   # 每轮重试前等待，给熔断器恢复时间


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
        self.retry_round = 0
        self.pending_retry: List[str] = []
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
            'retry_round': self.retry_round,
            'pending_retry': len(self.pending_retry),
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
        # 实测：连续快跑会触发 LLM 侧 429 → 熔断器开启 → 后面几十个板块全部"判定不可用"。
        # 所以每板块之间要限速，并且把"LLM 不可用"的板块留到后面的退避轮次重试，
        # 让它们和"确实找不到对口基金"区分开。
        pending = list(task.sectors)
        results: dict = {}
        try:
            for attempt in range(1, RETRY_ROUNDS + 2):
                if not pending:
                    break
                if attempt > 1:
                    task.retry_round = attempt - 1
                    logger.info('[AI批量匹配] 第 %d 轮重试 %d 个 LLM 不可用的板块',
                                attempt - 1, len(pending))
                    time.sleep(RETRY_BACKOFF_SECONDS)
                retry_next = []
                for sector in pending:
                    task.current_sector = sector
                    try:
                        decision = resolve_sector_fund(sector, budget_ms=45000, db=db)
                        applied = apply_decision(db, decision) if task.apply \
                            else {'applied': False}
                        results[sector] = {
                            'sector': sector,
                            'status': decision.status,
                            'confidence': round(decision.confidence, 3),
                            'fund_code': decision.chosen.code if decision.chosen else None,
                            'fund_name': decision.chosen.display_name if decision.chosen else None,
                            'match_kind': ('proxy' if (decision.chosen and decision.chosen.t3_proxy)
                                           else ('direct' if decision.chosen else None)),
                            'applied': bool(applied.get('applied')),
                            'note': applied.get('reason') or '',
                            'elapsed_ms': decision.elapsed_ms,
                            'degraded': decision.degraded,
                            'llm_unavailable': decision.llm_unavailable,
                            'timed_out': decision.timed_out,
                            'attempts': attempt,
                        }
                        if decision.llm_unavailable:
                            retry_next.append(sector)
                    except Exception as exc:
                        logger.exception('[AI批量匹配] 板块 %s 失败', sector)
                        db.rollback()
                        results[sector] = {'sector': sector, 'status': 'error',
                                           'note': str(exc)[:200], 'attempts': attempt}
                        retry_next.append(sector)
                    task.done = sum(1 for v in results.values() if not v.get('llm_unavailable'))
                    # 进度必须边跑边可见：之前整轮跑完才写 results/counts，
                    # 前端一直显示 0/87，看起来像卡死。
                    task.results = [results[s] for s in task.sectors if s in results]
                    counts = {'matched': 0, 'proxy': 0, 'needs_review': 0,
                              'conflict': 0, 'no_fund': 0}
                    for row in task.results:
                        key = row['status'] if row['status'] in counts else 'needs_review'
                        counts[key] += 1
                    task.counts = counts
                    task.pending_retry = retry_next
                    task.touch()
                    time.sleep(PACE_SECONDS)
                pending = retry_next
            task.results = [results[s] for s in task.sectors if s in results]
            counts = {'matched': 0, 'proxy': 0, 'needs_review': 0, 'conflict': 0, 'no_fund': 0}
            for row in task.results:
                key = row['status'] if row['status'] in counts else 'needs_review'
                counts[key] += 1
            task.counts = counts
            task.done = len(task.results)
            task.pending_retry = pending
            task.status = 'completed' if not pending else 'completed_with_retries'
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
