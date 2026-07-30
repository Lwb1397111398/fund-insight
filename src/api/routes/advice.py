"""
投资建议路由
处理投资建议相关的 API 请求
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import date
import logging
import traceback

from src.api.deps import get_db
from src.services.advice_service import AdviceService
from src.analyzer.llm_analyzer import get_analyzer
from src.services.advice_validation import (
    validate_evidence_for_advice,
    validate_advice_output,
    build_advice_cache_key,
    ADVICE_PROMPT_VERSION,
)
from src.core.config import config

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/advice", tags=["投资建议"])


class GenerateAdviceRequest(BaseModel):
    date: Optional[date] = None
    force: bool = False


def _model_version() -> str:
    return str(
        getattr(config, "LLM_MODEL", None)
        or getattr(config, "VOLCENGINE_MODEL", None)
        or ""
    )


def _log_advice_rejection(code: str, message: str, details: Optional[dict] = None):
    """区分「今日无建议」与「建议被拒绝」：至少落一条带原因的日志。"""
    logger.warning(
        "[Advice] rejected code=%s message=%s details=%s",
        code,
        message,
        details or {},
    )


@router.post("")
def generate_advice(request: GenerateAdviceRequest = None, db: Session = Depends(get_db)):
    """生成投资建议（证据校验在 LLM 前；缓存键=evidence_hash+版本）"""
    service = AdviceService(db)
    llm_analyzer = get_analyzer()

    try:
        force_generate = request.force if request else False

        # 先构建证据集（本地 DB，无 LLM）
        data = service.get_data_for_advice()
        evidence_hash = data.get("evidence_hash") or ""
        cache_key = build_advice_cache_key(
            evidence_hash,
            prompt_version=ADVICE_PROMPT_VERSION,
            model_version=_model_version(),
        )

        if not force_generate:
            latest_advice = service.get_latest_advice()
            if latest_advice and latest_advice.get("data_hash") == cache_key:
                logger.info("[Advice API] 缓存命中 cache_key 前缀=%s", cache_key[:16])
                return {
                    "success": True,
                    "message": "证据与模型版本未变化，返回最新投资建议",
                    "data": {**latest_advice, "is_new": False, "cache_hit": True},
                }

        # P1：证据不足 —— 在 LLM 之前拦截
        evidence_check = validate_evidence_for_advice(data)
        if not evidence_check.ok:
            _log_advice_rejection(
                evidence_check.code, evidence_check.message, evidence_check.details
            )
            return {
                "success": False,
                "message": evidence_check.message,
                "code": evidence_check.code,
                "details": evidence_check.details,
                "data": {
                    "persisted": False,
                    "rejected": True,
                    "evidence_hash": evidence_hash,
                },
            }

        logger.info(
            "[Advice API] 证据集就绪: 博主=%s 预测=%s 观点=%s hash=%s",
            len(data.get("bloggers") or []),
            len(data.get("predictions") or []),
            len(data.get("viewpoints") or []),
            (evidence_hash or "")[:12],
        )

        advice = llm_analyzer.generate_investment_advice_three_stage(
            data["bloggers"],
            data["predictions"],
            viewpoints=data["viewpoints"],
        )

        output_check = validate_advice_output(advice, evidence=data)
        if not output_check.ok:
            _log_advice_rejection(
                output_check.code, output_check.message, output_check.details
            )
            return {
                "success": False,
                "message": output_check.message,
                "code": output_check.code,
                "details": output_check.details,
                "data": {
                    "viewpoint_summary": advice.get("viewpoint_summary")
                    if isinstance(advice, dict)
                    else None,
                    "prediction_analysis": advice.get("prediction_analysis")
                    if isinstance(advice, dict)
                    else None,
                    "stage_statuses": advice.get("_stage_statuses")
                    if isinstance(advice, dict)
                    else None,
                    "persisted": False,
                    "rejected": True,
                    "evidence_hash": evidence_hash,
                },
            }

        normalized = output_check.normalized or advice

        referenced_predictions = []
        for p in data["predictions"][:20]:
            referenced_predictions.append(
                {
                    "prediction_id": p.get("prediction_id"),
                    "blogger_name": p.get("blogger_name", ""),
                    "sector": p.get("sector", ""),
                    "prediction_type": p.get("prediction_type", ""),
                    "prediction_content": p.get("prediction_content", ""),
                    "confidence": p.get("confidence", 50),
                    "days_to_target": p.get("days_to_target", 0),
                    "weight": p.get("weight"),
                }
            )

        result = service.create_advice(
            advice_type=normalized.get("advice_type"),
            advice_content=normalized.get("advice_content"),
            market_sentiment=normalized.get("market_sentiment"),
            confidence=normalized.get("confidence"),
            referenced_bloggers=[b["name"] for b in data["bloggers"] if b.get("name")],
            data_hash=cache_key,
            advice_date=request.date if request else None,
            reasoning=normalized.get("reasoning"),
            risk_warning=normalized.get("risk_warning"),
            suggested_sectors=normalized.get("suggested_sectors"),
            avoid_sectors=normalized.get("avoid_sectors"),
            referenced_predictions=referenced_predictions,
            short_term_advice=normalized.get("short_term"),
            mid_term_advice=normalized.get("mid_term"),
            avoid_reasoning=normalized.get("avoid_reasoning"),
        )

        result["viewpoint_summary"] = (
            advice.get("viewpoint_summary") if isinstance(advice, dict) else None
        )
        result["prediction_analysis"] = (
            advice.get("prediction_analysis") if isinstance(advice, dict) else None
        )
        result["evidence_hash"] = evidence_hash
        result["cache_key"] = cache_key
        result["stage_statuses"] = (
            advice.get("_stage_statuses") if isinstance(advice, dict) else None
        )
        result["persisted"] = True
        result["is_new"] = True

        return {
            "success": True,
            "message": "投资建议生成成功（三阶段分析）",
            "data": result,
        }

    except Exception as e:
        logger.exception("[API] 生成投资建议失败: %s", e)
        traceback.print_exc()
        return {
            "success": False,
            "message": f"生成失败: {str(e)}",
        }


@router.get("")
def get_latest_advice(db: Session = Depends(get_db)):
    """获取最新投资建议（读路径不做枚举硬校验，容忍历史值）"""
    service = AdviceService(db)
    advice = service.get_latest_advice()

    return {
        "success": True,
        "data": advice,
    }


@router.get("/history")
def get_advice_history(
    skip: int = 0,
    limit: int = 30,
    db: Session = Depends(get_db),
):
    """获取投资建议历史"""
    service = AdviceService(db)
    history = service.get_advice_history(skip=skip, limit=limit)

    return {
        "success": True,
        "data": history,
    }
