"""
Fund Insight API - FastAPI 主应用
模块化架构入口文件
"""
from fastapi import FastAPI, Response, Request, status, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import os
import sys
import shutil
import tempfile
import logging
from sqlalchemy import create_engine, text, insert as sa_insert
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

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
from src.api.routes.sector_flow import router as sector_flow_router

from src.api.routes.test_data import router as test_data_router


class CachedStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        response.headers.setdefault("Cache-Control", "public, max-age=86400")
        return response


def _startup_migrations_enabled() -> bool:
    return os.getenv("ENABLE_STARTUP_MIGRATIONS", "false").lower() == "true"


def _run_startup_migrations() -> None:
    try:
        from src.models.database import engine
        from sqlalchemy import text

        missing_columns = [
            ("fund_history", "data_quality", "VARCHAR(20) DEFAULT 'normal'"),
            ("fund_history", "quality_note", "VARCHAR(200)"),
        ]

        with engine.connect() as conn:
            db_type = str(engine.url).split("://")[0]
            for table, column, col_def in missing_columns:
                try:
                    if db_type.startswith("postgresql"):
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {col_def}"))
                    else:
                        try:
                            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}"))
                        except Exception:
                            pass
                    conn.commit()
                except Exception as e:
                    if "already exists" not in str(e).lower():
                        logger.warning(f"[Startup] 添加列 {table}.{column} 失败: {e}")
    except Exception as e:
        logger.error(f"[Startup] 列补全检查失败: {e}")

    try:
        from src.models.database import engine
        from sqlalchemy import text
        import sqlite3

        # 允许创建索引的表名白名单（防止 SQL 注入）
        ALLOWED_INDEX_TABLES = {"posts", "predictions", "viewpoints", "bloggers"}

        indexes = [
            ("ix_posts_blogger_id", "posts", ["blogger_id"]),
            ("ix_posts_post_date", "posts", ["post_date"]),
            ("ix_posts_blogger_date", "posts", ["blogger_id", "post_date"]),
            ("ix_predictions_blogger_id", "predictions", ["blogger_id"]),
            ("ix_predictions_status", "predictions", ["status"]),
            ("ix_predictions_fund_code", "predictions", ["fund_code"]),
            ("ix_predictions_is_deleted", "predictions", ["is_deleted"]),
            ("ix_predictions_blogger_status", "predictions", ["blogger_id", "status", "is_deleted"]),
            ("ix_predictions_target_date", "predictions", ["target_date"]),
            ("ix_viewpoints_is_deleted", "viewpoints", ["is_deleted"]),
            ("ix_viewpoints_viewpoint_date", "viewpoints", ["viewpoint_date"]),
            ("ix_viewpoints_blogger_id", "viewpoints", ["blogger_id"]),
            ("ix_viewpoints_source", "viewpoints", ["source"]),
            ("ix_bloggers_platform", "bloggers", ["platform"]),
            ("ix_bloggers_is_active", "bloggers", ["is_active"]),
        ]

        # 验证所有表名都在白名单中（防御性编程）
        for idx_name, table, columns in indexes:
            if table not in ALLOWED_INDEX_TABLES:
                logger.warning(f"[Startup] 跳过未授权的表 {table} 的索引创建")
                indexes.remove((idx_name, table, columns))

        db_url = str(engine.url)
        if db_url.startswith("sqlite"):
            db_path = db_url.replace("sqlite:///", "")
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            for idx_name, table, columns in indexes:
                try:
                    cursor.execute(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({', '.join(columns)})")
                except Exception:
                    pass
            conn.commit()
            conn.close()
        else:
            with engine.connect() as conn:
                for idx_name, table, columns in indexes:
                    try:
                        conn.execute(text(f"CREATE INDEX IF NOT EXISTS {idx_name} ON {table} ({', '.join(columns)})"))
                    except Exception:
                        pass
                conn.commit()
    except Exception as e:
        logger.error(f"[Startup] 索引创建检查失败: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理：启动和关闭逻辑"""
    # ========== Startup ==========
    init_db()
    if _startup_migrations_enabled():
        _run_startup_migrations()

    config.load_persisted_config()

    logger.info(f"[Startup] Fund Insight API v2.0.0 已启动")
    logger.info(f"[Startup] LLM API: {'已配置' if config.LLM_API_KEY else '未配置'}")
    logger.info(f"[Startup] 爬虫模块: {'已启用' if config.CRAWLER_ENABLED else '已禁用'}")

    yield

    # ========== Shutdown ==========
    from src.fund.fund_api import fund_api
    fund_api.close()
    logger.info("[Shutdown] 资源已释放")


app = FastAPI(
    title="Fund Insight API",
    description="基金观点追踪与分析系统",
    version="2.0.0",
    lifespan=lifespan
)

# 密码验证中间件
@app.middleware("http")
async def password_auth_middleware(request: Request, call_next):
    # 放行所有非 /api/ 路径的请求（静态资源、HTML 页面等）
    if not request.url.path.startswith("/api/"):
        return await call_next(request)

    # 放行 CORS 预检请求
    if request.method == "OPTIONS":
        return await call_next(request)

    # 放行健康检查接口
    if request.url.path == "/api/health":
        return await call_next(request)
    
    # 从环境变量获取密码
    expected_password = os.getenv("ACCESS_PASSWORD", "Lwb1397111398")
    
    # 从请求头获取密码
    provided_password = request.headers.get("X-Access-Password")
    
    # 验证密码
    if provided_password != expected_password:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Unauthorized: Invalid or missing access password"}
        )
    
    # 密码正确，继续处理请求
    return await call_next(request)

cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:8002")
allowed_origins = [o.strip() for o in cors_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# GZip 压缩：index.html(254KB)+vue(162KB)+axios 明文传输，免费层带宽敏感
app.add_middleware(GZipMiddleware, minimum_size=1000)

static_dir = Path(project_root) / "static"
if static_dir.exists():
    app.mount("/static", CachedStaticFiles(directory=str(static_dir)), name="static")

web_dir = Path(project_root) / "web"
if web_dir.exists():
    app.mount("/web", CachedStaticFiles(directory=str(web_dir)), name="web")

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
app.include_router(batch_analysis_router, prefix="/api")
app.include_router(sector_flow_router, prefix="/api")


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
    from src.models.database import DB_TYPE, SessionLocal
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
    finally:
        db.close()
    return {"status": "ok", "timestamp": datetime.now().isoformat(), "db_type": DB_TYPE, "version": "2.0.0"}


@app.get("/api/health/detail")
def health_detail():
    from src.models.database import DB_TYPE, SessionLocal
    from src.tasks.scheduler import get_scheduler

    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        database_ok = True
    finally:
        db.close()

    return {
        "status": "ok" if database_ok else "degraded",
        "timestamp": datetime.now().isoformat(),
        "version": "2.0.0",
        "app_env": os.getenv("APP_ENV", "development"),
        "db_type": DB_TYPE,
        "database_ok": database_ok,
        "llm_configured": bool(config.LLM_API_KEY or config.VOLCENGINE_API_KEY),
        "crawler_enabled": config.CRAWLER_ENABLED,
        "startup_migrations_enabled": _startup_migrations_enabled(),
        "scheduler_running": get_scheduler().running,
    }


@app.get("/favicon.ico")
async def favicon():
    """避免浏览器请求 favicon 时产生 404 日志"""
    return Response(status_code=204)


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


@app.get("/import-data.html")
async def serve_import_page():
    """服务数据导入页面"""
    html_file = web_dir / "import-data.html"
    if html_file.exists():
        return FileResponse(str(html_file), media_type="text/html")
    return {"error": "import-data.html not found"}


@app.post("/api/import-database")
def import_database(file: UploadFile = File(...), request: Request = None):
    """导入 SQLite 数据库到 PostgreSQL（使用 ORM 自动处理类型转换）"""
    if os.getenv("ENABLE_DATABASE_IMPORT", "false").lower() != "true":
        raise HTTPException(status_code=403, detail="数据库导入接口已禁用")
    if request is None or request.headers.get("X-Danger-Confirm") != "import-production-database":
        raise HTTPException(status_code=403, detail="缺少数据库导入确认头")

    # 验证文件扩展名
    if not file.filename or not file.filename.endswith('.db'):
        raise HTTPException(status_code=400, detail="仅支持 .db 文件")

    # 验证文件大小（最大 100MB）
    max_size = 100 * 1024 * 1024  # 100MB
    file.file.seek(0, 2)  # 移动到文件末尾
    file_size = file.file.tell()
    file.file.seek(0)  # 重置文件指针
    if file_size > max_size:
        raise HTTPException(status_code=400, detail="文件大小超过限制（最大 100MB）")

    try:
        import tempfile
        import json as json_module
        from src.models.database import (
            Blogger, Post, Prediction, Viewpoint, FundInfo, FundHistory,
            SectorFundMapping, InvestmentAdvice, CrawlerArticleRecord,
            PredictionGroup, BatchAnalysisTask, UserFundBinding, SyncLog,
            FundHolding, MarketData, PolicyData, SentimentData, SectorFundFlow,
            Base as TargetBase
        )
        
        # 创建临时文件
        temp_dir = tempfile.mkdtemp()
        temp_file = os.path.join(temp_dir, "import.db")
        
        # 保存上传的文件
        with open(temp_file, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # 验证文件头魔术字节（SQLite 数据库验证）
        with open(temp_file, "rb") as f:
            header = f.read(16)
            if header != b"SQLite format 3\x00":
                os.remove(temp_file)
                os.rmdir(temp_dir)
                raise HTTPException(status_code=400, detail="无效的 SQLite 数据库文件")

        # 连接源数据库（SQLite）- 使用反射获取表结构
        source_engine = create_engine(f"sqlite:///{temp_file}")
        SourceSession = sessionmaker(bind=source_engine)
        source_db = SourceSession()
        
        from src.models.database import SessionLocal, engine as target_engine, DB_TYPE
        target_db = SessionLocal()
        
        try:
            # 允许导入的表名白名单（防止 SQL 注入）
            ALLOWED_IMPORT_TABLES = {
                'bloggers', 'posts', 'predictions', 'viewpoints', 'fund_info',
                'fund_history', 'sector_fund_mapping', 'investment_advice',
                'crawler_article_records', 'prediction_groups', 'batch_analysis_tasks',
                'user_fund_bindings', 'sync_logs', 'fund_holdings', 'market_data',
                'policy_data', 'sentiment_data', 'sector_fund_flow'
            }

            orm_map = [
                ('bloggers', Blogger),
                ('posts', Post),
                ('predictions', Prediction),
                ('viewpoints', Viewpoint),
                ('fund_info', FundInfo),
                ('fund_history', FundHistory),
                ('sector_fund_mapping', SectorFundMapping),
                ('investment_advice', InvestmentAdvice),
                ('crawler_article_records', CrawlerArticleRecord),
                ('prediction_groups', PredictionGroup),
                ('batch_analysis_tasks', BatchAnalysisTask),
                ('user_fund_bindings', UserFundBinding),
                ('sync_logs', SyncLog),
                ('fund_holdings', FundHolding),
                ('market_data', MarketData),
                ('policy_data', PolicyData),
                ('sentiment_data', SentimentData),
                ('sector_fund_flow', SectorFundFlow),
            ]

            # 验证所有表名都在白名单中（防御性编程）
            for table_name, _ in orm_map:
                if table_name not in ALLOWED_IMPORT_TABLES:
                    raise HTTPException(status_code=400, detail=f"未授权的表名: {table_name}")

            tables_to_delete = [name for name, _ in reversed(orm_map)]
            
            imported_counts = {}
            skipped_counts = {}
            errors = []
            
            fk_disabled = False
            if DB_TYPE == "postgresql":
                try:
                    target_db.execute(text("SET session_replication_role = 'replica'"))
                    target_db.commit()
                    fk_disabled = True
                    logger.info("[导入] 已禁用 PostgreSQL 外键约束检查")
                except Exception as e:
                    logger.error(f"[导入] 禁用外键约束失败: {e}")
                    target_db.rollback()

            logger.info("[导入] 开始清空目标表...")
            for table_name in tables_to_delete:
                try:
                    target_db.execute(text(f"DELETE FROM {table_name}"))
                    target_db.commit()
                except Exception as e:
                    target_db.rollback()
                    if DB_TYPE == "postgresql":
                        try:
                            target_db.execute(text(f"TRUNCATE TABLE {table_name} CASCADE"))
                            target_db.commit()
                        except:
                            target_db.rollback()
            
            logger.info("[导入] 开始导入数据（ORM 模式）...")
            for table_name, ModelClass in orm_map:
                try:
                    result = source_db.execute(text(f"SELECT * FROM {table_name}"))
                    rows = result.fetchall()
                    
                    if not rows:
                        imported_counts[table_name] = 0
                        continue
                    
                    columns = list(result.keys())
                    model_columns = {c.name: c for c in ModelClass.__table__.columns}
                    
                    row_skipped = 0
                    batch_count = 0
                    for row in rows:
                        try:
                            row_dict = dict(zip(columns, row))
                            cleaned = {}

                            for key, val in row_dict.items():
                                if key not in model_columns:
                                    continue

                                col = model_columns[key]
                                col_type = str(col.type)

                                if val is None:
                                    cleaned[key] = None
                                elif 'DATE' in col_type.upper() and 'TIME' not in col_type.upper():
                                    if isinstance(val, str) and val:
                                        from datetime import date as date_type
                                        try:
                                            cleaned[key] = date_type.fromisoformat(val[:10])
                                        except:
                                            cleaned[key] = None
                                    elif isinstance(val, date):
                                        cleaned[key] = val
                                    else:
                                        cleaned[key] = None
                                elif 'DATETIME' in col_type.upper() or 'TIMESTAMP' in col_type.upper():
                                    if isinstance(val, str) and val:
                                        try:
                                            cleaned[key] = datetime.fromisoformat(val.replace('Z', '+00:00'))
                                        except:
                                            cleaned[key] = None
                                    elif isinstance(val, datetime):
                                        cleaned[key] = val
                                    else:
                                        cleaned[key] = None
                                elif 'BOOLEAN' in col_type.upper():
                                    if isinstance(val, int):
                                        cleaned[key] = bool(val)
                                    elif isinstance(val, str):
                                        cleaned[key] = val.lower() in ('true', '1', 'yes')
                                    else:
                                        cleaned[key] = val
                                elif 'JSON' in col_type.upper():
                                    if isinstance(val, str):
                                        try:
                                            cleaned[key] = json_module.loads(val) if val.strip() else None
                                        except:
                                            cleaned[key] = val
                                    else:
                                        cleaned[key] = val
                                else:
                                    cleaned[key] = val

                            target_db.execute(sa_insert(ModelClass.__table__).values(**cleaned))
                            batch_count += 1
                            # 每 500 行提交一次
                            if batch_count % 500 == 0:
                                target_db.commit()
                        except Exception as e:
                            target_db.rollback()
                            row_skipped += 1
                            if row_skipped <= 5:
                                logger.warning(f"[导入] 跳过 {table_name} 一行: {str(e)[:200]}")
                            continue
                    # 提交剩余的记录
                    target_db.commit()
                    
                    imported_counts[table_name] = len(rows) - row_skipped
                    if row_skipped > 0:
                        skipped_counts[table_name] = row_skipped
                    logger.info(f"[导入] 表 {table_name}: 导入 {len(rows) - row_skipped}/{len(rows)} 行")

                except Exception as e:
                    logger.error(f"[导入] 表 {table_name} 完全失败: {e}")
                    imported_counts[table_name] = 0
                    errors.append(f"表 {table_name} 失败: {str(e)[:200]}")
                    target_db.rollback()
            
            if DB_TYPE == "postgresql":
                for table_name, _ in orm_map:
                    try:
                        seq_sql = text(
                            f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), "
                            f"COALESCE((SELECT MAX(id) FROM {table_name}), 1), "
                            f"COALESCE((SELECT MAX(id) FROM {table_name}) IS NOT NULL, false))"
                        )
                        target_db.execute(seq_sql)
                    except:
                        pass
                try:
                    target_db.commit()
                    logger.info("[导入] PostgreSQL 序列重置完成")
                except:
                    target_db.rollback()
                
                if fk_disabled:
                    try:
                        target_db.execute(text("SET session_replication_role = 'origin'"))
                        target_db.commit()
                    except:
                        target_db.rollback()
            
            result_data = {
                "success": True,
                "message": "数据库导入成功",
                "imported": imported_counts
            }
            if skipped_counts:
                result_data["skipped"] = skipped_counts
            if errors:
                result_data["errors"] = errors
            return result_data
        finally:
            source_db.close()
            target_db.close()
            source_engine.dispose()
            try:
                os.remove(temp_file)
                os.rmdir(temp_dir)
            except:
                pass
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
