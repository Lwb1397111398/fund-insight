import importlib.util
import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session as SqlAlchemySession
from sqlalchemy.orm import sessionmaker

from src.models.database import AnalysisLog, Base, BatchAnalysisTask, Blogger, Post, Prediction


def test_post_analysis_service_module_exists():
    assert importlib.util.find_spec("src.services.post_analysis_service") is not None


class _Analyzer:
    model = "test-model"

    def __init__(self, fail_during_write=False):
        self.fail_during_write = fail_during_write
        self.target_date_calls = 0

    def analyze_post(self, title, content, post_date=None):
        return {
            "predictions": [
                {
                    "sector": "人工智能",
                    "sector_type": "科技",
                    "prediction_type": "up",
                    "prediction_content": "未来一周看涨",
                    "confidence": 80,
                    "prediction_period": "1周",
                },
                {
                    "sector": "半导体",
                    "sector_type": "科技",
                    "prediction_type": "up",
                    "prediction_content": "未来一周继续上涨",
                    "confidence": 70,
                    "prediction_period": "1周",
                },
            ],
            "summary": "测试分析",
        }

    def get_fund_for_sector(self, sector):
        return {"code": "015719", "name": "测试基金"}

    def calculate_target_date(self, prediction_date, prediction_period):
        self.target_date_calls += 1
        if self.fail_during_write and self.target_date_calls >= 2:
            raise RuntimeError("构造第二条预测失败")
        return prediction_date + timedelta(days=7)

    def calculate_next_verify_date(self, prediction_date, target_date):
        return target_date


class _FundManager:
    def auto_add_fund_for_prediction(self, sector, db):
        return True, "测试映射", SimpleNamespace(
            fund_code="999999",
            fund_name=f"{sector}基金",
        )

    def get_category_for_sector(self, sector):
        return "科技"


def _create_post(db, content=None):
    blogger = Blogger(name="任务测试博主", platform="wechat")
    db.add(blogger)
    db.flush()
    post = Post(
        blogger_id=blogger.id,
        title="测试帖子",
        content=content or "我继续看好人工智能和半导体板块，未来一周可能继续上涨，市场趋势和资金面都比较积极。",
        post_date=date(2026, 7, 10),
        analyzed=False,
    )
    db.add(post)
    db.commit()
    return post


def test_analysis_is_idempotent_and_records_succeeded_meta(test_db):
    from src.services.post_analysis_service import PostAnalysisService

    post = _create_post(test_db)
    service = PostAnalysisService(
        db=test_db,
        analyzer_factory=lambda: _Analyzer(),
        fund_auto_manager=_FundManager(),
    )

    first = service.analyze_post(post.id)
    second = service.analyze_post(post.id)

    test_db.expire_all()
    saved = test_db.get(Post, post.id)
    assert first["success"] is True
    assert first["status"] == "succeeded"
    assert second["status"] == "skipped"
    assert test_db.query(Prediction).filter(Prediction.post_id == post.id).count() == 2
    assert saved.analysis_result["_meta"]["status"] == "succeeded"


def test_analysis_write_failure_rolls_back_all_predictions_and_records_error(test_db):
    from src.services.post_analysis_service import PostAnalysisService

    post = _create_post(test_db)
    service = PostAnalysisService(
        db=test_db,
        analyzer_factory=lambda: _Analyzer(fail_during_write=True),
        fund_auto_manager=_FundManager(),
    )

    result = service.analyze_post(post.id)

    test_db.expire_all()
    saved = test_db.get(Post, post.id)
    assert result["success"] is False
    assert result["status"] == "failed"
    assert test_db.query(Prediction).filter(Prediction.post_id == post.id).count() == 0
    assert saved.analyzed is False
    assert saved.analysis_result["_meta"]["status"] == "failed"
    assert "构造第二条预测失败" in saved.analysis_result["_meta"]["error"]


def test_low_quality_post_is_marked_skipped_without_deletion(test_db):
    from src.services.post_analysis_service import PostAnalysisService

    post = _create_post(test_db, content="hi")
    result = PostAnalysisService(
        db=test_db,
        analyzer_factory=lambda: _Analyzer(),
        fund_auto_manager=_FundManager(),
    ).analyze_post(post.id)

    test_db.expire_all()
    saved = test_db.get(Post, post.id)
    assert result["status"] == "skipped"
    assert saved is not None
    assert saved.analyzed is False
    assert saved.analysis_result["_meta"]["status"] == "skipped"


def test_job_runner_closes_database_sessions_while_calling_llm(tmp_path):
    from src.services.post_analysis_service import PostAnalysisService

    tracker = {"active": 0}

    class TrackingSession(SqlAlchemySession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._tracker_closed = False
            tracker["active"] += 1

        def close(self):
            if not self._tracker_closed:
                tracker["active"] -= 1
                self._tracker_closed = True
            super().close()

    engine = create_engine(f"sqlite:///{(tmp_path / 'analysis-job.db').as_posix()}")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, class_=TrackingSession)

    seed = TestSession()
    post = _create_post(seed)
    task, created = PostAnalysisService.create_job(seed, post_ids=[post.id])
    task_id = task.id
    post_id = post.id
    seed.close()

    class SessionAwareAnalyzer(_Analyzer):
        def analyze_post(self, title, content, post_date=None):
            assert tracker["active"] == 0
            return super().analyze_post(title, content, post_date)

    PostAnalysisService.run_job(
        task_id,
        session_factory=TestSession,
        analyzer_factory=lambda: SessionAwareAnalyzer(),
        fund_auto_manager=_FundManager(),
    )

    check = TestSession()
    try:
        saved_task = check.get(BatchAnalysisTask, task_id)
        assert created is True
        assert saved_task.status == "succeeded"
        assert saved_task.processed_ids == [post_id]
        assert saved_task.processed_count == 1
        assert saved_task.success_count == 1
        assert check.query(AnalysisLog).filter(AnalysisLog.task_id == task_id).count() == 1
    finally:
        check.close()


def test_failed_job_can_resume_failed_items_without_duplicate_predictions(tmp_path):
    from src.services.post_analysis_service import PostAnalysisService

    engine = create_engine(f"sqlite:///{(tmp_path / 'analysis-resume.db').as_posix()}")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)

    seed = TestSession()
    post = _create_post(seed)
    task, _ = PostAnalysisService.create_job(seed, post_ids=[post.id])
    task_id = task.id
    post_id = post.id
    seed.close()

    class FailsOnceAnalyzer(_Analyzer):
        calls = 0

        def analyze_post(self, title, content, post_date=None):
            type(self).calls += 1
            if type(self).calls == 1:
                raise RuntimeError("临时 LLM 错误")
            return super().analyze_post(title, content, post_date)

    PostAnalysisService.run_job(
        task_id,
        session_factory=TestSession,
        analyzer_factory=lambda: FailsOnceAnalyzer(),
        fund_auto_manager=_FundManager(),
    )

    resume_session = TestSession()
    try:
        failed_task = resume_session.get(BatchAnalysisTask, task_id)
        assert failed_task.status == "failed"
        assert failed_task.failed_count == 1
        PostAnalysisService.resume_job(resume_session, task_id)
        assert failed_task.status == "pending"
        assert failed_task.failed_ids == []
    finally:
        resume_session.close()

    PostAnalysisService.run_job(
        task_id,
        session_factory=TestSession,
        analyzer_factory=lambda: FailsOnceAnalyzer(),
        fund_auto_manager=_FundManager(),
    )

    check = TestSession()
    try:
        saved_task = check.get(BatchAnalysisTask, task_id)
        assert saved_task.status == "succeeded"
        assert saved_task.processed_ids == [post_id]
        assert saved_task.failed_ids == []
        assert check.query(Prediction).filter(Prediction.post_id == post_id).count() == 2
    finally:
        check.close()


def test_automatic_job_excludes_failed_and_low_quality_posts(test_db):
    from src.services.post_analysis_service import PostAnalysisService

    pending = _create_post(test_db)
    skipped = Post(
        blogger_id=pending.blogger_id,
        title="低质量",
        content="hi",
        post_date=date(2026, 7, 10),
        analyzed=False,
        analysis_result={"_meta": {"status": "skipped"}},
    )
    failed = Post(
        blogger_id=pending.blogger_id,
        title="失败帖子",
        content="这是一条足够长但此前分析失败的帖子内容，需要用户明确点击重试后才能再次处理。",
        post_date=date(2026, 7, 10),
        analyzed=False,
        analysis_result={"_meta": {"status": "failed"}},
    )
    test_db.add_all([skipped, failed])
    test_db.commit()

    task, _ = PostAnalysisService.create_job(test_db, limit=100)

    assert task.task_params["post_ids"] == [pending.id]


def test_job_log_drops_post_foreign_key_if_post_was_deleted(monkeypatch, tmp_path):
    from src.services.post_analysis_service import PostAnalysisService

    engine = create_engine(f"sqlite:///{(tmp_path / 'analysis-delete-race.db').as_posix()}")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    seed = TestSession()
    post = _create_post(seed)
    task, _ = PostAnalysisService.create_job(seed, post_ids=[post.id])
    task_id, post_id = task.id, post.id
    seed.close()

    def delete_during_analysis(self, current_post_id, task_id=None):
        delete_db = TestSession()
        try:
            delete_db.query(Post).filter(Post.id == current_post_id).delete()
            delete_db.commit()
        finally:
            delete_db.close()
        return {
            "success": False,
            "status": "failed",
            "message": "帖子已删除",
            "error": "帖子已删除",
        }

    monkeypatch.setattr(PostAnalysisService, "analyze_post", delete_during_analysis)
    PostAnalysisService.run_job(task_id, session_factory=TestSession)

    check = TestSession()
    try:
        log = check.query(AnalysisLog).filter(AnalysisLog.task_id == task_id).one()
        assert check.get(Post, post_id) is None
        assert log.post_id is None
    finally:
        check.close()


def test_duplicate_runner_does_not_reprocess_fresh_running_job(tmp_path):
    from datetime import datetime
    from src.services.post_analysis_service import PostAnalysisService

    engine = create_engine(f"sqlite:///{(tmp_path / 'analysis-runner-claim.db').as_posix()}")
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine)
    seed = TestSession()
    post = _create_post(seed)
    task, _ = PostAnalysisService.create_job(seed, post_ids=[post.id])
    task.status = "running"
    task.updated_at = datetime.now()
    seed.commit()
    task_id = task.id
    seed.close()

    class MustNotRunAnalyzer(_Analyzer):
        def analyze_post(self, *args, **kwargs):
            raise AssertionError("新鲜 running 任务不应被第二个 runner 重复处理")

    PostAnalysisService.run_job(
        task_id,
        session_factory=TestSession,
        analyzer_factory=lambda: MustNotRunAnalyzer(),
        fund_auto_manager=_FundManager(),
    )

    check = TestSession()
    try:
        assert check.get(BatchAnalysisTask, task_id).status == "running"
        assert check.query(AnalysisLog).filter(AnalysisLog.task_id == task_id).count() == 0
    finally:
        check.close()
