"""L1 Beta 收缩与证据 flag 测试。"""
from datetime import date, timedelta

from src.core.config import config
from src.models.database import Blogger, Post, Prediction
from src.services import l1_weighting as l1
from src.services import prediction_lifecycle as lc
from src.services.advice_evidence import AdviceEvidenceBuilder


def test_beta_hit_rate_boundaries():
    p0, a = 0.609, 15.0
    assert abs(l1.beta_hit_rate(0, 0, p0=p0, alpha=a) - p0) < 1e-9
    assert abs(l1.beta_hit_rate(1, 1, p0=p0, alpha=a) - (1 + a * p0) / (1 + a)) < 1e-9
    # 1/1 → ~0.633，不是 1.0
    assert 0.63 < l1.beta_hit_rate(1, 1, p0=p0, alpha=a) < 0.64
    assert abs(l1.beta_hit_rate(5, 8, p0=p0, alpha=a) - (5 + a * p0) / (8 + a)) < 1e-9
    assert abs(l1.beta_hit_rate(30, 40, p0=p0, alpha=a) - (30 + a * p0) / (40 + a)) < 1e-9


def test_evidence_tier_and_floor():
    assert l1.evidence_tier(0, min_n=10) == "neutral"
    assert l1.evidence_tier(5, min_n=10) == "prior"
    assert l1.evidence_tier(10, min_n=10) == "empirical"
    floor = l1.compute_weight_floor([0.5, 0.7], floor_ratio=0.4)
    assert abs(floor - 0.4 * 0.6) < 1e-9
    w, floored = l1.prediction_weight_from_reliability(50, 100, weight_floor=0.6)
    assert floored is True
    assert w == 0.6


def test_legacy_reliability_matches_old_formula():
    # sample=10 → shrink=1 → reliability=acc
    assert l1.legacy_reliability_score(80, 10) == 80.0
    # sample=5 → shrink=0.5 → 50+(80-50)*0.5=65
    assert l1.legacy_reliability_score(80, 5) == 65.0
    # sample=0 → 50
    assert l1.legacy_reliability_score(90, 0) == 50.0


def _seed_blogger_with_hits(db, name, hits):
    """hits: list of bool|None for is_correct."""
    b = Blogger(
        name=name,
        platform="test",
        accuracy_rate=90.0,  # 加权分虚高
        total_predictions=len([h for h in hits if h is not None]),
        correct_predictions=sum(1 for h in hits if h is True),
        grade="A",
        is_active=True,
    )
    db.add(b)
    db.flush()
    for i, h in enumerate(hits):
        post = Post(
            blogger_id=b.id,
            content=f"c{i}",
            post_date=date(2026, 6, 1) + timedelta(days=i),
            analyzed=True,
        )
        db.add(post)
        db.flush()
        db.add(
            Prediction(
                post_id=post.id,
                blogger_id=b.id,
                fund_code=f"L1{i:02d}",
                fund_name="L1",
                sector="测试",
                prediction_type="up",
                prediction_content="up",
                prediction_date=post.post_date,
                target_date=post.post_date + timedelta(days=7),
                status="success" if h is True else ("failed" if h is False else "pending"),
                is_correct=h,
                is_deleted=False,
                confidence=80,
                verify_count=1 if h is not None else 0,
            )
        )
    db.commit()
    return b


def test_flag_off_uses_legacy_accuracy_rate(test_db, monkeypatch):
    monkeypatch.setattr(config, "ADVICE_L1_HIT_WEIGHTING", False)
    monkeypatch.setattr(config, "ADVICE_L1_SHADOW", False)
    as_of = date(2026, 8, 10)
    monkeypatch.setattr(lc, "current_as_of", lambda: as_of)
    # 加权分 90，但命中很差
    b = _seed_blogger_with_hits(test_db, "legacy博主", [True] + [False] * 9)
    # 未来可行动预测
    post = Post(blogger_id=b.id, content="f", post_date=as_of, analyzed=True)
    test_db.add(post)
    test_db.flush()
    test_db.add(
        Prediction(
            post_id=post.id,
            blogger_id=b.id,
            fund_code="FUT1",
            fund_name="f",
            sector="白酒",
            prediction_type="up",
            prediction_content="up",
            prediction_date=as_of,
            target_date=as_of + timedelta(days=3),
            status="pending",
            is_correct=None,
            is_deleted=False,
            confidence=80,
        )
    )
    test_db.commit()

    pack = AdviceEvidenceBuilder(test_db).build(as_of=as_of)
    assert pack.meta["weight_strategy_version"] == l1.LEGACY_STRATEGY_VERSION
    assert pack.meta.get("l1_hit_weighting") is False
    # reliability 来自 accuracy_rate=90, n=10 → 90
    row = next(x for x in pack.bloggers if x["blogger_id"] == b.id)
    assert row["reliability_score"] == 90.0


def test_shadow_dual_run_serves_legacy_and_logs_l1(test_db, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ADVICE_L1_HIT_WEIGHTING", False)
    monkeypatch.setattr(config, "ADVICE_L1_SHADOW", True)
    from src.services import l1_shadow

    monkeypatch.setattr(l1_shadow, "SHADOW_LOG_PATH", tmp_path / "l1_shadow.jsonl")

    as_of = date(2026, 8, 10)
    monkeypatch.setattr(lc, "current_as_of", lambda: as_of)
    b = _seed_blogger_with_hits(test_db, "shadow博主", [True] * 10)
    post = Post(blogger_id=b.id, content="f", post_date=as_of, analyzed=True)
    test_db.add(post)
    test_db.flush()
    test_db.add(
        Prediction(
            post_id=post.id,
            blogger_id=b.id,
            fund_code="SH01",
            fund_name="f",
            sector="白酒",
            prediction_type="up",
            prediction_content="up",
            prediction_date=as_of,
            target_date=as_of + timedelta(days=3),
            status="pending",
            is_correct=None,
            is_deleted=False,
            confidence=80,
        )
    )
    test_db.commit()

    pack = AdviceEvidenceBuilder(test_db).build(as_of=as_of)
    assert pack.meta["weight_strategy_version"] == l1.LEGACY_STRATEGY_VERSION
    assert pack.meta.get("l1_hit_weighting") is False
    assert pack.meta.get("l1_shadow_enabled") is True
    assert pack.meta.get("l1_shadow", {}).get("shadow_strategy") == l1.STRATEGY_VERSION
    assert (tmp_path / "l1_shadow.jsonl").exists()
    # 服务路径权重不含 l1 专用字段
    assert "blogger_p_hat" not in pack.predictions[0]


def test_reeval_trigger_by_count(test_db, monkeypatch):
    from src.services import l1_shadow

    monkeypatch.setattr(config, "L1_SHADOW_BASELINE_VERIFIED", 0)
    monkeypatch.setattr(config, "L1_SHADOW_REEVAL_NEW", 5)
    monkeypatch.setattr(config, "L1_SHADOW_REEVAL_WEEKS", 99)
    monkeypatch.setattr(config, "L1_SHADOW_STARTED_AT", "2026-07-29")
    monkeypatch.setattr(config, "L1_SHADOW_DATA_ERA", "pre_other")
    monkeypatch.setattr(config, "L3_OTHER_CUTOVER_AT", "")
    _seed_blogger_with_hits(test_db, "reeval", [True] * 6)
    st = l1_shadow.reeval_status(test_db)
    assert st["due"] is True
    assert st["by_count"] is True
    assert st["data_era"] == "pre_other"
    assert st["other_cutover_at"] is None
    assert "pre_other" in st["era_note"]


def test_reeval_era_post_other_flags_missing_cutover(test_db, monkeypatch):
    from src.services import l1_shadow

    monkeypatch.setattr(config, "L1_SHADOW_BASELINE_VERIFIED", 1000)
    monkeypatch.setattr(config, "L1_SHADOW_REEVAL_NEW", 150)
    monkeypatch.setattr(config, "L1_SHADOW_REEVAL_WEEKS", 99)
    monkeypatch.setattr(config, "L1_SHADOW_STARTED_AT", "2026-07-29")
    monkeypatch.setattr(config, "L1_SHADOW_DATA_ERA", "post_other")
    monkeypatch.setattr(config, "L3_OTHER_CUTOVER_AT", "")
    st = l1_shadow.reeval_status(test_db)
    assert st["data_era"] == "post_other"
    assert "cutover" in st["era_note"]


def test_flag_on_uses_beta_not_accuracy_rate(test_db, monkeypatch):
    monkeypatch.setattr(config, "ADVICE_L1_HIT_WEIGHTING", True)
    monkeypatch.setattr(config, "ADVICE_L1_SHADOW", False)  # 正式开闸不跑 shadow
    monkeypatch.setattr(config, "L1_P0", 0.609)
    monkeypatch.setattr(config, "L1_ALPHA", 15.0)
    monkeypatch.setattr(config, "L1_MIN_N", 10)
    monkeypatch.setattr(config, "L1_FLOOR_RATIO", 0.4)
    as_of = date(2026, 8, 10)
    monkeypatch.setattr(lc, "current_as_of", lambda: as_of)

    # 10 条结论 1 对 9 错 → raw 0.1，收缩后仍远低于 90 加权分
    hits = [True] + [False] * 9
    b = _seed_blogger_with_hits(test_db, "l1博主", hits)
    post = Post(blogger_id=b.id, content="f", post_date=as_of, analyzed=True)
    test_db.add(post)
    test_db.flush()
    test_db.add(
        Prediction(
            post_id=post.id,
            blogger_id=b.id,
            fund_code="FUT2",
            fund_name="f",
            sector="白酒",
            prediction_type="up",
            prediction_content="up",
            prediction_date=as_of,
            target_date=as_of + timedelta(days=3),
            status="pending",
            is_correct=None,
            is_deleted=False,
            confidence=100,
        )
    )
    test_db.commit()

    pack = AdviceEvidenceBuilder(test_db).build(as_of=as_of)
    assert pack.meta["weight_strategy_version"] == l1.STRATEGY_VERSION
    assert pack.meta.get("l1_hit_weighting") is True
    row = next(x for x in pack.bloggers if x["blogger_id"] == b.id)
    expected = l1.beta_hit_rate(1, 10, p0=0.609, alpha=15) * 100
    assert abs(row["reliability_score"] - round(expected, 2)) < 0.02
    assert row["reliability_score"] < 50  # 远低于 accuracy_rate 90
    assert row["evidence_tier"] == "empirical"
    assert pack.predictions
    assert "weight_floored" in pack.predictions[0]
    assert "blogger_p_hat" in pack.predictions[0]
