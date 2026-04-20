"""
Fund Insight API - FastAPI 主应用
模块化架构入口文件
"""
from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from datetime import datetime
from pathlib import Path
import os
import sys

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path:
    sys.path.insert(0, project_root)

from src.core.config import config
from src.models.database import init_db

from src.api import eastmoney_routes
from src.api.prediction_groups import router as prediction_groups_router
from src.api.routes.batch_analysis import router as batch_analysis_router

from src.api.routes import (
    bloggers_router,
    posts_router,
    predictions_router,
    funds_router,
    viewpoints_router,
    crawler_router,
    advice_router,
    stats_router,
    config_router,
)

from src.api.routes.test_data import router as test_data_router

app = FastAPI(
    title="Fund Insight API",
    description="基金观点追踪与分析系统",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = Path(project_root) / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

web_dir = Path(project_root) / "web"
if web_dir.exists():
    app.mount("/web", StaticFiles(directory=str(web_dir)), name="web")

app.include_router(eastmoney_routes.router)
app.include_router(prediction_groups_router)

app.include_router(bloggers_router, prefix="/api")
app.include_router(posts_router, prefix="/api")
app.include_router(predictions_router, prefix="/api")
app.include_router(funds_router, prefix="/api")
app.include_router(viewpoints_router, prefix="/api")
app.include_router(crawler_router, prefix="/api")
app.include_router(advice_router, prefix="/api")
app.include_router(stats_router, prefix="/api")
app.include_router(config_router, prefix="/api")
app.include_router(test_data_router, prefix="/api")


@app.get("/api")
def read_root():
    return {
        "name": "Fund Insight API",
        "version": "2.0.0",
        "status": "running",
        "docs": "/docs"
    }


@app.get("/api/health")
def health_check():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.get("/api/market-sentiment")
def get_market_sentiment():
    """获取市场情绪"""
    from src.models.database import SessionLocal, Viewpoint
    from datetime import date, timedelta
    
    db = SessionLocal()
    try:
        recent_viewpoints = db.query(Viewpoint).filter(
            Viewpoint.viewpoint_date >= date.today() - timedelta(days=7)
        ).all()
        
        if not recent_viewpoints:
            return {
                "success": True,
                "data": {
                    "overall_sentiment": "neutral",
                    "confidence": 50,
                    "bullish_count": 0,
                    "bearish_count": 0,
                    "neutral_count": 0,
                    "hot_sectors": [],
                    "analysis": "暂无近期观点数据"
                }
            }
        
        bullish_count = sum(1 for v in recent_viewpoints if v.market_direction == 'bullish')
        bearish_count = sum(1 for v in recent_viewpoints if v.market_direction == 'bearish')
        neutral_count = sum(1 for v in recent_viewpoints if v.market_direction == 'neutral')
        
        total = len(recent_viewpoints)
        if bullish_count > bearish_count and bullish_count > neutral_count:
            overall = "bullish"
            confidence = int(bullish_count / total * 100)
        elif bearish_count > bullish_count and bearish_count > neutral_count:
            overall = "bearish"
            confidence = int(bearish_count / total * 100)
        else:
            overall = "neutral"
            confidence = int(neutral_count / total * 100) if neutral_count > 0 else 50
        
        sector_counts = {}
        for v in recent_viewpoints:
            for sector in (v.sectors_bullish or []):
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
            for sector in (v.sectors_bearish or []):
                sector_counts[sector] = sector_counts.get(sector, 0) + 1
        
        hot_sectors = sorted(sector_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        
        return {
            "success": True,
            "data": {
                "overall_sentiment": overall,
                "confidence": confidence,
                "bullish_count": bullish_count,
                "bearish_count": bearish_count,
                "neutral_count": neutral_count,
                "hot_sectors": [{"sector": s, "count": c} for s, c in hot_sectors],
                "analysis": f"近7天共{total}条观点，看多{bullish_count}条，看空{bearish_count}条，中性{neutral_count}条"
            }
        }
    finally:
        db.close()


@app.get("/")
async def serve_index():
    """服务首页"""
    index_file = web_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file), media_type="text/html")
    return {"message": "Fund Insight API - 请访问 /docs 查看 API 文档"}


@app.get("/index.html")
async def serve_index_html():
    """服务 index.html"""
    index_file = web_dir / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file), media_type="text/html")
    return {"error": "index.html not found"}


@app.get("/viewpoint-manager.html")
async def serve_viewpoint_manager():
    """服务观点管理页面"""
    html_file = web_dir / "viewpoint-manager.html"
    if html_file.exists():
        return FileResponse(str(html_file), media_type="text/html")
    return {"error": "viewpoint-manager.html not found"}


@app.get("/article-crawler.html")
async def serve_article_crawler():
    """服务文章爬虫页面"""
    html_file = web_dir / "article-crawler.html"
    if html_file.exists():
        return FileResponse(str(html_file), media_type="text/html")
    return {"error": "article-crawler.html not found"}


@app.get("/cleanup-manager.html")
async def serve_cleanup_manager():
    """服务清理管理页面"""
    html_file = web_dir / "cleanup-manager.html"
    if html_file.exists():
        return FileResponse(str(html_file), media_type="text/html")
    return {"error": "cleanup-manager.html not found"}


@app.get("/diagnostic.html")
async def serve_diagnostic():
    """服务诊断页面"""
    html_file = web_dir / "diagnostic.html"
    if html_file.exists():
        return FileResponse(str(html_file), media_type="text/html")
    return {"error": "diagnostic.html not found"}


@app.get("/test.html")
async def serve_test():
    """服务测试页面"""
    html_file = web_dir / "test.html"
    if html_file.exists():
        return FileResponse(str(html_file), media_type="text/html")
    return {"error": "test.html not found"}


@app.get("/simple.html")
async def serve_simple():
    """服务简化页面"""
    html_file = web_dir / "simple.html"
    if html_file.exists():
        return FileResponse(str(html_file), media_type="text/html")
    return {"error": "simple.html not found"}


@app.get("/vue-test.html")
async def serve_vue_test():
    """服务 Vue 测试页面"""
    html_file = web_dir / "vue-test.html"
    if html_file.exists():
        return FileResponse(str(html_file), media_type="text/html")
    return {"error": "vue-test.html not found"}


@app.on_event("startup")
async def startup_event():
    init_db()
    from src.tasks.scheduler import start_scheduler
    start_scheduler()
    print(f"[Startup] Fund Insight API v2.0.0 已启动")
    print(f"[Startup] LLM API: {'已配置' if config.LLM_API_KEY else '未配置'}")
    print(f"[Startup] 爬虫模块: {'已启用' if config.CRAWLER_ENABLED else '已禁用'}")
    print(f"[Startup] 定时任务调度器已启动")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
