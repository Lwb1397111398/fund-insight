"""
配置路由
处理配置相关的 API 请求
"""
import os
import json
from fastapi import APIRouter, Depends, UploadFile, File
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional
from datetime import date, datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.core.config import config
from src.api.deps import get_db
from src.models.database import (
    Prediction, Viewpoint, Post, FundInfo, Blogger,
    SectorAlias, SectorFundMapping, InvestmentAdvice,
    FundHistory
)

router = APIRouter(prefix="/config", tags=["配置"])


class ConfigUpdate(BaseModel):
    """配置更新请求"""
    llm_api_key: Optional[str] = None
    llm_base_url: Optional[str] = None
    llm_model: Optional[str] = None
    llm_light_model: Optional[str] = None
    llm_provider: Optional[str] = None
    volcengine_api_key: Optional[str] = None
    volcengine_model: Optional[str] = None
    volcengine_light_model: Optional[str] = None


@router.get("")
async def get_config():
    """获取配置信息"""
    return {
        "success": True,
        "data": {
            "llm_provider": config.LLM_PROVIDER,
            "llm_api_key_set": bool(config.LLM_API_KEY),
            "llm_base_url": config.LLM_BASE_URL,
            "llm_model": config.LLM_MODEL,
            "llm_light_model": config.LLM_LIGHT_MODEL,
            "llm_strategy": config.LLM_STRATEGY,
            "volcengine_api_key_set": bool(config.VOLCENGINE_API_KEY),
            "volcengine_base_url": config.VOLCENGINE_BASE_URL,
            "volcengine_model": config.VOLCENGINE_MODEL,
            "volcengine_light_model": config.VOLCENGINE_LIGHT_MODEL,
            "server_host": config.SERVER_HOST,
            "server_port": config.SERVER_PORT,
            "crawler_enabled": config.CRAWLER_ENABLED,
            "crawler_request_delay": config.CRAWLER_REQUEST_DELAY,
            "max_posts_per_fund": config.MAX_POSTS_PER_FUND,
            "crawler_timeout": config.CRAWLER_TIMEOUT
        }
    }


@router.post("")
async def update_config(config_update: ConfigUpdate):
    """更新配置"""
    updated = []
    
    if config_update.llm_provider:
        config.LLM_PROVIDER = config_update.llm_provider
        updated.append("llm_provider")
    
    if config_update.llm_api_key:
        config.LLM_API_KEY = config_update.llm_api_key
        updated.append("llm_api_key")
    
    if config_update.llm_base_url:
        config.LLM_BASE_URL = config_update.llm_base_url
        updated.append("llm_base_url")
    
    if config_update.llm_model:
        config.LLM_MODEL = config_update.llm_model
        updated.append("llm_model")
    
    if config_update.llm_light_model:
        config.LLM_LIGHT_MODEL = config_update.llm_light_model
        updated.append("llm_light_model")
    
    if config_update.volcengine_api_key:
        config.VOLCENGINE_API_KEY = config_update.volcengine_api_key
        updated.append("volcengine_api_key")
    
    if config_update.volcengine_model:
        config.VOLCENGINE_MODEL = config_update.volcengine_model
        updated.append("volcengine_model")
    
    if config_update.volcengine_light_model:
        config.VOLCENGINE_LIGHT_MODEL = config_update.volcengine_light_model
        updated.append("volcengine_light_model")
    
    if updated:
        from src.analyzer.llm_analyzer import reset_analyzer
        reset_analyzer()
        config.save_persisted_config()

    return {
        "success": True,
        "message": f"已更新配置: {', '.join(updated)}" if updated else "无更新",
        "data": {
            "updated_fields": updated
        }
    }


@router.post("/cleanup")
async def run_cleanup():
    """运行数据清理（包括过期预测、观点、空帖子、孤儿基金等）"""
    try:
        from src.tasks.cleanup_tasks import get_cleanup_manager

        manager = get_cleanup_manager()
        result = manager.run_full_cleanup()

        if result.get("success"):
            predictions = result.get("predictions", {})
            viewpoints = result.get("viewpoints", {})
            fund_history = result.get("fund_history", {})
            empty_posts = result.get("empty_posts", {})
            advice = result.get("advice", {})
            orphan_funds = result.get("orphan_funds", {})
            total = result.get("total_deleted", 0)

            parts = []
            if predictions.get('deleted', 0) > 0:
                parts.append(f"{predictions.get('deleted', 0)} 个过期预测")
            if viewpoints.get('deleted', 0) > 0:
                parts.append(f"{viewpoints.get('deleted', 0)} 个过期观点")
            if fund_history.get('deleted', 0) > 0:
                parts.append(f"{fund_history.get('deleted', 0)} 条基金历史记录")
            if empty_posts.get('deleted', 0) > 0:
                parts.append(f"{empty_posts.get('deleted', 0)} 个空帖子")
            if advice.get('deleted', 0) > 0:
                parts.append(f"{advice.get('deleted', 0)} 条过期投资建议")
            if orphan_funds.get('deleted', 0) > 0:
                parts.append(f"{orphan_funds.get('deleted', 0)} 个孤儿基金")

            if parts:
                message = f"清理完成，共删除 {total} 项：{', '.join(parts)}"
            else:
                message = "清理完成，没有需要清理的数据"

            return {
                "success": True,
                "message": message,
                "data": result
            }
        else:
            return {
                "success": False,
                "message": "清理失败",
                "data": result
            }
    except Exception as e:
        return {
            "success": False,
            "message": f"清理失败: {str(e)}",
            "data": None
        }


@router.post("/cleanup/oldest")
async def cleanup_oldest_batch(days: int = 7, limit: int = 100):
    """
    温和清理：只清理最老的一批过期数据

    与 /cleanup 的区别：
    - /cleanup 清理所有过期数据
    - /cleanup/oldest 只清理过期最久的那一批，每批限制数量，避免一次性清理过多影响博主统计

    Args:
        days: 额外回溯天数，默认7天。即清理过期超过(7+days)天的数据
        limit: 每类数据最多清理条数，默认100
    """
    try:
        from src.tasks.cleanup_tasks import get_cleanup_manager

        manager = get_cleanup_manager()
        result = manager.cleanup_oldest_batch(batch_days=days, limit=limit)

        if result.get("success"):
            predictions = result.get("predictions", {})
            viewpoints = result.get("viewpoints", {})
            total = result.get("total_deleted", 0)

            parts = []
            pred_count = predictions.get("deleted_predictions", 0)
            vp_count = viewpoints.get("deleted_viewpoints", 0)
            if pred_count > 0:
                parts.append(f"{pred_count} 个过期预测")
            if vp_count > 0:
                parts.append(f"{vp_count} 个过期观点")

            if parts:
                message = f"温和清理完成，共删除 {total} 项：{', '.join(parts)}"
            else:
                message = "温和清理完成，没有需要清理的最老数据"

            return {
                "success": True,
                "message": message,
                "data": result
            }
        else:
            return {
                "success": False,
                "message": "温和清理失败",
                "data": result
            }
    except Exception as e:
        return {
            "success": False,
            "message": f"温和清理失败: {str(e)}",
            "data": None
        }


@router.get("/cleanup/orphan-funds/preview")
async def preview_orphan_funds():
    """
    预览可清理的孤儿基金

    孤儿基金条件：
    1. can_delete = True（没有活跃预测关联）
    2. 没有任何预测使用该基金（包括已删除的预测）
    3. 不是核心基金（is_core_fund = False）
    """
    try:
        from src.tasks.cleanup_tasks import get_cleanup_manager
        manager = get_cleanup_manager()
        result = manager.cleanup_orphan_funds(preview_only=True)

        if result.get("success"):
            orphan_count = result.get("total_orphans", 0)
            return {
                "success": True,
                "message": f"发现 {orphan_count} 个可清理的孤儿基金",
                "data": result
            }
        else:
            return {
                "success": False,
                "message": "获取孤儿基金列表失败",
                "data": result
            }
    except Exception as e:
        return {
            "success": False,
            "message": f"获取孤儿基金列表失败: {str(e)}",
            "data": None
        }


@router.post("/cleanup/orphan-funds")
async def cleanup_orphan_funds():
    """
    清理无关联的孤儿基金

    会同时删除基金的历史净值数据，请谨慎操作！
    """
    try:
        from src.tasks.cleanup_tasks import get_cleanup_manager
        manager = get_cleanup_manager()
        result = manager.cleanup_orphan_funds(preview_only=False)

        if result.get("success"):
            deleted_count = result.get("deleted_count", 0)
            if deleted_count > 0:
                message = f"清理完成，删除了 {deleted_count} 个孤儿基金及其历史净值数据"
            else:
                message = "没有需要清理的孤儿基金"
            return {
                "success": True,
                "message": message,
                "data": result
            }
        else:
            return {
                "success": False,
                "message": "清理孤儿基金失败",
                "data": result
            }
    except Exception as e:
        return {
            "success": False,
            "message": f"清理孤儿基金失败: {str(e)}",
            "data": None
        }


@router.get("/cleanup/preview")
async def get_cleanup_preview(db: Session = Depends(get_db)):
    """
    获取可清理数据预览
    
    返回真正满足清理条件的数据：
    - 过期预测：target_date < 今天-7天
    - 过期观点：viewpoint_date < 今天-10天
    - 空帖子：没有任何预测的帖子
    - 无用基金：没有预测关联的基金
    - 无用博主：没有任何预测的博主
    """
    try:
        today = date.today()
        
        cutoff_7_days = today - timedelta(days=7)
        expired_predictions = db.query(Prediction).filter(
            Prediction.target_date < cutoff_7_days,
            Prediction.is_deleted == False
        ).all()
        
        # 批量加载所有需要的 blogger，避免 N+1 查询
        blogger_ids = list(set(p.blogger_id for p in expired_predictions if p.blogger_id))
        bloggers_map = {b.id: b for b in db.query(Blogger).filter(Blogger.id.in_(blogger_ids)).all()} if blogger_ids else {}

        predictions_list = []
        for p in expired_predictions:
            blogger = bloggers_map.get(p.blogger_id)
            predictions_list.append({
                "id": p.id,
                "blogger_id": p.blogger_id,
                "blogger_name": blogger.name if blogger else "-",
                "sector": p.sector,
                "prediction_content": p.prediction_content,
                "target_date": p.target_date.isoformat() if p.target_date else None,
                "is_correct": p.is_correct
            })
        
        cutoff_10_days = today - timedelta(days=10)
        expired_viewpoints = db.query(Viewpoint).filter(
            Viewpoint.viewpoint_date < cutoff_10_days
        ).all()
        
        viewpoints_list = []
        for v in expired_viewpoints:
            viewpoints_list.append({
                "id": v.id,
                "source": v.source,
                "author": v.author,
                "content": v.content,
                "valid_until": v.valid_until.isoformat() if v.valid_until else None
            })
        
        posts_with_predictions = db.query(Prediction.post_id).filter(
            Prediction.is_deleted == False
        ).distinct().subquery()
        
        empty_posts = db.query(Post).filter(
            ~Post.id.in_(posts_with_predictions)
        ).all()
        
        # 批量加载所有需要的 blogger，避免 N+1 查询
        post_blogger_ids = list(set(p.blogger_id for p in empty_posts if p.blogger_id))
        post_bloggers_map = {b.id: b for b in db.query(Blogger).filter(Blogger.id.in_(post_blogger_ids)).all()} if post_blogger_ids else {}

        posts_list = []
        for p in empty_posts:
            blogger = post_bloggers_map.get(p.blogger_id)
            posts_list.append({
                "id": p.id,
                "title": p.title or "(无标题)",
                "blogger_name": blogger.name if blogger else "-",
                "post_date": p.post_date.isoformat() if p.post_date else None
            })
        
        # 与 cleanup_orphan_funds 保持一致的逻辑
        # 所有预测使用的基金代码（包括已删除的）
        used_fund_codes = set(
            row[0] for row in db.query(Prediction.fund_code).filter(
                Prediction.fund_code.isnot(None),
                Prediction.fund_code != ''
            ).distinct().all()
        )
        # 板块映射中的基金代码
        mapped_fund_codes = set(
            row[0] for row in db.query(SectorFundMapping.fund_code).filter(
                SectorFundMapping.fund_code.isnot(None),
                SectorFundMapping.fund_code != ''
            ).distinct().all()
        )

        all_funds = db.query(FundInfo).all()
        funds_list = []
        for f in all_funds:
            if f.is_core_fund:
                continue
            if not f.can_delete:
                continue
            if f.active_predictions and f.active_predictions > 0:
                continue
            if f.fund_code in used_fund_codes:
                continue
            if f.fund_code in mapped_fund_codes:
                continue
            funds_list.append({
                "id": f.id,
                "fund_code": f.fund_code,
                "fund_name": f.fund_name,
                "sector_type": f.sector_type
            })
        
        bloggers_with_predictions = db.query(Prediction.blogger_id).filter(
            Prediction.is_deleted == False
        ).distinct().subquery()
        
        unused_bloggers = db.query(Blogger).filter(
            ~Blogger.id.in_(bloggers_with_predictions)
        ).all()
        
        bloggers_list = []
        for b in unused_bloggers:
            bloggers_list.append({
                "id": b.id,
                "name": b.name,
                "grade": b.grade
            })
        
        return {
            "success": True,
            "data": {
                "predictions": predictions_list,
                "viewpoints": viewpoints_list,
                "posts": posts_list,
                "funds": funds_list,
                "bloggers": bloggers_list,
                "summary": {
                    "predictions_count": len(predictions_list),
                    "viewpoints_count": len(viewpoints_list),
                    "posts_count": len(posts_list),
                    "funds_count": len(funds_list),
                    "bloggers_count": len(bloggers_list),
                    "total": len(predictions_list) + len(viewpoints_list) + len(posts_list) + len(funds_list) + len(bloggers_list)
                }
            }
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"获取清理预览失败: {str(e)}",
            "data": None
        }


@router.post("/test-llm")
async def test_llm():
    """测试LLM连接"""
    try:
        from src.analyzer.llm_analyzer import get_analyzer
        
        analyzer = get_analyzer()
        
        test_prompt = "你好，请回复'LLM连接成功！'这四个字，不要回复其他内容。"
        
        result = analyzer._call_llm(test_prompt, task_type='simple', max_tokens=50, temperature=0.1)
        
        return {
            "success": True,
            "message": "LLM连接测试成功",
            "data": {
                "provider": config.LLM_PROVIDER,
                "model": analyzer.model if hasattr(analyzer, 'model') else config.LLM_MODEL,
                "response": result.strip() if result else None
            }
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"LLM连接测试失败: {str(e)}",
            "data": {
                "provider": config.LLM_PROVIDER,
                "error": str(e)
            }
        }


@router.post("/test-volcengine-light")
async def test_volcengine_light():
    """测试火山引擎辅助模型"""
    try:
        from src.analyzer.llm_analyzer import get_analyzer
        
        analyzer = get_analyzer()
        
        if config.LLM_PROVIDER != 'volcengine':
            return {
                "success": False,
                "message": "辅助模型测试仅支持火山引擎",
                "data": None
            }
        
        test_prompt = "你好，请回复'辅助模型连接成功！'这六个字，不要回复其他内容。"
        result = analyzer._call_llm_with_model(
            config.VOLCENGINE_LIGHT_MODEL,
            test_prompt,
            max_tokens=50,
            temperature=0.1
        )
        
        return {
            "success": True,
            "message": "火山引擎辅助模型测试成功",
            "data": {
                "provider": config.LLM_PROVIDER,
                "light_model": config.VOLCENGINE_LIGHT_MODEL,
                "response": result.strip() if result else None
            }
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"辅助模型测试失败: {str(e)}",
            "data": {
                "provider": config.LLM_PROVIDER,
                "error": str(e)
            }
        }


# ===== 板块别名管理 =====

class AliasCreate(BaseModel):
    """创建别名请求"""
    alias_name: str
    sector_name: str


@router.get("/aliases")
async def get_aliases(db: Session = Depends(get_db)):
    """获取所有别名（硬编码+自定义）"""
    from src.constants.sector_fund_map import SECTOR_ALIASES, SECTOR_FUND_MAP

    # 硬编码别名
    builtin = [
        {"alias_name": k, "sector_name": v, "source": "builtin"}
        for k, v in sorted(SECTOR_ALIASES.items())
    ]

    # 数据库自定义别名
    custom_rows = db.query(SectorAlias).order_by(SectorAlias.created_at.desc()).all()
    custom = [
        {
            "id": a.id,
            "alias_name": a.alias_name,
            "sector_name": a.sector_name,
            "source": "custom",
            "created_at": a.created_at.isoformat() if a.created_at else None
        }
        for a in custom_rows
    ]

    return {
        "success": True,
        "data": {
            "builtin": builtin,
            "custom": custom,
            "total": len(builtin) + len(custom),
            "standard_sectors": sorted(SECTOR_FUND_MAP.keys())
        }
    }


@router.post("/aliases")
async def create_alias(alias: AliasCreate, db: Session = Depends(get_db)):
    """添加自定义别名"""
    # 检查是否与已有别名冲突
    existing = db.query(SectorAlias).filter(SectorAlias.alias_name == alias.alias_name).first()
    if existing:
        return {
            "success": False,
            "message": f"别名 '{alias.alias_name}' 已存在（映射到 {existing.sector_name}）"
        }

    # 检查是否与硬编码别名冲突
    from src.constants.sector_fund_map import SECTOR_ALIASES, SECTOR_FUND_MAP
    if alias.alias_name in SECTOR_ALIASES:
        return {
            "success": False,
            "message": f"'{alias.alias_name}' 是系统内置别名（映射到 {SECTOR_ALIASES[alias.alias_name]}），无需重复添加"
        }
    if alias.alias_name in SECTOR_FUND_MAP:
        return {
            "success": False,
            "message": f"'{alias.alias_name}' 是系统内置板块名，无需添加为别名"
        }

    new_alias = SectorAlias(alias_name=alias.alias_name, sector_name=alias.sector_name)
    db.add(new_alias)
    db.commit()
    db.refresh(new_alias)

    # 刷新别名缓存
    from src.constants.sector_fund_map import refresh_db_aliases_cache
    refresh_db_aliases_cache()

    return {
        "success": True,
        "message": f"已添加别名: {alias.alias_name} → {alias.sector_name}",
        "data": {
            "id": new_alias.id,
            "alias_name": new_alias.alias_name,
            "sector_name": new_alias.sector_name
        }
    }


@router.delete("/aliases/{alias_id}")
async def delete_alias(alias_id: int, db: Session = Depends(get_db)):
    """删除自定义别名"""
    alias = db.query(SectorAlias).filter(SectorAlias.id == alias_id).first()
    if not alias:
        return {"success": False, "message": "别名不存在"}

    db.delete(alias)
    db.commit()

    # 刷新别名缓存
    from src.constants.sector_fund_map import refresh_db_aliases_cache
    refresh_db_aliases_cache()

    return {
        "success": True,
        "message": f"已删除别名: {alias.alias_name} → {alias.sector_name}"
    }


# ===== 板块匹配管理 =====

class MappingUpdate(BaseModel):
    """更新映射请求"""
    fund_code: Optional[str] = None
    fund_name: Optional[str] = None


class MappingCreate(BaseModel):
    """创建映射请求"""
    sector_name: str
    fund_code: str
    fund_name: Optional[str] = None


class BatchReviewRequest(BaseModel):
    """批量审查请求"""
    ids: list[int]
    reviewed: bool = True


@router.get("/sector-mappings")
async def get_sector_mappings(
    reviewed: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """获取所有板块映射（含 reviewed 状态 + 硬编码映射）"""
    from src.services.sector_fund_service import get_sector_fund_service
    from src.constants.sector_fund_map import SECTOR_FUND_MAP

    # 手动解析布尔值（FastAPI 对 query string 的 bool 解析不可靠）
    reviewed_filter = None
    if reviewed is not None:
        reviewed_filter = reviewed.lower() in ('true', '1', 'yes')

    service = get_sector_fund_service(db)
    db_mappings = service.get_all_mappings_with_status(reviewed_filter=reviewed_filter)

    # 收集 DB 中已有的板块名
    db_sectors = {m['sector_name'] for m in db_mappings}

    # 合入硬编码映射中 DB 没有的条目
    merged = list(db_mappings)
    for sector_name, fund_info in sorted(SECTOR_FUND_MAP.items()):
        if sector_name not in db_sectors:
            merged.append({
                'id': None,
                'sector_name': sector_name,
                'fund_code': fund_info.get('code', ''),
                'fund_name': fund_info.get('name', ''),
                'reviewed': True,
                'source': 'builtin'
            })

    mappings = merged
    reviewed_count = sum(1 for m in mappings if m['reviewed'])
    unreviewed_count = len(mappings) - reviewed_count

    return {
        "success": True,
        "data": {
            "mappings": mappings,
            "total": len(mappings),
            "reviewed_count": reviewed_count,
            "unreviewed_count": unreviewed_count
        }
    }


@router.put("/sector-mappings/{mapping_id}")
async def update_sector_mapping(mapping_id: int, update: MappingUpdate, db: Session = Depends(get_db)):
    """更新映射（自动标记为已审查）"""
    from src.services.sector_fund_service import get_sector_fund_service

    try:
        service = get_sector_fund_service(db)
        result = service.update_mapping(
            mapping_id=mapping_id,
            fund_code=update.fund_code,
            fund_name=update.fund_name
        )

        if not result:
            return {"success": False, "message": "映射不存在"}

        # 级联清理冲突
        if result.get('sector_name') and result.get('fund_code'):
            try:
                service.cascade_cleanup_conflicts(
                    result['sector_name'], result['fund_code'], result.get('fund_name', '')
                )
            except Exception as e:
                print(f"[板块匹配] 级联清理失败（不影响保存）: {e}")

        return {
            "success": True,
            "message": f"已更新映射: {result['sector_name']} → {result['fund_name']}（自动标记为已审查）",
            "data": result
        }
    except Exception as e:
        return {"success": False, "message": f"保存失败: {str(e)}"}


@router.post("/sector-mappings")
async def create_sector_mapping(mapping: MappingCreate, db: Session = Depends(get_db)):
    """创建新的板块映射（覆盖内置映射或新增）"""
    from src.services.sector_fund_service import get_sector_fund_service

    try:
        service = get_sector_fund_service(db)

        # 检查是否已存在同板块的 DB 映射
        existing_mapping = db.query(SectorFundMapping).filter(
            SectorFundMapping.sector_name == mapping.sector_name
        ).first()
        if existing_mapping:
            # 已存在，更新
            result = service.update_mapping(
                mapping_id=existing_mapping.id,
                fund_code=mapping.fund_code,
                fund_name=mapping.fund_name
            )
            if result and result.get('sector_name') and result.get('fund_code'):
                try:
                    service.cascade_cleanup_conflicts(
                        result['sector_name'], result['fund_code'], result.get('fund_name', '')
                    )
                except Exception as e:
                    print(f"[板块匹配] 级联清理失败（不影响保存）: {e}")
            return {
                "success": True,
                "message": f"已更新映射: {mapping.sector_name} → {mapping.fund_name or mapping.fund_code}",
                "data": result
            }

        # 不存在，创建新记录
        from src.models.database import SectorFundMapping
        new_mapping = SectorFundMapping(
            sector_name=mapping.sector_name,
            fund_code=mapping.fund_code,
            fund_name=mapping.fund_name or '',
            reviewed=True
        )
        db.add(new_mapping)
        db.commit()
        db.refresh(new_mapping)

        # 级联清理冲突
        if mapping.fund_code:
            try:
                service.cascade_cleanup_conflicts(
                    mapping.sector_name, mapping.fund_code, mapping.fund_name or ''
                )
            except Exception as e:
                print(f"[板块匹配] 级联清理失败（不影响保存）: {e}")

        return {
            "success": True,
            "message": f"已创建映射: {mapping.sector_name} → {mapping.fund_name or mapping.fund_code}",
            "data": {
                "id": new_mapping.id,
                "sector_name": new_mapping.sector_name,
                "fund_code": new_mapping.fund_code,
                "fund_name": new_mapping.fund_name
            }
        }
    except Exception as e:
        db.rollback()
        return {"success": False, "message": f"创建失败: {str(e)}"}@router.post("/sector-mappings/{mapping_id}/review")
async def review_sector_mapping(mapping_id: int, db: Session = Depends(get_db)):
    """标记映射为已审查"""
    from src.services.sector_fund_service import get_sector_fund_service

    service = get_sector_fund_service(db)
    success = service.mark_reviewed_by_id(mapping_id, reviewed=True)

    if not success:
        return {"success": False, "message": "映射不存在"}

    return {
        "success": True,
        "message": "已标记为已审查"
    }


@router.post("/sector-mappings/batch-review")
async def batch_review_sector_mappings(req: BatchReviewRequest, db: Session = Depends(get_db)):
    """批量标记映射为已审查/未审查"""
    from src.services.sector_fund_service import get_sector_fund_service

    service = get_sector_fund_service(db)
    count = service.batch_mark_reviewed(req.ids, reviewed=req.reviewed)

    action = "已审查" if req.reviewed else "未审查"
    return {
        "success": True,
        "message": f"已将 {count} 个映射标记为{action}",
        "data": {"count": count}
    }


@router.delete("/sector-mappings/{mapping_id}")
async def delete_sector_mapping(mapping_id: int, db: Session = Depends(get_db)):
    """删除映射"""
    from src.services.sector_fund_service import get_sector_fund_service

    service = get_sector_fund_service(db)
    success = service.delete_mapping(mapping_id)

    if not success:
        return {"success": False, "message": "映射不存在"}

    return {
        "success": True,
        "message": "已删除映射"
    }


@router.post("/sector-mappings/seed")
async def seed_sector_mappings(db: Session = Depends(get_db)):
    """导入预置板块映射数据"""
    try:
        import subprocess
        import sys
        result = subprocess.run(
            [sys.executable, "scripts/seed_sector_mappings.py"],
            capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        )

        # 刷新服务缓存
        from src.services.sector_fund_service import get_sector_fund_service
        service = get_sector_fund_service(db)
        service.refresh_cache()

        return {
            "success": True,
            "message": "预置数据导入完成",
            "data": {
                "stdout": result.stdout[-500:] if result.stdout else "",
                "stderr": result.stderr[-500:] if result.stderr else ""
            }
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"导入失败: {str(e)}"
        }


# ===== 数据导入导出 =====

def _serialize_row(obj, exclude_fields=None):
    """将 SQLAlchemy 模型对象序列化为字典"""
    exclude = set(exclude_fields or [])
    d = {}
    for col in obj.__table__.columns:
        if col.name in exclude:
            continue
        val = getattr(obj, col.name)
        if isinstance(val, (date, datetime)):
            d[col.name] = val.isoformat()
        else:
            d[col.name] = val
    return d


@router.get("/export")
async def export_all_data(db: Session = Depends(get_db)):
    """导出全部业务数据为 JSON 文件"""
    try:
        bloggers = [_serialize_row(b) for b in db.query(Blogger).all()]
        posts = [_serialize_row(p) for p in db.query(Post).all()]
        predictions = [_serialize_row(p, exclude_fields=['llm_raw_response'])
                       for p in db.query(Prediction).all()]
        viewpoints = [_serialize_row(v) for v in db.query(Viewpoint).all()]
        funds = [_serialize_row(f) for f in db.query(FundInfo).all()]
        fund_history = [_serialize_row(h) for h in db.query(FundHistory).all()]
        aliases = [_serialize_row(a) for a in db.query(SectorAlias).all()]
        mappings = [_serialize_row(m) for m in db.query(SectorFundMapping).all()]
        advice = [_serialize_row(a) for a in db.query(InvestmentAdvice).all()]

        export_data = {
            "export_version": "1.0",
            "export_date": datetime.now().isoformat(),
            "bloggers": bloggers,
            "posts": posts,
            "predictions": predictions,
            "viewpoints": viewpoints,
            "fund_info": funds,
            "fund_history": fund_history,
            "sector_alias": aliases,
            "sector_fund_mapping": mappings,
            "investment_advice": advice,
            "summary": {
                "bloggers": len(bloggers),
                "posts": len(posts),
                "predictions": len(predictions),
                "viewpoints": len(viewpoints),
                "fund_info": len(funds),
                "fund_history": len(fund_history),
                "sector_alias": len(aliases),
                "sector_fund_mapping": len(mappings),
                "investment_advice": len(advice)
            }
        }

        json_bytes = json.dumps(export_data, ensure_ascii=False, indent=2).encode('utf-8')
        filename = f"fund_insight_export_{date.today().isoformat()}.json"

        return Response(
            content=json_bytes,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        return {"success": False, "message": f"导出失败: {str(e)}"}


@router.get("/export/config")
async def export_config(db: Session = Depends(get_db)):
    """导出系统配置（LLM 设置 + 别名 + 板块映射）"""
    try:
        from src.constants.sector_fund_map import SECTOR_ALIASES

        aliases_custom = [_serialize_row(a) for a in db.query(SectorAlias).all()]
        mappings = [_serialize_row(m) for m in db.query(SectorFundMapping).all()]

        config_data = {
            "export_version": "1.0",
            "export_date": datetime.now().isoformat(),
            "type": "config",
            "llm_config": {
                "llm_provider": config.LLM_PROVIDER,
                "llm_base_url": config.LLM_BASE_URL,
                "llm_model": config.LLM_MODEL,
                "llm_light_model": config.LLM_LIGHT_MODEL,
                "llm_strategy": config.LLM_STRATEGY,
                "volcengine_base_url": config.VOLCENGINE_BASE_URL,
                "volcengine_model": config.VOLCENGINE_MODEL,
                "volcengine_light_model": config.VOLCENGINE_LIGHT_MODEL,
            },
            "builtin_aliases": SECTOR_ALIASES,
            "custom_aliases": aliases_custom,
            "sector_mappings": mappings
        }

        json_bytes = json.dumps(config_data, ensure_ascii=False, indent=2).encode('utf-8')
        filename = f"fund_insight_config_{date.today().isoformat()}.json"

        return Response(
            content=json_bytes,
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )
    except Exception as e:
        return {"success": False, "message": f"导出配置失败: {str(e)}"}


class ImportDataRequest(BaseModel):
    """导入数据请求"""
    data: dict


@router.post("/import")
async def import_data(req: ImportDataRequest, db: Session = Depends(get_db)):
    """
    导入 JSON 数据（合并模式：跳过已存在的记录，按 natural key 判断）
    """
    try:
        data = req.data
        imported = {}
        skipped = {}

        # 导入博主
        if "bloggers" in data:
            count, skip = 0, 0
            for item in data["bloggers"]:
                existing = db.query(Blogger).filter(Blogger.id == item.get("id")).first()
                if existing:
                    skip += 1
                    continue
                blogger = Blogger(**{k: v for k, v in item.items()
                                     if hasattr(Blogger, k) and k != 'id'})
                if 'id' in item:
                    blogger.id = item['id']
                db.add(blogger)
                count += 1
            imported["bloggers"] = count
            skipped["bloggers"] = skip

        # 导入帖子
        if "posts" in data:
            count, skip = 0, 0
            for item in data["posts"]:
                existing = db.query(Post).filter(Post.id == item.get("id")).first()
                if existing:
                    skip += 1
                    continue
                post = Post(**{k: v for k, v in item.items()
                               if hasattr(Post, k) and k != 'id'})
                if 'id' in item:
                    post.id = item['id']
                db.add(post)
                count += 1
            imported["posts"] = count
            skipped["posts"] = skip

        # 导入基金信息
        if "fund_info" in data:
            count, skip = 0, 0
            for item in data["fund_info"]:
                existing = db.query(FundInfo).filter(
                    FundInfo.fund_code == item.get("fund_code")).first()
                if existing:
                    skip += 1
                    continue
                fund = FundInfo(**{k: v for k, v in item.items()
                                   if hasattr(FundInfo, k)})
                db.add(fund)
                count += 1
            imported["fund_info"] = count
            skipped["fund_info"] = skip

        # 导入预测
        if "predictions" in data:
            count, skip = 0, 0
            for item in data["predictions"]:
                existing = db.query(Prediction).filter(
                    Prediction.id == item.get("id")).first()
                if existing:
                    skip += 1
                    continue
                pred = Prediction(**{k: v for k, v in item.items()
                                     if hasattr(Prediction, k) and k != 'id'})
                if 'id' in item:
                    pred.id = item['id']
                db.add(pred)
                count += 1
            imported["predictions"] = count
            skipped["predictions"] = skip

        # 导入观点
        if "viewpoints" in data:
            count, skip = 0, 0
            for item in data["viewpoints"]:
                existing = db.query(Viewpoint).filter(
                    Viewpoint.id == item.get("id")).first()
                if existing:
                    skip += 1
                    continue
                vp = Viewpoint(**{k: v for k, v in item.items()
                                  if hasattr(Viewpoint, k) and k != 'id'})
                if 'id' in item:
                    vp.id = item['id']
                db.add(vp)
                count += 1
            imported["viewpoints"] = count
            skipped["viewpoints"] = skip

        # 导入板块别名
        if "sector_alias" in data:
            count, skip = 0, 0
            for item in data["sector_alias"]:
                existing = db.query(SectorAlias).filter(
                    SectorAlias.alias_name == item.get("alias_name")).first()
                if existing:
                    skip += 1
                    continue
                alias = SectorAlias(**{k: v for k, v in item.items()
                                       if hasattr(SectorAlias, k) and k != 'id'})
                db.add(alias)
                count += 1
            imported["sector_alias"] = count
            skipped["sector_alias"] = skip

        # 导入板块映射
        if "sector_fund_mapping" in data:
            count, skip = 0, 0
            for item in data["sector_fund_mapping"]:
                existing = db.query(SectorFundMapping).filter(
                    SectorFundMapping.sector_name == item.get("sector_name"),
                    SectorFundMapping.fund_code == item.get("fund_code")
                ).first()
                if existing:
                    skip += 1
                    continue
                mapping = SectorFundMapping(**{k: v for k, v in item.items()
                                               if hasattr(SectorFundMapping, k) and k != 'id'})
                db.add(mapping)
                count += 1
            imported["sector_fund_mapping"] = count
            skipped["sector_fund_mapping"] = skip

        db.commit()

        total_imported = sum(imported.values())
        total_skipped = sum(skipped.values())

        parts = []
        for table, count in imported.items():
            if count > 0:
                parts.append(f"{table}: {count} 条")

        if total_imported > 0:
            message = f"导入完成，共导入 {total_imported} 条记录"
            if total_skipped > 0:
                message += f"（跳过 {total_skipped} 条已存在记录）"
        else:
            message = f"无新数据导入（{total_skipped} 条已存在）"

        return {
            "success": True,
            "message": message,
            "data": {
                "imported": imported,
                "skipped": skipped,
                "total_imported": total_imported,
                "total_skipped": total_skipped
            }
        }
    except Exception as e:
        db.rollback()
        return {"success": False, "message": f"导入失败: {str(e)}"}
