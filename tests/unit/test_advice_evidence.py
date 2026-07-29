"""EvidencePack P0 构造测试。"""
from datetime import date, timedelta

from src.models.database import Blogger, Post, Prediction, Viewpoint
from src.services import prediction_lifecycle as lc
from src.services.advice_evidence import AdviceEvidenceBuilder
from src.services.advice_service import AdviceService


def _blogger(db, name="证据博主", accuracy=80, total=10, correct=8):
    b = Blogger(
        name=name,
        platform="wechat",
        accuracy_rate=accuracy,
        total_predictions=total,
        correct_predictions=correct,
        grade="A",
    )
    db.add(b)
    db.flush()
    return b


def _pred(db, blogger, target, conf=70, sector="白酒", ptype="up"):
    post = Post(
        blogger_id=blogger.id,
        title="e",
        content="e",
        post_date=target - timedelta(days=5),
    )
    db.add(post)
    db.flush()
    p = Prediction(
        post_id=post.id,
        blogger_id=blogger.id,
        fund_code="EV01",
        fund_name="证据基金",
        sector=sector,
        prediction_type=ptype,
        prediction_content=f"{sector}{ptype}",
        confidence=conf,
        prediction_date=post.post_date,
        prediction_period="1周",
        target_date=target,
        status="pending",
        is_correct=None,
        is_deleted=False,
        is_expired=False,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


def _viewpoint(db, author, summary, direction="bullish", day=None, cred=80):
    v = Viewpoint(
        author=author,
        source="test",
        content=summary,
        summary=summary,
        reasoning="r",
        market_direction=direction,
        confidence=60,
        credibility_score=cred,
        weight=1.0,
        sectors_bullish=["白酒"] if direction == "bullish" else [],
        sectors_bearish=["白酒"] if direction == "bearish" else [],
        viewpoint_date=day or date(2026, 8, 1),
        is_deleted=False,
        is_summary=False,
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


def test_evidence_pack_uses_lifecycle_actionable_only(test_db, monkeypatch):
    as_of = date(2026, 8, 10)
    monkeypatch.setattr(lc, "current_as_of", lambda: as_of)
    b = _blogger(test_db)
    active = _pred(test_db, b, as_of + timedelta(days=3))
    due = _pred(test_db, b, as_of)  # 当天到期，不应进方向信号

    pack = AdviceEvidenceBuilder(test_db).build(as_of=as_of)
    ids = {p["prediction_id"] for p in pack.predictions}
    assert active.id in ids
    assert due.id not in ids
    assert all(p.get("lifecycle") == "active" for p in pack.predictions)


def test_evidence_pack_caps_predictions_per_blogger(test_db, monkeypatch):
    as_of = date(2026, 8, 10)
    monkeypatch.setattr(lc, "current_as_of", lambda: as_of)
    b = _blogger(test_db)
    for i in range(5):
        _pred(test_db, b, as_of + timedelta(days=i + 1), conf=50 + i)

    pack = AdviceEvidenceBuilder(test_db, max_predictions_per_blogger=2).build(as_of=as_of)
    assert len(pack.predictions) <= 2
    assert any(e["reason"] == "prediction_per_blogger_cap" for e in pack.exclusions)


def test_evidence_pack_viewpoint_dedup_and_author_cap(test_db, monkeypatch):
    as_of = date(2026, 8, 10)
    monkeypatch.setattr(lc, "current_as_of", lambda: as_of)
    day = as_of - timedelta(days=1)
    _viewpoint(test_db, "甲", "完全相同的摘要文本ABC", day=day, cred=90)
    _viewpoint(test_db, "甲", "完全相同的摘要文本ABC", day=day, cred=70)  # 重复
    for i in range(4):
        _viewpoint(test_db, "乙", f"乙的不同观点{i}", day=day, cred=60 + i)

    pack = AdviceEvidenceBuilder(
        test_db, max_viewpoints_per_author=2, top_viewpoints=20
    ).build(as_of=as_of)

    authors = [v.get("author") for v in pack.viewpoints]
    assert authors.count("乙") <= 2
    assert any(e["reason"] == "viewpoint_duplicate_content" for e in pack.exclusions)
    assert any(e["reason"] == "viewpoint_author_cap" for e in pack.exclusions)
    assert all("viewpoint_id" in v for v in pack.viewpoints)


def test_evidence_pack_hash_stable_and_changes_with_direction(test_db, monkeypatch):
    as_of = date(2026, 8, 10)
    monkeypatch.setattr(lc, "current_as_of", lambda: as_of)
    b = _blogger(test_db)
    p = _pred(test_db, b, as_of + timedelta(days=4), ptype="up")

    builder = AdviceEvidenceBuilder(test_db)
    h1 = builder.build(as_of=as_of).evidence_hash
    h2 = builder.build(as_of=as_of).evidence_hash
    assert h1 == h2 and h1 is not None

    p.prediction_type = "down"
    test_db.commit()
    h3 = builder.build(as_of=as_of).evidence_hash
    assert h3 != h1


def test_get_data_for_advice_returns_pack_fields(test_db, monkeypatch):
    as_of = date(2026, 8, 10)
    monkeypatch.setattr(lc, "current_as_of", lambda: as_of)
    from src.services import advice_evidence as ae

    monkeypatch.setattr(ae, "current_as_of", lambda: as_of)
    b = _blogger(test_db)
    _pred(test_db, b, as_of + timedelta(days=2))

    data = AdviceService(test_db).get_data_for_advice()
    assert "evidence_hash" in data
    assert "exclusions" in data
    assert "meta" in data
    assert data["as_of_date"] == as_of.isoformat()
    assert data["predictions"]
    assert "weight" in data["predictions"][0]
    assert "prediction_id" in data["predictions"][0]
