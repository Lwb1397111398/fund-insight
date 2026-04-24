"""
观点路由
处理观点相关的 API 请求
"""
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session
from typing import Optional
from datetime import date, timedelta
import traceback
import time

from src.api.deps import get_db
from src.services.viewpoint_service import ViewpointService
from src.models.database import Viewpoint

router = APIRouter(prefix="/viewpoints", tags=["观点"])


@router.get("")
async def get_viewpoints(
    skip: int = 0,
    limit: int = 1000,
    source: Optional[str] = None,
    market_direction: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """获取观点列表"""
    service = ViewpointService(db)
    viewpoints = service.get_viewpoints_with_filters(
        skip=skip,
        limit=limit,
        source=source,
        market_direction=market_direction
    )
    
    return {
        "success": True,
        "data": [
            {
                "id": v.id,
                "blogger_id": v.blogger_id,
                "post_id": v.post_id,
                "fund_code": v.fund_code,
                "fund_name": v.fund_name,
                "content": v.content or "",
                "summary": v.summary or "",
                "author": v.author or "未知",
                "source": v.source,
                "market_direction": v.market_direction,
                "confidence": v.confidence,
                "sectors_bullish": v.sectors_bullish or [],
                "sectors_bearish": v.sectors_bearish or [],
                "time_horizon": v.time_horizon or 'medium',
                "validity_period": v.validity_period or '1个月',
                "valid_until": v.valid_until.isoformat() if v.valid_until else None,
                "is_expired": v.valid_until < date.today() if v.valid_until else False,
                "viewpoint_date": v.viewpoint_date.isoformat() if v.viewpoint_date else None,
                "created_at": v.created_at.isoformat() if v.created_at else None
            }
            for v in viewpoints
        ]
    }


@router.get("/{viewpoint_id}")
async def get_viewpoint_detail(viewpoint_id: int, db: Session = Depends(get_db)):
    """获取观点详情"""
    service = ViewpointService(db)
    viewpoint = service.get_viewpoint_by_id(viewpoint_id)
    
    if not viewpoint:
        raise HTTPException(status_code=404, detail="观点不存在")
    
    return {
        "success": True,
        "data": {
            "id": viewpoint.id,
            "blogger_id": viewpoint.blogger_id,
            "post_id": viewpoint.post_id,
            "fund_code": viewpoint.fund_code,
            "fund_name": viewpoint.fund_name,
            "content": viewpoint.content,
            "summary": viewpoint.summary or "",
            "author": viewpoint.author,
            "source": viewpoint.source,
            "market_direction": viewpoint.market_direction,
            "confidence": viewpoint.confidence,
            "credibility_score": viewpoint.credibility_score or 50,
            "weight": viewpoint.weight or 1.0,
            "risk_level": viewpoint.risk_level or "medium",
            "action_suggestion": viewpoint.action_suggestion or "观望",
            "sectors_bullish": viewpoint.sectors_bullish,
            "sectors_bearish": viewpoint.sectors_bearish,
            "reasoning": viewpoint.reasoning,
            "is_summary": viewpoint.is_summary or False,
            "original_count": viewpoint.original_count or 0,
            "viewpoint_date": viewpoint.viewpoint_date.isoformat() if viewpoint.viewpoint_date else None,
            "created_at": viewpoint.created_at.isoformat() if viewpoint.created_at else None
        }
    }


@router.delete("/{viewpoint_id}")
async def delete_viewpoint(viewpoint_id: int, db: Session = Depends(get_db)):
    """删除观点"""
    service = ViewpointService(db)
    success = service.delete_viewpoint(viewpoint_id)
    
    if not success:
        raise HTTPException(status_code=404, detail="观点不存在")
    
    return {"success": True, "message": "观点删除成功"}


@router.post("/batch-analyze")
async def batch_analyze_viewpoints(
    data: dict = Body(...),
    db: Session = Depends(get_db)
):
    """
    批量分析观点（一键分析未分析的观点）

    请求体:
    {
        "limit": 30,  // 最多分析数量，默认30
        "source": "all"  // 来源筛选: all/eastmoney_blog/eastmoney_guide/sina_finance/sina_blog
    }
    """
    try:
        limit = data.get('limit', 10)
        source = data.get('source', 'all')

        print(f"[Viewpoint Batch Analyze API] 开始批量分析: limit={limit}, source={source}")

        service = ViewpointService(db)
        viewpoints_to_analyze = service.get_viewpoints_for_batch_analyze(
            limit=limit,
            source=source,
            days=7
        )

        print(f"[Viewpoint Batch Analyze API] 找到 {len(viewpoints_to_analyze)} 个需要分析的观点")

        if not viewpoints_to_analyze:
            return {
                "success": True,
                "message": "没有需要分析的观点",
                "data": {
                    "analyzed_count": 0,
                    "total": 0
                }
            }

        from src.analyzer.viewpoint_analyzer import get_viewpoint_analyzer
        from src.analyzer.llm_analyzer import get_analyzer as get_llm_analyzer

        analyzer = get_viewpoint_analyzer()
        llm_analyzer = get_llm_analyzer()

        analyzed_count = 0
        failed_count = 0

        for viewpoint in viewpoints_to_analyze:
            try:
                result = analyzer.analyze_viewpoint(
                    title=viewpoint.content[:100] if viewpoint.content else "",
                    content=viewpoint.content or "",
                    author=viewpoint.author or "",
                    source=viewpoint.source or ""
                )

                time_horizon = result.get('time_horizon', 'medium')
                validity_map = {
                    'short': '1周',
                    'medium': '1个月',
                    'long': '3个月'
                }
                validity_period = validity_map.get(time_horizon, '1个月')

                valid_until = llm_analyzer.calculate_target_date(
                    date.today(),
                    validity_period
                )

                reasoning = f"【AI深度分析】{result.get('analysis', '')}\n\n【判断理由】{result.get('reasoning', '')}"

                service.update_viewpoint_analysis(
                    viewpoint_id=viewpoint.id,
                    market_direction=result.get('market_direction', 'neutral'),
                    confidence=result.get('confidence', 50),
                    sectors_bullish=result.get('sectors_bullish', []),
                    sectors_bearish=result.get('sectors_bearish', []),
                    reasoning=reasoning,
                    time_horizon=time_horizon,
                    validity_period=validity_period,
                    valid_until=valid_until,
                    summary=result.get('summary', ''),
                    credibility=result.get('credibility', 50),
                    key_points=result.get('key_points', []),
                    action_suggestion=result.get('action_suggestion', '观望'),
                    risk_level=result.get('risk_level', 'medium'),
                    sentiment_score=result.get('sentiment_score', 0.5)
                )

                analyzed_count += 1
                time.sleep(0.5)

            except Exception as e:
                print(f"[Viewpoint Batch Analyze API] 分析观点 {viewpoint.id} 失败: {e}")
                traceback.print_exc()
                failed_count += 1
                continue

        print(f"[Viewpoint Batch Analyze API] 批量分析完成: 成功={analyzed_count}, 失败={failed_count}")

        return {
            "success": True,
            "message": f"批量分析完成: 成功 {analyzed_count} 个, 失败 {failed_count} 个",
            "data": {
                "analyzed_count": analyzed_count,
                "failed_count": failed_count,
                "total": len(viewpoints_to_analyze)
            }
        }

    except Exception as e:
        print(f"[Viewpoint Batch Analyze API] 批量分析失败: {e}")
        traceback.print_exc()
        return {
            "success": False,
            "message": f"批量分析失败: {str(e)}",
            "data": None
        }


@router.post("/update-summaries")
async def update_viewpoint_summaries(db: Session = Depends(get_db)):
    """批量更新所有观点的一句话摘要"""
    try:
        from src.analyzer.llm_analyzer import get_analyzer
        
        viewpoints = db.query(Viewpoint).filter(
            Viewpoint.is_deleted == False
        ).all()
        
        print(f"[Update Summaries] 找到 {len(viewpoints)} 个观点需要更新")
        
        if not viewpoints:
            return {
                "success": True,
                "message": "没有需要更新的观点",
                "data": {"updated_count": 0}
            }
        
        llm_analyzer = get_analyzer()
        updated_count = 0
        failed_count = 0
        
        for viewpoint in viewpoints:
            try:
                if not viewpoint.content or len(viewpoint.content.strip()) < 10:
                    continue
                
                prompt = f"""请将以下观点内容浓缩成一句话摘要（不超过50字）：

【观点内容】
{viewpoint.content[:500]}

要求：
1. 提取核心观点和判断
2. 包含关键板块或基金
3. 保持客观中立
4. 不超过50字

请直接输出摘要内容，不要包含其他说明。"""
                
                summary = llm_analyzer._call_llm(prompt, task_type='summary', max_tokens=100, temperature=0.3)
                summary = summary.strip()[:200]
                
                viewpoint.summary = summary
                updated_count += 1
                
                if updated_count % 10 == 0:
                    print(f"[Update Summaries] 已更新 {updated_count} 个观点")
                    db.commit()
                
                time.sleep(0.3)
                
            except Exception as e:
                print(f"[Update Summaries] 更新观点 {viewpoint.id} 失败: {e}")
                failed_count += 1
                continue
        
        db.commit()
        
        print(f"[Update Summaries] 批量更新完成: 成功={updated_count}, 失败={failed_count}")
        
        return {
            "success": True,
            "message": f"批量更新完成: 成功 {updated_count} 个, 失败 {failed_count} 个",
            "data": {
                "updated_count": updated_count,
                "failed_count": failed_count,
                "total": len(viewpoints)
            }
        }
        
    except Exception as e:
        print(f"[Update Summaries] 批量更新失败: {e}")
        traceback.print_exc()
        return {
            "success": False,
            "message": f"批量更新失败: {str(e)}",
            "data": None
        }


@router.post("/cleanup")
async def cleanup_old_viewpoints(
    data: dict = Body(...),
    db: Session = Depends(get_db)
):
    """
    手动清理过期观点
    
    请求体:
    {
        "days": 10  // 保留天数，默认10天。超过此天数的观点将被删除
    }
    
    说明:
    - 观点只有近7天才会被投资建议采纳
    - 建议保留10天，给用户一定的缓冲时间
    - 删除后无法恢复，请谨慎操作
    """
    try:
        days = data.get('days', 10)
        
        if days < 7:
            return {
                "success": False,
                "message": "保留天数不能少于7天，因为观点需要保留用于投资建议分析"
            }
        
        from src.tasks.cleanup_tasks import get_cleanup_manager
        
        manager = get_cleanup_manager()
        result = manager.manual_cleanup_viewpoints(days=days)
        
        if result["success"]:
            return {
                "success": True,
                "message": result["message"],
                "data": {
                    "deleted_count": result["deleted_viewpoints"],
                    "cutoff_date": result["cutoff_date"]
                }
            }
        else:
            return {
                "success": False,
                "message": f"清理失败: {result.get('error', '未知错误')}"
            }
        
    except Exception as e:
        print(f"[Cleanup Viewpoints] 清理失败: {e}")
        traceback.print_exc()
        return {
            "success": False,
            "message": f"清理失败: {str(e)}"
        }


@router.get("/cleanup/preview")
async def preview_cleanup(
    days: int = 10,
    db: Session = Depends(get_db)
):
    """
    预览将被清理的观点
    
    参数:
    - days: 保留天数，默认10天
    
    返回将被删除的观点数量和部分示例
    """
    try:
        from datetime import date, timedelta
        
        cutoff_date = date.today() - timedelta(days=days)
        
        viewpoints = db.query(Viewpoint).filter(
            Viewpoint.viewpoint_date < cutoff_date
        ).all()
        
        total_count = len(viewpoints)
        
        sample_viewpoints = [
            {
                "id": v.id,
                "author": v.author,
                "source": v.source,
                "viewpoint_date": v.viewpoint_date.isoformat() if v.viewpoint_date else None,
                "summary": (v.summary[:50] + "...") if v.summary and len(v.summary) > 50 else v.summary
            }
            for v in viewpoints[:5]
        ]
        
        return {
            "success": True,
            "data": {
                "total_count": total_count,
                "cutoff_date": cutoff_date.isoformat(),
                "sample_viewpoints": sample_viewpoints,
                "message": f"将有 {total_count} 条观点被删除（{cutoff_date} 之前的观点）"
            }
        }
        
    except Exception as e:
        print(f"[Preview Cleanup] 预览失败: {e}")
        traceback.print_exc()
        return {
            "success": False,
            "message": f"预览失败: {str(e)}"
        }


@router.get("/summary/stats")
async def get_summary_stats(db: Session = Depends(get_db)):
    """获取汇总统计信息"""
    service = ViewpointService(db)
    stats = service.get_summary_stats()
    
    return {
        "success": True,
        "data": stats
    }


@router.post("/summary/preview")
async def preview_summary(
    data: dict = Body(...),
    db: Session = Depends(get_db)
):
    """
    预览汇总结果（不保存）
    
    请求体:
    {
        "date": "2026-03-07"  // 要预览的日期
    }
    """
    try:
        target_date_str = data.get('date')
        if not target_date_str:
            return {
                "success": False,
                "message": "请指定日期"
            }
        
        from datetime import datetime
        target_date = datetime.strptime(target_date_str, '%Y-%m-%d').date()
        
        service = ViewpointService(db)
        viewpoints = service.get_viewpoints_by_date(target_date)
        
        if not viewpoints:
            return {
                "success": False,
                "message": f"{target_date_str} 没有待汇总的观点"
            }
        
        return {
            "success": True,
            "data": {
                "date": target_date_str,
                "viewpoint_count": len(viewpoints),
                "viewpoints": [
                    {
                        "id": v.id,
                        "summary": v.summary or "",
                        "market_direction": v.market_direction,
                        "confidence": v.confidence,
                        "author": v.author,
                        "source": v.source
                    }
                    for v in viewpoints
                ]
            }
        }
        
    except Exception as e:
        print(f"[Preview Summary] 预览失败: {e}")
        traceback.print_exc()
        return {
            "success": False,
            "message": f"预览失败: {str(e)}"
        }


@router.post("/summary/execute")
async def execute_summary(
    data: dict = Body(...),
    db: Session = Depends(get_db)
):
    """
    执行汇总（一键汇总所有待汇总日期的观点）
    
    请求体:
    {
        "dates": ["2026-03-07", "2026-03-06"]  // 可选，不传则汇总所有待汇总日期
    }
    """
    try:
        from src.analyzer.llm_analyzer import summarize_viewpoints_by_date
        
        service = ViewpointService(db)
        
        if data.get('dates'):
            from datetime import datetime
            dates_to_summarize = [
                datetime.strptime(d, '%Y-%m-%d').date()
                for d in data['dates']
            ]
        else:
            pending_dates = service.get_pending_summary_dates()
            dates_to_summarize = [
                date.fromisoformat(d['date'])
                for d in pending_dates
            ]
        
        if not dates_to_summarize:
            return {
                "success": True,
                "message": "没有待汇总的观点",
                "data": {
                    "summarized_count": 0,
                    "total_viewpoints": 0
                }
            }
        
        summarized_count = 0
        total_viewpoints = 0
        failed_dates = []
        
        for target_date in dates_to_summarize:
            try:
                viewpoints = service.get_viewpoints_by_date(target_date)
                
                if not viewpoints:
                    continue
                
                viewpoints_data = [
                    {
                        "summary": v.summary if v.summary else (v.content[:300] if v.content else ""),
                        "market_direction": v.market_direction or "neutral",
                        "confidence": v.confidence or 50,
                        "sectors_bullish": v.sectors_bullish or [],
                        "sectors_bearish": v.sectors_bearish or []
                    }
                    for v in viewpoints
                ]
                
                summary_result = summarize_viewpoints_by_date(
                    viewpoints_data,
                    target_date.isoformat()
                )
                
                if not summary_result.get("success"):
                    failed_dates.append({
                        "date": target_date.isoformat(),
                        "error": summary_result.get("error", "汇总失败")
                    })
                    continue
                
                original_ids = [v.id for v in viewpoints]
                
                summary_viewpoint = service.create_summary_viewpoint(
                    viewpoint_date=target_date,
                    content=summary_result.get("content", ""),
                    market_direction=summary_result.get("market_direction", "neutral"),
                    confidence=summary_result.get("confidence", 50),
                    topics=summary_result.get("topics", []),
                    sectors_bullish=summary_result.get("sectors_bullish", []),
                    sectors_bearish=summary_result.get("sectors_bearish", []),
                    reasoning=summary_result.get("reasoning", ""),
                    original_count=len(viewpoints),
                    original_ids=original_ids
                )
                
                service.delete_viewpoints_by_ids(original_ids)
                
                summarized_count += 1
                total_viewpoints += len(viewpoints)
                
                print(f"[Execute Summary] 已汇总 {target_date}: {len(viewpoints)} 条观点")
                
                time.sleep(1)
                
            except Exception as e:
                print(f"[Execute Summary] 汇总 {target_date} 失败: {e}")
                failed_dates.append({
                    "date": target_date.isoformat(),
                    "error": str(e)
                })
                continue
        
        message = f"汇总完成: {summarized_count} 天, 共 {total_viewpoints} 条观点"
        if failed_dates:
            message += f", {len(failed_dates)} 天失败"
        
        return {
            "success": True,
            "message": message,
            "data": {
                "summarized_count": summarized_count,
                "total_viewpoints": total_viewpoints,
                "failed_dates": failed_dates
            }
        }
        
    except Exception as e:
        print(f"[Execute Summary] 执行失败: {e}")
        traceback.print_exc()
        return {
            "success": False,
            "message": f"执行失败: {str(e)}"
        }