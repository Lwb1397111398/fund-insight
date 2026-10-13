"""
投资建议路由
处理投资建议相关的 API 请求
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List, Dict
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
    advice_cache_digest,
    ADVICE_PROMPT_VERSION,
)
from src.services.prediction_lifecycle import current_as_of
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


# 输出被拒的两族：**契约违规**（措辞/软闸不一致，重采样有机会变好 ⇒ 自纠错重试一次）
# 与**结构性失败**（阶段 failed、JSON 坏形——重调只会命中缓存拿回同一份，或本来就缺输入，不重试）。
_CONTRACT_VIOLATION_CODES = frozenset({
    "ambiguous_wording",
    "missing_action_word",
    "gate_conflict",
    "missing_override_reason",
})


def _log_advice_rejection(code: str, message: str, details: Optional[dict] = None):
    """区分「今日无建议」与「建议被拒绝」：至少落一条带原因的日志。"""
    logger.warning(
        "[Advice] rejected code=%s message=%s details=%s",
        code,
        message,
        details or {},
    )


def _attach_sector_fund_targets(sector_actions: List[Dict],
                                sector_states: Optional[Dict] = None) -> List[Dict]:
    """
    给每条板块动作确定性附挂标的（板块→基金）。

    代码**不由 LLM 报**（它会编），复用板块-基金映射的唯一现成尺子
    `get_fund_for_sector`（内置表 + 用户别名 + 子串匹配；答不出就留空，不硬凑）。
    另把板块**自身**近期强弱（证据算好的事实，2026-10-13）逐条挂上——大盘弱时
    对强势板块给 buy/add 是"跟闸"不是"逆行"，页面要把这份依据摆出来。
    """
    from src.constants.sector_fund_map import get_fund_for_sector

    states = sector_states or {}
    for a in sector_actions or []:
        sector = (a.get("sector") or "").strip()
        hit = (get_fund_for_sector(sector) or {}) if sector else {}
        a["fund_code"] = hit.get("code") or ""
        a["fund_name"] = hit.get("name") or ""
        st = states.get(sector) if sector else None
        if isinstance(st, dict):
            a["sector_stance"] = st.get("stance") or ""
            a["sector_stance_label"] = st.get("stance_label") or ""
            a["sector_r20"] = st.get("r20")
            a["sector_nav_as_of"] = st.get("nav_as_of") or ""
    return sector_actions or []


@router.post("")
def generate_advice(request: GenerateAdviceRequest = None, db: Session = Depends(get_db)):
    """生成投资建议（证据校验在 LLM 前；缓存键=evidence_hash+版本）"""
    service = AdviceService(db)
    llm_analyzer = get_analyzer()

    try:
        force_generate = request.force if request else False
        today_beijing = current_as_of()

        # 先构建证据集（本地 DB，无 LLM）
        data = service.get_data_for_advice()
        evidence_hash = data.get("evidence_hash") or ""
        cache_key = build_advice_cache_key(
            evidence_hash,
            prompt_version=ADVICE_PROMPT_VERSION,
            model_version=_model_version(),
        )
        # data_hash 列为 VARCHAR(32)，存缓存键的 32 位摘要而非完整 cache_key
        data_hash = advice_cache_digest(cache_key)

        # 复用判定**按北京日**（老板 2026-10-13 定的语义）：
        # 当天已经生成过 ⇒ 再点「生成」返回当天那条；只有「重新生成」（force）才重算。
        # 证据哈希仍照常计算并落库存档（审计用），但不再参与命中判断——旧的哈希比对
        # 会让"当天观点/净值一新增，复点就自动重算"，且跨天被拒后页面留着昨天的旧条，
        # 老板看到的就成了"点生成出来的是昨天的结果"。
        if not force_generate:
            latest_advice = service.get_latest_advice()
            if latest_advice and latest_advice.get("advice_date") == today_beijing.isoformat():
                logger.info(
                    "[Advice API] 今日已生成（北京日=%s），复用 id=%s",
                    today_beijing,
                    latest_advice.get("id"),
                )
                return {
                    "success": True,
                    "message": "今日投资建议已生成，返回当天结果；需要重算请点「重新生成」",
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

        # 大盘软闸状态：系统按宽基净值本地算出，进 stage3 提示词与落库前一致性校验
        market_state = data.get("market_state") or {}
        # 板块自身强弱（2026-10-13）：大盘弱 ≠ 板块弱，strong 板块的买/加不算逆闸
        sector_states = data.get("sector_states") or {}
        _strong = [s for s, st in sector_states.items()
                   if isinstance(st, dict) and st.get("stance") == "strong"]
        logger.info(
            "[Advice API] 市场闸: stance=%s 依据=%s；自身强势的板块=%s",
            market_state.get("stance"),
            (market_state.get("basis") or "")[:80],
            _strong or "无",
        )

        # force（「重新生成」）⇒ use_cache=False：三段都不回放结果缓存，
        # 否则同提示词命中缓存，重算只是 0.1 秒把同一份内容再落一次库（镜像实测撞到的）。
        advice = llm_analyzer.generate_investment_advice_three_stage(
            data["bloggers"],
            data["predictions"],
            viewpoints=data["viewpoints"],
            market_state=market_state,
            sector_states=sector_states,
            use_cache=not force_generate,
        )
        output_check = validate_advice_output(advice, evidence=data)

        # 契约违规（措辞含糊/软闸不一致）⇒ 当场自纠错重试一次：
        # 把拒绝理由注入 stage3 提示词，提示词一变，结果缓存的键也变，才会重新采样。
        # 阶段失败/坏 JSON 属结构性失败，重调大概率拿回同一份或本来就缺输入 ⇒ 不重试。
        if (
            not output_check.ok
            and output_check.code in _CONTRACT_VIOLATION_CODES
        ):
            logger.warning(
                "[Advice API] 输出被拒 code=%s，注入纠错指令重试一次 message=%s",
                output_check.code,
                output_check.message,
            )
            advice = llm_analyzer.generate_investment_advice_three_stage(
                data["bloggers"],
                data["predictions"],
                viewpoints=data["viewpoints"],
                market_state=market_state,
                sector_states=sector_states,
                correction_note=output_check.message,
                use_cache=not force_generate,
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
        # 标的由后端按板块映射附挂（防 LLM 编造代码；映射答不出则留空）
        sector_actions = _attach_sector_fund_targets(normalized.get("sector_actions") or [], sector_states)
        position_advice = {
            **(normalized.get("position") or {}),
            "stance": normalized.get("market_stance"),
            "stance_label": market_state.get("stance_label"),
            "basis": market_state.get("basis"),
            "index_code": market_state.get("index_code"),
            "nav_as_of": market_state.get("nav_as_of"),
            "r5": market_state.get("r5"),
            "r20": market_state.get("r20"),
            "drawdown": market_state.get("drawdown"),
        }

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
            data_hash=data_hash,
            advice_date=(request.date if request and request.date else today_beijing),
            reasoning=normalized.get("reasoning"),
            risk_warning=normalized.get("risk_warning"),
            suggested_sectors=normalized.get("suggested_sectors"),
            avoid_sectors=normalized.get("avoid_sectors"),
            referenced_predictions=referenced_predictions,
            short_term_advice=normalized.get("short_term"),
            mid_term_advice=normalized.get("mid_term"),
            avoid_reasoning=normalized.get("avoid_reasoning"),
            market_stance=normalized.get("market_stance"),
            position_advice=position_advice,
            sector_actions=sector_actions,
        )

        result["viewpoint_summary"] = (
            advice.get("viewpoint_summary") if isinstance(advice, dict) else None
        )
        result["prediction_analysis"] = (
            advice.get("prediction_analysis") if isinstance(advice, dict) else None
        )
        result["evidence_hash"] = evidence_hash
        result["cache_key"] = cache_key
        result["data_hash"] = data_hash
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
