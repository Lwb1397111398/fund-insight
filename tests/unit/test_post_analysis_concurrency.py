"""批量分析并发化：多线程正确性、内存库降级串行、worker 数解析。"""
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import AnalysisLog, Base, BatchAnalysisTask, Prediction
from src.services.post_analysis_service import PostAnalysisService

from tests.unit.test_post_analysis_tasks import _Analyzer, _FundManager, _create_post


def _file_session(tmp_path, name):
    engine = create_engine(
        f"sqlite:///{(tmp_path / name).as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


def test_resolve_workers_forces_serial_on_in_memory_sqlite(monkeypatch, test_db):
    monkeypatch.setenv("POST_ANALYSIS_MAX_WORKERS", "3")
    factory = lambda: test_db  # noqa: E731 conftest 的 :memory: 会话
    assert PostAnalysisService._resolve_workers(factory, 10) == 1


def test_resolve_workers_allows_concurrency_on_file_sqlite(monkeypatch, tmp_path):
    factory = _file_session(tmp_path, "resolve.db")
    monkeypatch.setenv("POST_ANALYSIS_MAX_WORKERS", "3")
    assert PostAnalysisService._resolve_workers(factory, 10) == 3
    # 不超过任务数量
    assert PostAnalysisService._resolve_workers(factory, 2) == 2
    # 坏值回退默认 3
    monkeypatch.setenv("POST_ANALYSIS_MAX_WORKERS", "not-a-number")
    assert PostAnalysisService._resolve_workers(factory, 10) == 3
    # 回滚旋钮：设 1 即串行
    monkeypatch.setenv("POST_ANALYSIS_MAX_WORKERS", "1")
    assert PostAnalysisService._resolve_workers(factory, 10) == 1


def test_run_job_processes_all_posts_exactly_once_with_three_workers(monkeypatch, tmp_path):
    monkeypatch.setenv("POST_ANALYSIS_MAX_WORKERS", "3")
    TestSession = _file_session(tmp_path, "concurrent-job.db")

    seed = TestSession()
    post_ids = [_create_post(seed).id for _ in range(6)]
    task, _ = PostAnalysisService.create_job(seed, post_ids=post_ids)
    task_id = task.id
    seed.close()

    class SlowAnalyzer(_Analyzer):
        def analyze_post(self, title, content, post_date=None):
            # 拉长单帖耗时，制造线程交叠（每帖自身的会话开闭顺序仍由
            # test_job_runner_closes_database_sessions_while_calling_llm 保证）
            time.sleep(0.05)
            return super().analyze_post(title, content, post_date)

    PostAnalysisService.run_job(
        task_id,
        session_factory=TestSession,
        analyzer_factory=lambda: SlowAnalyzer(),
        fund_auto_manager=_FundManager(),
    )

    check = TestSession()
    try:
        saved = check.get(BatchAnalysisTask, task_id)
        assert saved.status == "succeeded"
        assert saved.processed_count == 6
        assert saved.success_count == 6
        assert saved.failed_count == 0
        assert sorted(saved.processed_ids) == sorted(post_ids)
        # 每帖恰好一次：6 帖 × 2 预测 = 12，且分析日志 6 条
        assert check.query(Prediction).count() == 12
        assert check.query(AnalysisLog).filter(AnalysisLog.task_id == task_id).count() == 6
        for post_id in post_ids:
            assert check.query(Prediction).filter(Prediction.post_id == post_id).count() == 2
    finally:
        check.close()


def test_run_job_cancel_stops_concurrent_job_without_overwriting_status(monkeypatch, tmp_path):
    monkeypatch.setenv("POST_ANALYSIS_MAX_WORKERS", "3")
    TestSession = _file_session(tmp_path, "concurrent-cancel.db")

    seed = TestSession()
    post_ids = [_create_post(seed).id for _ in range(4)]
    task, _ = PostAnalysisService.create_job(seed, post_ids=post_ids)
    task_id = task.id
    # 直接置为已取消：worker 的 skip 检查会发现并停止
    task.status = "cancelled"
    seed.commit()
    seed.close()

    class MustNotAnalyze(_Analyzer):
        def analyze_post(self, *args, **kwargs):
            raise AssertionError("已取消任务不应再分析帖子")

    PostAnalysisService.run_job(
        task_id,
        session_factory=TestSession,
        analyzer_factory=lambda: MustNotAnalyze(),
        fund_auto_manager=_FundManager(),
    )

    check = TestSession()
    try:
        # 取消终态不得被收尾逻辑改写成 succeeded/failed
        assert check.get(BatchAnalysisTask, task_id).status == "cancelled"
        assert check.query(Prediction).count() == 0
    finally:
        check.close()
