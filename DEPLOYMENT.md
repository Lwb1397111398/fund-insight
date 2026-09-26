# Fund Insight 部署与运维指南

最后更新：2026-07-22

## 当前部署形态

Fund Insight 当前按三部分运行：

```text
Render Web Service
  -> FastAPI 应用
  -> Supabase PostgreSQL

Render Cron
  -> 每日任务：板块资金流、基金更新、预测验证
  -> Supabase PostgreSQL

GitHub Actions
  -> 交易日板块资金流抓取
  -> Supabase PostgreSQL
```

本地开发默认使用 SQLite，不需要 Supabase。

## 本地运行

```bash
pip install -r requirements.txt
copy .env.example .env
python -m src --init-db
python -m src --port 8002
```

访问：

```text
http://localhost:8002
```

## Render Web Service

配置文件：`render.yaml`

启动命令：

```bash
uvicorn src.api.main:app --host 0.0.0.0 --port $PORT
```

关键环境变量：

| 变量 | 说明 |
| --- | --- |
| `PYTHON_VERSION` | Render 当前为 `3.10.12` |
| `APP_ENV` | `production` |
| `DATABASE_URL` | Supabase/PostgreSQL 连接串，secret |
| `ACCESS_PASSWORD` | API 访问密码，secret |
| `CORS_ORIGINS` | 生产域名，例如 `https://fund-insight.onrender.com` |
| `LLM_PROVIDER` | 当前生产使用 `volcengine` |
| `VOLCENGINE_API_KEY` | 火山引擎密钥，secret |
| `VOLCENGINE_BASE_URL` | 火山引擎 API 地址 |
| `VOLCENGINE_MODEL` | 主模型 |
| `VOLCENGINE_LIGHT_MODEL` | 轻量模型 |
| `DB_POOL_SIZE` | PostgreSQL 连接池大小 |
| `DB_MAX_OVERFLOW` | PostgreSQL 最大溢出连接 |
| `DB_POOL_RECYCLE` | 连接回收秒数 |
| `DB_POOL_TIMEOUT` | 连接池等待秒数 |

注意：

- `ENABLE_DATABASE_IMPORT` 默认必须保持 `false`。
- `ENABLE_TEST_DATA_CLEANUP` 默认必须保持 `false`；它按关键词硬删除数据，仅能在隔离维护环境短时开启。
- `ENABLE_DATA_CLEANUP` 默认 `true`（保留策略已保护待验证/长期预测净值与博主归档准确率）；紧急停用时设 `false`。执行仍需 `X-Danger-Confirm`。
- `ENABLE_STARTUP_MIGRATIONS` 默认必须保持 `false`，除非明确要补列/补索引。
- `CRAWLER_ENABLED` 在 Render Web 当前为 `true`，但爬虫仍应由用户或任务触发。

## Render Cron

`render.yaml` 中定义了 `fund-insight-scheduler`。

计划：

```text
30 10 * * *
```

命令：

```bash
python scripts/run_scheduled_tasks.py daily
```

执行内容：

1. `init_db()`。
2. `_run_sector_flow(trigger="render_cron")`。
3. `_run_fund_update()`。
4. `_run_prediction_verify()`。

统一预测验证会同时处理普通到期预测和超过 30 天的旧待验证预测；旧补救入口只保留为兼容代理，不再由 Cron 重复调用。

失败时命令返回非 0，Render 会显示 Cron 失败。

## GitHub Actions

主要工作流：

| 文件 | 用途 |
| --- | --- |
| `.github/workflows/sector_flow_crawler.yml` | 交易日 13:30 北京时间抓取板块资金流 |
| `.github/workflows/discover_akshare.yml` | 探测 akshare 接口 |
| `.github/workflows/test_akshare.yml` | 测试 akshare 接口 |
| `.github/workflows/test_direct_api.yml` | 测试直接 API |
| `.github/workflows/test_sector_types.yml` | 测试板块类型 |

`sector_flow_crawler.yml` 使用：

```bash
python scripts/fetch_sector_flow.py
```

需要 GitHub Secret：

- `DATABASE_URL`

## 数据库

本地：

- SQLite 文件：`data/fund_insight.db`
- 未设置 `DATABASE_URL` 时自动使用。

生产：

- PostgreSQL/Supabase。
- 连接池设置见 `src/models/database.py` 的 `_get_postgres_pool_settings()`。
- SQLAlchemy 会在启动时 `Base.metadata.create_all(engine)`；它只负责补建缺失表，不能替代 Alembic 迁移。

重要限制：

- 项目已建立 Alembic 既有结构基线，首个增量迁移只新增 `prediction_change_logs`。
- `alembic/env.py` 只在**本地**把 `DATABASE_URL` 继承进来（且它指向 sqlite 时）；解析出的目标一旦看着是远程，命令行直接跑 `alembic` 会被 `[abort]` 拒掉。真要动生产库必须同时给两道旗子：`ALEMBIC_DATABASE_URL` + `ALEMBIC_ALLOW_REMOTE=1`（第 39 轮 B：只靠一个环境变量就放行太松，而上一版只在 ini 等于默认镜像串时才检查方向 —— 换一份 ini 就能让整道闸静默失效）。
- 修改 `src/models/database.py` 后，必须同时提供并验证只增量的迁移文件。
- 数据库导入接口 `/api/import-database` 默认关闭；开启后还需要确认头 `X-Danger-Confirm: import-production-database`。

### Alembic 基线与本地预检

现有数据库第一次纳入 Alembic 时，必须先完成备份和结构盘点，然后执行：

⚠ 本仓库 `.env` 里的 `DATABASE_URL` 指向**线上生产库**。第 38 轮起 `alembic/env.py` 不再把
这个值悄悄顶进 `alembic.ini`：裸 `alembic upgrade head` / `downgrade …` 在目标看起来是远程时
**直接拒跑**（旧写法会让一句文档命令就对生产发 DDL，而 `downgrade prediction_schema_baseline`
那支会 `drop_table("prediction_change_logs")`，即审计台账本体）。所以下面每条本地命令都要显式
带 `ALEMBIC_DATABASE_URL`，指向**副本**而不是日常开发库：

```bash
export ALEMBIC_DATABASE_URL="sqlite:///data/_migration_drill.db"   # 先复制一份再练
alembic current
alembic stamp prediction_schema_baseline
alembic upgrade head
alembic current
```

`stamp` 只登记版本，不创建旧业务表，因此只能用于已经具有 Fund Insight 既有表结构的数据库。全新本地数据库应先运行 `python -m src --init-db` 创建当前完整结构，再执行 `alembic stamp head`。

迁移前可生成离线 SQL 供人工检查（`--sql` 不连库，但选哪个方言取决于解析出的目标，所以同样要带上面那个变量）：

```bash
alembic upgrade head --sql
```

本地迁移回滚演练必须针对数据库副本，不得直接操作日常开发数据库：

```bash
alembic downgrade prediction_schema_baseline
alembic upgrade head
```

生产环境只在完成 `pg_dump`、隔离恢复和迁移预检后，才在受控维护窗口显式设置 `ALEMBIC_DATABASE_URL` **并且** `ALEMBIC_ALLOW_REMOTE=1`（少任何一道都会被 `env.py` 拒跑）。
**更正（第 38 轮）**：本段原先写"本仓库的测试、启动命令和 Render Web 启动均不会自动执行 Alembic"，
这是假的 —— `render.yaml:11` 的 `startCommand` 每次启动都跑 `python scripts/run_migrations.py`
（它先自报 `[库] …` 再 `upgrade head`；`tests/unit/test_prediction_migrations.py` 也会起它，
但对的是临时副本库）。真正"不会自动执行"的是 `ENABLE_STARTUP_MIGRATIONS` 控制的那条应用内补列路径。

## 数据备份与恢复演练

### 本地 SQLite

修改预测逻辑或执行本地维护脚本前，先创建一致性快照：

```bash
python scripts/backup_database.py --sqlite-path data/fund_insight.db --output-dir backup
```

脚本只读取命令行指定的 SQLite 文件，不读取 `DATABASE_URL`，输出到已被 Git 忽略的 `backup/` 目录。每次备份包含：

- `fund_insight_<UTC时间>.sqlite3`：SQLite 快照。
- `fund_insight_<UTC时间>.manifest.json`：SHA-256、文件大小、完整性检查结果和各表行数。

恢复前先核对清单中的 `integrity_check` 为 `ok`，重新计算快照 SHA-256，并在副本上执行 `PRAGMA integrity_check`。不要直接覆盖正在运行的本地数据库。

### Supabase/PostgreSQL

应用的 JSON 导出只能补充业务数据检查，不能替代 PostgreSQL 完整备份。任何生产结构变更前按以下顺序执行：

1. 只读盘点当前表结构、关键表行数、预测状态统计和数据库版本。
2. 使用受保护的 PostgreSQL service/密码文件创建自定义格式备份，不把连接串写入命令历史或备份清单：

   ```bash
   pg_dump --service=fund_insight_prod --format=custom --no-owner --no-acl --file=fund_insight_before_change.dump
   ```

3. 通过应用导出功能额外保存业务 JSON，用于抽样比对；格式 `1.3` 包含预测变更日志，但它仍不是 PostgreSQL 恢复源。
4. 新建与生产隔离的临时 PostgreSQL 数据库，先查看备份目录，再执行恢复：

   ```bash
   pg_restore --list fund_insight_before_change.dump
   pg_restore --service=fund_insight_restore --clean --if-exists --no-owner --no-acl fund_insight_before_change.dump
   ```

5. 在隔离库核对表结构、外键、索引、关键表行数以及预测的待验证/正确/错误/观望数量。
6. 只在隔离库运行迁移预检和应用启动检查，确认旧数据可读、归档预测可恢复、批量验证状态可读取。
7. 保存迁移前后比对结果和回滚命令，人工确认后才允许安排生产迁移。

生产 Web 启动必须保持 `ENABLE_STARTUP_MIGRATIONS=false`（那是应用内补列/建索引的另一条路径）。

**下面这句是愿望，不是现状，记在这里免得有人照它行动**：「不得把结构迁移绑定到 Render 启动命令」。事实是 `render.yaml:11` 的 `startCommand` = `python scripts/run_migrations.py && uvicorn ...`，也就是**每次 Render 启动都会对 `$DATABASE_URL` 指向的库跑一次 `alembic upgrade head`**（第 37 轮起它动手前先自报 `[库] …`；它没有 dry-run、没有确认头）。要把这句话落成现状（从 startCommand 里去掉迁移，或给它加确认旗子）＝改部署配置 ⇒ 老板决定项，见 `docs/迭代计划/S6-上线前检查单.md` §6 与任务 #57。未完成隔离恢复演练前，不得对 Supabase 手工执行迁移。

## 配置持久化

LLM 配置来源优先级：

1. 环境变量。
2. PostgreSQL `system_config` 表。
3. 本地 `data/llm_config.json`。

相关代码：

- `src/core/config.py`
- `src/api/routes/config.py`

## 健康检查

```text
GET /api/health
GET /api/health/detail
```

只有 `GET /api/health` 不需要访问密码（中间件放行的是它自己）；
**`/api/health/detail` 要带 `X-Access-Password`** —— 2026-09-26 线上实测：不带口令
`/api/health` 回 200、`/api/health/detail` 回 **401**（这一句以前写的是"健康检查不需要访问密码"，
照着它敲命令的人只会以为接口坏了）。

`/api/health/detail` 会返回：

- 数据库状态。
- 当前数据库类型。
- LLM 是否配置。
- 爬虫是否启用。
- 启动迁移是否启用。
- 本地调度器是否运行。
- **跑的是哪一版**：`git_commit` / `git_branch` / `git_commit_source`（来自 Render 注入的
  `RENDER_GIT_COMMIT`、`RENDER_GIT_BRANCH`；本机跑就是 `unknown` + `unavailable`），
  加 `started_at` / `uptime_seconds`（进程导入时刻 ⇒ 部署成功的直接信号就是它归零）。
  为什么要加：2026-09-25 量出来线上是 8 月 6 日的构建、本地领先 113 个提交，而那之前
  文档里每一句"页面上看得见"都没被送达过 —— 判"部署生效没有"以前只能人比对页面指纹
  （`curl …/index.html | md5sum` 对 `git show HEAD:web/index.html`），
  而那条路只对前端有效，后端改了什么都看不出来。
  取不到提交号时接口**明写 `unknown`**，不会拿 `version: 2.0.0` 那种静态串冒充答案。

```bash
# 线上是哪一版（要带口令；口令只从 .env 取，不进命令行历史也不回显）
PW="$(grep -m1 '^ACCESS_PASSWORD=' .env | cut -d= -f2- | tr -d '\r')"
curl -s -H "X-Access-Password: $PW" https://fund-insight.onrender.com/api/health/detail \
  | python -c "import json,sys; d=json.load(sys.stdin); print(d['git_branch'], d['git_commit'], d['git_commit_source'], d['uptime_seconds'])"
```

## 常见运维命令

```bash
# 本地初始化数据库
python -m src --init-db

# 本地模拟每日任务
python scripts/run_scheduled_tasks.py daily

# 手动抓取板块资金流
python scripts/fetch_sector_flow.py

# 检查最近预测
python scripts/check_today_predictions.py

# 重新验证预测
python scripts/reverify_predictions.py

# 重新计算博主分数
python scripts/recalculate_blogger_scores.py
```

维护脚本很多，运行前先读脚本顶部逻辑，特别是会写库的脚本。

## 安全注意事项

- 不要把 `.env`、API key、`DATABASE_URL`、访问密码提交到仓库。
- 不要把数据库连接串写入命令历史、备份文件名或 manifest；生产备份使用受保护的 PostgreSQL service/密码文件。
- `/api/import-database` 是高风险接口，默认关闭。
- 清理接口可能删除数据，前端已有预览和确认逻辑，后端也需要确认头。
- 生产数据库结构变更前必须备份或确认迁移 SQL。
- 爬虫遵守频率控制，不做高频采集。

## 故障排查

### 前端提示 Unauthorized

检查登录密码是否等于 `ACCESS_PASSWORD`，请求头是否带 `X-Access-Password`。

### LLM 分析失败

检查：

- `LLM_PROVIDER`
- `LLM_API_KEY` 或 `VOLCENGINE_API_KEY`
- `LLM_BASE_URL`
- `LLM_MODEL`
- `/api/config/test-llm`

### 生产数据库连接失败

检查：

- `DATABASE_URL` 是否是 `postgresql://...`
- Supabase 是否允许连接。
- Render secret 是否配置。
- 连接池参数是否过大。

### 板块资金流没有更新

检查：

- Render Cron 运行记录。
- GitHub Actions `Sector Flow Crawler` 运行记录。
- `sector_flow_fetch_runs` 表。
- `/api/sector-flow/fetch-status`。

### 预测没有验证

检查：

- 预测 `target_date` 是否到期。
- 基金是否有 `fund_history` 起点和终点附近净值。
- `/api/predictions/verify-all/status`。
- Render Cron 日志。

## 发布前检查

```bash
pytest tests/unit/test_deployment_optimization.py tests/unit/test_production_hardening.py -v
python -m src --init-db
codegraph sync .
codegraph status .
```

如果改了业务逻辑，还要跑对应模块测试。
