"""
Fund Insight API - FastAPI 主应用
模块化架构入口文件
"""
from fastapi import FastAPI, Response, Request, status, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from datetime import datetime
from pathlib import Path
import os
import sys
import shutil
import tempfile
from sqlalchemy import create_engine, text, insert as sa_insert
from sqlalchemy.orm import sessionmaker

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

# 密码验证中间件
@app.middleware("http")
async def password_auth_middleware(request: Request, call_next):
    # 放行所有非 /api/ 路径的请求（静态资源、HTML 页面等）
    if not request.url.path.startswith("/api/"):
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
    from src.models.database import DB_TYPE, SessionLocal
    # 附加数据库记录数，方便排查导入问题
    try:
        db = SessionLocal()
        from src.models.database import Blogger, Post, Prediction, Viewpoint, FundInfo, FundHistory
        counts = {
            "bloggers": db.query(Blogger).count(),
            "posts": db.query(Post).count(),
            "predictions": db.query(Prediction).filter(Prediction.is_deleted == False).count(),
            "viewpoints": db.query(Viewpoint).filter(Viewpoint.is_deleted == False).count(),
            "fund_info": db.query(FundInfo).count(),
            "fund_history": db.query(FundHistory).count(),
        }
        db.close()
    except Exception as e:
        counts = {"error": str(e)}
    return {"status": "ok", "timestamp": datetime.now().isoformat(), "db_type": DB_TYPE, "version": "2.0.0", "counts": counts}


@app.get("/favicon.ico")
async def favicon():
    """避免浏览器请求 favicon 时产生 404 日志"""
    return Response(status_code=204)


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


@app.get("/import-data.html")
async def serve_import_page():
    """服务数据导入页面"""
    html_content = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>数据导入工具</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 2rem;
        }
        .container {
            max-width: 800px;
            margin: 0 auto;
            background: white;
            border-radius: 16px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            padding: 2.5rem;
        }
        h1 {
            color: #1a202c;
            font-size: 2rem;
            margin-bottom: 0.5rem;
        }
        .subtitle {
            color: #718096;
            margin-bottom: 2rem;
        }
        .upload-area {
            border: 3px dashed #cbd5e0;
            border-radius: 12px;
            padding: 3rem 2rem;
            text-align: center;
            cursor: pointer;
            transition: all 0.3s ease;
            margin-bottom: 1.5rem;
        }
        .upload-area:hover, .upload-area.dragover {
            border-color: #667eea;
            background: #f7fafc;
        }
        .upload-icon {
            font-size: 4rem;
            margin-bottom: 1rem;
        }
        .upload-text {
            color: #4a5568;
            font-size: 1.1rem;
        }
        .upload-hint {
            color: #a0aec0;
            font-size: 0.9rem;
            margin-top: 0.5rem;
        }
        input[type="file"] {
            display: none;
        }
        .btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 1rem 2rem;
            border-radius: 8px;
            font-size: 1rem;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
            width: 100%;
        }
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 20px rgba(102, 126, 234, 0.4);
        }
        .btn:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        .progress-area {
            margin-top: 2rem;
            display: none;
        }
        .progress-bar {
            width: 100%;
            height: 20px;
            background: #e2e8f0;
            border-radius: 10px;
            overflow: hidden;
        }
        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #667eea, #764ba2);
            width: 0%;
            transition: width 0.3s ease;
        }
        .status-text {
            margin-top: 1rem;
            color: #4a5568;
        }
        .result-area {
            margin-top: 2rem;
            padding: 1.5rem;
            background: #f7fafc;
            border-radius: 8px;
            display: none;
        }
        .result-success {
            color: #2f855a;
            font-weight: 600;
        }
        .result-error {
            color: #c53030;
            font-weight: 600;
        }
        .result-details {
            margin-top: 1rem;
            font-size: 0.9rem;
            color: #4a5568;
        }
        .password-input {
            width: 100%;
            padding: 0.75rem 1rem;
            border: 2px solid #e2e8f0;
            border-radius: 8px;
            font-size: 1rem;
            margin-bottom: 1rem;
            transition: border-color 0.3s ease;
        }
        .password-input:focus {
            outline: none;
            border-color: #667eea;
        }
        .label {
            display: block;
            color: #4a5568;
            font-weight: 600;
            margin-bottom: 0.5rem;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>📊 数据导入工具</h1>
        <p class="subtitle">将本地 SQLite 数据库导入到云端 PostgreSQL</p>
        
        <div class="label">访问密码</div>
        <input type="password" id="password" class="password-input" placeholder="请输入访问密码">
        
        <div class="upload-area" id="uploadArea">
            <div class="upload-icon">📁</div>
            <div class="upload-text">点击或拖拽上传数据库文件</div>
            <div class="upload-hint">支持 .db 和 .sqlite 文件</div>
        </div>
        <input type="file" id="fileInput" accept=".db,.sqlite">
        
        <button class="btn" id="uploadBtn" disabled>开始导入</button>
        
        <div class="progress-area" id="progressArea">
            <div class="progress-bar">
                <div class="progress-fill" id="progressFill"></div>
            </div>
            <div class="status-text" id="statusText">准备中...</div>
        </div>
        
        <div class="result-area" id="resultArea">
            <div id="resultText"></div>
            <div class="result-details" id="resultDetails"></div>
        </div>
    </div>
    
    <script>
        const uploadArea = document.getElementById('uploadArea');
        const fileInput = document.getElementById('fileInput');
        const uploadBtn = document.getElementById('uploadBtn');
        const progressArea = document.getElementById('progressArea');
        const progressFill = document.getElementById('progressFill');
        const statusText = document.getElementById('statusText');
        const resultArea = document.getElementById('resultArea');
        const resultText = document.getElementById('resultText');
        const resultDetails = document.getElementById('resultDetails');
        const passwordInput = document.getElementById('password');
        
        let selectedFile = null;
        
        uploadArea.addEventListener('click', () => fileInput.click());
        
        uploadArea.addEventListener('dragover', (e) => {
            e.preventDefault();
            uploadArea.classList.add('dragover');
        });
        
        uploadArea.addEventListener('dragleave', () => {
            uploadArea.classList.remove('dragover');
        });
        
        uploadArea.addEventListener('drop', (e) => {
            e.preventDefault();
            uploadArea.classList.remove('dragover');
            if (e.dataTransfer.files.length > 0) {
                handleFile(e.dataTransfer.files[0]);
            }
        });
        
        fileInput.addEventListener('change', (e) => {
            if (e.target.files.length > 0) {
                handleFile(e.target.files[0]);
            }
        });
        
        function handleFile(file) {
            if (file.name.endsWith('.db') || file.name.endsWith('.sqlite')) {
                selectedFile = file;
                uploadArea.innerHTML = `
                    <div class="upload-icon">✅</div>
                    <div class="upload-text">已选择: ${file.name}</div>
                    <div class="upload-hint">文件大小: ${(file.size / 1024).toFixed(2)} KB</div>
                `;
                uploadBtn.disabled = false;
            } else {
                alert('请选择 .db 或 .sqlite 格式的数据库文件');
            }
        }
        
        uploadBtn.addEventListener('click', async () => {
            if (!selectedFile) return;
            
            const password = passwordInput.value;
            if (!password) {
                alert('请输入访问密码');
                return;
            }
            
            // 先验证密码是否正确
            try {
                const testRes = await fetch('/api/health');
                const testRes2 = await fetch('/api/stats', {
                    headers: { 'X-Access-Password': password }
                });
                if (testRes2.status === 401) {
                    alert('密码错误，请重新输入');
                    return;
                }
            } catch (e) {
                alert('无法连接到服务器，请检查网络');
                return;
            }
            
            const formData = new FormData();
            formData.append('file', selectedFile);
            
            progressArea.style.display = 'block';
            uploadBtn.disabled = true;
            
            try {
                progressFill.style.width = '10%';
                statusText.textContent = '正在上传文件（大文件可能需要较长时间，请耐心等待）...';
                
                const controller = new AbortController();
                const timeoutId = setTimeout(() => controller.abort(), 300000); // 5分钟超时
                
                const response = await fetch('/api/import-database', {
                    method: 'POST',
                    headers: {
                        'X-Access-Password': password
                    },
                    body: formData,
                    signal: controller.signal
                });
                
                clearTimeout(timeoutId);
                
                if (!response.ok) {
                    throw new Error(`服务器返回错误: ${response.status} ${response.statusText}`);
                }
                
                const result = await response.json();
                
                progressFill.style.width = '100%';
                
                resultArea.style.display = 'block';
                
                if (result.success) {
                    resultText.className = 'result-success';
                    resultText.textContent = '✅ 导入成功！';
                    resultDetails.innerHTML = `
                        <p>📊 导入详情：</p>
                        <ul>
                            ${Object.entries(result.imported || {}).map(([table, count]) => 
                                `<li>${table}: ${count} 条记录</li>`
                            ).join('')}
                        </ul>
                    `;
                    statusText.textContent = '导入完成！';
                } else {
                    resultText.className = 'result-error';
                    resultText.textContent = '❌ 导入失败';
                    resultDetails.textContent = result.error || '未知错误';
                    statusText.textContent = '导入失败';
                }
            } catch (error) {
                resultArea.style.display = 'block';
                resultText.className = 'result-error';
                resultText.textContent = '❌ 导入失败';
                if (error.name === 'AbortError') {
                    resultDetails.textContent = '请求超时（5分钟），文件可能过大，请尝试使用更小的数据库文件';
                } else {
                    resultDetails.textContent = error.message || '网络错误，请检查服务器是否正常运行';
                }
                statusText.textContent = '导入失败';
            }
        });
    </script>
</body>
</html>
    """
    return Response(content=html_content, media_type="text/html")


@app.post("/api/import-database")
async def import_database(file: UploadFile = File(...), request: Request = None):
    """导入 SQLite 数据库到 PostgreSQL（使用 ORM 自动处理类型转换）"""
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
        
        # 连接源数据库（SQLite）- 使用反射获取表结构
        source_engine = create_engine(f"sqlite:///{temp_file}")
        SourceSession = sessionmaker(bind=source_engine)
        source_db = SourceSession()
        
        # 连接目标数据库
        from src.models.database import SessionLocal, engine as target_engine, DB_TYPE
        target_db = SessionLocal()
        
        # ORM 模型映射：表名 -> (源查询类, 目标ORM类)
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
        
        # 清空顺序：反向删除
        tables_to_delete = [name for name, _ in reversed(orm_map)]
        
        imported_counts = {}
        skipped_counts = {}
        errors = []
        
        # PostgreSQL: 尝试禁用外键约束
        fk_disabled = False
        if DB_TYPE == "postgresql":
            try:
                target_db.execute(text("SET session_replication_role = 'replica'"))
                target_db.commit()
                fk_disabled = True
                print("[导入] 已禁用 PostgreSQL 外键约束检查")
            except Exception as e:
                print(f"[导入] 禁用外键约束失败: {e}")
                target_db.rollback()
        
        # 第一步：清空所有目标表
        print("[导入] 开始清空目标表...")
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
        
        # 第二步：逐表导入数据（使用 ORM 方式，自动处理类型转换）
        print("[导入] 开始导入数据（ORM 模式）...")
        for table_name, ModelClass in orm_map:
            try:
                # 从源数据库用原始 SQL 读取（因为源库没有 ORM 模型）
                result = source_db.execute(text(f"SELECT * FROM {table_name}"))
                rows = result.fetchall()
                
                if not rows:
                    imported_counts[table_name] = 0
                    continue
                
                columns = list(result.keys())
                # 获取目标 ORM 模型的列信息
                model_columns = {c.name: c for c in ModelClass.__table__.columns}
                
                row_skipped = 0
                for row in rows:
                    try:
                        row_dict = dict(zip(columns, row))
                        cleaned = {}
                        
                        for key, val in row_dict.items():
                            if key not in model_columns:
                                continue  # 跳过目标表中不存在的列
                            
                            col = model_columns[key]
                            col_type = str(col.type)
                            
                            # 类型转换
                            if val is None:
                                cleaned[key] = None
                            # Date 类型
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
                            # DateTime 类型
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
                            # Boolean 类型
                            elif 'BOOLEAN' in col_type.upper():
                                if isinstance(val, int):
                                    cleaned[key] = bool(val)
                                elif isinstance(val, str):
                                    cleaned[key] = val.lower() in ('true', '1', 'yes')
                                else:
                                    cleaned[key] = val
                            # JSON 类型
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
                        
                        # 用 SQLAlchemy Core insert 语句写入（避免 ORM relationship 级联问题）
                        target_db.execute(sa_insert(ModelClass.__table__).values(**cleaned))
                    except Exception as e:
                        row_skipped += 1
                        if row_skipped <= 3:
                            print(f"[导入] 跳过 {table_name} 一行: {str(e)[:200]}")
                        continue
                
                target_db.commit()
                imported_counts[table_name] = len(rows) - row_skipped
                if row_skipped > 0:
                    skipped_counts[table_name] = row_skipped
                print(f"[导入] 表 {table_name}: 导入 {len(rows) - row_skipped}/{len(rows)} 行")
                
            except Exception as e:
                print(f"[导入] 表 {table_name} 完全失败: {e}")
                imported_counts[table_name] = 0
                errors.append(f"表 {table_name} 失败: {str(e)[:200]}")
                target_db.rollback()
        
        # PostgreSQL 序列重置
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
                print("[导入] PostgreSQL 序列重置完成")
            except:
                target_db.rollback()
            
            if fk_disabled:
                try:
                    target_db.execute(text("SET session_replication_role = 'origin'"))
                    target_db.commit()
                except:
                    target_db.rollback()
        
        # 清理
        source_db.close()
        target_db.close()
        try:
            os.remove(temp_file)
            os.rmdir(temp_dir)
        except:
            pass
        
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
        
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


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
