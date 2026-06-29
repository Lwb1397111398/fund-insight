"""
板块资金流向 API 路由
提供：抓取触发、排行榜查询、历史趋势、板块-基金联动、统计摘要
"""
import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session

from src.api.deps import get_db
from src.services.sector_flow_service import SectorFlowService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sector-flow", tags=["板块资金分析"])

# 有效参数白名单
VALID_SORT_BY = {"turnover", "intensity", "change_pct"}
VALID_BEHAVIORS = {"grab", "build", "wash", "sell"}


@router.post("/fetch")
async def fetch_sector_flow(
    limit: int = Query(default=30, ge=1, le=200, description="取前N个板块"),
    db: Session = Depends(get_db),
):
    """
    手动触发板块资金流向数据抓取

    从东方财富 API 获取最新数据，计算衍生指标后保存到数据库。
    同日期同板块的数据会被更新（幂等操作）。

    Args:
        limit: 取前 N 个板块补充成交额（按主力净流入排序）

    Returns:
        {"success": True, "saved_count": N}
    """
    try:
        service = SectorFlowService(db)
        count = service.fetch_and_save(limit=limit)
        return {
            "success": True,
            "data": {"saved_count": count},
            "message": f"成功保存 {count} 条板块资金流向数据",
        }
    except Exception as e:
        logger.error(f"[SectorFlow API] 抓取失败: {e}")
        return {
            "success": False,
            "error": f"数据抓取失败: {str(e)}",
        }


@router.get("/ranking")
async def get_ranking(
    sort_by: str = Query(default="intensity", description="排序方式: turnover/intensity/change_pct"),
    query_date: Optional[str] = Query(default=None, description="查询日期 YYYY-MM-DD，默认今天"),
    behavior: Optional[str] = Query(default=None, description="行为过滤: grab/build/wash/sell"),
    limit: int = Query(default=50, ge=1, le=200, description="返回条数"),
    db: Session = Depends(get_db),
):
    """
    获取板块资金流向排行榜

    支持按成交额、主力强度、涨跌幅排序，可按行为类型过滤。

    Args:
        sort_by: 排序字段 - turnover(成交额) / intensity(主力强度) / change_pct(涨跌幅)
        query_date: 查询日期（ISO格式），默认今天
        behavior: 行为过滤 - grab(抢筹) / build(建仓) / wash(洗盘) / sell(出货)
        limit: 返回条数上限

    Returns:
        {"success": True, "data": [...]}
    """
    # 参数校验
    if sort_by not in VALID_SORT_BY:
        raise HTTPException(
            status_code=422,
            detail=f"无效的 sort_by: {sort_by}，可选: {VALID_SORT_BY}",
        )
    if behavior and behavior not in VALID_BEHAVIORS:
        raise HTTPException(
            status_code=422,
            detail=f"无效的 behavior: {behavior}，可选: {VALID_BEHAVIORS}",
        )

    # 解析日期
    parsed_date = _parse_date(query_date)

    try:
        service = SectorFlowService(db)
        data = service.get_ranking(
            sort_by=sort_by,
            query_date=parsed_date,
            behavior=behavior,
            limit=limit,
        )
        return {
            "success": True,
            "data": data,
            "message": f"获取 {len(data)} 条数据",
        }
    except Exception as e:
        logger.error(f"[SectorFlow API] 排行榜查询失败: {e}")
        return {
            "success": False,
            "error": f"查询失败: {str(e)}",
        }


@router.get("/history")
async def get_history(
    sector_name: str = Query(..., description="板块名称"),
    days: int = Query(default=30, ge=1, le=365, description="查询天数"),
    db: Session = Depends(get_db),
):
    """
    获取某板块最近 N 天的历史趋势

    Args:
        sector_name: 板块名称（精确匹配）
        days: 查询天数

    Returns:
        {"success": True, "data": [...]}
    """
    try:
        service = SectorFlowService(db)
        data = service.get_history(sector_name=sector_name, days=days)
        return {
            "success": True,
            "data": data,
            "message": f"获取 {sector_name} 最近 {days} 天的 {len(data)} 条数据",
        }
    except Exception as e:
        logger.error(f"[SectorFlow API] 历史查询失败: {e}")
        return {
            "success": False,
            "error": f"查询失败: {str(e)}",
        }


@router.get("/fund-link")
async def get_fund_link(
    query_date: Optional[str] = Query(default=None, description="查询日期 YYYY-MM-DD"),
    behavior: Optional[str] = Query(default=None, description="行为过滤: grab/build/wash/sell"),
    limit: int = Query(default=20, ge=1, le=100, description="返回条数"),
    db: Session = Depends(get_db),
):
    """
    板块-基金联动分析

    查询当日主力强度最高的板块，并通过板块-基金映射表关联基金。
    用于发现"主力抢筹板块 → 相关基金"的投资机会。

    Args:
        query_date: 查询日期，默认今天
        behavior: 行为过滤
        limit: 返回条数

    Returns:
        {"success": True, "data": [{sector_name, behavior, main_intensity, funds: [...]}]}
    """
    if behavior and behavior not in VALID_BEHAVIORS:
        raise HTTPException(
            status_code=422,
            detail=f"无效的 behavior: {behavior}",
        )

    parsed_date = _parse_date(query_date)

    try:
        service = SectorFlowService(db)
        data = service.get_fund_link(
            query_date=parsed_date,
            behavior=behavior,
            limit=limit,
        )
        return {
            "success": True,
            "data": data,
            "message": f"获取 {len(data)} 条板块-基金联动数据",
        }
    except Exception as e:
        logger.error(f"[SectorFlow API] 联动查询失败: {e}")
        return {
            "success": False,
            "error": f"查询失败: {str(e)}",
        }


@router.get("/stats")
async def get_stats(
    query_date: Optional[str] = Query(default=None, description="查询日期 YYYY-MM-DD"),
    db: Session = Depends(get_db),
):
    """
    获取统计摘要

    返回当日各行为类型的板块数量统计。

    Args:
        query_date: 查询日期，默认今天

    Returns:
        {"success": True, "data": {grab: N, build: N, wash: N, sell: N, total: N}}
    """
    parsed_date = _parse_date(query_date)

    try:
        service = SectorFlowService(db)
        data = service.get_stats(query_date=parsed_date)
        return {
            "success": True,
            "data": data,
            "message": "获取统计摘要成功",
        }
    except Exception as e:
        logger.error(f"[SectorFlow API] 统计查询失败: {e}")
        return {
            "success": False,
            "error": f"查询失败: {str(e)}",
        }


def _parse_date(date_str: Optional[str]) -> date:
    """
    解析日期字符串

    Args:
        date_str: ISO 格式日期字符串

    Returns:
        date 对象，解析失败返回今天
    """
    if not date_str:
        return date.today()
    try:
        return date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"无效的日期格式: {date_str}，请使用 YYYY-MM-DD",
        )
