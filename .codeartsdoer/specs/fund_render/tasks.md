# 编码任务规划

## 任务 1：修复后端密码中间件 - 放行非 API 请求
- **文件**: `src/api/main.py`
- **描述**: 修改 `password_auth_middleware`，仅对 `/api/` 路径的请求验证密码，放行所有静态资源请求（HTML、JS、CSS 等）
- **验收条件**: 访问 `/`、`/index.html`、`/web/vue.global.prod.js` 等路径不需要密码；访问 `/api/*` 路径仍需密码

## 任务 2：修复前端 axios 请求 - 添加密码请求头
- **文件**: `web/index.html`
- **描述**: 
  1. 在 Vue app 初始化前，配置 axios 默认请求头 `X-Access-Password`
  2. 添加密码输入弹窗：首次访问时从 localStorage 读取密码，若无则弹出输入框
  3. 添加 axios 响应拦截器：收到 401 时清除 localStorage 密码并重新弹出输入框
  4. 密码验证：输入后调用 `/api/stats` 验证密码正确性
- **验收条件**: 前端所有 API 请求自动携带 `X-Access-Password` 头；密码错误时弹出重新输入框

## 任务 3：增强数据库引擎选择逻辑 - PostgreSQL 连接池
- **文件**: `src/models/database.py`
- **描述**:
  1. 添加 `DB_TYPE` 全局变量（"postgresql" 或 "sqlite"）
  2. PostgreSQL 引擎添加连接池参数：`pool_size=5, max_overflow=10, pool_recycle=300, pool_pre_ping=True`
  3. 添加 PostgreSQL 驱动缺失时的回退处理（try-except 导入 psycopg2）
  4. 修复 `init_db()` 函数：根据 `DB_TYPE` 输出不同的日志信息
- **验收条件**: 设置 `DATABASE_URL=postgresql://...` 时使用 PostgreSQL 引擎和连接池；未设置时使用 SQLite

## 任务 4：优化健康检查接口 - 返回数据库类型
- **文件**: `src/api/main.py`
- **描述**: 修改 `/api/health` 接口，返回 `db_type` 和 `version` 字段
- **验收条件**: `GET /api/health` 返回 `{"status": "ok", "timestamp": "...", "db_type": "postgresql", "version": "2.0.0"}`

## 任务 5：完善数据导入接口 - PostgreSQL 序列重置
- **文件**: `src/api/main.py`
- **描述**:
  1. 扩展导入表列表：添加 `prediction_groups`、`batch_analysis_tasks`、`user_fund_bindings`、`sync_logs`、`fund_holdings`、`market_data`、`policy_data`、`sentiment_data`、`sector_fund_flow`
  2. 导入完成后，如果目标数据库是 PostgreSQL，对每张表执行序列重置 SQL
  3. 改进错误处理：单行导入失败时记录日志但不中断整表导入
- **验收条件**: 导入数据后，PostgreSQL 中新插入的记录能正确获取自增 ID

## 任务 6：完善 render.yaml 部署配置
- **文件**: `render.yaml`
- **描述**: 添加必要的环境变量配置，包括 ACCESS_PASSWORD、LLM_PROVIDER、VOLCENGINE_API_KEY、VOLCENGINE_BASE_URL、VOLCENGINE_MODEL 等（敏感值使用 sync: false）
- **验收条件**: Render 部署后应用能正常启动，所有环境变量正确注入

## 任务 7：更新 .env.example 配置示例
- **文件**: `.env.example`
- **描述**: 添加 DATABASE_URL 和 ACCESS_PASSWORD 配置项及注释说明
- **验收条件**: .env.example 包含所有新增环境变量的示例和说明
