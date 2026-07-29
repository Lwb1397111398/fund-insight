from datetime import date, datetime

from src.models.database import InvestmentAdvice, Viewpoint
from src.services.advice_service import AdviceService
from src.services.advice_validation import (
    ADVICE_PROMPT_VERSION,
    build_advice_cache_key,
)
from src.core.config import config


def test_check_data_changed_uses_evidence_cache_key(test_db, monkeypatch):
    """P2：缓存键=evidence_hash|prompt|model；证据不变则命中。"""
    service = AdviceService(test_db)
    evidence_hash = "ev_hash_stable"
    monkeypatch.setattr(
        service,
        "get_data_for_advice",
        lambda: {
            "bloggers": [],
            "predictions": [{"prediction_id": 1}],
            "viewpoints": [],
            "evidence_hash": evidence_hash,
            "meta": {},
        },
    )
    model = str(getattr(config, "LLM_MODEL", None) or "")
    cache_key = build_advice_cache_key(
        evidence_hash,
        prompt_version=ADVICE_PROMPT_VERSION,
        model_version=model,
    )
    test_db.add(
        InvestmentAdvice(
            advice_date=date.today(),
            advice_type="hold",
            advice_content="保持观察",
            data_hash=cache_key,
        )
    )
    test_db.commit()

    has_changed, new_hash, latest = service.check_data_changed()
    assert has_changed is False
    assert new_hash == cache_key
    assert latest is not None


def test_check_data_changed_miss_when_evidence_changes(test_db, monkeypatch):
    service = AdviceService(test_db)
    model = str(getattr(config, "LLM_MODEL", None) or "")
    old_key = build_advice_cache_key(
        "old_ev", prompt_version=ADVICE_PROMPT_VERSION, model_version=model
    )
    test_db.add(
        InvestmentAdvice(
            advice_date=date.today(),
            advice_type="hold",
            advice_content="旧建议",
            data_hash=old_key,
        )
    )
    test_db.commit()

    monkeypatch.setattr(
        service,
        "get_data_for_advice",
        lambda: {
            "predictions": [{"prediction_id": 2}],
            "viewpoints": [],
            "evidence_hash": "new_ev",
            "meta": {},
        },
    )
    has_changed, new_hash, _ = service.check_data_changed()
    assert has_changed is True
    assert new_hash != old_key
    assert "new_ev" in new_hash


def test_check_data_changed_miss_when_prompt_version_changes(test_db, monkeypatch):
    service = AdviceService(test_db)
    model = str(getattr(config, "LLM_MODEL", None) or "")
    evidence_hash = "same_ev"
    stored = build_advice_cache_key(
        evidence_hash, prompt_version="advice.three_stage.v0", model_version=model
    )
    test_db.add(
        InvestmentAdvice(
            advice_date=date.today(),
            advice_type="hold",
            advice_content="旧 prompt 版本建议",
            data_hash=stored,
        )
    )
    test_db.commit()
    monkeypatch.setattr(
        service,
        "get_data_for_advice",
        lambda: {
            "predictions": [{"prediction_id": 1}],
            "evidence_hash": evidence_hash,
            "viewpoints": [],
            "meta": {},
        },
    )
    has_changed, new_hash, _ = service.check_data_changed()
    assert has_changed is True
    assert ADVICE_PROMPT_VERSION in new_hash
    assert new_hash != stored


def test_legacy_statistical_hash_still_callable(test_db):
    """旧统计哈希方法保留（兼容/诊断），但不再作为生成缓存键。"""
    viewpoint = Viewpoint(
        content="看好人工智能板块",
        author="测试作者",
        source="manual",
        market_direction="bullish",
        confidence=70,
        sectors_bullish=["人工智能"],
        summary="人工智能板块偏强",
        reasoning="【AI深度分析】资金流改善",
        viewpoint_date=date.today(),
        created_at=datetime(2026, 7, 10, 9, 0, 0),
        is_deleted=False,
        is_expired=False,
    )
    test_db.add(viewpoint)
    test_db.commit()
    h1 = AdviceService(test_db)._calculate_data_hash()
    viewpoint.is_deleted = True
    viewpoint.deleted_at = datetime(2026, 7, 10, 10, 0, 0)
    test_db.commit()
    h2 = AdviceService(test_db)._calculate_data_hash()
    assert h1 != h2
