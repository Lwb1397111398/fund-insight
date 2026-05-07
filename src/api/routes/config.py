"""
配置路由
处理配置相关的 API 请求
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional
from datetime import date, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.core.config import config
from src.api.deps import get_db
from src.models.database import Prediction, Viewpoint, Post, FundInfo, Blogger

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
    """运行数据清理"""
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
        
        predictions_list = []
        for p in expired_predictions:
            blogger = db.query(Blogger).filter(Blogger.id == p.blogger_id).first()
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
        
        posts_list = []
        for p in empty_posts:
            blogger = db.query(Blogger).filter(Blogger.id == p.blogger_id).first()
            posts_list.append({
                "id": p.id,
                "title": p.title or "(无标题)",
                "blogger_name": blogger.name if blogger else "-",
                "post_date": p.post_date.isoformat() if p.post_date else None
            })
        
        funds_with_predictions = db.query(Prediction.fund_code).filter(
            Prediction.fund_code != None,
            Prediction.is_deleted == False
        ).distinct().subquery()
        
        unused_funds = db.query(FundInfo).filter(
            ~FundInfo.fund_code.in_(funds_with_predictions)
        ).all()
        
        funds_list = []
        for f in unused_funds:
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
