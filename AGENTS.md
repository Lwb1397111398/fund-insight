# AGENTS.md

This file provides guidance to Codex when working in this repository.

## 项目一句话

Fund Insight 是一个基金博主观点分析系统：用户录入或抓取基金相关帖子/文章，系统用 LLM 提取预测和观点，再用基金净值和后台任务验证预测表现，最终辅助判断哪些博主和板块观点更可靠。

## 当前技术栈

| 层 | 技术 |
| --- | --- |
| 后端 | Python + FastAPI + SQLAlchemy 2.0 + Pydantic 2.x |
| 前端 | `web/` 原生 HTML/CSS/JS，Vue 3 CDN，axios，本项目没有前端构建链 |
| 数据库 | 本地 SQLite `data/fund_insight.db`；生产 PostgreSQL/Supabase，通过 `DATABASE_URL` 切换 |
| LLM | OpenAI 兼容 SDK，支持硅基流动、DeepSeek、火山引擎 |
| 基金/市场数据 | 天天基金、东方财富，另有多源基金 API 兜底 |
| 部署 | Render Web Service + Render Cron + Supabase |
| 测试 | pytest |
| 代码索引 | `.codegraph/` 是本地索引产物，改代码/文档后运行 `codegraph sync .` |

## 常用命令

```bash
# 启动本地服务，默认 8002
python -m src
python -m src --port 8002

# 仅初始化数据库（.env 指向生产时会被守卫 abort，
# 真要连生产得显式 ALLOW_REMOTE_INIT_DB=1 —— 第 23 轮：文档从没写过这个前提）
python -m src --init-db

# 直接通过 uvicorn 启动
python -m uvicorn src.api.main:app --host 0.0.0.0 --port 8002

# 运行测试
pytest tests/ -v
pytest tests/unit/ -v
pytest tests/integration/ -v

# 计划中的关键验证
python -m src --init-db
pytest tests/unit/test_prediction_verify_batch_task.py tests/unit/test_scheduler_fixes.py -v

# CodeGraph
codegraph status .
codegraph sync .
```

## 环境变量

复制 `.env.example` 为 `.env`。本地最少需要：

- `LLM_API_KEY`：LLM 密钥。若使用火山引擎，则配置 `VOLCENGINE_API_KEY` 并设置 `LLM_PROVIDER=volcengine`。
- `LLM_BASE_URL` / `LLM_MODEL`：OpenAI 兼容模型地址和模型名。
- `ACCESS_PASSWORD`：所有 `/api/` 请求的访问密码；前端通过请求头 `X-Access-Password` 访问。

生产常见配置：

- `DATABASE_URL`：PostgreSQL/Supabase 连接串。不设置时自动使用 SQLite。
- `CORS_ORIGINS`：生产域名白名单。
- `CRAWLER_ENABLED`：Render 当前设为 `true`，本地默认可保持 `false`。
- `ENABLE_DATABASE_IMPORT=false`：数据库导入接口默认禁用，启用还必须带确认头。
- `ENABLE_STARTUP_MIGRATIONS=false`：启动补列/索引默认禁用，避免生产启动时隐式改结构。

## 系统分层

```text
web/*.html + web/common.* 
        |
        v
src/api/main.py  ->  src/api/routes/*  ->  src/api/schemas/*
        |
        v
src/services/*  业务服务、事务、批处理、统计、验证
        |
        +--> src/analyzer/*  LLM/本地趋势/帖子价值分析
        +--> src/fund/*      基金数据、多源 API、同步、技术指标
        +--> src/crawler/*   文章/帖子抓取与筛选
        |
        v
src/models/database.py  SQLAlchemy ORM，SQLite/PostgreSQL 共用
```

后台任务入口：

- `src/tasks/scheduler.py`：本地常驻调度器，按北京时间窗口运行清理、基金更新、预测验证。
- `scripts/run_scheduled_tasks.py`：Render Cron 一次性入口，执行 `daily`。

## 核心业务流

1. 用户添加博主和帖子：`POST /api/bloggers`，`POST /api/posts`。
2. LLM 分析帖子：`PostService` 调用 `src/analyzer/llm_analyzer.py`，生成标题、预测方向、基金/板块、预测周期和目标日期。
3. 预测入库：`Prediction` 记录关联 `Blogger`、`Post`、基金代码和目标验证日期。
4. 基金数据同步：`src/fund/fund_api.py`、`FundDataManager`、`FundSyncManager` 拉取净值和历史。
5. 预测验证：`src/services/prediction_verify_service.py` 根据起点/终点净值、过程涨跌、震荡阈值和预测方向打分。
6. 博主统计：`src/utils/blogger_stats.py`（`recalculate_blogger_stats`，**不是表**；统计列直接落在
   `bloggers` 行上）、`BloggerService`、`StatsService` 统计准确率、等级、预测数量。

观点和建议流：

1. 爬虫或人工录入观点，保存到 `Viewpoint`。
2. `ViewpointService` 支持批量分析、汇总观点、权重和有效期。
3. `AdviceService` 与 `LLMAnalyzer.generate_investment_advice_three_stage()` 生成投资建议。

## 重点文件

| 文件 | 说明 |
| --- | --- |
| `src/api/main.py` | FastAPI 入口、中间件、路由注册、静态页面、危险的数据库导入接口 |
| `src/api/routes/` | 按领域拆分的 REST API |
| `src/models/database.py` | ORM 表模型（张数以 `python -c "from src.models.database import Base; print(len(Base.metadata.tables))"` 为准，今天 27），兼容 SQLite/PostgreSQL |
| `src/services/prediction_verify_service.py` | 预测验证核心，涉及准确率和评分，改动需谨慎 |
| `src/services/prediction_verify_task.py` | 批量验证后台状态对象，避免长请求卡死前端 |
| `src/analyzer/llm_analyzer.py` | LLM 核心，包含模型选择、熔断、缓存、解析兜底、预测/建议/图像分析 |
| `src/fund/fund_api.py` | 天天基金与基金数据管理 |
| `src/tasks/scheduler.py` | 本地后台调度 |
| `web/index.html` | 主前端 SPA，大文件，修改前先定位相关 section |
| `render.yaml` | Render Web/Cron 部署配置，通常不要顺手改 |

## API 路由总览

`src/api/main.py` 注册以下路由：

- `/api/bloggers`：博主管理。
- `/api/posts`：帖子录入、分析、低质量清理。
- `/api/predictions`：预测 CRUD、批量验证、合并相似预测。
- `/api/funds`：基金列表、同步、趋势状态。
- `/api/viewpoints`：观点列表、汇总、清理。
- `/api/crawler`：爬虫抓取（新浪博客、微信公众号）。
- `/api/advice`：投资建议生成、历史。
- `/api/stats`：总体统计。
- `/api/config`：LLM 配置、清理、别名、板块-基金映射、配置导入导出。
- `/api/test-data`：测试数据扫描和清理。
- `/api/prediction-groups`：相似预测组。

## 数据模型心智图

核心表：

- `bloggers`、`posts`、`predictions`：博主、帖子、预测主链路。
- `fund_info`、`fund_history`：基金基础信息和历史净值。
- `viewpoints`、`crawler_article_records`：观点和爬虫去重记录。
- `investment_advice`、`advice_reasoning`：投资建议链路。
- `sector_fund_mapping`、`sector_alias`、`user_fund_bindings`：板块与基金映射。
- `batch_analysis_tasks`、`analysis_logs`：批量分析和日志。
- `cleanup_*`：清理任务、规则、日志。
- `system_config`：生产环境持久化配置。

## 前端约束

- 主界面是 `web/index.html`，使用 Vue 3 CDN 和 axios。
- 没有构建步骤，不要引入需要打包的新依赖。
- `web/common.js` 与 `web/common.css` 存放跨页面公共逻辑和样式。
- UI 风格应偏数据工具：清晰、克制、可扫描；不要做营销 landing page、大渐变、重动画。
- 所有 `/api/` 请求需要带 `X-Access-Password`。

## 部署和定时任务

### 生产库结构现状（2026-09-22 实测）
- 生产 Supabase 的库**不是 alembic 建的**（原来没有 `alembic_version` 表），
  `sector_fund_mapping` 缺 0007+0008 的 10 个列。老板已授权，已用
  `python scripts/sync_db_columns.py --against-production --apply --confirm SYNC-COLUMNS --stamp-head` 补齐
  （第 23 轮加了 `--against-production`：目标看起来是远程库时连 dry-run 都拒跑，
  因为它带 `--apply` 就是发 DDL 的工具；`LOCAL_DB_URL` 设了却连着远程也会被拒）
  （12 列 + 5 索引，只加列/建索引、不动任何数据），并把 `alembic_version` 记成 head
  `add_sector_mapping_keywords` ⇒ 后续 0010+ 可以正常 `alembic upgrade head`。
  复核：`sector_fund_mapping` 19 列、映射 118 行、未删预测 1616 行、fund_info 162 行（与补列前一致）。
- **当时为什么没直接用 `scripts/run_migrations.py`**：生产库里**压根没有 `alembic_version` 表**，
  任何 `upgrade head` 都会从 base 开始重跑 ⇒ 撞上已存在的表就报错（或直接改到既有对象）。
  这不是"它天生要从 base 全量跑"：`command.upgrade(config, 'head')` 本身是按 `alembic_version`
  增量走的 —— **现在已经 stamp 到 head，后续 0010+ 就该用它**（Render 每次启动也在跑它，见 `render.yaml:11`）。
  直接 stamp 又是在没核对前置对象的前提下撒谎，所以那次用 `sync_db_columns.py`：它的唯一真值是
  `src/models/database.py` 的元数据（库里缺整表会被**说出来**并且拒绝 stamp）。
  ⚠ 用之前知道两件事：① 它没有 dry-run，② 裸 `alembic` CLI（`upgrade head` / `downgrade …`）
  在 `.env` 指向生产时**会被 `alembic/env.py` 当场拒跑**（第 38 轮 B 的 BLOCKER：旧写法把
  `DATABASE_URL` 悄悄顶进 ini，等于给生产发 DDL；要动远程得**同时**给 `ALEMBIC_DATABASE_URL` 与 `ALEMBIC_ALLOW_REMOTE=1`（第 40 轮 B：只给一道旗子、目标还来自 `.env`，等于把那条 BLOCKER 重新打开））。

- **线上到底跑的是哪一版 —— 2026-09-25 20:3x 第一次量出来**（在这之前包括我在内都按"线上＝最近改完的那一版"
  说话，文档里每一句"页面上看得见"因此都没被送达）：`curl -s https://fund-insight.onrender.com/index.html`
  的 md5 = `5325f47d5ab67c5f3b1b77d56f5537bc`，与提交 **`a7d2057`（2026-08-06）** 的 `web/index.html`
  **逐字节相同**，而 `origin/main` 就是 `138ab0a`（2026-08-06 19:44）⇒ 本地领先 **113 个提交**
  （复核 `git rev-list --count a7d2057..HEAD`）。三条接口面同向：`GET /api/stats/evidence` → **404**、
  线上映射列表 229 行里**没有** `relevance_state`、`GET /api/health/detail` 的
  `scheduler_running: false`。
  **后果一**：目标①②（页面上看得见 ⚠ 条数、只报带库名与截止日的区间）在生产上从没成立过。
  **后果二**：那两个按钮在线上走的还是**第 18 轮之前**的老改标规则（`git grep -n "retag_prediction" a7d2057 -- src/`
  为空）。我照那条规则原样写了只读 SQL 量今天的暴露面（全文见 `docs/迭代计划/S6-上线前检查单.md` §0
  第 48 轮那行）：**会被改标的活预测 345 条，其中 223 条带着已判结论**；而老代码"改标就清结论"那一支
  今天**一条都不触发** —— 线上 `status` 存的是 `success 591 / pending 559 / failed 466`，老判据要的是
  `correct/wrong/expired` ⇒ 净效果是 **223 行"标的换了、结论还挂着"、无台账**，正是第 18 轮那族脏数据
  ⇒ **所以这次没点**。老板 09-25 的决定：破例上线一次，上线后这批**先出清单再点**。
  **但这里我当场说错过一句，按实测更正**：345 / 223 是**旧构建那条规则**的暴露面，不是新代码的 ——
  现在 `full_sync` 只剩"预测的 `fund_code` 为空、而它板块有基金"才关联，且必须走
  `retag_prediction`（留台账、有结论要清、随后重算博主统计列）；生产这种行**实测 0 条**
  （`python scripts/q.py --production "select count(*) filter (where fund_code is null or fund_code='') from predictions where is_deleted=false"` → 0）
  ⇒ **上线本身就是拆掉这 345 条的那颗雷**，"先出清单再点"仍然照做（清单预期为空，那就把"为空"这件事
  连同逐行回执一起报出来，而不是拿一个 0 当默认结论）。
  **上线不需要新 DDL**：`alembic/versions/` 只有 9 支，head 仍是生产 09-22 已 stamp 的
  `add_sector_mapping_keywords`，且 `python scripts/sync_db_columns.py --against-production`
  （不带 `--apply`）回 `[元数据] 模型 27 张表；库里缺 0 张` + `[ok] 列与索引都已存在`
  ⇒ `render.yaml:11` 的 `run_migrations.py` 启动时是对生产的一次 no-op；库里另有 9 个"模型没声明"的
  表/对象（`advice_feedback`、`market_data` 等），那个脚本不动它们。
  **生产净值现状**（2026-09-25 20:2x，只读）：`fund_history` 末条 **2026-09-13** / 9541 行 / 基金 162 只，
  未删预测 1616、已判 1057（判对 591）、到期未判 **149**，末次验证 09-13 15:23，而
  `GET /api/predictions/verify-all/status` 仍停在 `total 83 / processed 65 / success 62`
  ⇒ 那一次批量验证**中途断了没人接手**（净值停在 09-13 的原因老板已答过：一直是手点，见上面那条）。
- **上面那笔"净值落后"的账已经在 2026-09-25 夜结掉**（上线后我自己调的接口，没让老板点）：
  `POST /api/funds/update-all` 回执"检测 1616 个预测、新增 0 只基金、关联 131、更新 156 只、失败 6 只
  （就是 S6 那 6 个垃圾码）"，`fund_history` 9541 → 10882 行、末条净值到 **2026-09-25**；
  **那句括号是归因归错了**（2026-09-26 再点一次量清楚）：删掉三个垃圾码之后失败变成 4 只 =
  `603758 / 152788 / 600189`（老板留下的那三个）+ **`000725`（货币基金，详情接口取不到，不是 S6 的码）**，
  所以"失败 N 只"从来不等于"那 N 个垃圾码"，报这种数必须把代码逐个列出来。
  随后 `POST /api/predictions/verify-all` 把已判从 **1057 推到 1190**（判对 591 → 644），
  到期未判 149 → 16。**这批 16 条里今天量清了哪 15 条是问不出来的**（2026-09-26 21:4x 只读 + 一次真跑批）：
  它们全挂在 `003033`（14 条）与 `002413`（1 条）两只**已经停更的产品**上 —— 库里各只有 20 行净值，
  末条分别停在 **2020-12-08 / 2023-07-07**，而预测窗口是 2026-08~09 ⇒ 任何一次同步都补不到那个窗口，
  `no_source_history` 说的就是这件事（不是"还没更新"，更新已经跑过了）。现在验证器判出这一条时
  会把行压到重问日（生产实测 15 行 `next_verify_date=2026-09-29`），于是
  `GET /api/predictions?lifecycle=unverifiable` 印 `due: 0 / unverifiable: 15`，
  页面上「待验证到期」不再是"每次点都白跑 15 条然后还是 15 条"。
  线上 `/api/stats/evidence` 与 `python scripts/audit_verdict_evidence.py --production` 同源同数
  （两条独立路径互相印证），2026-09-27 04:22（北京）实测印
  `已判 1191 / 判对 638 = 53.57%、⚠ 265（22.3%）、区间 41.39% ~ 63.64%`；页面另一侧
  `GET /api/predictions?lifecycle=due` 当场回 `due: 0 / unverifiable: 0 / all: 1601`。
  **这两个数每天在动，别抄这里的文本，跑命令。**
  ⚠ 从 419 掉到 265 不是"证据自己复现了"，是**任务 #101 那一次「按板块对齐标的」**（09-26 18:33，
  `run_id=ui-sync-20260926-183329`）把 320 条换到了给得出这段窗口净值的标的上、其中 239 条旧结论被清掉重判、
  6 条因标的给不出证据当场没绑（逐行原因在回执 `skipped_unservable_details`，还原走
  `scripts/restore_prediction_batch.py`）；随后 `update-all`（净值末条到今天、失败 1 只 = `603758`）
  + `verify-all`（239 条全判出来）。清掉的那 6 条判对/判错都算过一遍 ⇒ 判对从 644 变 638 是**这次重判的结果**，
  不是数据回补。桶的构成也从 `verdict_under_other_fund 220` 掉到 88（这正是那条族的本体）。
  **两条要说清的**：① 那 131 条"关联"没动任何预测（`prediction_change_logs` 当天新增 0 行），
  但其中一条分支会给 `fund_info.sector_type` 补空值 ⇒ 这类副作用要写进预检清单；
  ② 上游会给**预签发的未来净值行**（实测 `000725` 货币B 在 09-25 给了 09-26/09-27，07-31 给过 08-01/08-02），
  入库侧以前没门 ⇒ 现在门在 `src/fund/fund_api.py:usable_history_rows`（判据
  `tests/unit/test_nav_future_row_gate.py`），存量残留用 `scripts/drop_future_nav_rows.py`
  点名删（默认 dry-run、真删要 `--apply --confirm DROP-FUTURE-NAV --json 备份`；
  2026-09-26 已删生产 4 行并把档案头从 09-27 倒回 09-25，删后 ⚠ 419 一条没涨）。
  **同步只补没有的日期、不覆盖已有行**（覆盖就是 `nav_rewritten` 那族 ⚠ 的成因）⇒ 提前签发的行
  一旦落下就永远不会自纠，"日期被追平"不等于没问题，这类数据必须点名清。
- **第一次真尝试上线（21:44 推 ⇒ 22:0x 部署）失败了，而且日志第一句本来就会骗人**：
  应用在**导入阶段**就死在 `src/models/database.py`，印的是
  `RuntimeError: DATABASE_URL is PostgreSQL, but psycopg2 is not installed; refusing to fall back to SQLite.`
  —— 那个 `try` 当时把 `create_engine(...)` 与 `logger.info(...)` 一起圈了进去 ⇒ 深处任何
  `ImportError` 都会被这句话顶包（已收窄，并把原始异常 / `sys.version` / `sys.platform` 写进那句话，
  判据 `test_a_broken_postgres_driver_says_what_is_actually_broken` 拿"存在但一导入就抛"的假驱动喂它）。
  **另一件必须先知道的事**：`render.yaml` 钉的是 `PYTHON_VERSION: "3.10.12"`，而 Render 实际在跑
  **Python 3.14.3**（证据就在 traceback 的路径里：`.venv/lib/python3.14/site-packages`、
  `Python-3.14.3/lib/python3.14/importlib`）⇒ **那份 Blueprint 的版本钉对现有服务没生效**（仪表板设置盖过它），
  而本机的 1082 条用例全跑在 **3.12** 上 ⇒ 上线前先把解释器对齐到 3.12，否则连失败原因都拿不到第二手证据。
  这次失败**没有动到生产数据**：应用没起来，跑的仍是 8 月 6 日那一版（页面 md5 未变）。
  **根因在第二次部署（钉到 3.12.10 之后）自己浮出来了**：`ModuleNotFoundError: No module named 'psycopg'`
  —— 不是 psycopg2 没装，是 **`postgresql://` 这种不带 `+driver` 的连接串用哪个驱动，是 SQLAlchemy 的
  默认值**：2.0.x 默认 psycopg2，2.1 起默认改成 psycopg(v3)，而 `requirements.txt` 里只有
  `psycopg2-binary`、且写的是 `sqlalchemy>=2.0.0`（没上界）⇒ 构建时解析到 2.1 就直接起不来。
  **两处一起修**：`src/models/database.py` 把驱动名钉死（`postgresql+psycopg2`，判据看的是
  "交给 `create_engine` 的那个 URL"，不是"方言用的是谁"——后者在 2.0.48 上结构性看不见这个缺陷）；
  `requirements.txt` 给 sqlalchemy 加上界 `<2.1.0`，与本机测过的 2.0.48 对齐。
  另立一条规矩：**`.python-version` 已经在仓库里钉住 3.12.10**（Render 构建读它；仪表板里的
  `PYTHON_VERSION` 优先级更高，所以那个值要么别填、要么同样填 3.12.10 —— 2026-09-25 第一次失败时
  Render 实际在跑 3.14.3，而 `render.yaml` 里钉的 3.10.12 对现有服务根本没生效）。
- **只读门现在能读线上了**（任务 #51）：`python scripts/audit_verdict_evidence.py --production`
  ⇒ 引擎级只读 + 真试一次写，第一行自报哪一台；2026-09-25 21:5x 首次实跑印
  `已判 1057 / 判对 591 = 55.91%`、`⚠ 419（39.6%）`、`区间 33.68% ~ 73.32%`（与 09-22 手算逐字对上）。
- Render Web Service：`uvicorn src.api.main:app --host 0.0.0.0 --port $PORT`。
- Render Cron：每天 10:30 运行 `python scripts/run_scheduled_tasks.py daily`。
- Supabase/PostgreSQL：通过 `DATABASE_URL` 连接；连接池参数见 `render.yaml` 和 `src/models/database.py`。

## 修改规则

- 用户是编程小白，默认自动判断、修改、验证和收尾，不把技术选型抛给用户。
- 不要回滚用户已有改动；当前工作树可能已有未提交文件。
- 不要手改 `.codegraph/codegraph.db`；需要时运行 `codegraph sync .`。
- 数据库结构变更、删除/迁移大量文件、改部署配置、改公共接口、移除依赖等高风险操作必须先确认。
- **本地镜像类脚本必须显式写 `pin_local_sqlite(use_mirror_default=True)`**：`.env` 指向生产，
  守卫现在会读 `.env`，不设这个显式参数就会 abort（第 22 轮评审的探针就是靠"看不见 .env"
  把写操作落进了 `data/fund_insight.db`）。一次性探针不设该参数 ⇒ 会被拦下，这是有意的。
- `src/analyzer/llm_analyzer.py`、`src/models/database.py`、`src/services/prediction_verify_service.py`、`src/api/main.py`、`web/index.html` 是高风险区域，先读测试和调用方再动。
- 文档类改动也要跑最小验证或至少格式/链接/命令检查。
- **评分门禁（2026-09-27 老板改口，以这一条为准）**：原规矩是"两份独立复评、取低分 ≥80 才推"。
  老板 09-27 原话「我发现你的速度有些过于慢了……可以适当降低分数要求来提升速度，具体由你自己来控制」
  ⇒ 现在按 **一份独立复评 ≥75 即推**执行，第二份不再等。降线降的是**等待**，不是纪律：
  BLOCKER 一律先复现再动手、基线两个口径必须绿、`audit_doc_claims` 必须退 0、生产写入仍是
  只读预检→dry-run→显式确认→逐行回执；评审分数不到 75 就继续修，不许拿"老板说可以慢着来"当借口。

## 推荐工作流

1. 先看本文件、`ARCHITECTURE.md`、`PRODUCT.md`。
2. 查结构优先用 CodeGraph：`codegraph query`、`codegraph callers`、`codegraph context`；不可用时用 `rg`。
3. 修改前定位对应测试；能写测试就写测试，不能写则运行最小验证。
4. 改完运行相关 pytest，再运行 `python -m src --init-db` 做启动级数据库初始化检查。
5. 涉及索引或文档入口变化后运行 `codegraph sync .`。

## 当前测试基线

最近一次核对（2026-09-28 19:0x（北京），**任务 #151 + #152：第 63 轮复评 73/100 —— 不到 75 这条线，
所以这一批先修完、再评、分数够了才推**（六条 MAJOR 全在判据侧，条目号取自第 63 轮报告结论表、正文不随
仓库走，下面按**修法**记，不抄它的编号）——

① **恒假规则只接了 `If` 一臂**：同一把 `_live_nodes` 里 `IfExp`（三目）、`While`、`match` 的 guard
三臂要么不把测试式当活节点、要么恒假时不剪那一支 ⇒ `x = 1 if 1 == 0 else release(...)`、`while 1 == 0:`、
`case ... if 1 == 0:` 与诚实的 `if release(...):` 四种臂三种待遇。**这是我上一批提交信息里那句"五腿同判"
被当场驳回的那一条**（第 56~62 轮"说满话"那一族第七次扣分）。修法：四臂同规则（恒假 ⇒ 收 `test` +
够得着的那臂、不收够不到的身体；诚实 ⇒ 测试式自己进活节点集），每臂正反两格各一。
② **`_kids` 这一族第三批复发，只是换了个位置**：默认值不是一串节点而是 `arguments.defaults` 与位置参数
**zip 出来的偏移** ⇒ "签名里 `days` 的默认值"这一腿此前有两份各自实现（`_param_default` 与扫描器自己那段），
一份算错位就把诚实写法判红。现在并成一把 `_signature_defaults`（posonly + args 共享一段 defaults、
kwonly 各自一段），并配两格样品：`def f(code=[], /, days=30)` 与 `def f(code, *, days=31)` 都要报对格位
（`/, days=30` 上一批是**碰巧**被抓到的，`*, days=30` 才是真漏 —— 评审原文那句"posonly 那一格没接"
按实测收窄，不照抄）。
③ **别名链只认 `Assign`**：`d: list = []` 与 `(d := [])` 绑出来的别名不进 `_accumulation_names`
⇒ 诚实的"先建容器再填"被判"没递"。补 `AnnAssign` / `NamedExpr` 两种目标形状，且**这个名字后来又绑过别的东西
就不认**（`_bindings` 数 store 次数，>1 直接跳过）⇒ 反面样品：只绑不填 / 填的是别的容器仍判"没递"。
④ **`**payload` 摊进调用那一腿不查键**：`f(**{'changed_dates': []})` 把"新日期递没递"这把尺子买通。
新增 `_splat_value`：字面量字典查键、名字回溯一跳；**查不清就放行**（`_SPLAT_UNCLEAR`）而不是硬猜
（本仓尺度：猜 = 把正常写法打死）。
⑤ **`TryStar` 在 3.9 上取不到就把工具弄崩**：`_try_nodes()` 按语言版本现取 `('Try','TryStar')`，
不支持的那一档不参与、也不恒空。
⑥ **归档那把的 `setdefault` 只数一格**：`d.setdefault(列)`（没给值）此前直接 `IndexError`（`args[1]`）
⇒ 补 `len(n.args) > 1` 这道门，两格样品：裸的那格**不崩且不数**、填上值的那格仍数成写。
⇒ 判据文件当场 **48 passed**；样品表 **evasions 49 / honest_live 44**（条数一律现数，命令写在那个文件的注释里）。
**这一批 ①~⑥ 全在判据侧，`src/` 行为一字未动** ⇒ 体检不新增条目（30 处，末行数）。

⑦ **产品这一半：任务 #132「打开网站就补一次」落地** —— 老板那句"打开后我不会看到任何应该验证、
或过了验证期间仍然没被验证的预测"的最后一格。`web/index.html` 两条登录出口（本机存过口令 / 刚输口令）
之后自动做一次：**每个北京日一次**（`beijingDay()` 与验证队列那把 `current_as_of()` 同一口径，
判据两格钉 UTC 边界 17:00→次日 / 15:59→当日）；该不该动手只问接口给的那两个数
（`/api/stats/evidence` 的 `nav_lag_stale` + `/api/predictions` 的 `meta.facets.due`），页面**不比大小**
（"落后几天算旧"这个阈值仍只有 `NAV_LAG_WARN_DAYS` 一个出处）；**两个数没取到就不动手、也不占掉今天这一次**；
跑失败 ⇒ 放开键（一次抖动不许哑一整天）。它把 #132 的账说得更准了：这 17 条**设计上会自愈**，
前提从"明天有人点那两个按钮"换成"明天有人打开网站"。
⚠ **这一条不是评审给的，是我自己真浏览器跑出来的第二版缺陷**：第一版把 `POST /api/funds/update-all`
当成同步接口，而它是后台任务 ⇒ 回执印「更新净值：基金更新任务已启动；验证：已开始后台验证 17 个预测」——
那 17 条是拿**旧净值**判的，正是 #132 要消灭的那次白跑（生产 09-28 那次是老板手点、中间等了 4 分钟）。
现在 `waitFundUpdateToFinish()` 轮 `GET /api/funds/update-status` 到 `in_progress:false` 才发验证，
并把给老板看的那句换成**跑完之后**那份回执（`last_result.message`；接口自己分两行 ⇒ 页面这一格是普通
`<span>`，换行会塌成一片连字，所以并成一句 —— 这条由 node 判据负责，不再靠浏览器看）；
超过 12.5 分钟没跑完 ⇒ 只补一半并明说"这次没有发起验证，明天打开会自动接上"（这一格**不放开**当天的键：
后台任务已经在跑，再点一次是重复发起）。镜像 09-28 18:3x~18:5x 真浏览器两遍：调用流水
`GET evidence → GET predictions → POST update-all → GET update-status ×N → POST verify-all`，
屏上那句现在是「打开网站补了一次 ⇒ 更新净值：同步完成：检测 1611 个预测…更新 236 个基金；6 只基金域查无此码…；
验证：已开始后台验证 17 个预测，请稍后等待完成」。
**另一件由已有闸替我拦住的（这一族本批又复发一次）**：`test_a_write_that_succeeded_is_never_reported_as_a_failure`
把这条轮询腿判成"写后刷新没单独 try"⇒ 那是**闸过宽**（轮询是这件事自己的一步，不是事后的装饰，
它失败时说"补跑中断"是真话）。收窄成按**腿**判："这条之后还有一笔写 ⇒ 不是刷新腿"，并配两格控制
（光秃秃一条 `await` 必须仍红；两笔写之间豁免、最后那条刷新腿必须仍点红 ⇒ 证明豁免是按腿不是按函数）。
判据：`tests/unit/test_frontend_cold_start.py` **+3 条**（49→52，A 该不该动手 / B 一天一次 + 失败放开 +
调用流水 / C 只借那两个接口），变异：`scripts/mutation_proof_frontend.py` 新增 **10 处**、本批逐条跑过全 RED。

⑧ **老板 09-28 授权"没妨碍的数据可以删"这一条，现读到的答案是"没有可删的"**（两库各量一遍，日期现算）：
```
D=$(date -u -d '+8 hours' +%F)
python scripts/q.py --production "select count(*) filter (where is_deleted=false) active, count(*) filter (where is_deleted=true) archived, count(*) filter (where is_deleted=false and is_correct is null and target_date < date '$D' - interval '180 day') half_year_overdue_unjudged, min(target_date) filter (where is_deleted=false and is_correct is null) oldest_open_target from predictions"
# 镜像同一句、把 `date '$D' - interval '180 day'` 换成 sqlite 写法 date('$D','-180 day')
```
今天印：生产 `active 1601 / archived 577 / half_year_overdue_unjudged 0 / oldest_open_target 2026-09-28`；
镜像 `1611 / 567 / 0 / 2026-07-09` ⇒ **"很早期、验证代价极大、收益极小"那一档在活预测里是 0 条**
（最早的未判目标日就是当天），要清的那批早就走回收站了（577/567 行已经不在任何活视图里）。
⚠ 那句 `interval '180 day'` 是 PostgreSQL 写法，sqlite 上直接 `OperationalError` ⇒ 两库各一条拼法，
别再写成"一条命令两库通用"（第 54 轮 ⑥ 同一族）。**物理硬删回收站那批需要新开一条可还原通道**
（`HARD_DELETE_DISABLED=True`），换来的用户可见收益为 0 ⇒ 本批不动手，等真有一批"占着库又永不复活"的行再说。

⑨ **一条门禁账，写在最前面防下一轮的我**：我把 #149 那批（`ae8c943` + `0e21673`）在**它自己那份复评
（第 63 轮 = 73 分）回来之前就推上去并部署了** —— 门禁判的是"这一批 ≥75 才推"，而"这一批"指的是
**被推的那一版**。线上现在跑的是一版**已知 73 分**的代码（`/api/health/detail` 自报
`git_commit=0e216731ea76`、`scheduler_running=False` ⇒ #132 那件事一个字没变）。
⇒ 这一批改完必须先拿一份**新的**独立复评，≥75 才推；不许再拿"分数还在路上"当已推的理由。

最后一次核对（**串行**、默认 locale cp936、子进程显式 `PYTHONIOENCODING=utf-8`、跑期间不起第二个会话；
⚠ 本批两个口径都比上一批慢（746.86s / 721.32s vs 565s / 453s）——本机内存负载高、不是回归，
判回归看的是 passed/skipped/failed 三个数与退码）：

- `pytest tests/unit -q` → **1232 passed / 16 skipped / 0 failed**（746.86 秒）。
- `pytest tests/ -q` → **1241 passed / 16 skipped / 0 failed**（721.32 秒）。
  （上一基线 1229/1238 → 本批 **+3 条 / 两个口径同增** = `test_frontend_cold_start.py` 49→**52**
  （#132 那三条：A 该不该动手 / B 一天一次 + 失败放开 + 调用流水 / C 只借两个接口）。
  **判据侧那六条一律改契约不增条数**，分布用
  `for f in $(git diff --name-only HEAD -- tests/); do echo "$f $(git show HEAD:$f | grep -c '^def test_') -> $(grep -c '^def test_' $f)"; done`
  ⇒ `test_structurally_unverifiable_hold.py 32 → 32`、`test_one_ruler_per_question.py 5 → 5`。
  判据文件当场：`python -m pytest tests/unit/test_one_ruler_per_question.py
  tests/unit/test_structurally_unverifiable_hold.py -q` ⇒ **48 passed**。）
  变异（逻辑侧）：`python scripts/mutation_proof_lifecycle.py` **30 处全 RED**（CONTROL-GREEN = 7 个判据文件
  在干净代码上全绿；无 ANCHOR-MISS / 无 HARNESS-FAIL / 无 `[还原失败]`；跑完 `git status --porcelain -- src/`
  为空、无 `.mutbackup` 残留）。原始日志随仓库走
  `docs/迭代计划/run-20260927-mutation/round63-lifecycle-mutations.txt`（首行
  `# run @ 2026-09-28T18:27:09+08:00  git=0e216731ea76  python=3.12.10  共 30 处变异 / 7 个判据文件`，
  md5 `28af7cde8c600d78f5a38d146d0d7386`，与上一批那份 `2dfadcd74e29e567cd1c0f55a8e03d07` 不同）。
  ⚠ 第一次跑它**起手就被自己的锁拒了**（`[abort] 已经有一个 pytest 会话握着 .pytest-session.lock`，
  因为我为了追基线数字让 `pytest tests/` 在后台跑着）⇒ 退 0 只印一行，**这不是满分通过**；
  并发这一条由锁守着，不是由我记得住守着（第 58 轮同签名，本批又复发一次）。
  变异（前端）：本批改到 `web/index.html`，跑了与本批相关的 **10 处**（`--only catch_up` 五处 +
  `a_nav_update_that_never_finishes_still_verifies` / `the_first_screen_never_asks` /
  `the_page_starts_comparing_lag_days_itself` / `the_finished_nav_receipt_is_thrown_away` /
  `the_receipt_keeps_its_line_breaks`）全 RED、CONTROL 全绿、逐文件回读还原一致。
  ⚠ **全套 126 处本批没有逐条重跑** ⇒ 别说成"前端体检全绿"，那一句的凭据只到本批那 10 处。
  `audit_doc_claims.py` 退 **0**（数字见下）。
  **镜像此刻**（同日 18:4x，只出计划不写库：`python scripts/close_unknowable_predictions.py`）：
  `[计划] 到期未判里可以判定"永远问不出来"的：0 条；仍在等的：29 条`（与上一批同数）；
  **生产此刻**（同日 18:4x~19:0x，只读 + 日期现算）：`expired_unjudged 17 / held_by_lock 0 /
  actionable_today 17`（还是那 17 条 `target_date=当天`，等明天的净值）；
  `GET /api/stats/evidence` 自报 `as_of=2026-09-28`、`已判 1191 / 判对 638 = 53.57%`、
  `区间 41.39%~63.64%`、`nav_as_of=2026-09-27`、`nav_lag_days=1`、`nav_lag_stale=False`、`nav_future_rows=0`；
  `python scripts/audit_verdict_evidence.py --production` 那条独立路径印的
  `已判 1191 / 对不上 265（22.3%）` 与分桶 `nav_rewritten 92 / verdict_under_other_fund 88 / nav_row_missing 85`
  与接口同数 ⇒ **但"两条路径互印证"仍然只覆盖这三行**（区间与覆盖面那一行本批又没跑到底：
  那条只读审计在"体检覆盖面"那一步会被 Supavisor 掐线，#150 一个字没动）。
  线上跑的哪一版：`/api/health/detail` 自报 `git_commit=0e216731ea76`、`scheduler_running=False`
  ⇒ **#132 到现在仍然没有任何东西在跑**，本批第 ⑦ 条那个"打开网站就补"就是去顶它的，**还没上线**。

（上一批：2026-09-28 15:0x（北京），**任务 #149：第 62 轮复评 76/100 —— 过 75 这条线，本批已推
（线上现在是 `3fbff95`，`/api/health/detail` 自报 `git_commit=3fbff9507845`）；返修的四条里两条是
"同一把尺子两腿两种待遇"与"对某类节点取不存在的属性"连着两批复发**（条目号取自报告结论表，正文不随仓库走）——
① **MAJOR（M-1）：`_live_nodes` 的 `If` 分支不把 `root.test` 当活节点** ⇒ `if release(db, code, dates):`
这种**诚实**写法被判"没接"（运行时那一把明明被叫了）。方向与第 60/61 轮那批相反：这一格不是买通闸，
是**闸过宽**，而本仓记过的教训是"过宽的结局就是整条闸被关掉"。修法把测试式一起进活节点集，且恒假时
只收 `orelse + test`、不收 `body`：
`arms = list(root.orelse) + [root.test] if _never_runs(root.test) else _prune_suite(root.body) + list(root.orelse) + [root.test]`
⇒ 两格样品各配：`if release(…):` 必须仍判**接上**、`if 恒假:` 分支里的调用必须仍判**没接**。
同一次返修又量出**同族的第二、第三格**（都判"过宽"方向、`src/` 今天一个对应物都没有，
但按本仓尺度"过宽的闸活不过一轮"必须当场收）：
②′ **别名累加**——`d = []` + `tmp = d` + `tmp.append(…)` 判"明着掏空"。现在 `_accumulation_names`
沿"`Name → Name` 的赋值链迭代到不动点"认别名，并配**两格反面对照**（只绑不填、填的是别的容器 ⇒
必须仍判"没递"，否则这条腿从"过宽"翻成"恒真"）。**边界**：`def collect(bucket): bucket.append(…)`
而 `collect(d)` 在别处 ⇒ **仍然不认**（那要跨函数的参数位数据流），写在 `_is_accumulated` 的
docstring 里并给出"今天没有这种形状"的复核命令（`grep -rn "release_holds_after_nav" src/`）。
③′ **整个内层 `def` 交给外面**——`@deco def inner(): release(…)` + `return inner` 判"没人叫"⇒ 整棵剪掉，
而同义的 `hooks = {'r': λ}` + `return hooks`（λ 那一腿）判接上 ⇒ 两种待遇；现在"活路径上有任何一处
**不是被调位置**的名字读取（`return` / 递进别的调用 / 赋给别人）"就算交出去，与 λ 那条 exemption
并成同一条规则（"它叫它自己"仍然不算）。
② **MAJOR（M-2）：`_kids` 会当场崩**（与上一批 `ast.Dict.elts` **同一族、连着第二批**）：`Global.names` /
`Nonlocal.names` / `MatchClass.kwd_attrs` 是 **str 列表**不是节点列表，下钻时取属性 ⇒
`AttributeError`（评审自己的探针崩过一次、我复现三格：函数体里有 `global`、嵌套 `nonlocal`、
`case C(x=1)`）。⚠ 光补"我喂过的那几类"仍然是半族 ⇒ 现在在**唯一的下钻入口**过滤
`items = [x for x in value if isinstance(x, _ast.AST)]`，一把闸盖住整族而不是三格。
反面样品：`global` 之后紧跟诱饵死 def 必须仍判"没接"（过滤不能把剪枝一起滤掉）。
③ **MAJOR（M-3）：λ 递进字面量容器、整包 return 出去 ⇒ holder 逃逸**。上一批的 holder 规则只认
"作为**实参**递进调用"那一档（`dict(r=λ)` / `.setdefault('r', λ)` / `.append(λ)`），而
`hooks = {'r': lambda: release(…)}` + `return hooks` 与 `hooks = [λ]` 两档**没接** ⇒ 同一件事两种待遇
（第 56~61 轮那一族第六次）。修法：字面量绑定也走 holder 规则，并且要用
`owned = {id(x) for group in held.values() for x in group}` 让 holder 规则**压过** key-dispatch 规则 ——
否则 `@r`/`#0` 那条先把它当"按键派发"剪掉，holder 规则永远轮不到（这一层顺序依赖是实测撞出来的，不是推的）。
④ **MINOR：归档那把补一条腿、把一条族登记成边界而不是"已封"**。新腿 `d.__setitem__(列, 值)`（受
`_dict_sink_names` 门控：只有那份字典真会递给库才数，`get_detail` 那种回显字典照旧不许数成写）；
`setattr(row, *['deleted_at', datetime.now()])` 这一档**不补**，写进 docstring 边界④：列名与值都在
运行时才定，与已登记的"列名是变量"同族 ⇒ 按本仓尺度"看不见就明说看不见"，别假装数得到。
⇒ 判据文件当场 **48 passed**；两把尺子的样品表**一律 AST 现数**（复核命令与上一批同一条，今天印
`evasions 45 / honest_live 38`）。**这批的账如实记**：①②③④ 全在**判据侧/文字侧**（① 里那三格
"过宽"各配反面对照），`src/` 行为
**一字未动** ⇒ 体检**不新增条目**（条数一律 `python scripts/mutation_proof_lifecycle.py --list` 看末行）。
⑤ **上线与生产 runbook（#133）的两步已经跑完，而且这次是**我替老板点的**（走既有 HTTP 接口，
没碰任何库连接）**：推 `dee6371..3fbff95` ⇒ 线上 `git_commit=3fbff9507845`、
`git_commit_source=RENDER_GIT_COMMIT`、`started_at=2026-09-28T14:03:50+08:00`、
`web/index.html`（LF 归一后）md5 `cce67fc57888e05cb6e94b3827d307cd`；`scheduler_running=False`
⇒ **#132 那件事一个字都没变**。`POST /api/funds/update-all`（14:05→14:09 北京）回执
"检测 1601 / 新增 0 只 / 关联 0 / already_ok 131 / 更新 164 / 失败 0"，`unsyncable` 逐行点名只剩
`603758`；`POST /api/predictions/verify-all`（14:10→14:12）`total 17 / not_processed 0`、
`failure_summary = 目标日净值尚未发布，等待中（17 条）`。
⑥ **那 17 条不是新坏的、也不是"没人管"——现读到行**：`target_date` **全部 = 2026-09-28（就是今天）**，
压在 11 只标的上（`515000`×5、`159928`/`160221` 各 2、其余各 1），标的库内末条净值 09-24
（`006105` 宏利印度 QDII 是 09-23）⇒ 走的是 `prediction_verify_service.py:643` 那条既有分支
`waiting_target_nav`（等待期 `data_wait_days` 内不判；到期后用目标日前最近净值判）⇒
**设计上会自愈**。它同时把 #132 说得更准了：**自愈的前提是"明天还有人点那两个按钮"**。
复核（只读，日期必须现算）：
```
D=$(date -u -d '+8 hours' +%F)
python scripts/q.py --production "select p.target_date, count(*) n, max(h.last_nav) last_nav from predictions p left join (select fund_code, max(nav_date) last_nav from fund_history group by fund_code) h on h.fund_code=p.fund_code where p.is_deleted=false and p.is_correct is null and p.prediction_type<>'flat' and p.target_date <= date '$D' and (p.next_verify_date is null or p.next_verify_date <= date '$D') group by 1 order by 1"
python scripts/q.py --production "select nav_date, count(distinct fund_code) funds from fund_history where nav_date >= date '2026-09-14' group by 1 order by 1 desc"
```
第二条今天印 `09-27/1 · 09-26/1 · 09-25/1 · 09-24/152 · 09-23~09-21 各 156 · 09-18~09-14 各 155~158`
⇒ **09-25 是周五却只有 1 只**（货币基金 `000725`），"绝对只数"仍分不清"周五缺行 / 休市 / 没补到"
（#135 那条根因一字未动；别拿它当休市凭据）。
⑦ **线上四数（部署 + 跑完两步之后，14:2x~14:4x，只读）**：`GET /api/stats/evidence` 自报
`database = 线上生产库（postgresql+psycopg2://aws-1-ap-south-1.pooler.supabase.com:6543/postgres）`、
`as_of = 2026-09-28`、**`已判 1191 / 判对 638 = 53.57%`、`⚠ 265（22.25%）`、`区间 41.39% ~ 63.64%`**、
`nav_as_of = 2026-09-27`、`nav_lag_days = 1`、`nav_lag_stale = False`、`nav_future_rows = 0`；
`python scripts/audit_verdict_evidence.py --production` 那一条独立路径印的 `已判 1191 / 对不上 265（22.3%）`
与分桶 `nav_rewritten 92 / verdict_under_other_fund 88 / nav_row_missing 85` **与接口逐字同数**
（两条路径互印证 ⇒ 这些数不是"我说的"）。`GET /api/predictions?lifecycle=due|unverifiable|all` 的
`meta.total` = **17 / 0 / 1601**。⚠ **一条运行账（不是回归）**：那次审计脚本跑到"体检覆盖面"那一步
`psycopg2.OperationalError: SSL connection has been closed unexpectedly` **中途崩**（Supavisor 池子把
长连接掐了；主账三行已经落盘，缺的是区间与覆盖面那两行 —— 我从接口那一侧取到了同数才敢这么说），
而本机走代理打 Render 接口同一个晚上重试了 5 次才通 ⇒ **只读工具对"线上会断"没有重试**，
登记成新任务（脚本侧补退避重跑，不改判据）。
⑧ **一条关于"怎么跑"的新账（我自己撞的，写在这儿防下一轮）**：这一批第一次跑 `pytest tests/unit -q`
**崩在半路**——退码 **139**、`Windows fatal exception: access violation`，崩点在子进程的
`subprocess` 读线程里。根因不是代码：**我让 `scripts/audit_verdict_evidence.py --production` 与基线并发了**，
而那两把锁只挡 pytest↔pytest 与 pytest↔体检，**挡不住一个长时远程读脚本**（它不起 pytest、不抢锁）。
⇒ 规矩从"跑基线期间不许起第二个 pytest 会话"再收窄一步：**也不许并发起任何长任务**
（远程只读体检、生产接口轮询都算）。判"这次跑数能不能信"看退码 + 日志字节数；139 那一类
`access violation` 直接作废重跑，别拿它当回归。
最后一次核对（**串行**、默认 locale cp936、子进程显式 `PYTHONIOENCODING=utf-8`、跑期间不起第二个会话，
也不起任何长任务 —— 就是上面 ⑧ 那一条；本次五步串成一条链 `data/_review_tmp/r62_chain.sh`，逐步取退码）：

- `pytest tests/unit -q` → **1229 passed / 16 skipped / 0 failed**（732.36 秒，退 0）。
- `pytest tests/ -q` → **1238 passed / 16 skipped / 0 failed**（567.37 秒，退 0）。
  （与上一批**同数** —— 每一组新样品都并进已有用例的样品表，`def test_` 条数一条没增；分布用
  `for f in $(git diff --name-only HEAD~1 -- tests/); do echo "$f $(git show HEAD~1:$f | grep -c '^def test_') -> $(grep -c '^def test_' $f)"; done`
  ⇒ `test_one_ruler_per_question.py 5 -> 5`、`test_structurally_unverifiable_hold.py 32 -> 32`。
  判据文件当场：`python -m pytest tests/unit/test_one_ruler_per_question.py
  tests/unit/test_structurally_unverifiable_hold.py -q` ⇒ **48 passed**；
  两把尺子的样品表 AST 现数 ⇒ `evasions 45 / honest_live 38`。）
  变异：`python scripts/mutation_proof_lifecycle.py` **30 处全 RED**（CONTROL-GREEN = 7 个判据文件在干净代码上
  全绿；无 ANCHOR-MISS / 无 HARNESS-FAIL / 无 `[还原失败]`；跑完 `git status --porcelain -- src/` 为空、
  无 `.mutbackup` 残留），原始日志随仓库走
  `docs/迭代计划/run-20260927-mutation/round62-lifecycle-mutations.txt`
  （首行 `# run @ 2026-09-28T15:31:15+08:00  git=ae8c94308975  python=3.12.10  共 30 处变异 / 7 个判据文件`，
  md5 `2dfadcd74e29e567cd1c0f55a8e03d07`，与上一批那份 `13b4022a90e599297a5286af416b1030` 不同 ⇒
  ⚠ 这一笔的 `git=` 是**判据那一笔提交**（`ae8c943`），不是文档这一笔 —— 体检改的是 `src/`，
  与判据文件同一份代码，所以日志里那个哈希才是它真正跑在上的版本）。
  `audit_doc_claims.py` 退 **0**（"全部对得上（条数 3 条、数据源 4 行都认得出来自哪个库）；
  另有 16 条'看得见但不判'"）。
  **镜像此刻**（同日 15:38，只出计划不写库：`python scripts/close_unknowable_predictions.py`）：
  `[计划] 到期未判里可以判定"永远问不出来"的：0 条；仍在等的：29 条`（与上一批同数）；
  **生产此刻**（同日 14:2x~14:4x，只读 + 日期现算 `D=$(date -u -d '+8 hours' +%F)`）：
  `expired_unjudged 17 / held_by_lock 0 / actionable_today 17`，而这 17 条的 `target_date` **全是当天**
  ⇒ 走 `waiting_target_nav`（⑥ 现读到行），**生产仍然没有任何东西在跑**（#132）——
  把"明天有人点"换成"打开网站就补"的实施清单已写成 `data/_review_tmp/r62-catchup-plan.md`，落在任务 #132。）

（上一批：2026-09-28 13:3x（北京），**任务 #148：第 61 轮复评 73/100 返修——三条 MAJOR 里最重的
一条又是我上一批自己写的那句"没有一处过宽"（这次不是漏判，是**闸过宽** + **尺子会崩**），
评审同时确认第 60 轮那条 BLOCKER 撤谎改对了**（条目号取自报告结论表，正文不随仓库走）——
① **MAJOR（M-1）：`_live_nodes` 的 `Try` 分支把 `else` 整段剪了** ⇒ `try: … except: … else: release(…)`
判"没接"。同一个函数里 `For`/`While` 两档的注释逐字写着"`else` 照进" ⇒ **同一把尺子两腿两种待遇**
（本仓第 45 轮起反复扣分那一族）。另一格：`_is_accumulated` 只数**方法调用**、不数 `AugAssign`
⇒ `d = []` 后 `d += [code]`（`src/` 里真有 3 处 `+= [`）被判"明着掏空"。两格**在父提交同样判 False**
⇒ 前批遗留，而我上一批把它写成"已封"。修法：`Try` 补 `+ list(root.orelse or [])`、`_is_accumulated`
认 `AugAssign`（右值不猜，看不清就算累加），各配反面样品。
⚠ 从此"过宽"这一族只许说"这 N 格内没复发"，不许说"没有一处过宽"。
② **MAJOR（M-2）：字典那一腿不是漏判，是崩**。`_empty_container` 把 `ast.Dict` 与 List/Tuple/Set 并列取
`.elts` ⇒ 一喂 `{}` 就 `AttributeError: 'Dict' object has no attribute 'elts'`，而它自己的 docstring
逐字点名 `{}` 算空（复现三格：`for _ in {}`、`d = {}` 再递进去、`[k for k in {}]`；父提交也崩 ⇒ 又是遗留；
`{k: release(…) for k in {}}` 父提交是**静默判已接线**、改成崩之后方向不算坏）。
⇒ `Dict` 单独看 `.keys`；这一格也说明"清单就是那份样品表"有一个前提：**样品得真把那一腿喂过**，
没喂过的列等于没有清单。
③ **MAJOR（M-3）：解锁那把仍被 7 格买通**（评审给 8 格，⚠ **其中一格不成立**：`for _ in (d := []): release(…, d)`
实测已判 False，不改，写在这儿防下一轮照抄评审原文）。成立的七格与修法（**都不靠按名字猜 callee**）：
`hooks = dict(r=λ)` / `hooks.setdefault('r', λ)` / `hooks.append(λ)` ⇒ 问一句可证的"**那个容器后来有没有被取用**"
（不 return、不递进别的调用、不下标取、不迭代 ⇒ 谁也拿不到那个 λ）；绑到别处的名字、模块级/形参容器、
`return dict(r=λ)`、`xs = sorted(…, key=λ); return xs` ⇒ 全部仍算接上（反面样品各一格）。
`release(db, code, [x for x in []])` / `*[]` / 形参默认 `dates=[]` ⇒ 新的 `_provably_empty`（空推导式、
`Starred` 壳、只在"这名字不是本函数赋的"时才看签名默认值）；`assert False` 之后那一行 ⇒ `_terminates`
认**恒假的 `assert`**（`assert x` 那种要到运行时才知道的照旧不剪）。
⇒ 规避样品表 **32 → 42** 格、诚实表 **17 → 30** 格，**条数一律 AST 现数**：
`python -c "import ast,io;t=ast.parse(io.open('tests/unit/test_structurally_unverifiable_hold.py',encoding='utf-8').read());print([(n.targets[0].id,len(n.value.keys)) for n in ast.walk(t) if isinstance(n,ast.Assign) and getattr(n.targets[0],'id','') in ('evasions','honest_live')])"`
④ **MINOR：上一批的提交信息把没补的写成了补的**。"别名 `s = object.__setattr__`"当时实测回 `[]`
（同一批的 docstring 写的才是对的："仍然没补"）⇒ 这一批真补：`alias_names` 只认"本函数里唯一一次把名字绑到
`setattr` 或 `object.__setattr__`"（认**值**不认名字，与第 43 轮 `create_engine as ce` 同判法），并配
"`s = row.get` 不许被当成 setattr"的反面对照；另补 `row.__dict__ |= {列: 值}`（`python -c` 实测运行时真换字典）。
复现命令也修了一把：登记注释里那句 `grep -rn .update( src/` **量不出"只有 4 条"** ⇒ 换成
`grep -rn "self.update(" src/services/`（AGENTS ⑤ 段用的这把才是对的）。
⑤ **精度一句（结论不变、话得说准）**：§2d 那句"读出来的数就在页面上"**用错了键** —— `:447` 记的是
`protected_counts["active_viewpoints"]`（`:448/:454`），而页面 `web/index.html:667-671` 只渲染五档
`protected_counts`、不含这一档（`grep -rn active_viewpoints web/` ⇒ 0）⇒ 屏上真会动的是**补集**
`counts['viewpoints']`（候选侧，`:657` 那个 `v-for`），并且它挂着 `v-show="count > 0"` ⇒ 单桶减到 0 时整块消失、
"没数"与"归零"同形。两处文字已按这一层收窄（"补那一列会动页面上那个数"仍然成立，动的是候选数）。
⑥ **产品侧新账写进 #146 的 D 段**（只立项不动手，三条都是我现读复核过的）：
`bloggers.py:72` 的"近 7 天"墙钟直接决定博主榜 `active_posts_count`（`:82` 筛 → `:109` 交出 → `index.html:148`）；
`viewpoint_workflow_service.py:920` 那把墙钟既是"今天已汇总过"的幂等闸、又写进台账 `run_date`
⇒ 北京 00:00~08:00 点「每日汇总」会**再跑一次**；`prediction_service.py:558/673/800` 三处墙钟分属
`get_expiring_predictions`(`:548`)/`get_anomaly_predictions`(`:597`)/`get_history_lookup`(`:769`)，
`grep -rn <名字> src/ scripts/ web/ tests/` **除定义外零命中**（只剩 `.pyc`）⇒ 死路、不进产品账；
顺带清掉一条假活路：`config.py:602 GET /cleanup/preview-legacy` 函数体第一句就 `return`（`:604`），
底下约 150 行（含第二把墙钟 `:616`）是**不可达代码**、且 `grep -rn preview-legacy web/` ⇒ 0
⇒ 上一批数"几个预览用哪把钟"时它容易被误数成一站；删这段死代码是独立决定项（改公共接口面）。
**这批的账也要如实记**：①②③④⑤ 全在**判据侧/文字侧**，`src/` 行为**一字未动** ⇒ 体检**不新增条目**
（30 处与上一批同数，条数一律 `python scripts/mutation_proof_lifecycle.py --list` 看末行）。
最后一次核对（**串行**、默认 locale cp936、子进程显式 `PYTHONIOENCODING=utf-8`、跑期间不起第二个会话；
⚠ 本次两条口径**都比上一批慢**（565s / 453s vs 323s / 310s），是本机内存负载高、不是回归 —— 判回归看的是
**passed/skipped/failed 三个数与退码**，不是秒数）：

- `pytest tests/unit -q` → **1229 passed / 16 skipped / 0 failed**（565.04 秒）。
- `pytest tests/ -q` → **1238 passed / 16 skipped / 0 failed**（453.14 秒）。
  （与上一批**同数** —— 每一组新样品都并进已有用例的样品表，`def test_` 条数一条没增；分布用
  `for f in $(git diff --name-only HEAD -- tests/); do echo "$f $(git show HEAD:$f | grep -c '^def test_') -> $(grep -c '^def test_' $f)"; done`
  ⇒ `test_one_ruler_per_question.py 5 -> 5`、`test_structurally_unverifiable_hold.py 32 -> 32`。
  判据文件当场：`python -m pytest tests/unit/test_one_ruler_per_question.py
  tests/unit/test_structurally_unverifiable_hold.py -q` ⇒ **48 passed**。）
  变异：`python scripts/mutation_proof_lifecycle.py` **30 处全 RED**（CONTROL-GREEN = 7 个判据文件在干净代码上全绿；
  无 ANCHOR-MISS / 无 HARNESS-FAIL / 无 `[还原失败]`；跑完 `git status --porcelain -- src/` 为空、无 `.mutbackup` 残留），
  原始日志随仓库走 `docs/迭代计划/run-20260927-mutation/round61-lifecycle-mutations.txt`
  （首行 `# run @ 2026-09-28T13:27:15+08:00  git=c705b788895c  python=3.12.10  共 30 处变异 / 7 个判据文件`，
  md5 `13b4022a90e599297a5286af416b1030`，与上一批那份 `d3142579ea444a1aa90c7f690290cf25` 不同）。
  `audit_doc_claims.py` 退 **0**（"全部对得上（条数 3 条、数据源 4 行…）；另有 16 条'看得见但不判'"）。
  **镜像此刻**（同日 13:3x，只出计划不写库：`python scripts/close_unknowable_predictions.py`）：
  `[计划] 到期未判里可以判定"永远问不出来"的：0 条；仍在等的：29 条`；
  **生产此刻**（同日 13:3x，只读探针 + 日期现算 `D=$(date -u -d '+8 hours' +%F)`）：
  `expired_unjudged 17 / held_by_lock 0 / actionable_today 17` —— 与上一批同数，因为
  **生产仍然没有任何东西在跑**（#132），这 17 条一天天变旧。

（上一批：2026-09-28 12:2x（北京），**任务 #147：第 60 轮复评 72/100 返修——最重的一条又是我自己写的
假话（这一次错在"依据"、结论那半句站得住），第二条是同一把解锁尺子又被八种拼法买通、而其中两种拼法
本批刚在隔壁那把里认下过**（条目号是报告那张结论表的编号，报告正文不随仓库走）——
① **BLOCKER（条目 7）："今天没有活消费面读观点那一列"是假话**（第 58~60 轮**连续第三个批次**犯
"死路 / 活路归因写反"同一族，本仓尺度里这一条最贵）：第 59 轮我为了纠正 #142 的旧立论写下这句，
并把它同时用作 AGENTS ① 段与 `docs/模块总览/预测验证与准确率统计.md` §2d 的依据。现读驳回：
`retention_cleanup_service.py:443` 的 SELECT 里带着 `Viewpoint.restore_before`、`:447` 比
`restore_before >= self.today` ⇒ 还在窗口内的行挪进 `protected_counts`、**不进候选**；那条链是活的
（`:194 build_plan()` → `config.py:333 GET /api/config/cleanup/preview`（回执含 `counts['viewpoints']`
与 `protected_counts`）→ `web/index.html:2549 fetchCleanupPreview`），`cleanup_tasks.py:769` 与调度侧也 import 它
⇒ **补那一列会动页面上那个数**，不许把它当死列处理掉。更要命的是它与**同一段文字 6 行前**我刚引用的
`:447` 那句自相矛盾。**结论那半句仍然站得住**，正确的立论是"**预览保护得到、真删保护不到**"这一分裂：
线上真在删观点行的是三桶那把（`retention_three_buckets._deleted_viewpoint_ids:582` 只比
`deleted_at < cutoff`、一个字不看那一列，复核 `grep -c restore_before src/services/retention_three_buckets.py`），
而这把 `build_plan` 背后的执行器已永久下线（`HARD_DELETE_DISABLED = True` :46、`execute()` 起手 raise、路由回 403）
⇒ 补列能改**页面上那个数**，改不了**会不会被删**。两处文字已按这一分裂重写，#142 的立论跟着换。
⚠ 由这一分裂又量出两件新账写进 §2d：`:447` 那个窗口用的是**墙钟**（`:157 self.today = today or date.today()`）
⇒ 生产容器在 UTC 时恢复窗口今天就在**提前一天**关（#145 落在这一列上）；以及"那 18 行永远进不了清理桶"
说的是**今天**，它靠的是那把旗 = True —— `scheduler.py:183` 的 import 排在 `:172` 的 return **之后**，而
`cleanup_tasks.py:122` 那把旧尺子按 `viewpoint_date` 删、`is_deleted` 与 `deleted_at` 一个字都不看
⇒ 旗一放就是另一件事（条目 18，这层前提现已写进 §2d）。
② **MAJOR（条目 8）：「页面三个清理预览都在用墙钟那把的 `build_plan`」数量与"都"两处都不对** ⇒ 改成
"两个预览各用一把（`/api/config/cleanup/preview` 走墙钟、`/cleanup/three-buckets/preview` 走北京钟）
+ 第三个按钮 `/api/test-data/find` 压根不看日历"⇒ 北京 00:00~08:00 之间"预览"与"真删"算的不是同一个今天。
③ **MAJOR（条目 9）：`_releases_live` 又被 8 格 / ≥7 族买通**，而其中两种是本批刚在隔壁 `_bound_value` 认下的
（同一件事两种待遇）：λ 绑在 `AnnAssign` / 海象 / 列表元组字面量槽位上、`for` 那一腿不认**推导式生成器**
（`ListComp`/`SetComp`/`DictComp`/`GeneratorExp` over 空容器）、`return`/`raise` **之后**的语句、`match` 的恒假 guard。
现在 `_empty_container_names` 与 `_live_nodes` 走同一份"可证为空"的名字（剪的是不可达，不是这一族写法）、
`_dead_inner_defs` 按 Name / Attribute / 下标常量键三种绑法收 λ 并迭代到不动点、`_kids`/`_prune_suite` 剪掉终止语句之后、
`match` 按**类名**认（不在 3.9 上取属性；`_match_supported()` 判语言支持，不支持就跳过那一格，不许恒空）。
⇒ 规避样品表 **22 → 32** 格、诚实写法表 **9 → 17** 格，逐格真跑：10 格新形状 `releases_live` True→False，
17 格诚实写法仍 True（~~**没有一处过宽**~~ ⇒ ⚠ **这句被第 61 轮当场证伪**：`try/except/else` 里接锁、
`d = []` 后 `d += [code]` 再递进去两格都判"没接"＝**过宽**，同一把尺子 `For`/`While` 那两档注释里逐字写着
"`else` 照进"。两格在第 61 轮补腿 + 各配反面样品；"过宽这一族"从此不许再说成"已封"，只许说"这 N 格内没复发"）。
**表条数一律现数**：`python -c "import ast,io;t=ast.parse(io.open('tests/unit/test_structurally_unverifiable_hold.py',encoding='utf-8').read());print([(n.targets[0].id,len(n.value.keys)) for n in ast.walk(t) if isinstance(n,ast.Assign) and getattr(n.targets[0],'id','') in ('evasions','honest_live')])"`。
⚠ 这些形状 `src/`+`scripts/` **今天一个都没有**（`match` 全仓 0 处、空迭代推导式 0 处）⇒ 这一条扣的是
**"补全"那句承诺说过头**与尺子自身的洞，不是产品行为坏了；docstring 那句"把 λ 的绑法**补全**"已改成
"清单就是那份样品表"，③ 段那句"三种绑法"也收窄成同一写法（**说满话这一族第 56~60 轮第五次扣分**）。
④ **MINOR（条目 10）：归档那把还漏五种隐身拼法**——`row.__dict__.update({...})`、`vars(row)[...]`、
~~`s = object.__setattr__` 再 `s(...)`~~、`db.session.set(row, {...})`、`p = {}` + `p.update({...})` + `q.update(p)`
实测全回 `[]`。现在 `_instance_dict()` 认 `x.__dict__` 与 `vars(x)` 两种、`.setdefault` / `__dict__.update` /
`vars(...).update` 各补一腿、`session` 这个收件人算"像查询"、组字典那一腿按 sink 动词门控
（`echo_dict_update` 那格——返回给前端的字典——必须仍为空，第一版不门控就是过宽，被新控制当场点红）。
⚠ **上面那句"补了五格"里有一格是假的**（第 61 轮 MINOR）：别名 `s = object.__setattr__` 当时实测仍回 `[]`
（同一批的 docstring 写的才是对的："仍然没补"），`row.__dict__ |= {列: 值}` 也没数到 ⇒ 两格到第 61 轮才真补，
各配"绑到别的东西上的名字不算 setattr"的反面对照。
⚠ **仓库里今天没有活对应物** ⇒ 按本仓尺度定 MINOR，不许写成"产品洞补上了"。
⑤ **两条边界的前提以前没写进文字**（条目 11 / 18）："列名是变量的 `setattr` 看不见"之所以**成立**，依据是
"**当前调用方名单里递不进能写归档列的载荷**"（`src/services/base.py:99-101` 那条载荷驱动写列名**真实存在**，
`PredictionService`/`ViewpointService` 都继承它 ⇒ 潜伏面是真的），**不是**"这种拼法不存在" ⇒ docstring 按前者改写。
调用方名单现读：`blogger_service.py:162/174`、`post_service.py:115/132` 四条字面量，
`grep -rn "self.update(" src/services/` 只命中这些；另三个 `setattr(变量)` 站点逐个核过
（`fund_service.py:110` 目标表**没有**归档列、`post_service.py:294` 键被白名单钉死、`core/config.py:197/224`
写的是 `Settings` 类属性、不碰 ORM 行）。⚠ **评审自己第一版探针按 `ast.AnnAssign` 取列名** ⇒ 对
`Prediction`/`Viewpoint` 报了 `deleted_at=False`，那是错的（本仓模型用 `deleted_at = Column(Date)` 这种
`ast.Assign`）⇒ 下一轮别照那份抄，列名要 `Assign`/`AnnAssign` 两种都收。
⑥ **产品侧新洞只立项、不动手**（条目 16 / 17）⇒ **任务 #146**：`rollback_invalid_verifications:1730`
跑**墙钟**，而它的签名（`:1683-1685`）**压根没有 `today`/`as_of` 参数** ⇒ 连"两面钟冲突必须按北京"那条用例
都没有抓手；同一个 helper 在 `verify_prediction` 那支是 `:1061-1066 today = current_as_of()`（第 52 轮 B-3 改的），
而这条是**页面按钮、真会写**（`predictions.py:134/172` → `prediction-manager.js:255/270` → `index.html:306/314`）。
同类两处创建排期（`llm_analyzer.py:1278`、`prediction_service.py:354`）与观点/建议侧"近 N 天"窗
（`advice_service.py:54/56/238/330`、`viewpoint_service.py:101/114/148/290`）一并写进 #146 ——
隔壁 `advice_evidence.py:8` 已逐条收 `as_of` ⇒ **相邻两个模块两种待遇**。**改哪一把都会动"撤几条结论 /
哪些行还算在窗口内"** ⇒ 与 #145 一起定，不一处一处漂。
**这批的账也要如实记**：①②③④⑤ 全是**判据侧 / 文字侧**，`src/` 行为**一字未动** ⇒ 变异体检**不新增条目**
（30 处与上一批同数，条数一律 `python scripts/mutation_proof_lifecycle.py --list` 看末行），
"新腿有没有牙"由那 18 格新样品（10 格规避 + 8 格诚实）与归档那把的 6 格负责，
别拿"变异全 RED"当这一批的证据。
最后一次核对（**串行**、默认 locale cp936、子进程显式 `PYTHONIOENCODING=utf-8`、跑期间不起第二个会话）：

- `pytest tests/unit -q` → **1229 passed / 16 skipped / 0 failed**（323.22 秒）。
- `pytest tests/ -q` → **1238 passed / 16 skipped / 0 failed**（310.44 秒）。
  （与上一批**同数** —— 本批每一组新样品都并进已有用例的样品表，`def test_` 条数一条没增；
  分布用 `for f in $(git diff --name-only HEAD -- tests/); do echo "$f $(git show HEAD:$f | grep -c '^def test_') -> $(grep -c '^def test_' $f)"; done`
  ⇒ `test_one_ruler_per_question.py 5 -> 5`、`test_structurally_unverifiable_hold.py 32 -> 32`。
  判据文件当场：`python -m pytest tests/unit/test_one_ruler_per_question.py
  tests/unit/test_structurally_unverifiable_hold.py -q` ⇒ **48 passed**。）
  变异：`python scripts/mutation_proof_lifecycle.py` **30 处全 RED**（M1~M28 含 M3b/M5b；
  CONTROL-GREEN = 7 个判据文件在干净代码上全绿；无 ANCHOR-MISS / 无 HARNESS-FAIL / 无 `[还原失败]`），
  原始日志随仓库走 `docs/迭代计划/run-20260927-mutation/round60-lifecycle-mutations.txt`
  （首行 `# run @ 2026-09-28T12:00:15+08:00  git=548d7833bc40  python=3.12.10  共 30 处变异 / 7 个判据文件`，
  md5 `d3142579ea444a1aa90c7f690290cf25` **与 round59 那份 `1188a3454943e0d2a3219c54f54a9811` 不同**）。
  `audit_doc_claims.py` 退 **0**。
  **镜像此刻**（同日 12:1x，只出计划不写库：`python scripts/close_unknowable_predictions.py`）：
  `[计划] 到期未判里可以判定"永远问不出来"的：0 条；仍在等的：29 条`；
  **生产此刻**（同日 12:1x，只读探针 + 日期现算 `D=$(date -u -d '+8 hours' +%F)`）：
  `expired_unjudged 17 / held_by_lock 0 / actionable_today 17` —— 与上一批同数，因为
  **生产仍然没有任何东西在跑**（#132），这 17 条一天天变旧。

（上一批：2026-09-28 10:2x（北京），**任务 #143：第 59 轮复评 70/100 返修——三条 MAJOR 里最重的
一条是"我上一批用来收尾的那段『我自己驳回自己』，把同一个错（死路当产品事实）又写了一遍"；
第二条是这把尺子对"整包摊进构造函数"失明、而它有一个活的对应物；第三条是同一把尺子内部两腿
用了两种遍历**（条目号 M-*/m-* 落在 #143 的 metadata 里，报告正文不随仓库走）——
① **"我更正一下"本身是一句要被现读的断言**（M-1，本批最贵）：第 58 轮那段驳回 M-1 的新文字里，
我把 `viewpoint_service.delete_viewpoint` 写成"页面那条按钮走它"、把 `cleanup_enhanced.SoftDeleteManager`
写成"会把两列一起填上的通路"。两句都错（命令与逐字纠正见上面 #141 那段里第 59 轮新加的 ⚠⚠ 块，
和 `docs/模块总览/预测验证与准确率统计.md` §2d ①~④）：服务层那条**零调用方**、页面那条是
带 `X-Danger-Confirm` 的**硬删**、那个模块**全仓零 import** ⇒ 连"药方"都是把活路接到死路上。
真正活着的是 AI 判拒绝 `viewpoint_workflow_service.py:328`（两个时间戳一个都不写），
而活的硬删要求 `deleted_at.isnot(None)` ⇒ 镜像那 **18** 行 `rejected:` 永远进不了清理桶
⇒ **任务 #142 整条重写**（旧立论"补 `restore_before` 就有保护"**拦不住物理删除**：真在删行的
是三桶那把，它的 `_deleted_viewpoint_ids` 只看 `deleted_at`，而这把尺子的 `execute()` 起手就 raise）。
⚠⚠ **第 60 轮 BLOCKER：这一句当时给的"理由"半句是假话**（结论半句站得住，错在依据）——
我写"今天没有活消费面读观点那一列"，而现读代码是**有人读、而且读出来的数就在页面上**：
`retention_cleanup_service.py:443` 的 SELECT 里带着 `Viewpoint.restore_before`，`:447`
`if is_deleted and restore_before and restore_before >= self.today:` ⇒ 还在窗口内的行挪进
`protected_counts`、不再进候选。那条链一路活的：`:194 build_plan()` →
`config.py:333 GET /api/config/cleanup/preview`（回执带 `counts['viewpoints']` 与
`protected_counts`，:355）→ `web/index.html:2549 fetchCleanupPreview`；另有
`config.py:300 /cleanup/orphan-funds/preview`（**页面不打它**，`grep -rn orphan web/` ⇒ 0）
与 `src/tasks/cleanup_tasks.py:769` ⇒ **补那一列确实会把行从候选挪进保护**，别把它当死列处理掉。
⚠ **"读出来的数就在页面上"这一句在第 61 轮又被收窄一次**：`:447` 记的是 `protected_counts["active_viewpoints"]`
（`:448/:454`），而页面 `web/index.html:667-671` 只渲染五档 `protected_counts`、**不含这一档**
（`grep -rn active_viewpoints web/` ⇒ 0）⇒ 屏上真会动的是**补集** `counts['viewpoints']`（候选侧，`:657` 那个 `v-for`），
且它挂着 `v-show="count > 0"` —— 单桶减到 0 时整块消失，"没数"与"归零"在屏幕上同形。细节与复现命令写在
`docs/模块总览/预测验证与准确率统计.md` §2d（结论一个字不变，措辞按这一层收窄）。
要说清的分裂是"**预览保护得到、真删保护不到**"：预览这一把认那一列，而线上唯一会删行的三桶
那一把（`retention_three_buckets.py:584/592`）只比 `deleted_at < cutoff`、一个字都不看那一列，
加上旧执行器永久下线 ⇒ 补列能改**页面上那个数**，改不了**会不会被删**。
⚠ 而 :447 那个窗口用的是**墙钟**（`:157 self.today = today or date.today()`）⇒ 生产容器在 UTC
时它今天就在**提前一天**关（这正是 #145 那件事落在这一列上，第 60 轮 BLOCKER 的另一半）。
② **归档那把尺子对"整包摊进构造函数"失明，而且有活的对应物**（M-2）：
`Model(**{'deleted_at': …})` / `Model(**payload)` 实测都回 `[]`（第 56 轮 m-3 为 **NAV** 那把
补过同一族，归档这把没接）。现在 `_splat_dicts` 认两档来路（字面量 / 一跳变量），**并且不要求**
收件人长得像查询（建行本身就是写，与批量那一腿的边界不同）；样品与反面对照各若干格，
**清单就是那份判据里的控制样品表**（第 59 轮 m-2 立的规矩：数一抄就过期）。⚠ **边界要说白**：
`data_portability_service.py:231` 的
`spec.model(**cleaned)` 里**列名是运行时从元数据拼出来的**，任何按 AST 数的尺子都看不见 ⇒
这一族的闸不在这里，而在 `_clean_row` 剔列 + 行为判据（现在两样都没有 ⇒ **新任务 #144**，
它属"改公共接口行为"，老板决定项）。⚠ 另外两族今天**仍然看不见**，按同一尺度写进那把尺子的
docstring 而不是当已封：**列名是变量的 `setattr`**（免疫那把为此开了 `IMMUNITY_OPAQUE_SITES`
登记通道，这把没有 ⇒ 两把尺子的又一处不对称）、以及**查询从函数参数递进来**（`q.update({列: 值})`
而 `q` 是形参 —— 按参数名猜它是查询就是本仓反复驳回的"按名字猜"，所以宁可看不见）。
③ **同一把尺子内部两腿用了两种遍历**（M-3）：`_releases_live` 还能被**五种**新拼法买通（探针实测
`releases_live=True`）：`d: list = []`（AnnAssign）、`(d := [])`（海象）、
lambda 绑在**属性**上（`C.r = lambda: …`）或塞进**字典字面量**而没人叫、
`d = []` + `for _ in d:` 体里的解锁；另有一格"同一名字两次空赋值"上一批已写进豁免（只回溯唯一一次
赋值），本批仍然按豁免处理、不算新账。根因两族：① **赋值那腿只认 `ast.Assign`**、而 `for` 那一腿
不做同函数内回溯（参数做一跳、`for` 不做 ⇒ 同一把尺子两种待遇）；② **lambda 只认绑在 Name 上**，
属性位与字典格里的没人管。现在 `_bound_value`（认 `Assign` / `AnnAssign` / `NamedExpr`，
绑过两次算"看不清算递到了"）+ `_is_accumulated` 被**两腿共用**，`_empty_container_names` 从同一份
活节点里推出"可证为空"的名字、`_live_nodes` 第二遍据此剪掉整个 `for` 体，
`_dead_inner_defs` 按 Name / Attribute / 字典 `@key` 三种绑法收 lambda。
⚠ **这句到第 60 轮只算"当时补了三种"**：同一批刚给参数那一腿认下 `AnnAssign`/`NamedExpr`，
λ 这一腿没跟上 ⇒ 带标注、海象、列表/元组字面量、下标位**四种绑法**实测仍判"已接线"
（第 60 轮 M-1，docstring 里那句"把 λ 的绑法**补全**"因此是说过头，已改）。
反面样品同步补（λ 真被叫、`for` 过非空字面量、活 def 里真累加、带标注但真累加……）
⇒ 剪的是"不可达"，不是"这一族写法"；条数一律看那两份 dict，别在这里抄。
④ **同一件事在两份文档里给了两种引用**（m-1）：AGENTS 说"两处消费面都比北京 today 减 N 天"、
模块总览只引三桶 —— 实测前者第二半不成立（`retention_cleanup_service.py:447` 是
`restore_before >= self.today` 的**窗口检查**，且那个服务的 `today` 是 `date.today()` :157 墙钟）。
两处现已统一成"只有三桶那一处 + 那一站跑墙钟"。**顺带量出一条产品侧的账**：两个清理服务的"今天"
是两把钟，而页面**能取到的两个清理预览各用一把**（`/api/config/cleanup/preview` 走墙钟那把的
`build_plan`、`/api/config/cleanup/three-buckets/preview` 走北京那把；第三个按钮
`/api/test-data/find` 压根不看日历）⇒ ⚠ **上一版写"页面三个清理预览都在用墙钟那把"是说过头**
（第 60 轮 MAJOR：数错了按钮、也把北京那把说成墙钟）。换北京钟会让硬删**提前** ⇒
**新任务 #145，决定项、不顺手改**。
⑤ **会过时的数又不留文字版了**（m-2 / m-3）：`archive_stamp()` 的 docstring 与两份判据注释都写着
"五种拼法"，而第 57~59 轮实际连着往里补了批量三档来路、裸 SQL 一整族、`object.__setattr__` /
下标组字典 / `setdefault` / 整包 `**` 摊进构造函数 ⇒ 那三处文字全改成"清单就是那份控制样品表"。
"回收站那三条活路"里有一站挂在零 import 的模块上 ⇒ 注释改成"三站里两站活着，第三站的意思是
'以后接回去不许换墙钟'"。
⑥ **体检日志要能自证是真重跑**（m-4）：`round58-lifecycle-mutations.txt` 与 `round57` 那份
**逐字节相同**（md5 `19acd5b822c563495ae444cd9f400dce`）而里面没有任何运行时刻 ⇒
"原始日志随仓库走"这一件事上一批无法自证。现在 `scripts/mutation_proof_lifecycle.py` 起手印一行
`# run @ <北京 ISO 时刻>  git=<HEAD>  python=<版本>  共 N 处变异 / M 个判据文件`；
时刻按固定 UTC+8 偏移现算，**不 import `src.*`**（那会把全局 engine 按 `.env` 建出来，
而体检工具不该连任何库 —— 这条注释就写在函数里）。
**这批的账也要如实记**：①②③ 全是**判据侧 / 文字侧**，`src/` 只改了一处 docstring（那只钟的
说明）**、行为一字未动** ⇒ 变异体检**不新增条目**
（条数一律 `python scripts/mutation_proof_lifecycle.py --list` 看末行），"新腿有没有牙"由那些
控制样品负责，别拿"变异全 RED"当这一批的证据。
⑦ **我这一批破了"冻结"这条规矩一次，代价是白跑一遍体检**（写在这儿防下一轮的我）：
第一次体检跑到 M13 时我去往判据文件里补控制样品 —— **体检是"改写 `src/` → 从磁盘重读判据文件
跑 pytest"**，所以 CONTROL 用的是旧判据、后面每一处 RED 用的却是新判据 ⇒ 这份日志只能作废。
强杀（`taskkill /F`）会跳过 `finally` 的还原，当场 `src/fund/fund_api.py` 留着变异载荷
（`days: int = 30` 顶掉了 `days: Optional[int] = None`）⇒ 收法是 `cp <文件>.mutbackup <文件>`
再删那个 `.mutbackup`，**确认手段是 `git status --porcelain -- src/` 里那一行消失**，
不是拿 `git show HEAD:<文件>` 比字节（那串是 LF、工作树是 CRLF，同一内容 56046 vs 57250 必然不等）。
杀完 lock 文件仍在磁盘上但操作系统已放开 ⇒ 看见 `.mutation-harness.lock` 就 `rm` 是错的。
最后一次核对（**串行**、默认 locale cp936、子进程显式 `PYTHONIOENCODING=utf-8`、跑期间不起第二个会话）：

- `pytest tests/unit -q` → **1229 passed / 16 skipped / 0 failed**（353.69 秒）。
- `pytest tests/ -q` → **1238 passed / 16 skipped / 0 failed**（343.49 秒）。
  （与上一批**同数** —— 本批每一组新样品都并进已有用例的样品表，`def test_` 条数一条没增；
  分布用 `for f in $(git diff --name-only HEAD -- tests/); do echo "$f $(git show HEAD:$f | grep -c '^def test_') -> $(grep -c '^def test_' $f)"; done`
  ⇒ `test_one_ruler_per_question.py 5 -> 5`、`test_structurally_unverifiable_hold.py 32 -> 32`。
  判据文件当场：`python -m pytest tests/unit/test_one_ruler_per_question.py
  tests/unit/test_structurally_unverifiable_hold.py -q` ⇒ **48 passed**。）
  变异：`python scripts/mutation_proof_lifecycle.py` **30 处全 RED**（M1~M28 含 M3b/M5b；
  CONTROL-GREEN = 7 个判据文件在干净代码上全绿；无 ANCHOR-MISS / 无 HARNESS-FAIL / 无 `[还原失败]`），
  原始日志随仓库走 `docs/迭代计划/run-20260927-mutation/round59-lifecycle-mutations.txt`
  （首行 `# run @ 2026-09-28T09:53:09+08:00  git=0853e343e5e6  python=3.12.10`，
  md5 `1188a3454943e0d2a3219c54f54a9811` **与上一批、上上批那两份 `19acd5b822c563495ae444cd9f400dce`
  不同** ⇒ ⑥ 那行运行头生效，"我真重跑过"这句话从此有凭据）。
  `audit_doc_claims.py` 退 **0**（"全部对得上（条数 3 条、数据源 4 行都认得出来自哪个库）；
  另有 16 条'看得见但不判'"）。
  **镜像此刻**（同日 10:1x，只出计划不写库：`python scripts/close_unknowable_predictions.py`）：
  `[计划] 到期未判里可以判定"永远问不出来"的：0 条；仍在等的：29 条`；
  **生产此刻**（同日 10:1x，只读探针 + 日期现算 `D=$(date -u -d '+8 hours' +%F)`）：
  `expired_unjudged 17 / held_by_lock 0 / actionable_today 17` —— 与上一批同数，因为
  **生产仍然没有任何东西在跑**（#132），这 17 条一天天变旧。

（上一批：2026-09-28 06:4x（北京），**任务 #141：第 58 轮复评 74/100 返修——三条 MAJOR 里
最重的一条不是代码错，是我把一条零调用方的死路写成了产品事实；另外两条都是"同一把尺子的第二份
分身"（可达性与"有没有累加"用了两套遍历）；还有一条是我接共用尺子时只取了一半**（条目号 M-*/m-*
落在 #141 的 metadata 里，报告正文不随仓库走）——
① **死路被我说成"页面一直在用"**（M-1，本批最该记的一条）：上一批我在三处（AGENTS、模块总览、
判据注释）都写"`viewpoint_service.delete_viewpoints_by_ids` 是页面「批量删除观点」一直在走的整条批量
UPDATE"。实测驳回：`grep -rn "delete_viewpoints_by_ids" src/ scripts/ web/` ⇒ **只命中定义那一行**
（`viewpoint_service.py:504`）；`/api/viewpoints` 只有单条 `DELETE /{viewpoint_id}`；前端只有
`viewpoint-manager.js` 那一条 `axios.delete('/api/viewpoints/${id}')` ⇒ **零调用方**。
三处措辞已改成"这是死路"，并写明**登记它的理由与调用方无关**（它确实往那一列写值，所以"以后谁把它
接上活路必须先改用那只钟"这句要有抓手）；按本仓规矩（第 49 轮 `_save_fund_mapping`、第 54 轮
`sync_predictions_by_sector_mapping`）**死路不配绿灯判据**，只配登记 + 写明是死路。
⚠ **而我在写这条更正的时候自己又说错两句**（现读代码驳回，比评审更早一步抓到的其实是我自己）：
① 我说"`Viewpoint` 模型压根没有 `restore_before` 这一列" —— **错**，AST 按类数一遍：
`restore_before` 挂在 `Prediction`(`database.py:252`) / **`Viewpoint`(:360)** / `CleanupItemLog`(:888)；
真的那半句是"**页面不读它**"（`grep -c restore_before web/index.html web/*-manager.js` ⇒ 0）。
② 我说"墙钟在观点那一站后果方向安全（UTC 让行显得更年轻 ⇒ 硬删**延后**）" —— **方向反了**：
UTC 容器里 `datetime.now()` 比北京**早** 8 小时 ⇒ 那一行显得**更老** ⇒ 阈值**提前**到 ⇒ 硬删**提前**，
站在危险那一侧。**但"两处消费面"只有三桶那一处**（第 59 轮 m-1 驳回我这句话的第二半）：
上一版并列引的 `retention_cleanup_service.py:447` 比的是 `restore_before >= self.today`
（**窗口检查**，不是"today 减 N 天"），而那个服务的 `self.today` 是 `date.today()`（:157，墙钟）
⇒ 它既不吃北京钟、也压根不读 `Viewpoint.deleted_at`。
⚠⚠ **顺着 ② 写下的那件"更实在的"（旧任务 #142 的立论）在第 59 轮被整条驳回，而且驳回的正是
第 58 轮 M-1 刚罚过的那个错**：那两句都不成立 ——
① "`delete_viewpoint` 只写两列 ⇒ 对**页面删掉的观点**恒不成立"：**页面今天没有"软删观点"这一档**。
`grep -rn "delete_viewpoint\b" src/ scripts/ web/ tests/` ⇒ 服务层那条 `viewpoint_service.py:254`
除定义外**零调用方**（同名命中是路由 `viewpoints.py:463`，两条测试叫的也是路由）；
前端 `viewpoint-manager.js:303` 打的是带 `X-Danger-Confirm: delete-viewpoint` 的
`axios.delete('/api/viewpoints/${id}')`，路由里是 `db.delete(viewpoint)`（:477）＝**硬删、不进回收站**。
② "会把这一对列一起填上的通路是 `cleanup_enhanced.SoftDeleteManager`"：那比死路更死 ——
`grep -rn cleanup_enhanced --include=*.py src/ scripts/ tests/ | grep -i import` ⇒ **全仓零 import**
⇒ 那句药方是"把活路接到死模块上"。
**真正活着的那一站**（现读，第 59 轮 M-2）：观点唯一的活软删是 AI 判拒绝那一处
`viewpoint_workflow_service.py:328`，它写 `is_deleted = True` + `analysis_summary='rejected:…'`，
**两个时间戳一个都不写**；而活的硬删路（`retention_three_buckets._deleted_viewpoint_ids:591`）
要求 `deleted_at.isnot(None)` ⇒ 那些行**永远进不了清理桶**（镜像 2026-09-28 实测：总 489 行 /
软删 418 / 无恢复下界 418 / 无归档时刻 **18** ＝ 全部 `rejected:` 那 18 行。复现
`python scripts/q.py "select count(*) total, count(*) filter (where is_deleted) soft,
count(*) filter (where is_deleted and restore_before is null) no_deadline,
count(*) filter (where is_deleted and deleted_at is null) no_stamp,
count(*) filter (where is_deleted and analysis_summary like 'rejected:%') rejected from viewpoints"`）。
⇒ 任务 #142 已按这个重写（旧的"补 `restore_before` 就能保护"**拦不住物理删除**：真删行的三桶
那一把只看 `deleted_at`，而旧执行器起手就 raise）。
⚠ **第 60 轮 BLOCKER：当时给的那半句理由是假的** —— 我写"根本没人读那一列"，而
`retention_cleanup_service.py:443/447` **就在读它**，并且那条链是活的（`build_plan()` →
`config.py:333 GET /api/config/cleanup/preview` → `web/index.html:2549`）⇒ 补那一列**会**把行
从"候选"挪进 `protected_counts`、那个数在页面上看得见。正确的分裂是"**预览保护得到、真删保护不到**"，
不是"这一列没人读"。详见上面 #143 那一段 ① 的 ⚠⚠ 块（同一处还写着墙钟那把今天就在提前一天关这个窗，
⇒ #145）。
**教训写给下一轮的我**：驳回别人的话之前，先按同一把尺子自己现读一遍代码 —— 这一批两句"更正"
全是没核就写，与第 47 轮那次"凭印象重列评审条目"同族；**而"我更正一下"本身也是一句要被现读的断言**
（第 59 轮 M-1 就是这句更正里又犯的同一个错，连着两版）。
② **"调用点可达"与"参数递没递"是两把尺子**（M-2）：上一批我把 `_releases_live` 的调用点那一腿
改成只走活路径，而 `_passes_the_new_dates` 里判"有没有累加"那一腿还走 `ast.walk` 整棵树 ⇒
一行诱饵就买通整条判据：`d = []` + `def _never(): d.append(1)` + 递 `d`，运行时那个 def 从不被叫、
`d` 永远空，而判据看见"有 append"就点头（探针实测 `releases_live=True`）。现在两半共用**同一份
`live` 列表**（`_releases_live` 里算一次、传进去），样品 ① 判 False、真累加 ② 仍判 True。
③ **"从不被叫"还有三种形状没进剪枝表**（m-1，与第 45 轮同族）：`for _ in ():` 的体、
`_r = lambda: release(...)` 绑在名字上而没人叫、只有递归会叫自己的 `def` —— 探针实测三种全判"已接线"。
现在 `_live_nodes` 认下这三种（`for` 只认"迭代对象是空容器字面量"这一种可证不进入的形状，
`range(0)`/空生成器不猜，与 `_proves_sqlite` 的"不到运行时去猜"同一尺度），
`_dead_inner_defs` 交回 `{'names', 'node_ids'}` 两份并按不动点迭代（剪掉一个死 def 之后，
只有它才会叫的那个也一起死），认调用点时**扣掉它自己体内那些**（只有递归不算有人叫）。
每格都配一条"真被叫到必须仍算接上"的反面样品（λ 被调、`for` 过非空字面量、自递归 def 另有外部调用点、
累加发生在活 def 里）⇒ 修剪的是"不可达"，不是"这一族写法"。
④ **批量写归档列还有两条隐身拼法**（M-3）：`.update(values={列: 值})`（关键字递字典）与
`payload = {列: 值}` 再 `.update(payload)`（字典先交变量）—— 上一批补的那一腿只数**位置参数字典字面量**，
两格实测都回 `[]`。现在走 `_bulk_dicts` 三档来路；变量那一腿只回溯"这个函数里唯一一次字典赋值"，
**行号锚在那一格字典上**（值在哪一行算出来就该在哪一行追责，锚到调用行会把组 payload 的函数
和发 UPDATE 的函数算成两处）。
⑤ **接共用尺子时我只取了一半**（m-2）：裸 SQL 那一腿（`db.execute(text("UPDATE … SET deleted_at = …"))`）
不搓第四把尺子，改问 `scripts/sql_write_policy.py`（"这句 SQL 在写哪一列"从第 48 轮起三处棘轮共用一份）。
`classify_sql` 交回的是 `(授予, 看不清)` 两档，而**日期列的值永远不是 `true`/`1` 那种"看得见即授予"的
字面量 ⇒ 一律落进"看不清"**；第一版只取前一档 ⇒ 整条腿恒空（探针 ⑨ 那格回 `[]` 才点红）。
现在两档都数，并配"只在 `WHERE` 里出现那一列不许数成写"的反面样品。
⚠ **边界要说清**：这一批四条修法（②③④⑤）都是**判据侧**的洞，能被它们各自的新样品钉死，
但**不是** `src/` 里的行为 —— 所以变异体检**没有新增条目**（30 处与上一批同数），
"新腿有没有牙"由那几格控制样品负责，别拿"变异全 RED"当这一批的证据。
⑥ 两处卫生（m-3）：`TODAY` 从 `date.today()` 换成北京那把钟 `lc.current_as_of()`（生产容器在 UTC ⇒
"今天"与队列/分类差一天，正是第 52 轮 B-3 那一族换到了测试自己身上），并删掉一个重复的
`from pathlib import Path`。
最后一次核对（**串行**、默认 locale cp936、子进程显式 `PYTHONIOENCODING=utf-8`、跑期间不起第二个会话；
⚠ 本批我又犯了一次同一族的错：先台起了 `nohup … &` 的体检，随后又用后台任务起了**第二份** ⇒
锁把它当场拒了（退 0 但只印一行 `[abort]`，那行话来自前端那把锁），两份都没污染 `src/`——
**并发这一条由锁守着，不是由我记得住守着**）：

- `pytest tests/unit -q` → **1229 passed / 16 skipped / 0 failed**（350.06 秒）。
- `pytest tests/ -q` → **1238 passed / 16 skipped / 0 failed**（340.34 秒）。
  （与上一批**同数** —— 这一批四组新样品全部并进已有用例的样品表，`def test_` 条数一条没增：
  改动面用 `git diff --stat HEAD -- tests/` 现看，只有归档那把与解锁那把两份判据文件。
  当场条数一律 `python -m pytest tests/unit/test_one_ruler_per_question.py
  tests/unit/test_structurally_unverifiable_hold.py -q` ⇒ 今天印 **48 passed**。）
  变异：`python scripts/mutation_proof_lifecycle.py` **30 处全 RED**（M1~M28 含 M3b/M5b；
  CONTROL-GREEN = 7 个判据文件在干净代码上全绿；条数一律 `--list` 看末行），原始日志随仓库走
  `docs/迭代计划/run-20260927-mutation/round58-lifecycle-mutations.txt`。
  `audit_doc_claims.py` 退 **0**（"全部对得上（条数 3 条、数据源 4 行…）；另有 16 条看得见但不判"）。
  判据文件当场：`python -m pytest tests/unit/test_structurally_unverifiable_hold.py
  tests/unit/test_one_ruler_per_question.py -q` ⇒ **48 passed**。

（上一批：2026-09-28 05:1x（北京），**任务 #139：第 57 轮复评 73/100 返修——失分集中在
"我上一批刚写下的那句『判可达』"，而它四种形状里三种当场复现；另有一把棘轮补了三种拼法之后
立刻量出一条隐身多年的活路**（条目号 M-*/m-* 落在 #139 的 metadata 里，报告正文不随仓库走）——
① **"接线判据"的可达性一跳即瞎**（M-1，本批最重）：上一批那句"改成判可达，不再用 `ast.walk`"
只算了**一层** —— 探针实测四种形状：①解锁搬进 `def _release()` 而唯一调用点压在 `if 1 == 0:` 里
⇒ 判"接了"；②同样搬进内层 def、调用点只写在 `except Exception:` 里 ⇒ 也判"接了"；
④`d = []` 先赋空列表再递进去 ⇒ 也判"接了"（参数那腿只认字面量）；
只有③（两层内层 def、最外层从不被叫）**已被抓到 ⇒ 那条不改**。
运行时那种代码**一把锁都不解**，而 `pytest tests/unit -q` 在注入①之后 1228 passed / 0 failed
（两条相关判据双双绿）。修法三处：`_live_nodes`（恒假 `If` 主体 / 恒假三目那一臂 / `while 恒假` 循环体 /
`except` 支 / 死内层 `def` 整棵，全部按同一套规则剪）+ `_dead_inner_defs`（**迭代到不动点**，
剪掉一个死 def 之后只有它才会叫的那个 def 也一起死）+ 参数那腿补**一跳回溯**
（`d = []` 且此后没有任何 `.append/.add/...` ⇒ 判"没递"；有累加才是正常的 `inserted = []` 形状，
这一句是防过宽 —— 少了它每条真同步都会被判成没接）。共用那把 `_is_dead_test` 补 `Or` 全臂恒假
（`ast.literal_eval` 在 3.12 上对 BoolOp/Compare 直接 `ValueError: malformed node`，上一版只补了 `And`），
守卫侧三条样品钉住（`if False or False:` / `if () or 0:` 恒假、`if x or True:` 恒活）。
② **`FundSyncManager._update_fund_history` 那条腿零行为判据**（M-1 的另一半）：
三条行为判据全都直接调 `fund_api.update_fund_history`，第二条腿只有 AST 作保 ⇒ 新增端到端
`::test_the_fund_sync_writer_leg_also_unlocks`（真造 `FundHistory` 行 + 真叫那条腿 + 核锁真的解开），
变异 **M28**（解锁的日期先经一个空容器变量再递进去）RED。**边界**：①那种"死内层 def"形状
目前由判据里的规避样品钉住，体检里没有对应的真代码变异 —— M26 钉的是同一条腿上的恒假比较。
③ **"量不到槽位"那一格以前是猜**（M-2）：`days` 的槽位从被扫的树里推，
扫描集合里没有那个 `def`（咽喉挪进第三方包、或写在另一个还没被扫的文件里）时兜底写死 `1 if is_bound else 2`
⇒ `update_fund_history(code, 30)`（裸函数）整个漏掉。现在量不到槽位就**把该调用里任何整数位置实参都点名**，
话里明说"量不到 days 的槽位"，不再安静地按猜的数走。
④ **一把棘轮补上三种拼法，当场多量出一条真活路**（m-4）：`_archive_writes` 以前只认属性赋值 / 解包 /
setattr / 关键字，实测 `row.deleted_at = archive_stamp()[0]` 与"钟先交给变量再取下标"都判成
"出自别处"（**诚实写法被拦 ⇒ 闸过宽的结局就是被整条关掉**）、`.update({列: 值})` 一格都不数、
`sorted(set(...))` 把同一行两处相同写并成一条。补完下标这一腿、补完批量写（收件人那条链上必须有
查询动词，否则 `get_detail` 里 `detail.update({...})` 那种**返回给前端的字典**会被数成写 —— 第一版就是这么过宽的，
被新控制当场点红），去重键换成 `(行, 列偏移, 列名, 来路)` ⇒ **批量这一腿立刻量出
`viewpoint_service.delete_viewpoints_by_ids`**。⚠ **上一版把这一条写成"页面「批量删除观点」一直走整条批量
UPDATE"是假话**（第 58 轮 M-1）：`grep -rn "delete_viewpoints_by_ids" src/ scripts/ web/` ⇒ **只命中定义那一行**、
`/api/viewpoints` 只有单条 `DELETE /{viewpoint_id}`、前端只有一条 `axios.delete('/api/viewpoints/${id}')`
⇒ **零调用方的死路**。登记它的理由与调用方无关——它确实往那一列写值，所以"谁以后把它接上活路必须先改用那只钟"
这句要有抓手；按本仓规矩（第 49 轮 `_save_fund_mapping`、第 54 轮 `sync_predictions_by_sector_mapping`）
**死路不配绿灯判据，只配登记 + 写明它是死路**。它按"别的模型"登记（观点的 `deleted_at` 只当硬删年龄锚点，
页面上没有一句观点的"保留到 X 日"：`grep -c restore_before web/index.html web/*-manager.js` ⇒ 0）。
第 58 轮 M-3 / m-2 又给这一腿补上批量关键字递字典（`.update(values={列: 值})`）、
字典先交变量再整份递进去（`payload = {列: 值}` 再 `.update(payload)`，一跳回溯；行号锚在**那一格字典**上，
因为值在哪一行算出来就该在哪一行追责）、以及**裸 SQL 那一整族**（`db.execute(text("UPDATE … SET deleted_at = …"))`）——
最后这一条不搓第二把尺子，直接问 `scripts/sql_write_policy.py`（"这句 SQL 在写哪一列"从第 48 轮起有共用那把，
归档这一把当时没接上，等于第四份没写）。⚠ 接它时我先只取了 `classify_sql` 的 `granted` 那一半 ⇒
**日期列一律落在 `unclear`**（值永远不是 `true`/`1` 那种"看得见即授予"的字面量），整条腿恒空；
探针 ⑨ 那格回 `[]` 才点红，现在两档都数。裸 SQL 归档列**今天 0 处**（这一腿不是空检查面：
`src/` + `scripts/` 里 `execute`/`exec_driver_sql` 调用共 **101** 处，逐处问共用那把尺子，
命中归档列的 0 处 —— 复核 `python -c` 走 `_functions('src')` / `_functions('scripts')` 那张表），
所以登记名单没动 —— 但从现在起加一处就得登记并写明依据（`other` 那一档）。
⚠ **这句话第 59 轮 M-2 只补上了一半**："加一处就得登记"目前成立的范围是**这把尺子看得见的拼法**。
`Model(**payload)` 这一族（键落在列名上、整包摊进构造函数）现在数得到，**但泛型建行数不到**：
`data_portability_service.py:231` 的 `spec.model(**cleaned)` 里列名是**运行期按元数据拼出来的**，
静态看不见 ⇒ 这把尺子对它结构性失明（同一条第 56 轮 m-3 已为 **NAV 那把**补过腿：写净值的证据
分"点名构造 / `TABLE_SPECS` 泛型建行"两腿，归档这把没有第二腿）。⇒ 那一族的闸不在这里，
在 `_clean_row` 剔列 + 行为判据（见下面第 59 轮那条与任务 #144）。
⑤ **判据自己的红路径会崩**（同一处顺手抓到的）：那条"写站集合与名单不一致"的**解释语句**写成
`registered - set(found)`（dict 减 set）⇒ 一有新站点就抛 `TypeError` 而不是说出"新增了哪一站"；
补完批量那一腿的第一次跑就是它，报错长得像"工具坏了"。已改 `set(registered) - set(found)`。
⑥ **两处数与指针不对表**（m-1 / m-3）：`NAV_WRITE_SITES` 是 **5** 条（父提交 4 条），
上一批三处（AGENTS、docs §2e、提交信息）都写成"从 2 条扩到 4 条"——那个 2 是**接了解锁**的条数、
不是登记面；现在三处都改成绑命令的写法（复核印 `[5]`）。`src/services/prediction_lifecycle.py` 里
`archive_stamp()` 的 docstring 还指着"`test_prediction_migrations.py` 里那把按 (文件, 函数) 数站点的棘轮"
—— 棘轮在 `test_one_ruler_per_question.py` 而且上一批已改成按**语句条数** ⇒ 那句把已被驳回的旧形状教给下一轮。
⑦ **体检工具自己会在 cp936 控制台上崩**（一条运行账）：它每行回执都带 `⇒`，而输出重定向到文件时
python 仍按 locale 编码 ⇒ 实测跑到 CONTROL 那行就 `UnicodeEncodeError`、退出码非 0，看起来像"体检失效"。
现在 import 时对 stdout/stderr 各 `reconfigure(encoding='utf-8', errors='replace')`（已被换成别的对象就跳过）。
最后一次核对（**串行**、默认 locale cp936、子进程显式 `PYTHONIOENCODING=utf-8`、跑期间不起第二个会话）：

- `pytest tests/unit -q` → **1229 passed / 16 skipped / 0 failed**（316.65 秒）。
- `pytest tests/ -q` → **1238 passed / 16 skipped / 0 failed**（374.98 秒）。
  （上一基线 1228/1237 → 本批 **+1 条 / 两个口径同增**：`test_structurally_unverifiable_hold.py`
  31→**32** 个 `def`（② 那条端到端，当场账 `--collect-only -q` 印 **38**）。
  **改契约不增条数**：`test_one_ruler_per_question.py`（④⑤，控制样品从 3 格扩到 9 格，当场账 **10**）、
  `test_script_db_guards.py`（① 的 `Or` 那一臂 + 守卫侧新加的三格样品，当场账 **36**）。
  变异：`python scripts/mutation_proof_lifecycle.py` **30 处全 RED**（M1~M28，含 M3b/M5b；
  `--list` 末行印"共 30 处变异，覆盖 7 个判据文件"），CONTROL 那 7 个判据文件在干净代码上全绿、
  无 ANCHOR-MISS / 无 HARNESS-FAIL；原始日志随仓库走
  `docs/迭代计划/run-20260927-mutation/round57-lifecycle-mutations.txt`，跑完逐文件字节回读一致。
  **镜像此刻**（同日 05:0x，只出计划不写库：`python scripts/close_unknowable_predictions.py`）：
  `[计划] 到期未判里可以判定"永远问不出来"的：0 条；仍在等的：29 条`；
  **生产此刻**（同日 05:0x，只读门 + 引擎级只读探针）：`expired_unjudged 17 / held_by_lock 0 / actionable_today 17`
  —— 与 09-28 凌晨那次同数，因为**生产仍然没有任何东西在跑**（#132），这 17 条一天天变旧。）
（上一批：2026-09-28 04:2x（北京），**任务 #137：第 56 轮复评 72/100 返修——四条 MAJOR
全都"上一批只修了一半"，其中一条是我说满了话；另有一条评审给的尺子被我在生产上证伪**
（条目号 M-*/m-* 落在 #137 的 metadata 里，报告正文不随仓库走）——
① **那把"不许把回补天数写死"的尺子只认 `days=` 关键字**（M-1）：
`update_fund_history(code, 30)` 这种**按位置**传天数完全隐身（探针样品实测扫描器回 `[]`）。
现在 `_literal_backfill_windows` 先从被扫的树里量出 `days` 落在第几个槽（绑定方法扣掉 `self`），
再看那个槽是不是整数字面量；三条样品：位置写死必抓、`(code, db=None)` 与"位置传变量"不误伤，
变异 **M24** 就是把 `fund_auto_manager.py:287` 改成位置传 30（RED）。
② **"接了解锁"以前不看参数**（M-2）：`release_holds_after_nav_commit(db, code, [], ...)`
——把"新落的那几天"掏空，＝第一道闸当场失效——仍判 `True`（实测 85 passed 一声不响）。
现在第三个实参不递、或递空容器/`None` 都判"没接"；并补一条**穿过同步那条路**的端到端
`::test_the_daily_sync_writer_is_the_thing_that_unlocks`（真 sqlite：源端给窗口内两行 ⇒ 锁解开并
回到「待验证到期」；只给窗口外那行 ⇒ 净值照入库、锁一个字不动）。以前三条行为判据全是直接调
解除函数，没有人从 `update_fund_history` 打进来。变异 **M25** 就是那个 `[]`。
③ **那份 `_never_runs` 是"死路不算守卫"的第二份、更弱实现**（M-3，与第 45 轮同族）：
`literal_eval` 根本不算比较，于是 `while False:`、`if ins and 1 == 0:`、恒假三目、
藏在从不被调的内层 `def`、只写在 `except` 里 **五种**全被判成"已接线"（探针实测五种全 `True`）。
现在共用守卫侧那把 `_is_dead_test`（常量折叠 + 自己算常量比较，第 46 轮就建好了），
八种规避写法各一条样品 + 一条"内层 def 被真调用必须算接上"的反面对照；变异 **M26** 用
`if added and 1 == 0:` 那一格（旧尺子对它是瞎的）。
④ **我说满了一句：归档那把棘轮其实没改成按条数核**（M-4，本批最该记的一条）：
上面 ④ 那段（#134 那批）写着"归档时间戳那把现在登记条数"——**实测是假的**：
按条数核的是 `status` 那把，归档那把仍按 `(文件, 函数)` 收集合。探针往已登记的
`_soft_archive` 里插一行 `prediction.deleted_at = datetime.now()` ⇒ 写站集合不变、
函数里那次 `archive_stamp()` 调用也还在 ⇒ 两条断言都不红。现在它登记
`{(文件, 函数): 写那一列的语句条数}`，并要求回收站那三条活路**每一处**写的值都出自那只钟
（来路三档 `stamp`/`none`/`other`，出现 `other` 即红）；三条控制（两处写算 2、
"钟交给变量再逐列赋"不误判、现插一处正好 +1 且判成 `other`）+ 变异 **M27**。
⚠ "三条活路"这措辞第 59 轮 m-3 收回：三站里两站活着，`cleanup_enhanced` 整模块零 import
⇒ 第三格的意思改成"以后接回去不许换墙钟"，不是说页面在走它。
⑤ **"两条活路"是半句**（m-3）：合并式整库导入 `data_portability_service.import_data` 也往
`fund_history` 灌行（`TABLE_SPECS` + `spec.model(**row)`，一个字没写 `FundHistory`）⇒
那把扫描器对它结构性失明。现在写净值的证据分两腿（点名构造 / `TABLE_SPECS` 泛型建行），
**要登记的写净值站点从 4 条扩到 5 条**（复核 `python -c "import io,ast; t=ast.parse(io.open('tests/unit/test_structurally_unverifiable_hold.py',encoding='utf-8').read()); print([len(n.value.keys) for n in ast.walk(t) if isinstance(n,ast.Assign) and getattr(n.targets[0],'id','')=='NAV_WRITE_SITES'])"` ⇒ `[5]`；
第 57 轮 m-1：上一批这里写的"从 2 条扩到 4 条"是**把两个口径拼成了一句** —— 登记面 4→5，
而**接了解锁**的那两条从头到尾都是 2 条），新登记的那条**登记为"故意不接"并写依据**（换库不是"补了几行"，那里没有
"新落的那几天"这个量可递）；那条判据的名字也跟着从 `..._both_sync_writers` 改成
`..._every_nav_writer`（三处引用一起改，旧名留着就是句假承诺）。
⑥ **两处"判据自己不合格"**：a) `_never_runs` 之外，`test_the_nav_lookback_has_one_home_for_both_questions`
当时**正向**钉着"`get_fund_history` 的签名默认值要含那个键" —— 而签名默认值在 import 那一刻就算死，
"只有一个出处"因此有第二处（m-5）；现在它反过来判"不许是导入时默认值"，扫描范围从
`prediction_lifecycle.py` 一个文件扩到**整个 `src/`**。b) `test_prediction_migrations` 用
`env.setdefault('PYTHONIOENCODING', 'utf-8')` ⇒ 父进程带**空串**进来时原样放过 ⇒ 子进程按 cp936 写、
父进程按 utf-8 解 ⇒ `[库]` 那行成替换符、用例稳定假红（评审为此白跑一次全量；m-4）。
现在无条件赋值，新用例 `::test_the_child_always_writes_utf8_no_matter_what_the_parent_carries`
把"缺失 / 空串 / 全空白 / gbk / utf-8"五格各钉一次。
⑦ **评审给的那把"跨基金广度"尺子，我在生产上证伪了**（#135 的处置因此改写）：
只读实测 `python scripts/q.py --production "select nav_date, count(distinct fund_code) n from fund_history where nav_date >= '2026-09-18' group by nav_date order by nav_date"`
⇒ 工作日 09-18/09-21/09-22/09-23/09-24 各 **152~158** 只、周末 09-19/09-20/09-26/09-27 各 **1** 只，
而 **2026-09-25 是个周五、也只有 1 只**（货币基金 `000725`）⇒ 绝对只数 / 当日占比 /
"目标日前 7 天最大广度"三种定法**都会把一个"全库没补到"的交易日判成休市并永久关单**。
镜像这边末条 2026-09-24、**当天只有 7 只**（全库 245 只）。正解是改用**星期**：
`1669`/`1709` 的目标日**都是 2026-07-11**（复核 `python scripts/q.py "select id, target_date, strftime('%w', target_date) dow from predictions where id in (1669,1709)"`
⇒ 两行都是 `2026-07-11 / 6`＝**周六**；上一批这里写的"07-11/07-12，Sat/Sun"是我照着净值日期抄错的，
第 57 轮 m-2 点出来后才拿库里的行改回），`python -c "from datetime import date;print(date(2026,7,11).weekday())"` ⇒ 5＝周六，
当场判得出来、不需要新表；**法定节假日那一档仍然没有凭据**，所以话只能说成"周末规则"，不许说成"非交易日规则"。
⑧ **M-5 不成立**（写在这儿防下一轮照评审原文再"修"一遍）：它说按条数核的尺子对推导式失明
（`[p for p in ps if p.status == "pending"]` 计 0）。探针实测 `_status_judgments` 回 `[2]`，
与 lambda 同待遇 ⇒ 那条不改，已在报告与任务里写明。

最后一次核对（**串行**、默认 locale cp936、子进程显式 `PYTHONIOENCODING=utf-8`、跑期间不起第二个会话；
⚠ 本批中途我自己起了两条 `pytest tests/unit` 会话 —— 第二条起手就印了
`[警告] 已经有一个 pytest 会话握着 .pytest-session.lock`，那行警告正是替这件事盯梢的；
两条都用 `Get-CimInstance … CommandLine -like '*pytest*'` 找 PID 收干净后**重跑**，下面两个数是重跑后的）：

- `pytest tests/unit -q` → **1228 passed / 16 skipped / 0 failed**（302.70 秒）。
- `pytest tests/ -q` → **1237 passed / 16 skipped / 0 failed**（308.02 秒）。
  （上一基线 1226/1235 → 本批 **+2 条 / 两个口径同增**，分布用
  `for f in $(git diff --name-only HEAD -- tests/); do echo "$f $(git show HEAD:$f | grep -c '^def test_') -> $(grep -c '^def test_' $f)"; done`
  数出：`test_structurally_unverifiable_hold.py` 30→**31**（②那条端到端）、
  `test_prediction_migrations.py` 7→**8**（⑥b 那五格）。**改契约不增条数**：
  `test_one_ruler_per_question.py`（④ 改成按语句条数 + 来路三档，控制断言并进去）、
  `test_script_db_guards.py`（③ 那把 `_is_dead_test` 现在被两个测试文件共用）。
  变异：`python scripts/mutation_proof_lifecycle.py` **29 处全 RED**（M1~M27，含 M3b/M5b；
  CONTROL-GREEN 6→7 个判据文件；条数一律 `--list` 看末行），原始日志随仓库走
  `docs/迭代计划/run-20260927-mutation/round56-lifecycle-mutations.txt`。
  ⚠ 第一次整份跑它**输出 0 字节**：本机内存 load 88~91%、可用 0.60 GB ⇒ 子进程被系统打死
  （第 55 轮记过同一签名）。判据是"退码 + 输出字节数"，不是"命令跑完了"；重跑要带 `-u`。
  `audit_doc_claims.py` 退 **0**。
  **镜像此刻**（同日 03:3x，只出计划不写库：`python scripts/close_unknowable_predictions.py`）：
  `[计划] 到期未判里可以判定"永远问不出来"的：0 条；仍在等的：29 条` ——
  这 29 条是**两档**：22 行"源端这段给了 1~64 条 ⇒ 本地没补到"（其中 5 行段内只 1 笔）
  + 7 行"重问锁 2026-09-30 还没到点"，加起来才是 29（上一批把三个数并列写成一句，读起来像 34 ⇒ m-1）。
  **生产此刻**（同日 03:5x，只读探针，命令里那个日期必须现算，别抄 `date '2026-09-27'` ⇒ m-2）：
  `D=$(date -u -d '+8 hours' +%F); python scripts/q.py --production "with h as (select id, (next_verify_date is not null and next_verify_date > target_date) as locked from predictions where is_deleted=false and target_date is not null and is_correct is null and prediction_type<>'flat' and target_date <= date '$D') select count(*) as expired_unjudged, count(*) filter (where locked) as held_by_lock, count(*) filter (where not locked) as actionable_today from h"`
  ⇒ `expired_unjudged 17 / held_by_lock 0 / actionable_today 17`（净值已到 2026-09-25、198 只 11061 行）
  —— 这 17 条不是新坏的，是目标日一天天过去而**生产没人跑验证**（#132）⇒ #133 第一件事就是 update-all + verify-all。）

（上一批：2026-09-28 01:5x（北京），**任务 #134：第 55 轮返修——复评 73/100 的四条 MAJOR
全部复现并修完，另加两处"我自己新写的判据"被它们各自逮回来，以及追一条偶发红追出来的生产级真相**
（条目号 A-*/B-* 落在 #134 的 metadata 里，报告正文不随仓库走）——
① **"重问节奏跟着每日回补范围走"以前只是看着成立**（#134 M-1）：那个键只喂到
   `FundAPI.get_fund_history` 的**签名默认值**，而 `update_fund_history` 一路有 8 处把 30 写死
   （复核 `git grep -n "update_fund_history" 4ef48ce -- src/ | grep days`）⇒ 改键只动间隔、不动取数。
   现在取数天数只有 `prediction_lifecycle.nav_backfill_days()` 一处在**调用时**读那个键
   （签名默认值在导入时就算死了，这条也是判据之一）；两条新判据：
   `::test_the_sync_lookback_is_not_hard_coded_at_any_call_site`（按调用点扫字面量窗口，
   配"良性 `days=1` 不许误伤"的过宽对照）与
   `::test_the_backfill_window_is_read_at_call_time_not_at_import`（把键改成 47，真走一遍
   `update_fund_history`，交给源端的那个数字必须跟着变）。
   ⚠ **当时只扫了 `days=` 关键字**：`update_fund_history(code, 30)` 这种按**位置**传天数
   完全隐身（第 56 轮 M-1 注入它 ⇒ 1226 条一声不响），而"只有一个出处"那句还漏了
   `get_fund_history` 的签名默认值自己 —— 下面新批次（#137）两条都补上了。
② **"补到净值就该解开的锁"从来没有人解**（#134 M-2）：被锁的行不在到期队列里 ⇒ 验证器再也不会
   问它一次，第二天就补到的净值也要白等一整个间隔。新增 `release_holds_after_nav_update` /
   `..._commit(...)`，两条往 `fund_history` 插行的活路都接上；解锁只报"**新落**的那几天"
   （否则每天同步在别处补一行 ⇒ 撤锁 ⇒ 再锁的循环），且判"够不够"仍问 `target_cannot_evidence_window`
   那把尺子。`backfill_history_range` **故意不接**并写明依据（它跑在 `verify_prediction` 内部，
   同一次验证两行之后才读 `previous_hold`，在那里撤锁等于自己清掉"问过两次"的证据）；
   `fund_service.add_history` 是零调用方的死路 ⇒ 按规矩不给它写绿灯，但登记在名单里要求理由。
   ⚠ **"两条活路"是半句**：合并式整库导入（`data_portability_service.import_data`）也往
   `fund_history` 灌行，而它一个字都没写 `FundHistory` ⇒ 那把扫描器对它结构性失明；
   并且"接了锁"只看调用存不存在、**不看第三个实参**（第 56 轮 M-2：换成 `[]` 也绿）。两处都在 #137 修。
③ **一处"接线"判据被自己的变异打回 GREEN**（#134 M-3 的现场，与第 45 轮"死路不算守卫"同族、
   只是换到了写侧）：`_nav_writers` 用 `ast.walk` 找那个函数名 ⇒ 把 `if inserted:` 改成
   `if False:`（M19）全套绿灯一声不响。现在判**可达**（`_never_runs` 认恒假条件，含 `and False`
   那一族），并补一条"死分支里的调用不算接线"的控制断言。
   ⚠ 那份 `_never_runs` 自己是**第二份、更弱**的恒假识别（`literal_eval` 根本不算比较）：
   `while False:`、`if ins and 1 == 0:`、恒假三目、藏在从不被调的内层 `def`、只写在 `except` 里
   五种全被判成"已接线"（第 56 轮 M-3）。#137 改成共用守卫侧那把 `_is_dead_test`，八种规避各一条样品。
④ **棘轮按"出现过没有"数，就会按"出现过"被骗**（#134 M-4）：`test_one_ruler_per_question.py`
   的归档时间戳那把尺子以前只登记 `(文件, 函数)` ⇒ 同一函数里再造一处 `datetime.now()` 它看不见。
   ⚠ **这一条当时说满了，第 56 轮 M-4 实测驳回**：上一批真正改成按**条数**核的是
   `status` 那把（"有没有结论"），**归档那把仍然按 `(文件, 函数)` 收集合** ——
   探针往已登记的 `_soft_archive` 里插一行 `prediction.deleted_at = datetime.now()`，
   写站集合一个字不变、函数里那次 `archive_stamp()` 调用也还在 ⇒ 两条断言都不红。
   **这一批才把它改对**：登记 `{(文件, 函数): 写那一列的语句条数}`，并且回收站那三处写要求
   **每一处**写的值都出自那只钟（来路三档 `stamp`/`none`/`other`，出现 `other` 即红）；
   三条控制（两处写算 2、正确写法与"先交给变量再逐列赋"不许误判、现插一处必须正好 +1 且判成
   `other`），变异 **M27** 就是那处现插。
⑤ **追一条偶发红追出来的生产级真相（本批最贵的一条，它不在任何评审清单里）**：
   `test_current_as_of_fallback_leaves_a_trail` 在全量跑批里红、单跑永远绿。用一次性探针插件
   （拦 `dictConfig`/`fileConfig` 与 `Logger.disabled` 的赋值）量到真因：`alembic/env.py` 照抄官方
   模板那句 `fileConfig(config.config_file_name)`，而这个调用的 `disable_existing_loggers`
   **默认是 True** ⇒ 进程内跑一次迁移，就把当时已建好的 **24 个 `src.*` logger 永久静音**
   （`Logger.handle()` 第一句就 return）。已显式传 `disable_existing_loggers=False`，新用例
   `test_prediction_migrations.py::test_running_a_migration_in_process_does_not_silence_the_application_loggers`
   问的是**结果**（跑完之后哪些 logger 哑了）而不是"env.py 里有没有那个参数"，另配 M22/M23 两条变异。
   **两条推论**：a) 取证别借道别人的管道 —— 这条现在把探针 handler 直接挂在那个 logger 上，
   并配"正常取到时不许留痕"的对照（**我第一版的对照塞了个假 tzinfo，它其实走的是回退那一支 ⇒
   对照当场是假的**，已换成真 `tzinfo`）；b) 生产上"跑一次迁移的那个进程"从此日志不再是哑的。
⑥ **体检子进程的 env 判据也只认字面量**（本批基线那三条红的根因）：`_child_env()` 一收，
   `'env=env' in 源码` 就把一份**真的在传放行标记**的工具判成"会把自己拦死"，三条端到端用例连带红。
   现在按 AST 数"起了几次子 pytest、其中几次递出去的 env 真带着 `ENV_PID`"，五格样品：
   直白写法 / helper 交出去 / helper 里摘掉标记那行 / 递一个不含标记的字典 / 干脆不传 env
   —— 前两格必须算接上、后三格必须不算。
⑦ **守卫的文本证据：`via_cli` 是两个互不相干的文件级条件**（第 54 轮那条教训的第三个方向）：
   "文件里出现过 `alembic` 字样" + "这个文件调用过 run/system 之一" ⇒ 体检工具把另一支脚本的锚点
   原文当**变异载荷**抄进名单，就被判成"会借道 alembic 改表结构"（本批基线第 4 条红）。
   现在只认真正**递进某次起进程调用**的字符串（保留一跳：命令先装进变量、变量再递进去仍算），
   正反两条样品 `_x_alembic_via_variable` / `_x_alembic_mentioned_only` 进
   `test_write_capability_is_judged_by_what_a_script_can_do_not_by_its_names`。
最后一次核对（**串行**、默认 locale cp936、子进程显式 `PYTHONIOENCODING=utf-8`、跑期间不起第二个会话）：

- `pytest tests/unit -q` → **1226 passed / 16 skipped / 0 failed**（368.21 秒）。
- `pytest tests/ -q` → **1235 passed / 16 skipped / 0 failed**（365.58 秒）。
  （上一基线 1218/1227 → 本批 **+8 条 / 两个口径同增**，分布用
  `for f in $(git diff --name-only HEAD -- tests/); do echo "$f $(git show HEAD:$f | grep -c '^def test_') -> $(grep -c '^def test_' $f)"; done`
  数出：`test_structurally_unverifiable_hold.py` 24→**30**（+6，①②③，当场账 `--collect-only -q` 印 **36**）、
  `test_prediction_migrations.py` 6→**7**（⑤ 那条）、`test_stats_evidence_report.py` 23→**24**（⑤ 的对照）。
  **改契约不增条数**：`test_one_ruler_per_question.py`（④ 按条数核）、`test_mutation_lock.py`（⑥ 按 AST 核）、
  `test_script_db_guards.py`（⑦ 那一跳 + 两条 argv 比较样品）、`test_close_unknowable_predictions.py`
  （备份目录改指 `tmp_path`，并断言仓库 `backup/` 一个字节都不许多 —— 这条是上一轮那份
  CONTROL-RED 的真因：用例往仓库级 `backup/` 落了 51 份只带秒数的夹具残渣，已清）。
  变异：`python scripts/mutation_proof_lifecycle.py` **25 处全 RED**（M1~M23，含 M3b/M5b；条数一律
  `--list` 看末行），且开头多一条 **CONTROL**：这一轮涉及的 **6 个判据文件**在干净代码上必须先全绿，
  红则整轮判 4 ⇒ "改完代码才红"与"判据本来就是红的"从此分得开。原始日志随仓库走
  `docs/迭代计划/run-20260927-mutation/round55-lifecycle-mutations.txt`。
  **镜像此刻**（同日 01:5x，只出计划不写库：`python scripts/close_unknowable_predictions.py`）：
  `[计划] 到期未判里可以判定"永远问不出来"的：0 条；仍在等的：29 条` —— 与 09-27 那次的 12 条
  不是同一批形状，因为镜像净值停在 **2026-09-24**（`select max(nav_date), count(*) from fund_history`
  ⇒ `2026-09-24 / 17626 行 / 245 只`）：22 行的回执是"源端这段给了 1~64 条 ⇒ 是本地没补到，不是它没有"
  （这正是 ② 那条新路的存在理由 —— 跑一次「更新基金」当场解锁，而不是白等 31 天），
  7 行等 09-30 那一把锁到点。**生产此刻**（同日 01:5x，只读门 + 引擎级只读探针）：
  `expired_unjudged 17 / held_by_lock 0 / actionable_today 17` —— 昨天同一把尺子是 `0 / 0 / 0`，
  这 17 条不是新坏的，是**目标日一天天过去而生产没人跑验证**（#132：生产没有任何自动化在跑）
  ⇒ 上线后 #133 的 runbook 第一件事就是 update-all + verify-all 把这 17 条判出来。）
（上一批：2026-09-27 15:4x（北京），**任务 #119~#128：第 54 轮返修——
"修一半 + 说满一半"那一族、结构性不可验的分档重问节奏、收口脚本的话与备份的方向**——
两份复评报告的分数**没随仓库走**（正文只在会话里，条目号 A-*/B-* 已逐条落进 #119~#128），
下一轮要引用请以任务条目为准）——
① **「这一行有没有结论」全仓只许一处回答**（#120 / A-2 + B-3）：`_conclusion_conditions` 上一批只改了
   `PredictionQueryService` 自己，而 `src/` 里还有别处在拿遗留列 `predictions.status` 当答案 ——
   其中 `cleanup_tasks.py` 的**三处会删行的过滤器**（两条"只清已过期的验证过的预测"、一条"没结论就留着净值"）
   现在改问 `Prediction.is_correct`；`fund_service.get_with_predictions` 与 `PredictionService` 里四条
   按 `status` 取预测的方法（`get_active` / `get_pending_verification` / `get_expired` /
   `get_predictions_with_filters`）**`src/` 与 `scripts/` 零调用方，整条删掉**，
   留下的登记名单由新文件 `tests/unit/test_one_ruler_per_question.py` 用 AST 逐处核
   （当场账 `--collect-only -q` 印 **10**；名单里只有一处是**判断**，其余是写侧同步、回显与取列）。
   第三档（`success` / `failed` 这种按字面值筛）仍按 `status` 列查，那是另一件事、不归这把尺子管。
   **为什么删死路而不是给它写绿灯**：留着就是留着一个随时会被接上的错误口径（第 49 轮
   `_save_fund_mapping` 那一课：绿灯替死路作保）。
② **归档时间戳与恢复下界也只有一个来源**（#119 / A-1 + B-2）：上一批只把 `_soft_archive` 换成北京钟，
   而页面「合并相似预测」那条活路还在自己 `date.today() + 30`、`cleanup_enhanced.SoftDeleteManager`
   那条腿还在 `datetime.now()` ⇒ 同一座回收站里两种"保留到 X 日"（Render 容器在 UTC，北京
   00:00~08:00 归档的行少一天）。现在三处都走 `prediction_lifecycle.archive_stamp()`，
   判据按 `(文件, 函数)` 登记写侧站点，新造一处不登记就红。
③ **结构性不可验按档各等自己的钟**（#121 / A-5）：`same_nav_endpoint`（退化端点）那一档原来与
   `no_source_history` 共用凭据 TTL（三天）⇒ 到点弹回「待验证到期」、当场又只锁不关，永远没有终局。
   现在 `unverifiable_retry_days(verdict_reason)` 分两档：端点退化那档等 `config.NAV_HISTORY_LOOKBACK_DAYS + 1`
   （**每日回补范围之外，常规同步就再也补不进这段** ⇒ 这个数是"重问一次会不会换个答案"的唯一出处），
   `no_source_history` 仍按凭据 TTL。**为什么端点档要等回补范围**：镜像今天挂在 `1669/1709`（目标日是周六）
   与 `3099/3126/3178`（窗口跨过 `158038` 首笔净值 2026-09-07）这 5 行，收口脚本 dry-run 对它们说的是
   "源端这段给了 1 条 ⇒ 是本地没补到，不是它没有" —— 那是**还能等**的形状，不是永久没有，
   所以既不能关、也不该三天问一次（复现：`python scripts/close_unknowable_predictions.py`，
   2026-09-27 15:5x 印 `0 条可关 / 12 条仍在等`，其中 **7 行**是"重问锁 2026-09-30 还没到点"、**5 行**是上面那句）。
   这不是终局处置（三条解锁路径与"为什么不直接关"写在 `docs/模块总览/预测验证与准确率统计.md` §2e：
   第 52 轮 A-1 驳掉的"休市日 vs 这只标的自己有数据洞"那道证据至今没补上，所以只锁不关）。
   判据三条（含 M15 那次 GREEN 教出来的牙齿：默认 30 天 ⇒ `+1` 恰好等于写死的 31，把 monkeypatch
   改成 47 才量得出"间隔真的跟着这个数走"）。
④ **文案订正的备份不是关闭的备份**（#122 / B-1 + A-8）：`--fix-wording` 的 `plan[]` 只有 `old/new` 两句
   话、没有 `note`，喂给 `--restore-from` 会被当成"撤销关闭"把该留在回收站的行放回活跃列表；
   现在 `restore()` 按 `reason_kind` 当场拒（退 4），并说清反向动作是把 `old` 写回那一列。
⑤ **`--fix-wording` 从此有 CLI 判据**（#123 / A-7 + B-4）：以前只有内部函数用例，而那句"生产 15 行逐行核过"
   其实是镜像跑的 ⇒ 文档里的归因已改成"镜像 2026-09-27 10:31 真订正 5 行、生产一行都没订正"。
   新用例真起子进程走 CLI：dry-run 退 **2** 且 `delete_reason` 逐字节不动，`--apply --confirm` 才真改。
⑥ **"12 行"是错的，7 行才是**（#124 / B-5 + A-4 + A-3 + B-6）：文档里那条复核命令写着 `group by 1,2,3`
   又把 `count(*)` 放进分组 ⇒ sqlite 直接报 `aggregate functions are not allowed in the GROUP BY clause`，
   没人能照它复现；换成能跑的写法印 `158006/1 · 158038/1 · 515440/5` ＝ **7** 行。同批把两处归因过头收窄
   （② 那两个库的 `status`↔`is_correct` 实测**零漂移**，换尺子是加固不是修 bug）。
⑦ **永久形状的样品补上"首笔净值说不清"那一格**（#125 / A-6）：`stale_close_evidence` 的判定表原来缺
   `first=None` 那格 ⇒ 库里一行净值都没有时"放行不关"没被量过。
⑧ **页面那句加法式子是句错话**（#126 / A-9 + B-7）：`pending`＝"还没有结论的全部"，含观望行与缺目标日的行，
   而三个子桶都排除这两类 ⇒ "待验证＝到期＋结构性＋未到期"只在 `flat=0 且缺目标日=0` 时**碰巧**平
   （今天两库确实都是 0，那是巧合不是构造）⇒ 措辞改成"含这三档"，并加一条反向断言不许再把"含"写成"＝"。
⑨ **收口脚本的三句话**（#127 / B-8 + B-9）：库里一行净值都没有时旧话写"净值 None 至 None **覆盖得到**这段窗口"
   正好说反 ⇒ 改成"说不清它给不给得出这段，不关"；文件头那道门从"两条证据"改成**三条**（现场再问源端 /
   两种永久形状 / `was_locked_previously` 真问过第二次），与验证器同尺；`[已订正]` 回执排在 `db.commit()` 之后。
⑩ **新变异体检工具接了两把互斥锁**（#128 / B-10）：`scripts/mutation_proof_lifecycle.py` 会就地改写 `src/*.py`
   再起 pytest，上一批一处锁都没接 ⇒ `test_both_sides_are_actually_wired` 从"只认前端那一份"改成按名单逐个核，
   并登记一个"形状像但不是 hazard"的邻居（`audit_doc_claims.py`）——登记了却不解释、或解释了却已不存在都红。
⑪ **一条环境账（不是代码回归）**：本机 8 GB 内存在跑批期间只剩 0.13 GB，Windows 会把刚起的子进程直接打死
   （退码 `0xC0000374`、stdout/stderr 全空）⇒ 上一次基线那条红就是这个形状。
   `test_alembic_target_direction.py` 里的裸 `subprocess.run` 全部收进一个 `_spawn`，
   **只对"像被系统打死"的退码**重跑；"被系统打死"怎么认，共用 `killed_by_the_os`
   （定义在 `test_prediction_migrations.py`，不搓第二把）。脚本自己返回非 0 时一次都不许多试。
最后一次核对（**串行**、默认 locale cp936、子进程显式 `PYTHONIOENCODING=utf-8`、跑期间不起第二个会话）：

- `pytest tests/unit -q` → **1218 passed / 16 skipped / 0 failed**（821.60 秒）。
- `pytest tests/ -q` → **1227 passed / 16 skipped / 0 failed**（612.79 秒）。
  （上一基线 1202/1211 → 本批 **+16 条 / 两个口径同增**，分布用
  `for f in $(git diff --name-only HEAD -- tests/); do echo "$f $(git show HEAD:$f | grep -c '^def test_') -> $(grep -c '^def test_' $f)"; done`
  数出：新文件 `test_one_ruler_per_question.py` 0→**5** 个 `def`（当场账 `--collect-only -q` 印 **10**：
  「有没有结论」只许那把尺子回答、归档时间戳只许一个来源、合并去重也走北京钟、永久形状判定表含
  `first=None` 那一格、`archive_stamp()` 一次交出一对）、
  `test_close_unknowable_predictions.py` 12→**15**（+3：文案订正的备份不许当关闭备份、CLI dry-run 退 2
  且一个字都不动、库里一行净值都没有不许说"覆盖得到"）、
  `test_structurally_unverifiable_hold.py` 21→**24**（+3，当场账 `--collect-only -q` 印 **30**：
  两档各等自己的钟、页面那句重问日取于行上那根日期、`NAV_HISTORY_LOOKBACK_DAYS` 两处问题一个出处）。
  **改契约不增条数**：`test_frontend_cold_start.py`（"含"不许写成"＝"，加反向断言）、
  `test_mutation_lock.py`（按名单核两份体检工具 + 邻居必须解释）、`test_script_db_guards.py`（守卫扫描的
  文本证据改走 `_payload_blanked`：变异工具把另一支脚本的 `'…--apply --confirm TOKEN'` 当**载荷**嵌进自己，
  老写法按原文 grep 就把体检工具当成"待管的写脚本"点名了两次 —— 现在只认 `add_argument` 那类"自己在声明旗子"
  的字面量，解析失败一个字都不挖、fail-closed，并配三条控制断言；复跑该文件 36 passed / 142.79 秒）。
  变异：`python scripts/mutation_proof_lifecycle.py` **18 处全 RED**（M1~M16 + CONTROL 全绿，原始日志随仓库走
  `docs/迭代计划/run-20260927-mutation/round54-lifecycle-mutations.txt`）；前端侧
  `python scripts/mutation_proof_frontend.py --only queue_caliber` 全 RED（`round54-frontend-queue-caliber.txt`）。
  ⚠ 两处判据本身被体检逮到过：M15 第一次跑是 **GREEN**（默认 30 天 ⇒ `+1` 恰等于写死的 31，用例对
  "换了个数"失明），M11 第一次是 **ANCHOR-MISS**（锚点还停在 `_conclusion_conditions` 委托之前）⇒
  一条只在"验的事情"变坏时才红，才叫判据。
  镜像那 12 行的逐行原因就在上面那条 dry-run 的回执里（`0 可关`，无一行进入关闭动作）。
  **生产此刻**（同日 16:0x，只读探针）：`expired_unjudged 0 / held_by_lock 0 / actionable_today 0`。）
（上一批：2026-09-27 12:0x（北京），**任务 #112~#118：第 53 轮两份复评（A 74 / B 61）返修——
「结构性不可验」补上第二种永久形状、页面分母换成同一把尺子、改标必清重问锁、存量错文案订正**——
① **窗口整段早于标的首笔净值**是一档新的永久形状（#112，B-1 的 BLOCKER）：上一版只认"末条净值早于窗口起点"
   （＝标的停更），而镜像实测 `515440`／`158006`／`158038` 上压着 **7 行**窗口的目标日**早于该基金第一笔净值**
   （复核 `python scripts/q.py "select f.fund_code, date(f.mn) first_nav, count(p.id) rows_held from (select fund_code, min(nav_date) mn from fund_history group by fund_code) f join predictions p on p.fund_code=f.fund_code where p.is_deleted=false and p.is_correct is null and p.next_verify_date > p.target_date and f.mn > p.target_date group by f.fund_code, date(f.mn) order by 1"` ⇒ 印 `158006/1 · 158038/1 · 515440/5`；
   第一版那条命令写着 `group by 1,2,3` 又把 `count(*)` 放进分组 ⇒ sqlite 直接 `aggregate functions are not allowed in the GROUP BY clause`，**没人能照它复现这个 7**）
   ⇒ 末条永远晚于起点 ⇒ 光认停更把这 7 行永远留在"重问 ⇒ 弹回到期 ⇒ 再踢出去"的圈里。
   现在 `stale_close_evidence()` 答两种（`stopped` / `pre_inception`），回收站那句话按形状各说各话，
   `should_close_as_stale_target` 与存量收口脚本共用它，验证器多问一句"第一笔净值在哪天"。
② **「待验证」这一档的分母改成"有没有结论"**（A-5）：新 `_conclusion_conditions()` 一处实现，
   列表过滤器、facets 计数、行上那个标签三头共用 —— 旧写法读遗留列 `predictions.status`。
   **归因按实测收窄（第 54 轮 A-3；2026-09-28 复核同数）**：两个库各数了一遍（`python scripts/q.py "select status,
   (is_correct is null), count(*) from predictions where is_deleted=false group by 1,2"`，加 `--production` 走线上。
   **这里必须写 `false` 不能写 `0`** —— 上一版这句命令在生产 PostgreSQL 上直接报错，等于"生产上量过"这句话
   没法复核；sqlite 两种都收，所以只在镜像上跑过的人看不见这个差别）
   ⇒ 镜像 `pending/未判 422 · success+failed/已判 1189`、生产同形状 `410 / 1191` ⇒ **零漂移**，
   不许写成"生产上量到过"。那句"未到期与观望之和差 12 条"是**另一件事**（旧话把 `held` 漏在加和之外，
   与 `status` 无关；同一批把那句话删了）。
③ **改标必清旧标的的重问锁**（#113 / #114，A-3 + B-5）：`retag_prediction` 把压在旧标的上、晚于目标日的那根日期
   退回目标日；页面「编辑预测」改绑不再直接写 `prediction.fund_code`，而是先过 `retag_gap` 那道证据门、
   再走同一个咽喉 ⇒ "换到新标的后第一次问就满足关闭全部条件、当场进回收站还写着已问过两次"这条路断了。
④ **归档时间戳与恢复下界都按北京那把钟**（A-11）：`_soft_archive` 原来用 `datetime.now()` / `date.today()`，
   Render 容器在 UTC ⇒ 北京 00:00~08:00 归档的行"保留到 X 日"比页面其它日期口径少一天。
⑤ **存量收口脚本补上"锁未到点不关"那一半**（#115 / A-2）：判"问过几次"的尺子与验证器共用（`was_locked_previously`），
   并把 `asked_times` 传进回收站那句话（见上一批）。**同批新增 `--fix-wording`**（B-6）：默认 dry-run，
   真写要 `--apply --confirm CLOSE-UNVERIFIABLE`，先备份再只改 `delete_reason`、台账记 `archive_note_fixed`；
   镜像 2026-09-27 10:31 真跑订正 **5 行**（`3016/3191/3194/3346/3355`，那句"已问过两次"是 09-26 那版脚本写多的），
   生产 15 行逐行核过**全部有锁证据** ⇒ 一行都没订正（复现：`python scripts/q.py --production` 数
   `position('已问过两次' in delete_reason) > 0` 与 `was_locked_previously` 的等价条件）。
⑥ **`--restore-from` 从此比对现状**（#117，B-3）：只按 id 盖回会无声抹掉老板手动归档的署名与原因；
   现在哪一行被别人动过就拦哪一行，`--force-restore` 才硬盖。**这条旗子上一版是假承诺**：它打印"照样盖回"
   却把拦下的行踢出名单 ⇒ 退码 0、一行没还原，是新用例点红的（`_block` 里 `if not force` 才加入 blocked）。
⑦ **两处页面的话**（#116 / A-1、B-2）：那句"哪两种原因见上方…"自指一个不渲染的栏 ⇒ 改成中性一句；
   「待验证」按钮的 title 与口径灰字现在明说这一档＝待验证到期＋结构性不可验＋未到期，
   **"今天点验证只跑到「待验证到期」那一档"**。
最后一次核对（**串行**、默认 locale cp936、子进程显式 `PYTHONIOENCODING=utf-8`）：

- `pytest tests/unit -q` → **1202 passed / 16 skipped / 0 failed**（693.05 秒）。
- `pytest tests/ -q` → **1211 passed / 16 skipped / 0 failed**（862.22 秒）。
  （上一基线 1190/1199 → 本批 **+12 条 / 两个口径同增**，分布用
  `for f in $(git diff --name-only HEAD -- tests/); do echo "$f $(git show HEAD:$f | grep -c '^def test_') -> $(grep -c '^def test_' $f)"; done`
  数出：`test_close_unknowable_predictions.py` 7→**12**（+5：北京钟的归档戳与恢复下界、"已问过两次"只在行真锁过
  时才算第二次、订正只动那一列、还原不许盖掉别人动过的行、CLI 缺确认词那一支）、
  `test_structurally_unverifiable_hold.py` 16→**21**（+5，当场账 `--collect-only -q` 印 **27**：
  首笔净值之前的窗口该关、两种永久形状分得开、改标把旧锁退回目标日、只有那一把锁写晚于目标日的日期、
  验证器整份词汇表都要有处置、异步函数也要被 AST 扫到）、`test_prediction_query.py` 12→**13**
  （「待验证」问结论不问 `status` 列，配"两把尺子给出不同数时以结论为准"的对照 + 三档可加和）、
  `test_prediction_management_safety.py` 7→**8**（页面改绑必须问证据门并清锁）。
  **改契约不增条数**：`test_verdict_evidence_badge.py`、`test_frontend_cold_start.py` 两处口径判据跟着改钉新灰字，
  并加"未到期与观望之和"这句不许再出现的反向断言。
  变异：**新工具 `python scripts/mutation_proof_lifecycle.py` 15 处全 RED**（M1~M13，CONTROL 全绿，原始日志随仓库走
  `docs/迭代计划/run-20260927-mutation/round53-structural-mutations.txt`）；前端侧
  `python scripts/mutation_proof_frontend.py --only unverifiable` / `--only queue_caliber` 全 RED ——
  两处锚点本批改了形状，旧锚点会报 ANCHOR-MISS 而不是绿。⚠ **M11 第一次跑就是 ANCHOR-MISS**：
  跨行锚点写死 `\n`，而仓库 `.py` 在这台机器按 CRLF 检出 ⇒ `count(anchor)==0` 而**单行锚点全部正常**，
  症状是"只有跨行那几条量不到"，极易误读成"这条判据不存在"；现在脚本按文件自己的换行重拼。
  **一次镜像真跑存量收口**（2026-09-27 11:5x 只读 dry-run，命令 `python scripts/close_unknowable_predictions.py`）：
  `[计划] 到期未判里可以判定"永远问不出来"的：0 条；仍在等的：12 条` —— 6 条"重问锁 2026-09-30 还没到点
  ⇒ 这一轮连第二次都还没成立，不许关"、4 条"源端这段给了 1 条 ⇒ 是本地没补到，不是它没有"、
  2 条源端答得出。⇒ 这 7 行 `pre_inception` 形状**今天不该关、到 09-30 那一问就会关**，
  而旧代码是永远关不掉。**生产此刻的状态**（同日 11:5x，只读门 + 引擎级只读探针，命令
  `python scripts/q.py --production "with h as (select id, (next_verify_date is not null and next_verify_date > target_date) as locked from predictions where is_deleted=false and target_date is not null and is_correct is null and prediction_type<>'flat' and target_date <= date '2026-09-27') select count(*) as expired_unjudged, count(*) filter (where locked) as held_by_lock, count(*) filter (where not locked) as actionable_today from h"`）：
  `expired_unjudged 0 / held_by_lock 0 / actionable_today 0`。）
（上一批：2026-09-27 06:5x（北京），**任务 #110 + 第 52 轮返修：「结构性不可验」要升级成关闭，
证据链重做**——
① "问过两次"从此由一把共用的尺子回答（`was_locked_previously`：那根日期落在**自己的目标日之后**）。
上一版拿"创建时排的那根日期过去了"当证据，2026-09-27 在镜像上真跑一次批量验证时，那两条周六目标日
**第一次**判出结构性结论就一跳进了回收站 ⇒ 存量收口脚本共用同一把尺子，并把 `asked_times` 传进
回收站那句话（它自己那一问只在行上原本压着到点的锁时才算第二次，那句"已问过两次"才写得出口）。
② `same_nav_endpoint` 的关闭证据**整条撤回**（第 52 轮 A-1）：我上一版配的是"库里目标日之后已有净值
⇒ 那天不是交易日"，实测分不清"休市日"与"这只标的自己有数据洞"（镜像逐日数过行数：真休市的
2026-07-11 周六全库 **1** 行 —— 货币基金照发；交易日的 2026-09-08 有 **202** 行）⇒ 这一档只锁不关，
由 `CLOSABLE` / `LOCK_ONLY` 两张名单登记（合起来必须逐字等于结构性总名单，加一档不登记就红）。
③ 页面上两处话改正（A-2 / B-2）：行内灰字与口径那句原来写"数据源给不出这段净值"，对退化端点那半
是**说反的**（那只基金活得好好的、起点那条给得出）⇒ 改成中性一句 + 一条"这句反话不许再出现在页面上"
的反向断言，两处变异锚点跟着改形状。
④ 验证路径的"今天"改取北京那把钟（B-3）：它原来是 `date.today()` 而队列/分类/新鲜度按北京日，
Render 容器在 UTC ⇒ 北京 00:00~08:00 手点"验证全部"时比队列少一天，上一轮的锁被读成"还没到点"；
本机就在 +8，这一格永远看不到 ⇒ 补一条"两面钟冲突必须按北京"的用例。
⑤ 关不成就照样上锁（A-6）：旧写法是 `else`，"该关但咽喉拒了"那一格既不锁也不报。
⑥ "哪些失败算结构性"只有一处回答（B-1）：AST 扫 `src/` 钉两种拼法（`== 'xxx'` 与抄整张名单的
`in ('xxx', 'yyy')`）—— 第一版只认比较运算，是变异 M6 打绿之后补的。
**这批自己抓出来的两条关于"判据本身"的**：变异脚本还留着**已被驳回的设计**的锚点（`return bool(target_is_non_trading_day)`
那行早就不在了）⇒ 它报"锚点不唯一"而不是"判据有效"，ANCHOR-MISS 必须当失败处理；
`test_a_degenerate_endpoint_is_never_closed_even_on_the_second_ask` 第一版**没造净值历史** ⇒ 它拒的其实是
"覆盖不了窗口"，M2（把退化端点塞进可关名单）在它身上打绿 —— 一条只在"验的事情"变坏时才红的用例
才叫判据，把另外两条关闭证据摆足之后它才只验这一档能不能关。最后一次核对（**串行**、默认 locale cp936、
子进程显式 `PYTHONIOENCODING=utf-8`、跑期间不起第二个会话）：

- `pytest tests/unit -q` → **1190 passed / 16 skipped / 0 failed**（594.21 秒）。
- `pytest tests/ -q` → **1199 passed / 16 skipped / 0 failed**（626.89 秒）。
  （上一基线 1182/1191 → 中间那次 1189/1198 是本批返修中途的实测，最后一次改用例后重跑为上面两个数：
  **+8 条 / 两个口径同增**，分布用 `git diff --name-only dee6371..HEAD -- tests/` 逐文件数 `^def test_` 前后相减
  （复核就这一条命令）：`test_structurally_unverifiable_hold.py` +6（退化端点入锁、退化端点第二次也不关、
  第一次问出来永不关、两张名单必须拼得上不重叠、"算不算结构性"只许一处回答、关不成仍上锁；
  当场账 `python -m pytest tests/unit/test_structurally_unverifiable_hold.py --collect-only -q` 印 22，
  比 `^def test_` 多 6 条是"排期不许越过目标日"那条按 7 个周期展开）、
  `test_close_unknowable_predictions.py` +1（行上没锁过 ⇒ 脚本不许关、也不许写"已问过两次"）、
  `test_prediction_verify_date_boundary.py` +1（两面钟给冲突日期时必须按北京）。
  **改契约不增条数**：`test_frontend_cold_start.py` 里那条口径判据改成钉新灰字，并加
  `assert '数据源给不出这段' not in html`（反话不许回来）。
  变异：`python scripts/mutation_proof_frontend.py --only unverifiable` 与 `--only queue_caliber` 全 RED、
  CONTROL 全绿；逻辑侧 `python data/_review_tmp/mutate_structural.py` 8 处（M1-M7 + M3b）全 RED、
  跑完逐文件字节回读比对还原。⚠ 那份脚本在 `data/`（不入库），锚点是这一批形状的，下一轮要按当时的行重读。）
（上一批：2026-09-27 05:02（北京），**任务 #108 + #109：基金同步的"失败 N 个"只装真失败，
整库落后时逐行那句停更话不再说过头**——
`update_all_funds_info` 原来两档（`get_fund_info` 回 None 记失败、抛异常记失败），而生产上那两个 None
的形状完全相反：`000725` 大成添利宝货币B 是**真基金**（详情接口取不到、历史接口照常给行）却被算成失败、
**连历史都不拉** ⇒ 一天天变旧，在页面上长成老板点名要清零的那一档"无法更新的基金"；
`603758` 秦安股份 是**混进基金库的 A 股代码**（只读实测：`fund_info 1 行 / 净值 0 行 / 活预测 0 /
映射 0 / 回收站 1 / 台账引用 1`）⇒ 它唯一的作用就是每次点按钮都报"失败 1 个"，逼老板去查一个不存在的问题。
现在分三档：`nav_only`（详情没答但净值补到了）/ `unsyncable`（两个接口都答不出且库里 0 行 ⇒ 基金域查无此码，
**不计入 `failed`、不再让整次同步 `success:false`**，回执逐行点名）/ `failed`（真接口故障，原因里带
"库里还有 N 行历史 ⇒ 可重试"）。分档靠两个真值，不靠猜代码形状：`_update_fund_history` 现在返回
"**历史接口答了几条**"（不是"新入库几行"——净值可能早在库里，0 行新增不等于源端没答）、`_nav_rows`。
**#85 的处置由此改向**：不再走"硬删档案 + 清台账"（破坏性、且台账 `ON DELETE RESTRICT` 镜像演练已证删不掉），
先做不毁数据的一半。同批 #109：`nav_stop_note` 只看"比库内最新一笔落后多少"仍会撒谎 ——
镜像实测 `/api/funds` 100 行**每一行**都挂着"别的基金还在更新、只有它不更新"，因为参照物（09-22）自己
已经过期、而多数基金停在 09-11；现在参照物自身落后超过 `NAV_LAG_WARN_DAYS` 时改说
"先跑一次「更新基金」，再看这一行是不是真的源端停更"，库是新的、只有一只掉队 ⇒ 原话仍说。
最后一次核对（**串行**、默认 locale cp936、子进程显式 `PYTHONIOENCODING=utf-8`、跑期间不起第二个会话）：

- `pytest tests/unit -q` → **1182 passed / 16 skipped / 0 failed**（598.37 秒）。
- `pytest tests/ -q` → **1191 passed / 16 skipped / 0 failed**（565.03 秒）。
  （上一基线 1179/1188 → 本批 **1182/1191：+3 条 / 两个口径同增** = `test_fund_sync_missing_funds.py` +3
  （真基金详情答不出但净值照补、股票码查无此码、真故障不许被新桶吞掉——三档各一条，全部走真 sqlite）
  + `test_stats_evidence_report.py` +1（整库旧不许说"别的基金还在更新"，并配"整库新必须继续说"的对照）
  **−1**：删掉 `test_fund_sync_manager.py::test_update_all_funds_info_partial_failure` ——
  它拿"任何 `.all()` 都返回同一份基金列表"的 Mock 当数据库，函数多问一句"库里几行净值"之后
  它给的数已经没有意义（实测把 1 条失败读成 2 条），三档的真值由上面那三条钉。
  **这是第 4 次撞到同一族**：Mock 夹具会替错误行为作保 ⇒ 记进记忆里那条"绿灯会说谎的形状"。
  变异 4 处全 RED：`if answered` 改 `if False`、`elif not _nav_rows` 两个方向各改一次、
  库龄那一支改 `if False`；还原后逐文件字节一致。
  另有一处**改契约不增条数**：`test_a_stopped_target_says_so_on_its_own_row` 的夹具原来只造 `FundInfo`
  不造 `fund_history` ⇒ 参照物落到 env 那批 6 月的行上，整库看起来 111 天没动，那条用例验的
  "库是新的、只有一只停更"当场不成立；补一行新净值进历史表后才对上它本来要验的形状。）
  （上一批：2026-09-27 04:19（北京），**任务 #107 + #106：验证批次改问"心跳"，回执只数真的动了的行**——
先纠我自己一句说错的话：我上一轮汇报说"verify-all 报 completed 却有 30 条从没被跑过"。**这是错的**，
错在我拿**中途轮询**的 `209 / 239` 当终态。真去读 `batch_analysis_tasks`（只读，219/220 两行）看到的是
219：`total 239 / processed 239 / failed 0`、18:44:01→19:18:35（**34.5 分钟**）；220：19:16:02→19:18:36。
⇒ 那 30 条只是还没轮到，而**这两行重叠在跑才是缺陷**：`PredictionVerifyTask._is_stale` 拿
`started_at`（永不推进）当超时参照 ⇒ 第 30 分钟 `status()` 把还在推进的批次标成 failed、
`in_progress` 翻 false、按钮解锁 ⇒ 第二批并发写同一批预测的结论。兄弟两支
（`post_analysis_service.heal_stale_job` / `viewpoint_workflow_service.heal_stale_task`）**早就是心跳语义**
（`updated_at or started_at or created_at`，注释还写明"绝不能误杀仍在推进的慢任务"），只有预测这一支漂了两个月。
连带纠一处文档谎话：`预测验证与准确率统计.md` 那句"靠进程锁 + advisory lock 保证同时只有一个批量进程"
已被这两行台账证伪（advisory lock 是事务级、`start()` 一 commit 就放开；`_PROCESS_LOCK` 也只覆盖 `start()`）。
四处改动：① `_is_stale` 改问最后一条心跳（真卡在某条上 30 分钟仍会判死，那才是要判死的形状）；
② `finish()` 只在回执**真的带了数**时才改写计数 —— 崩在半路那一支只填 message，旧写法三个
`int(... or 0)` 把已推进的 209/239 抹成 0 ⇒ "跑了 209 条然后断"与"一条没跑"在台账上长成同一个样子；
③ `_serialize` 补 `not_processed`，并把"这一批队列里 N 条、只跑到 M 条 ⇒ 还有 K 条没验证（原因）"
拼进页面那一栏已经在读的 `failure_summary`（不新接模板，避免"接口有字段≠老板看得见"）；
④ 按钮的分母 `_count_due_predictions` 原来自己抄了一套 `status == 'pending'` + 目标日，而批次实际走
`filter_due_for_verify`（`is_correct is null` + 重问锁）⇒ 现在直接数同一个队列，"今天"也从
`date.today()` 换成北京 `current_as_of()`（生产此刻 `status` 与 `is_correct` 恰好逐档对得上：
pending↔未判 410、success 638 / failed 553↔已判 1191，所以今天两个数一样 —— **那是巧合不是等价**）。
#106 那三处同类：`retag_prediction` 的 bool 分不清"没动/被拒/动了"，所以 `sync_missing_funds` 里两处
`linked += 1` 与 `sync_predictions_by_sector_mapping` 的 `predictions_updated += 1` 会把拒掉的行算成做了事
⇒ 新增公用的 `retag_gap(db, pred, code) -> (理由 / None, 证据)`（**窗口判据在整个同步器里只此一处调用，
且必须待在 `retag_gap` 里**，AST 钉住、只数真的调用不数注释），拒了的行进 `skipped_unservable` 桶并带人话原因，
`update-all` 那句成功消息把这条数说出来。要说清：`sync_predictions_by_sector_mapping` 在 `src/` 里
**零调用方**（`grep -rn` 只命中定义那行）⇒ 按仓库规矩**不给死路写绿灯判据**，判据都落在活的 `sync_missing_funds` 上。
最后一次核对（**串行**、默认 locale cp936、子进程显式 `PYTHONIOENCODING=utf-8`、跑期间机器安静）：

- `pytest tests/unit -q` → **1179 passed / 16 skipped / 0 failed**（604.13 秒）。
- `pytest tests/ -q` → **1188 passed / 16 skipped / 0 failed**（560.61 秒）。
  （上一基线 1172/1181 → 本批 **1179/1188：+7 条 / 两个口径同增**：
  `test_prediction_verify_batch_task.py` +4（还在推进的慢批次不许判死、跑不够要报出条数、
  崩了的批次要留住进展、分母与队列同一把尺子）+ `test_fund_sync_missing_funds.py` +3
  （被证据门拒的行不许算进"关联 N 个"、能出证据的照常绑上、唯一出处那条 AST）。
  **两条改契约不增条数**：`test_prediction_verify_batch_task.py::test_prediction_verify_task_replaces_stale_running_task`
  与 `test_stuck_task_and_fund_speed.py::test_status_marks_stale_running_task_failed` —— 它们的样品原来只把
  `started_at` 挪到 31 分钟前，那在新语义下正是"跑了很久但仍在推进"，所以样品改成连心跳一起挪；
  另有一条**被我自己的重构打红**：`test_the_evidence_gate_is_wired_into_both_the_move_and_the_preview`
  原来断言 `retag_prediction` 函数体里出现 `target_cannot_evidence_window`，而我把这一跳收进 `retag_gap`
  ⇒ 判据跟着改判**两跳**（少任何一跳都红；掏空 `retag_gap` 复跑 3 failed，已实测）。
  变异：本次 5 处手工变异全 RED（心跳改回 `started_at` / `finish` 改回无条件归零 / `not_processed` 那一支
  改 `if False` / 证据门那一支改 `if False` / `retag_gap` 掏空），跑完逐文件回读比对还原字节一致。）
  （上一批：2026-09-27 02:14（北京），**任务 #105 + #103/#104：改标那道证据门改成问「验证器判得出来吗」，
而 dry-run 与实跑从此是同一个数**——
在生产上点了一次「按板块对齐标的」的预览，回执说「将更新 326 个预测」；拿只读连接按验证器自己的那两把尺子
（窗口内 ≥ `VERIFY_MIN_DATA_POINTS` 个点、终点距目标日 ≤ `VERIFY_MAX_END_NAV_AGE_DAYS` 天，见
`_check_fund_data_availability`）逐条量：**320 条绑过去判得出来，6 条绑过去永久判不出来**
（要绑 `158038` 的那几条——它库里首笔净值 2026-09-07、`012765` 首笔 2026-08-28，
而压在它们身上的预测窗口在 07-01 / 08-28~09-14）。上一批 #100 那道门只问"末笔不早于窗口起点"，
这两只的末笔都是 09-24 ⇒ 点头放行；可验证器对这种窗口报的是 `insufficient_points`，**不是**
`no_source_history` ⇒ 任务 #8 那档重问锁根本不接 ⇒ 六条会永远躺在「待验证到期」里，
正是老板点名要清零的那一档。现在门换成 `target_cannot_evidence_window`（点数 + 终点年龄，
两个阈值都从 `config` 取，门里不立第二个数字；"库里一条净值都没有""窗口起点说不清""还没到期"
三律一律放行，防止把门修成墙），预览在建候选时就把这类行分进 `predictions_skipped_unservable`
＋逐条 `reason`，路由那句回执从此会说"N 条没动"；计数改成**回查 `pred.fund_code` 才 +1**
（`retag_prediction` 的布尔说的是"清没清结论"，拿它当"改标成功"就是把空操作报成做了事 ——
第 51 轮 B-2 同一族，那一次长在 `update-all`，这一次长在 `sync_sector_mappings`，
剩下三处同类已经立成任务 #106）。
同批两处是**真开浏览器**照出来的，而它们一直在绿灯里被背书：#103 `prediction_query_service._serialize`
从来不给 `delete_reason` / `deleted_by` / `deleted_at` / `restore_before` ⇒ 页面那句
`v-if="p.is_deleted && p.delete_reason"` 恒假，回收站里"系统关掉的"与"谁手动归档的"长得一模一样
（老判据只读 HTML 文本，结构上不可能发现这件事）；#104 逐行"源端停更"那句的参照物是**今天**
⇒ 整库一起落后时**每一行**都在喊，真正停更的那几只反而被淹成噪声，现在比的是 `nav_reference_date`
（库里最新一笔；`nav_freshness` 的全表截止日也改成从这一个函数拿，"库里最新一笔"从此只有一处实现）。
接在任务 #8 第二半 + #100/#102（判不了的第二轮不再留在页面上，而"会验不了的绑标"这条路当场断掉）之后，
最后一次核对（上一批：2026-09-26 23:35（北京），
验证器第二次判出 `no_source_history`、且**库里末条净值早于窗口起点**（＝这只产品的源端已经不给这段
发净值，任何一次同步都补不到）时，走 `PredictionService.close_as_unverifiable`：进回收站、
`deleted_by='system'`、`delete_reason` 写清"已问过两次仍无答案 ⇒ 既不算判对也不算判错、不计入准确率、
可随时恢复"，台账 `action='archived'` 一并留下；只判过一次、或末条净值还在窗口之后（真在等的）⇒ 仍然只上重问锁。
一次性收存量用 `scripts/close_unknowable_predictions.py`（默认 dry-run、真写要 `--apply --confirm
CLOSE-UNVERIFIABLE`、先 `backup/close-unknowable-*.json` 再逐行回执、`--restore-from` 默认也是 dry-run；
关闭条件与验证器**共用同一把尺子**，不在脚本里留第二份判据）。生产已用它收掉 15 行（`003033`×14 / `002413`×1）。
同批把**来路**也断了：`retag_prediction` 不再允许把预测绑到"净值覆盖不了这段窗口"的标的上
（生产台账 57 行 `maintenance_sync/sector_mapping` 就是那 15 条的制造机制），基金页每一行按
`NAV_LAG_WARN_DAYS` 这**一个**阈值自己说明"源端停更/一条净值都没有"，不再让停更产品长得像我们更新坏了。
接在任务 #8 第一半（重问锁 + 页面「结构性不可验」那一档）之后，
最后一次核对（上一批：2026-09-26 21:38（北京），**任务 #8：「结构性不可验」从此是一个会自愈的持久状态**——
验证器判出 `no_source_history`（真按区间问过数据源、它给不出这段净值）时，把行上那根从来没被读过的
`next_verify_date` 写成"今天 + 凭据 TTL + 1 天"：`classify` 报 `unverifiable`、到期队列与页面「待验证到期」
把它减出去、另开一档「结构性不可验」报条数（两个数加起来仍等于全部到期未判），到重问日自己回队，
净值补上了当场撤锁；全程不写 `is_correct`、不改 `status`、不碰台账。接在第 51 轮返修（查回被 `2c227c9`
一起删掉的帖子编辑路由、关掉"少给一列就免检"那三道缝）之后，
最后一次核对（上一批：2026-09-26 16:53（北京），**第 50 轮返修四批（`39a9abe` / `1d729d4` / `b2d1ba1` / `7c142fe`：删除博主的路由、净值新鲜度按引用面、建档身份门搬到咽喉、依赖面从元数据现推，加上一处我自己造出来的崩溃与三处写过头的文档）**之后，
最后一次改用例后立刻**串行**重跑两个口径；**默认 locale（cp936，不设 `PYTHONIOENCODING`）下跑**，
子进程一律显式 `PYTHONIOENCODING=utf-8`）：

- `pytest tests/unit -q` → **1172 passed / 16 skipped / 0 failed**（598.09 秒）。
- `pytest tests/ -q`（含 integration/services）→ **1181 passed / 16 skipped / 0 failed**（595.50 秒）。
  （上一基线 1161/1170 → 本批 **1172/1181：+11 条 / 两个口径同增**，复核就一条命令
  `for f in $(git diff --name-only 50a913a..HEAD -- tests/); do echo "$f $(git show 50a913a:$f | grep -c '^def test_') -> $(grep -c '^def test_' $f)"; done`，
  分布：`test_verdict_evidence_badge.py` **+3**（① 一把纯函数尺子的八格边界表：点数刚好够 / 差一个点 /
  终点年龄正好在上限 / 越界一天 / 末笔早于窗口起点 / 库里一条都没有 / **还没到期 ⇒ 点数不够也放行** /
  窗口起点说不清 —— 样品按 `config` 那两个常量排，阈值一改这张表就得重排；② `retag_prediction` 对
  未到期窗口必须放行且仍按"改标必清结论"走；③ AST 钉**两个调用方都接了这把尺子**，
  且 `retag_prediction` 与 `sync_sector_mappings` 两处都不许出现 `VERIFY_MIN_DATA_POINTS` 这个属性名）；
  `test_prediction_maintenance.py` **+3**（预览与实跑**同数**那条主账：把 `calendar_gap` 那一支摘掉 ⇒
  `would_update` 从 1 变 2 就红；把唯一入口换成"什么都不做"的桩 ⇒ `predictions_updated` 必须 0、
  `predictions_skipped_unservable` 必须 1；路由那句话必须带上"N 条没动"）；
  `test_stats_evidence_report.py` **+3**（整库一起旧 ⇒ 一行都不喊、只有掉队那一只说话并且带上参照物；
  库里没有净值时退回今天 ⇒ 不许修成哑巴；"全库最新一笔"按 `(文件, 函数)` 登记名单 +
  **临时目录现造一处新站点必须被点名**的控制）；`test_frontend_cold_start.py` **+2**
  （回收站那条**改判据形状**不只看文本：从 `<tr v-for="p in filteredPredictions">` 那一行抽出所有
  `p.<字段>` 拿 `_serialize` 的真返回对表，锚点找不到也响；配"页面凭空读一个接口没有的名字 ⇒ 必须点名"
  的空判对照。只扫这一行不扫全篇 —— `p` 这个循环名在帖子表与清理预览表也在用，扫全篇就是假红））
  （上一基线 1144/1153 → 那批 **1161/1170：+17 条 / 两个口径同增**，任务 #8 第二半 + 生产量到的那条机制账：
  新文件 `test_close_unknowable_predictions.py` 当场收集 **6** 条（关闭必须不写 `is_correct` 且台账记
  `source='system'`；三条**反面对照**——"净值刚补到窗口附近"、"源端答不出（`None`）"、"源端还答得出行"
  都不许关；还原默认 dry-run 再真还原；CLI 缺确认词与 `--production` 指向不对各退 4）；
  `test_structurally_unverifiable_hold.py` 7 → 10 个 `def`、`--collect-only -q` 从 13 到 **16**
  （+3 条走"第二次才关"那条链：活基金有历史缺口**永不**被关、停更标的第二次答"没有"才关、
  关闭决定读**两个**条件不是只读那一句答复）；
  `test_verdict_evidence_badge.py` **+3**（改标**不许**把预测绑到"净值覆盖不了这段窗口"的标的上——
  生产实测 `003033` 末条 2020-12-08 还压着 37 条活预测、台账 57 行 `maintenance_sync/sector_mapping`，
  这就是那 15 条验不了的预测的来路；对照组"一条净值都没有的新档案"**照绑**，防这道门建成墙；
  再加一条 AST 判据：**这把尺子只许有一处实现**，验证器与收口脚本都必须调
  `nav_cannot_cover_window`，自己比日期就红）；
  `test_stats_evidence_report.py` **+2**（基金页**每一行**自己说"源端停更"，而且把
  `NAV_LAG_WARN_DAYS` 改掉那一行的说法必须跟着变 ⇒ 页面与服务里都不许藏第二个数）；
  `test_frontend_cold_start.py` **+2**（回收站那一行必须把 `delete_reason` 说到屏幕上，不只是存在库里；
  `nav_stop_note` 光有 `v-if` 没有插值也算没显示）。
  上面那条 +16 之外另有 `test_prediction_maintenance.py` **+1**（「按板块对齐标的」的预览**不许**随预测条数线性查库：
  生产实测同一趟本地镜像 3.5 秒、线上 **100 秒零字节**（curl 连上、请求发完、拿不到响应），
  根因是循环里每条没直接命中的预测各查一次 `sector_alias` ＝ 900+ 次远程往返；
  判据形状是"从 5 条加到 405 条，**语句条数必须一模一样**"，退回旧写法实测 11 → 411 条点红。
  修完镜像同一趟 3.5 秒 → **0.4 秒**）。
  变异：`python scripts/mutation_proof_frontend.py --only unverifiable` / `--only archive_reason` /
  `--only stopped_fund_note` 全 RED、CONTROL 全绿（条数一律 `--list` 看末行）。
  **本批我自己抓到自己的两处**（都由新判据当场点红）：① 我在收口脚本里手写了一份 `latest >= start`，
  被刚写下的"只许一处实现"那条点红 —— 第 44 轮那道棘轮第二次逮到作者本人；
  ② 同一脚本的探针缓存**按 `(code,start,end)` 写、按 `code` 读** ⇒ 命中永远为空、"同一窗口只问一次"
  那句注释是假的（生产那次因此白打 15 次接口）。）
  （再上一基线 1130/1139 → 那批 **1144/1153：+14 条 / 两个口径同增**，任务 #8 那一批：
  新文件 `test_structurally_unverifiable_hold.py` 当场收集 **13** 条（含"排期不许越过目标日"那条
  按 7 个周期展开；`--collect-only -q` 数），含两条**反面对照**——"没问过的失败（`insufficient_points`）
  不许被锁"与"两档必须互斥、两个数加起来等于全部到期未判"，以及一条第一次走到的旧接线
  （`verify_all_pending` 的 `skipped` 以前因为 `filter_unverifiable` 恒为空从来没数过东西）；
  `test_frontend_cold_start.py` **+1**（页面上有数、行上有话、按钮走后端那一档）。
  **一条改契约不增条数**：`test_prediction_query.py` 里"`lifecycle=unverifiable` 不再支持、回退无过滤"
  那半已经不成立 ⇒ 改成"认不出的值才回退无过滤"，并把两档互斥与加和对表钉上。
  变异：`python scripts/mutation_proof_frontend.py --only unverifiable`（2 处）与
  `--only queue_caliber`（口径灰字换了锚点，旧锚点必须跟着改，否则是 ANCHOR-MISS 不是绿）
  三处全 RED、CONTROL 全绿。）
  （上一基线 1126/1135 → 那批 **1130/1139：+4 条 / 两个口径同增**，第 51 轮返修那一批：
  新文件 `test_post_edit_route_and_empty_code_refusal.py` **+3**（① 页面上"编辑帖子"那句
  `axios.patch` 必须对上**按方法**注册的 `app.routes` —— A-1 那条 BLOCKER 的机制账；
  ② 改标题要落库、"有预测只能改标题与链接"那句拒绝要带着原因到屏幕上；③ 改绑传空白代码
  ⇒ 整笔拒、旧标的一个字不动，夹具自己 `PRAGMA foreign_keys = ON`）；
  `test_sector_mapping_api.py` **+1**（新建映射那处豁免授予两档都钉：不给 `owner_confirm`
  不署名不锁定、给了就署名与锁定一起给）。
  **这一批的账要如实记**：为修 B-13 新增的那处写死授予值被免疫棘轮当场点红 ⇒ 按它的要求登记
  （登记成 **1** 不是 2：扫描器只认写死的授予值，`bool(owner_confirm)` 的值来自参数、看不见真值，
  那一档由行为判据补），这正是第 44 轮那道棘轮第一次替我拦住"我自己新加的来源"。）
  （上一基线 1123/1132 → 本批 **1126/1135：+3 条**，全在 `test_fund_info_archive_gate.py`（4 → 7）：一条"夹具自己必须真的拒悬空写"的控制断言（上一批的 `create_engine('sqlite:///:memory:')` 默认不开外键 ⇒ "档案被拒建、映射行却改到那个码上"这个形状在绿灯里过；镜像开 FK 会 `IntegrityError`，生产 `pg_constraint` 查该表外键 **0 行**于是静默留脏，两个库各坏一种）、一条"拒改必须一个字都不动旧标的"、一条"同一次保存身份门只被问一次"（数的是桩收到的代码列表，不是"有调用"）。另有一条改契约不增条数：`test_sector_mapping_api.py` 里"指控 ⇒ 行保留标不可服务"改成"指控且无档案 ⇒ 整行不建 + 理由回给调用方"，并补一句为什么第 7 轮那半在这里做不到。净值那两档的变异 2 处新增、3 处原有锚点跟着改形状（`python scripts/mutation_proof_frontend.py --list` 看末行，别抄这里），本轮复跑 `nav_staleness` / `stale_coverage` / `median_date` / `majority_staleness` 四组共 5 处全 RED、CONTROL 全绿。）
  （上一基线 1106/1115 → 那批 **1123/1132：+17 条**，两个口径同增 ⇒ 没有只挂在 integration/services 里的。
  分布是 `git diff --name-only 9c143fa..HEAD -- tests/` 逐个文件数 `^def test_` 前后相减量出来的（复核就这一条命令）：
  `test_fund_info_archive_gate.py` **新建 +4**（建档身份门从路由/服务层打进去：股票码拒建档、真基金照建、
  `identity_checked` 旁路、已存在档案不许重复探）、`test_purge_junk_funds.py` **+3**（引用面来自元数据、
  外部载荷带 `is_correct` 就拒还、**"数不出来"那条形状不许把 `plan()` 的打印弄崩** —— 这一格是第 50 轮
  A 席抓到我上一版只断言了键名、绕过了消费侧）、`test_bloggers_top_route.py` **+2**（`DELETE` 路由真注册 +
  三种结局各说各话）、`test_frontend_api_urls_resolve.py` **+2**（通配那一臂删掉后：现造的不存在路径必须红、
  且**过抽取器**不走手搓三元组）、`test_nav_future_row_gate.py` **+2**（净值日期 11 处落笔点的登记名单 +
  临时现造一处必须被量到）、`test_no_secrets_in_tracked_files.py` **+2**（形状与真值拆两条 + 占位符只判凭据组）、
  `test_production_hardening.py` **+2**（错误细节按 `DB_TYPE` 不按 `APP_ENV`、`started_at` 带偏移）、
  `test_stats_evidence_report.py` **+2**（一只新基金不许替引用面代言、跑批/体检的告警"算了必须说出口"）、
  `test_blogger_hit_rate_map.py` **+1**（回收站行也要数，不然删博主撞 FK）；
  `test_llm_analyzer_cache.py` **−3**（死函数 `_save_fund_mapping` 连它的三条绿灯一起删）。）
  （第 49 轮那批：1086/1095 → 1106/1115，**+20 条**，分布与"为什么"——

  ① `tests/unit/test_no_secrets_in_tracked_files.py` 新增 **3**（任务 #46：`.env` 真值 + 6 种凭据形状
  扫 `git ls-files`，只报"哪个文件、哪一类"绝不打印命中内容；两条控制断言里有一条专门钉
  "第一版形状正则漏了 `?` ⇒ 整条恒空而主用例全绿"这件事，同族教训又复现了一次）；
  ② `tests/unit/test_frontend_api_urls_resolve.py` 新增 **4**（任务 #47：前端 88 条 `/api` 字面量
  必须对上 `app.routes` 注册的那 94 条路由 —— 不抄第二份路由清单；两条控制断言 + 归一化自己的账，
  边界：只判路径不判方法）；
  ③ `test_stats_evidence_report.py` +5 / `test_frontend_cold_start.py` +1（净值新鲜度
  `nav_as_of`/`nav_lag_days`/`nav_future_rows`/`nav_used_stale_majority` 要钉住的四点：截止日不许被预签发的未来行顶上去、
  落后天数按北京 `as_of` 现算、阈值只在 `NAV_LAG_WARN_DAYS` 一处（页面不许自己比大小）、
  每日跑批日志与页面说同一句话，判据在 node 里跑 `navFreshNote` 真源码，配 **4 处变异全 RED**）；
  ④ `test_purge_junk_funds.py` +3（`--drop-dead-predictions` 的动作/备份/还原与"带结论不许删"，
  外加一条**副本演练逼出来的**：`prediction_change_logs.prediction_id` 是 `ON DELETE RESTRICT` ⇒
  旧写法"计划里承诺删、执行时 IntegrityError 整批回滚"，现在动手前数依赖并整批拒）；
  ⑤ `test_llm_analyzer_cache.py` +1（**S6 那批垃圾档案的来路**：`_save_fund_mapping` 在"该板块还没有映射"
  那一支不问身份就建档 + 建映射 ⇒ LLM 抽出的 A 股代码被机器写进基金库；现在建之前过
  `_manual_identity_verdict`，判"不是基金"拒建、判"没意见"照建 —— 两侧都钉）；
  `test_audit_fund_info_identity.py` +2（`--production` 四道拒：非 PostgreSQL / 要改名 / 缺确认词 /
  **旗子与实际连接不一致**，第 ④ 条只认进程里那个 engine 真正绑在哪台；外加一条"计划回执要数得出
  列出的行数"—— dry-run 列了 10 行 `[可补]` 却印"补上 0 行"是说反话）；
  ⑥ `test_deployment_optimization.py` +1：**线上跑的是哪一版从此有接口可答**
  （`/api/health/detail` 多出 `git_commit` / `git_branch` / `git_commit_source` / `started_at` /
  `uptime_seconds`，取自 Render 注入的 `RENDER_GIT_COMMIT`；取不到就写 `unknown`，
  不许拿 `version: 2.0.0` 那种静态串冒充答案）。这条判据同时钉"这个出口不许泄露连接串/口令"，
  顺带改正 `DEPLOYMENT.md` 一句会坑人的话：`/api/health/detail` **要带口令**
  （线上实测不带口令回 401，旧文档写的是"健康检查不需要访问密码"）。
  **同一批还做了一件不在用例数里的**：`scripts/audit_verdict_evidence.py` 的 `accuracy_span` 改成
  返回整份 `span_report()`（脚本里不留第二份算式），并多印一行 `[净值新鲜度]`。）
  （上一基线 1085/1094 → 本批 1086/1095：+1 条 = `test_every_list_fetch_point_asks_the_wake_gate_before_giving_up`
  （在 node 里跑三个 manager 的真实源码、数 `withWakeRetry` 被调几次；摘掉一处立刻红，已实测）。）
  （上一基线 1073/1082 → 本批 1085/1094：**+12 条 = 本批 9 条 + 前两笔提交欠跑数的 3 条**
  （那 3 条是"驱动名钉死"、"requirements 上界"、"体检脚本递 `--production` 到门口" ——
  每一笔提交都该带一次跑数，这里又欠了一次，记在案上而不是当成 0）。本批 9 条分布在
  `test_nav_future_row_gate.py` 新增 3 条（真起 sqlite 走 `update_fund_history` 的"提前签发行不许入库" +
  "丢弃必须说出来、条数要点名" + "两个取数入口与档案头都过同一道门"的 AST 接线判据，含"今天/昨天的行不许误伤"
  的防过宽对照）、`test_drop_future_nav_rows.py` 新增 6 条（dry-run 一行不删、没备份就拒删、
  点名日期库里没有就拒跑、确认词排在连库之前、档案头倒回真实末条、`--dates` 并入删除集）。
  当场复核这两份文件的条数：`python -m pytest tests/unit/test_nav_future_row_gate.py --collect-only -q`
  印 3、`... test_drop_future_nav_rows.py --collect-only -q` 印 6。
  本批另外两处是**改已有判据的样品**，不另起条数：`test_backfill_negative_proof.py` 的翻页判据把
  窗口从写死 `2026-09-01~10-20` 改成锚在"真实昨天往前 47 天" —— 不是我改坏了老判据，是**我新加的净值门
  会把样品里"还没到的那天"滤掉**，写死的窗口跨过今天之后，它量的就不再是"翻页有没有到底"（47→26 那次红是它替我盯住我）。）
  （上一基线 1072/1081 → 本批 1073/1082：+1 条 —— `test_database_url_routing.py` 的
  "驱动坏了要说出坏在哪"（见下面 `S6-上线前检查单.md` §0 那次部署失败那一条）。
  同批还做了两件不增条数的事：① `scripts/audit_verdict_evidence.py` 改走只读门并加 `--production`
  ⇒ 任务 #51 结掉，生产那行"⚠ 419 / 区间 33.68%~73.32%"从此有了可跑命令（实跑与 09-22 手算逐字对上）；
  ② `src/models/database.py` 那个把 `create_engine` 一起圈进去的 `try` 收窄，
  报错从此带**原始异常 + 解释器版本 + 平台**，不再用一句"psycopg2 is not installed"替深处的问题顶包。）
  （**老板这一轮破了一次例**：门禁仍是"两份取低分 ≥80"，本轮实际 70 未达，但线上构建被量出来
  落后 113 个提交（详见下面"生产库结构现状"那条）⇒ 他选"破例上线一次（推荐）"，
  并选"改标那 345 条先出清单再点"。**除这一次上线授权外，其余纪律一律不动**：
  生产写入仍是只读预检→dry-run→显式确认→逐行回执。）
  （上一基线 1070/1079 → 本批 1072/1081：+2 条，都在 `test_sweep_restore_owner_immunity.py`
  （7 → 9 条，当场 `pytest tests/unit/test_sweep_restore_owner_immunity.py --collect-only -q` 印 9）——
  一条钉"过期 `created_at` 必须拒还原，且 `[计划]` 那一行必须排在 `[还原]` 之前"，
  一条钉"免疫授予的棘轮不许把**读**同一列判成授予、绑参数式授予必须在名单里"。
  本批其余改动（三把棘轮并成一把、sqlite 口令两支、迁移委托样品）都**并在已有用例的样品表里**，
  不另起条数。）
  （**本批只修了"会丢数据的 + 会说谎的标签"这一类**，是老板圈定的范围（原话："先修产品，
  守卫只修会丢数据的（推荐）"）：① `sweep_sector_mappings.py --restore-from` 以前**在 commit 之后**
  才报"清掉几行净值"（第 48 轮 B-1）—— 现在动手前先印 `[计划]`，并且给 `created_at` 加了一道
  **合理性边界**：它离清单自身时间戳超过 90 天（或晚于一天以上）就退 4。上一轮修掉的是
  "我不知道从哪天起 ⇒ 全删"，这一轮修的是"**我猜错了那一天** ⇒ 还是全删"，净值不可再生；
  ② 写死授予值 / `is_correct` / `fund_code` 三把棘轮的"这句 SQL 在写哪一列"**改共用一把尺子**
  （新 `scripts/sql_write_policy.py`）—— 上一轮我只在两处接了新尺子、把免疫那把的老正则留下没换，
  于是它**两个方向都是坏的**（第 48 轮 A-4 / B-2 各自量到）：`SELECT … WHERE owner_locked = true`
  算 2 条授予（墙），`SET owner_locked = :ok` + 参数字典算 0 条（漏）。现在 SET 子句 /
  INSERT 列清单才算、`WHERE` 一律不算、绑参数按调用的实参字典求值；③ sqlite 串的口令**不分位置**
  都要剥（A-6 / B-3）：`sqlite:///E:/u:S3cr3tPW@data/x.db`（口令在带盘符的路径段里）上一版那条
  正则不许跨过 `/` ⇒ 整段回显，而两把尺子**一致地错**（"逐条相等"那条判据对它结构性失明）。
  ④ 撤一句我上一轮写在下面的**谎话**（两席同条点到，我今天自己也复现了一次）：
  "pytest 与 pytest 不互斥（那把锁只挡体检）"**是错的** —— 套件里 `test_mutation_lock.py` 有一条
  要抢**机器全局**的体检锁，第二个真会话会让它响。今天实测：我为了复现一条失败用例并发跑了
  1 条单测，同一时刻后台那次 `pytest tests/ -q` 就红了 `test_a_second_session_under_the_default_locale_still_runs`
  ⇒ 规矩从"数会漂"升级成"**跑基线期间不许再起第二个 pytest 会话**"。
  ⑤ 一条我自己的卫生账（这次由闸替我盯住）：新模块 `scripts/sql_write_policy.py` 我建了却**没
  `git add`** ⇒ `test_the_scanned_set_is_the_repository_s_not_this_disk_s` 当场红 ——
  那条"受检集合由 `git ls-files` 定义"的用例存在的意义正是"你造了个真模块，它不在受检集合里"。）
  （**本轮按老板的决定没修的，全部记在这儿**，别当成已封：A 席 A-1（helper 返回一句写死的
  `[目标]` 仍买到自报）、A-2（方向不明只在**字面下标 key** 时才算）、A-3（`importlib.import_module("sqlite3")`
  再 `connect` 仍不算 DB-API 能力）、A-5（迁移里 `from . import helpers; helpers.wipe()` 仍报干净，
  而 `run_migrations.py` 每次 Render 启动先问它）、A-8/B-5（能力类别与样品的对应仍没有断言）、
  B-4（`sqlite+aiosqlite:///data/fund_insight.db` 自报成 `/data/fund_insight.db` ⇒ **真镜像被说成
  "不是镜像库"**，复核：`PYTHONIOENCODING=utf-8 python -c "import sys; sys.path.insert(0,'scripts');
  import _db_guard as g; print(g.machine_name('sqlite+aiosqlite:///data/fund_insight.db'))"`）、
  B-6（`q.py` 的 sqlite 腿仍只有 `mode=ro`，没 `PRAGMA query_only`）、B-7（`audit_doc_claims`
  当场只判 3 条账）。转去做产品那一半：#51 生产区间没有可跑命令、#58 三个列表取数点没铺
  `withWakeRetry`、生产净值落后的监控与页面可见（第 48 轮 A-9 / B-8 同条：净值停在 2026-09-13）。
  **这三条到 2026-09-26 全部关掉**：#51 走 `audit_verdict_evidence.py --production`（只读门）、
  #58 三个 manager 都过注入的唤醒门（node 里数调用次数）、净值新鲜度进了 `span_report()`
  与页面灰字与 Cron 日志（阈值只在 `verdict_evidence.NAV_LAG_WARN_DAYS` 一处）。）
  （上一基线 1067/1076 → 本批 1070/1079：+3 条 —— `test_script_db_guards.py` 的
  "整趟散落连接扫描跑不完就不许钉库"（第 47 轮 B-4 那一支以前 0 覆盖）、
  `test_mutation_lock.py` 的"默认 locale 下第二个会话不许崩"（A4）、
  `test_alembic_target_direction.py` 的"env.py 报目标必须用共用那把尺子"（B-2）。
  本批改的判据形状样品（if/else 方向、写死的 `[目标]`、DB-API 三种拼写、迁移里藏在
  类方法/改名/跨模块 helper 里的删除、裸 SQL 授予换位置）都**并在那几条已有用例的样品表里**，
  不另起条数。）
  （**串行跑**是第 46 轮加的规矩：A 席并发时段读到过 1064 而干净复跑是 1063 ——
  pytest 与 pytest **不互斥**（那把锁只挡体检；**第 48 轮已证伪，见上面第 ④ 条**），
  第二个会话抢不到锁时现在会印一行
  `[警告] 已经有一个 pytest 会话握着 …` ⇒ "跑不动"与"跑不绿"从此分得开。
  上一基线 1054/1063 → 本批 1067/1076：+13 条，分布在 `test_script_db_guards.py` +5
  （危险语句**换个位置/换个深度**仍要被点名（赋值、`if`、推导式、`with`、嵌套 `def`、
  `os.system`）+ 三条"正常写法不许误伤"的对照 / 递归扫描与"空目录不许报干净" /
  测试用的名单就是运行期那份 JSON（行为判据：登记进临时 JSON ⇒ 运行期那道闸跟着放行）/
  `--dry-run` 的自报行不许说"会发 DDL" / async 引擎与实例属性里的散落连接），
  `test_sweep_restore_owner_immunity.py` +4（还原默认 dry-run、缺 `created_at` 一行净值都不清、
  清单点名字段过白名单、`--apply` 缺确认词必须**在连库之前**退出），
  `test_database_label_targets.py` +1（主机只写在 `?host=` 里、多主机列表、
  "认不出主机"那一档不许冒充本机），`test_doc_claims.py` +2
  （流水段里"没判几条"必须印出来 + 尺子拿不到时要 skip 不要 fail），
  `test_sector_identity_audit.py` +1（**用例产物不许堆进 `docs/`**：`write_manifest` 的两条用例
  以前把回滚清单写进仓库里那份真清单的同名目录，断言中途红就不清 —— 本机实测漏过两份
  `sweep-manifest-pytest-realign*.json`；现在 `OUT_DIR` 用 `monkeypatch` 钉到 `tmp_path`，
  闸自带"现造一份泄漏必须被点名"的控制，撤掉重定向实测 2 条一起红），
  `test_database_label_targets.py` 与守卫侧同步扩了**主机**这一维的样品池。
  **这一轮被两份评审共同打脸的，是上一轮我写在"当前测试基线"里的那三句撤谎话本身**：
  "方向只认值"当时的实现是"表达式里**出现过** sqlite 字样" ⇒ 一条三目就能买到（A-M1/B-M3）、
  "每条 return 都是 sqlite"漏掉 `except` 那条出口（A-M2）、
  "同一条分支要可达且排在危险动作之前"里，可达只问到"函数名有没有被写过"、
  顺序只比"同一个最内层函数里的行号" ⇒ DDL 抽进 helper 就绕过（A-M4）。
  现在这四条各自有样品钉死，另加：`_is_dead_test` 改**求值**、顺序改走**调用图**（行号元组字典序，
  第一版用累加小数把顺序弄反过，被真护栏对照组当场点红 ⇒ 又是"控制断言抓自己"那一族）。
  **还有一条要记的**：B 席建议把文档流水账"按句判"，我照做后立刻假红一处
  （AGENTS 基线那段是整条链，切句把"新增 3 条"变成当场账：文档写 3、当场 35）
  ⇒ 建议的修法**实测驳回**，只保留它要的另一半（没判的条数必须印出来）。）
  （上一基线 1041/1050 → 本批 1054/1063：+13 条，分布在 `test_script_db_guards.py` +5
  （守卫那条分支必须**可达**且**排在危险动作之前**（死路样品 + 先动手后拒跑样品）/
  "方向"只能来自**值**（函数每条 `return` 都是 sqlite 才算，按名字猜一律不算）/
  借道子进程那份名单不许是**过期闸门**（现造一条借道调用必须被点名）/
  动态 `importlib` + `exec` 买的透明性必须被点名（含"别拿 importlib 读文件"的过宽对照）/
  `run_migrations` 问迁移闸门排在**建连接与导入 ORM 之前**），
  `test_sweep_restore_owner_immunity.py` +3（新文件：还原默认拒绝盖回老板免疫 /
  显式旗子才还原 / manifest 里一处免疫都没写时不许虚报"我拦下了几行"），
  `test_database_label_targets.py` +1（PostgreSQL 三档：本机 / Supabase 生产 / 别的远程，
  含"子串混进生产档"的对照与"三档必须真的互不相同"的控制）、`test_doc_claims.py` +1
  （文件名在行末、数在行首的**软换行**必须仍然被对账，且 `--fix` 改的是数所在那一行；
  反向对照：上一句已用 `。` 收口 ⇒ 两句不许拼成一条承诺）、
  `test_read_only_door.py` +1（两份 L3 报告的日期各自都要过现算那把尺子 —— 以前只读了一支）、
  `test_push_writeback_gate.py` +1（`--base` 未知时那行目标不许自称线上生产库）、
  `test_purge_junk_funds.py` +1（`--production` 有牙：旗子与实际连接不一致 ⇒ 退 4，
  并用 AST 钉"这道判在第一次 commit 之前"）。
  **本批改了我自己上一轮写的三句谎话**：① "`del sys.modules[...]` 那条绕法作废" ——
  散落连接扫描按 `__name__` 跳过 `src.models.database`，而 `del` 之后那个对象的 `__name__`
  还在 ⇒ 我要堵的路自己留着门（现在只有"确实还挂在那个键下"才交给 `already_built_url()`）；
  ② "豁免共五条来源" —— 我这一批自己加了第六条（`sweep_sector_mappings.py
  --restore-owner-immunity`）而那句话当场过期 ⇒ 文档与用例的注释都不再抄这个数，
  真值就是 `IMMUNITY_GRANT_SITES` + `IMMUNITY_OPAQUE_SITES` 两张表；
  ③ "守卫信号改成判同一条分支" —— 同一条分支如果不可达、或者排在 `upgrade()` 之后，
  等于没判（这正是第 45 轮两份报告独立点到的同一层）。
  还有一条关于**我自己写判据**的：我用子串断言"不许说线上生产库"，而"（…，不是线上生产库）"
  里就含那四个字 ⇒ 对的话被读成谎话；改成判"这一档的开头是什么"。）
  （上一基线 1029/1038 → 本批 1041/1050：+12 条，分布在 `test_script_db_guards.py` +5
  （"赋 DATABASE_URL"方向不明时不再算守卫，含现造样品与对照组 / 钉库前先看**别的模块**还连着谁，
  含"怪对象不许把守卫弄崩"的对照 / 只 import 一个 service 也算碰库（走 import 图，配一棵临时 src）/
  git 不可用时退回磁盘那一支自己跑一次 / `alembic/versions/*.py` 从此在扫描范围内：
  upgrade 里删结构要登记，现造的坏形状必被点名、正常迁移不许误伤（形状清单在那条用例里））、
  `test_read_only_door.py` +2（L3 报告的日期必须现算：子进程真问一次 + 全文件不许有写死日期，
  并当场把日期抄回字面量证明尺子会响 / 只读门的 `ATTACH` 侧门：门内写侧库要红、门外普通连接要绿）、
  `test_database_label_targets.py` +1（key=value 写法：**摘掉口令但留下 host 与库名**，
  只剩口令时要说"隐去了"而不是回显原串）、`test_doc_claims.py` +1（`--fix` 只当代改条数：
  该改的改了、不该动的字节不动、回执两笔账各报各的、第二次跑必须零改动）、
  `test_push_writeback_gate.py` +1（`_target_line` 必须真的被 `print` 出来：
  现造一个"算了但没说"的形状必须判为没自报）、`test_drop_probe_residue.py` +1
  （`declared_tables()` 这把 oracle 自己没人测过 ⇒ 拿 AST 读 `__tablename__` 与元数据逐字对表）、
  `test_review_ownership_and_matching.py` +1（写死"老板已确认"的**授予点**从此有棘轮：
  写死授予值的拼写都要认、搬运已有值不许误伤；下一轮把形状清单补全，见上面那条棘轮）。
  **本批没动 `web/`**，所以前端那 104 处变异不需要重跑（原始日志随仓库走：
  `docs/迭代计划/run-20260924-mutation/round43-frontend-mutations.txt`）。
  **这一轮最值得记的一条**：新加的"探测散落连接"守卫把自己弄崩了 —— 它对 `sys.modules`
  里每个值取 `vars()`，而 Windows 上那里混着 `ctypes` 的 `kernel32.dll` 对象，
  `ffi.error: symbol 'RtlNtStatusToDosError' not found` 直接让 `pin_local_sqlite()` 退出，
  **12 条** in-process 用例一起红（`test_audit_fund_info_identity` ×4 +
  `test_sector_seed_route_honesty` ×2 + `test_seed_owner_proxies_gate` ×4 +
  `test_snapshot_prod_mappings.py` ×2，这份分布是从当场失败清单里读的，不是回忆）；
  同族第二起：第一版按"有没有 url 属性"过滤，而 `sqlalchemy` 包自己有个叫 `engine` 的**子模块**
  ⇒ 每个导入过 sqlalchemy 的进程都被判成"绑在远程库上"。⇒ **每加严一道守卫，
  都要当场跑一遍全量 in-process 用例**，只看新用例绿不绿看不见它把别的调用方打死了。）
  （上一基线 1019/1028 → 1029/1038：+10 条，分布在 `test_drop_probe_residue.py` 新增 4 条
  （探针残渣工具：报告模式不改库 / 没口令不删 / 有行的表让整批不删、改完名才删且不动别的表 /
  模型声明过的名字绝不许当残渣删）、`test_script_db_guards.py` 新增 3 条
  （说明文与样板句买不到守卫信号（含两个真护栏对照组）/ 落笔能力逐类有牙（含"只读不许被判成能写"的
  过宽对照）/ 解析失败兜底 dict 的键集合不许与 `_facts` 漂开）、
  `test_database_label_targets.py` 新增 1 条（手写剥口令的**只许变短**棘轮，自带"现造一处违规"控制）、
  `test_doc_claims.py` 新增 1 条（"指向收不到的文件"必须算进退码 + 同一形状收得到时必须退 0）、
  `test_push_writeback_gate.py` 新增 1 条（`--base` 指到非生产域名时不许出现"线上生产库"那四个字）。
  那一批自己被抓出来的两条（这一族的标准死法，仍然有效）：
  ① 那道棘轮的第一版按文本 grep ⇒ 我被"解释这句的 docstring"自己点红；改成判 AST 后第二版仍然**恒空**
  —— `x[-1]` 的 slice 既不是 `ast.Slice` 也不是 `Constant(-1)`，而是 `UnaryOp(USub, Constant(1))`；
  两次都是同一条"现造一处违规必须被点名"的控制断言抓出来的 ⇒ **没有控制断言的判据等于没有判据**。
  ② 手抄的"一律 abort 会打死 8 条正经用例"数对但**归错了文件** ⇒ 已在两处更正并写明复核方式。）
  （再上一基线 1000/1009 → 本批 1019/1028：净 +19（新增 20 条、把一条已被替换的
  `test_delete_blogger_tells_four_different_endings_apart` 删掉），分布在
  `test_script_db_guards.py` +3（钉库来得太晚必须 abort / 已建在 SQLite 只许警告并说清写的哪个文件 /
  受检集合由 `git ls-files` 定义，本机 `_tmp_*` 草稿不许改变"全绿"的含义）、
  `test_database_label_targets.py` +2（副本与夹具不许被报成"本地镜像库" / src 侧与守卫侧两份
  `machine_name` 逐条相等）、`test_doc_claims.py` +6（`数据源：`那一行认不出库要红 /
  承认"未记录"却不给复现命令也要红 / `backtest_l1_weighting.py` 这种脚本名不许被切成
  `test_l1_weighting.py` 误报）、`test_read_only_door.py` +6（`--help` 与坏旗子跑完 `docs/`
  一个字节都不许变 / 先定镜像后又要求 `--production` 必须说破 / 探针残渣与自报顺序那几条）、
  `test_frontend_cold_start.py` +2（失败横幅必须在"列表非空"那一支可达 / 删除博主的四种结局
  由真实状态驱动，且先证明那条分支可达）、`test_sector_mapping_audit_import.py` +1
  （`unchanged` 行一个字都没写，不许进"本次已照样写入"那个桶）。
  本批前端 4 处变异全 RED：`python scripts/mutation_proof_frontend.py --only bloggers_notice_` /
  `--only stale_rows_never_counted` / `--only refresh_death_blamed_on_the_delete`；
  **全套 104 处本轮逐条跑过**（原始输出随仓库走：
  `docs/迭代计划/run-20260924-mutation/round43-frontend-mutations.txt`——
  别指 `data/_mutation_round43.log`，`.gitignore` 有一条全局 `*.log` 而 `data/` 整目录不入库）：
  CONTROL-GREEN + 101 RED + 3 处 ANCHOR-MISS），那 3 处 ANCHOR-MISS 已修锚点并各自复跑为 RED
  —— 修的是锚点不是判据：`pagination_reports_zero_on_failure` /
  `predictions_pager_claims_fresh_rows` 的锚点还停在"翻页条直接印 `total || 0`"的旧形状，
  `unexport_numOrDash` 停在加 `bloggersStale` 之前的导出名单 ⇒ **ANCHOR-MISS 必须当失败处理**，
  它意味着"这一条判据有没有效"当场没答上来（条数一律看 `--list` 末行）。）
  （上一基线 984/993 → 1000/1009：+16 条，分布在 `test_read_only_door.py` 新增 5 条
  （**只读**连库口：没给旗子必须钉镜像 / 给了 `--production` 才走线上 / `mode=ro` 的引擎写不进去
  而普通 URL 写得进（对照组）/ 探针在可写连接上必须报警 / 三个 L3·L1 脚本真走了这把门）、
  `test_script_db_guards.py` 新增 3 条（读侧触发器 + 触发器正反两侧现造样品 +
  "`import src.*` 必须排在定库之后"）、`test_doc_claims.py` 新增 4 条（文档条数账的尺子本身）、
  `test_frontend_cold_start.py` 新增 3 条（失败提示在**列表非空**那条分支可达 /
  `fetchBloggers` 数得出表上挂着几位 / `deleteBlogger` 四种结局各说各话）、
  `test_sector_mapping_audit_import.py` 新增 1 条（写失败的行不许同时进"定不了价"桶）、
  `test_alembic_target_direction.py` 拆出新增 1 条（自报措辞按放行依据分叉，且每条都得是真的
  `print("[库]` 语句）。）
  （上一基线 976/985 → 本批 984/993：+8 条，分布在 `test_alembic_target_direction.py`
  （"只给一道远程旗子、目标来自 `.env`"必须仍拒跑）、`test_script_db_guards.py`
  （三条**机器无关**的重写：临时目录现造 `_x.py` 验前缀豁免 / 现造语法坏的文件验 fail-closed /
  用仓库真在用的 `urlopen(Request(..., method='POST'))` 验 HTTP 触发器）、
  `test_push_writeback_gate.py`（两路剔除都要计数、整批被拒不许发真写）、
  `test_sector_mapping_audit_import.py`（**路由级** dry-run 必须报出总开关没开 —— 以前只有内部函数判据）、
  `test_frontend_cold_start.py`（evidence 失败要放下旧区间 + 四个体检按钮的失败守卫）、
  `test_verdict_evidence_badge.py`（批量写判据的六格样品：列对象当 key 要认，
  只出现在筛选条件里不许误伤）。
  ⚠ 本批还有一条操作事故要记：我写判据时在 bash heredoc 里用了嵌套三引号，字符串提前截断
  把 `_WRITER` 那几行**当成真代码执行**了一次（`SessionLocal()` 建了会话、`db.add(1)` 当场抛
  `UnmappedInstanceError`）—— 没 flush、没 commit、没连库，但它正是"一次性代码不钉库"的形状。
  同类第二起：我把三条新变异插进体检脚本时转义写错，**把 `scripts/mutation_proof_frontend.py` 写成了
  语法错误** —— 而第 39 轮刚加的"解析失败的文件按能写处理"立刻把它报成 `write_capable=True` 并让判据变红
  ⇒ 那条新判据是活的（这也是它第一次在真实场景里起作用）。）
  （上一基线 967/976 → 本批 976/985：+9 条 = `test_alembic_target_direction.py` +4
  （第二个 ini 指远程也拒 / 一个环境变量解不开第二道旗子 / 两道旗子都给了必须自报且自报走 stderr /
  任何往下走的分支都得报 `[库]`）、`test_seed_owner_proxies_gate.py` +1
  （`ok=True` 但官方名为空 ⇒ 同样不写）、`test_script_db_guards.py` +3
  （两个新触发器的合成判据 / `_` 前缀不再是整族豁免 / 解析失败的文件必须 fail-closed 而不是崩扫描器）、
  `test_push_writeback_gate.py` +1（服务端早期拒收某行 ≠ 对端是旧构建，不该锁死整批）。）
  本批新增/复核的 4 处前端变异全 RED：`--only _title`（3 处）与 `--only caliber_note`（1 处）。
  变异与判据的条数**一律跑命令看末行**：`python scripts/mutation_proof_frontend.py --list`
  （第 39 轮另修掉一处证据机器自身的洞：`--only` 打错字以前是"0 处变异、CONTROL 全绿、退码 0"
  ＝**满分通过一次什么都没测的体检**；现在匹配不到就失败，且 CLI 换成真 argparse——
  它顶部曾 `import argparse` 却从不调用，`--help` 会让它整套开跑就地改写 `web/`）。
  （上一基线 955/964 → 本批 967/976：+12 条 = `test_alembic_target_direction.py` 新增 4 条
  （远程 + 裸 CLI 必须拒跑 / 本地 sqlite 仍继承 / 显式 `ALEMBIC_DATABASE_URL` 覆盖 / 已交连接的启动路径不受影响）、
  `test_seed_owner_proxies_gate.py` +2（探针说取不到 ⇒ 0 行 0 档案；"桩的键 == 真返回的键"）、
  `test_sector_seed_route_honesty.py` +1（拿真脚本真 stdout 喂真解析器，未知列不许被静默丢掉）、
  `test_push_writeback_gate.py` +2（旧版服务端不回 `nav_priced_here` / 只答一半行数 ⇒ 一行都不发）、
  `test_script_db_guards.py` +1（DDL 动词集：`downgrade`/`stamp`/裸 CLI 也算）、
  `test_frontend_cold_start.py` +2（两个列表页的 12 个数不许报 0；三个预测队列的口径不许只活在 title）。）
  本批新增的 6 处前端变异各自 RED：`python scripts/mutation_proof_frontend.py --only lose_their_guard`
  ／ `--only init_back_to_zero` ／ `--only stops_being_a_value_card`（条数一律看 `--list` 末行，别抄文档）。
  上一批（948/957 → 955/964）另外抓到两条**关于"怎么跑"**的坑：
  ① 断言子进程的中文输出时，父进程不设 `PYTHONIOENCODING` ⇒ 子进程按 cp936 写、测试按 utf-8 读，
  那行明明印了却解成一串替换符 ⇒ **假红**（`test_prediction_migrations.py` 的
  `_run_the_migration_script` 现在 `setdefault` 了 utf-8；这条坑以前只写在文档里，没有机器闸）。
  ② 满负载时 Windows 会把刚启动的子进程打死（退码 `0xC0000374` = STATUS_HEAP_CORRUPTION，
  stdout/stderr 全空）。A/B 证明与本次改动无关（把那两行删掉，3 次仍崩 1 次）；
  现在的处理是**只对这种"被系统打死"的退码重跑**，脚本自己返回 1 的失败一次都不许多试。
  （上一基线 948/957 → 本批 955/964：+7 条 =
  `test_database_label_targets.py` 新增 4 条（Session / Engine / Connection / 认不出的一族）、
  `test_script_db_guards.py` +1 条（"赋值过 `DATABASE_URL`"不等于守卫，发 DDL 的脚本不认它）、
  `test_prediction_migrations.py` +2 条（只重试被系统打死的退码 / 崩溃退码怎么认）。）
  （上一基线 929/938 → 948/957：+19 条 =
  `tests/unit/test_sector_seed_route_honesty.py` 新增 10 条（seed 后门：默认关 / 确认头 / 看退码 /
  无回执算失败 / cwd 指得到真脚本 / 成功要刷缓存 / 脚本拒写 / 只补缺不覆盖 / dry-run 不写）
  + `test_seed_owner_proxies_gate.py` 新增 5 条（`--owner-confirm SEED-PROXY` 闸，含"闸排在钉库之前"；今天该文件 8 条，数一律 `grep -c '^def test_' <文件>`）
  + `test_sector_mapping_api.py` +2（`batch-review` 路由级转发 `owner_confirm`、`/verify-fund` 探针形状）
  + `test_frontend_cold_start.py` +2（汇总统计三种失败形状、模板祖先链「确认执行」）。
  判据与变异数**一律跑命令看末行**：`python scripts/mutation_proof_frontend.py --list`
  （末行印"共 N 处变异，覆盖 M 条判据"；文档里不抄这个数），全量逐条 RED（其中两条先报 GREEN/ANCHOR-MISS、
  修完判据与锚点后各自复跑为 RED）。）
  （上一基线 885/894（红过一次：897/**1 failed**/16）→ 912/921：+14 条 =
  清理脚本离线往返 6、`/api/bloggers/top` 路由形状 3、互斥闸反向 2、失败态铺满 3。）
  （上一基线 885/894 → 本批 898/907：+13 条 = 前端失败态 7 条（`test_frontend_cold_start.py` 16→23）、
  变异体检互斥闸 5 条（`test_mutation_lock.py` 新文件）、"只面向生产的白名单不是后门" 1 条。
  **这批中途出过一次真事故**：`tests/conftest.py` 顶层的一条 import 排在钉库之后写反了位置，
  4 条夹具把数据写进了生产 Supabase ⇒ `test_database_url_routing.py` 抓到（详见上面 conftest 那条规矩），
  期间 `tests/unit` 一度报 4 errors + 1 failure —— 那条 failure 就是闸门本身在响。）
  （再上一批 866 —— 那一批把 `tests/` 口径欠了一次实测，本批两个口径都实测过。
  再往前 835 那一批后来被证明**是红的**：两条用例 09-22 23:39 测完全绿，
  跨过北京零点后因凭据时间戳自己变红（见下面"凭据写侧"那条）。**基线数字必须带日期与时刻**。）
  （再早 808/817 → 819/828。**同一个错我连犯两轮**：写完基线数字后又加用例却没复测。
  规矩改成：最后一次改用例之后先跑数、再写文档，数字与用例改动必须在同一个提交里。）
  （更早的基线 776 / 785；那一批 +19 条新用例、删掉 2 条打在已删死路上的用例。
  第 24 轮评审抓到的是我自己：`AGENTS.md` 里写着 `tests/ 765` 却小于实测的 `tests/unit 776`
  —— 超集不可能比子集小，说明有一行是填数时没跑。）
- **静态板块表 `SECTOR_FUND_MAP` 现在有用例盯着了**（第 27 轮）：改这张表必须同时跑
  `python scripts/audit_static_sector_map.py --fixture tests/fixtures/sector_map_roster_snapshot.json`
  （退码 0 才算干净，判据是"板块↔**名册官方名**"，不看手写标签）与
  `pytest tests/unit/test_sector_map_guard.py -q`；新增/改代码后要 `--emit-fixture` 刷新夹具、
  并跑 `scripts/sync_sector_map_funds.py --apply --confirm SYNC-MAP-FUNDS` 确认新代码取得到净值。
  三条硬规矩：① 非字面命中的条目必须登记进 `SECTOR_PROXY_ALLOWED` 并写理由（登记了其实字面
  命中的也算死条目，会红）；② "名册里没有对口基金、宁可交给 agent"的板块要写进
  `SECTOR_NO_STATIC_FUND`——**只把键从表里删掉不够**，第 5~7 步子串模糊匹配还会把它吸到
  别的板块的标的上（实测 `卫星互联网 → 517200 互联网ETF`），而 `normalize_sector_name` 同样会
  因此改写 `sector_core`；**第 28 轮又补一层：只写精确串也不够**，`卫星互联网产业` /
  `A股互联网平台` 这种"长一点的说法"照样绕过名单、仍被吸到 517200。现在判据是
  `_literal_block_hit()`（名单词命中长度 ≥ 表内命中键长度 ⇒ 按屏蔽，"最具体的一方说了算"），
  用例两侧都钉：既测"长名字绕不过"，也测"109 个表键一个都不许被误挡"；
  ③ 表里的 `name` 必须逐字等于名册官方名（`--fix-labels` 可代改；该脚本现在有跳过就退码 5，
  不再"报着跳过却算成功"）。
  两根轴上还有三处**别再说满话**的地方（第 27 轮两份复评各抓到一处，第 41 轮 A 席又数出第三条）：
  ④ "官方名与板块字面相关"里混着**只共用一个汉字**的弱命中（2026-09-23 实测字面过关 99 条里
  **13 条**，如 `建材→基建ETF`、`家居→家电ETF`；`relevance_kind()` 分 `core`/`char`，
  报告与 CSV 单列，条数钉在 `test_weak_literal_hits_are_labeled_as_weak`）⇒ 别说成"全部已核对"；
  ⑤ D1 有两条腿（夹具 `d1_words` 与 `by_code`），变异实验证明任一条断掉当时 16 条用例全绿 ⇒
  两条腿各有用例，且 `main()` 里那次 `classify(...)` 必须显式带 `d1_words=`（AST 检查）；
  ⑥ 弱命中不能因为"字面算相关"就不去查更同名的标的（第 28 轮 I-MAJOR）：旧写法
  `if not relevant:` 让 13 条弱命中里 **11 条**"名册里另有含整词的同名基金"永远隐身
  （`建材→基建ETF` 而名册里有 `159745 建材ETF国泰`）。现在这类行单列 **R1_弱命中有同名** 桶
  （`classify` 只对 `core` 档跳过查找，其余都查），条数同样钉在用例里。
  **R1 不计入退码**（换标的会动到 911 条活预测的解析方向，是判断不是 bug 修复）⇒
  逐行处理清单见任务 #38。
  这张表"管多少条预测 / 这一轮动了多少条"一律用 `python scripts/measure_static_table_reach.py`
  现量（2026-09-23 镜像：**活预测 1616 条，只被静态表覆盖 911 条 / 50 个板块**；
  `--impact-against tests/fixtures/sector_map_before_round27.py` 量影响面（上一版表已钉进 fixtures —— 第 35 轮 B 抓到原来那条命令指向 `data/_old_map.py`，而 `data/` 整目录不入库，命令当场 FileNotFoundError）：**31 个板块（改码 18 / 删键 13）= 78 条 = 4.8%**）。
  以前文档里写过的"916 条""124 条 / 7.7%"都是手抄没绑口径，已撤回（同一个数被复现成 925/911）。
- **只读脚本也要答"连的是哪个库"，而且要走同一把门**（第 41 轮 B-MAJOR-1：读侧以前不算攻击面）：
  `scripts/_db_guard.py` 现在有两件东西 —— `resolve_read_target()`（**定库但不建连接**，
  默认钉本地镜像、命令行出现 `--production` 才用 `.env` 那条）与 `read_only_connect()`
  （在它上面建**引擎级只读**连接：pg 走 `postgresql_readonly` 执行选项 + 真试一次写临时表的探针，
  sqlite 走 `file:…?mode=ro&uri=true`；探针不通就 abort），并且第一行自报**机器名**
  （`machine_name()`：sqlite 给文件路径、远程给 `scheme://host/db`，口令一个字符都不出现）。
  为什么必须有这把门：`audit_l3_clear_labels.py` / `estimate_l3_vague_labels.py` /
  `backtest_l1_weighting.py` 以前都写 `create_engine(os.getenv("DATABASE_URL"))`，
  而 `.env` 里那条**就是生产 Supabase** ⇒ "跑一下 L3 估算"默认读线上，
  还把结论连同一个只写着 `"DATABASE_URL"` 的标签落进 `docs/` 报告（变量名不告诉你连的是哪台）。
  同一条改动里另外两件事：① 守卫扫描新增 `engine_from_env` 触发器（"读了 `DATABASE_URL` 又自己
  `create_engine`"＝受管，四选一守卫信号才算过；`create_engine('sqlite:///固定路径')` 那种副本不算）；
  ② `import src.*` **必须排在定库之后** —— 判据走可达性（`src/services/l1_weighting.py:16` 写着
  `from src.models.database import Prediction`，导入它的那一刻全局 `engine` 就按当时的
  `DATABASE_URL` 建好了，之后再 `pin_local_sqlite` 只是改环境变量、救不回那个 engine；
  这与 `tests/conftest.py` 那条第 33 轮的规矩同源）。
- **守卫信号必须是"代码在做这件事"，说明文与样板句不算**（第 43 轮 A-MAJOR-1）：
  上一版 `_facts()` 用 `ast.walk` 把所有字符串常量收走，而 **docstring 就是一个 `Constant` 节点**；
  `raised` 又只看异常名，于是"在 docstring 里写 `postgres` / `[目标]` / `postgresql_readonly`
  + 保留入口那句 `raise SystemExit(main())`"就能让三个识别器一起点头 —— 而其中一个识别器的
  docstring 逐字写着"写在注释或 docstring 里不算"。现在：docstring 整段剔除
  （`_docstring_consts`），"会拒跑"必须是**条件分支里**"印了 `[abort]` 并且停下来"
  （raise 或 `return 4` 都算，只认 raise 会误伤 `sync_db_columns.py` 这一类真护栏）。
  判据：`test_prose_cannot_buy_a_guard_signal`（三个"只有说明文"的样品 + 两个"真护栏"的对照组）。
- **落笔的能力要按类别枚举，不按名字匹配**（第 43 轮 B 的"能力×判据"表）：两份评审独立指出
  守卫只认"SQLAlchemy + 顶层 import + 字面量旗子"这一种形状。`_facts()` 现在有一张
  `capabilities` 表：`create_all` / 裸 SQL DML / DB-API 直连（`sqlite3.connect`、`psycopg2.connect`）/
  `df.to_sql(if_exists='replace')` / 覆盖 `.db`·`.env` 的文件操作 / 子进程借道 alembic·run_migrations /
  `requests.request('DELETE')` 这种通用入口，外加"函数体内 `from src.models.database import SessionLocal`
  的**纯读**脚本"（B-MAJOR-2：读侧触发以前只看 `engine_from_env`，ORM 会话这条路三道判据一条不响，
  而 `.env` 默认就是生产）。每类一个合成样品，由
  `test_write_capability_is_judged_by_what_a_script_can_do_not_by_its_names` 逐类钉"会被抓"，
  并配一个"只读查询不许被判成能写"的过宽对照。**别名也是同一族**（B-MAJOR-1）：
  `from sqlalchemy import create_engine as ce` 以前一个词就隐身。
- **`run_migrations.py` 现在有 argparse**（第 43 轮 B-BLOCKER，是同一个缺陷类的第三次）：
  评审员为了"看这脚本有什么参数"跑了 `python scripts/run_migrations.py --help`，
  当时没有参数层 ⇒ 两句都直接执行 `command.upgrade(head)`，而 `.env` 就是生产。
  **当场只读核对生产**：`alembic_version = add_sector_mapping_keywords`（＝仓库唯一 head）
  ⇒ 那两次是 no-op、没有 DDL 落地（`python scripts/q.py --production "select version_num from alembic_version"`），
  但它确实取放了一次 advisory lock —— 这笔账记在 `docs/迭代计划/S6-上线前检查单.md` §0。
  现在：`--help`/坏旗子在连线前就退出，`--dry-run` 只报目标（退 2），**无参数仍然照旧迁移**
  （`render.yaml:11` 的 startCommand 就是无参数那一支；把默认改成要确认要动部署配置 = 任务 #57，老板决定）。
- **自报行必须排在动手前面**（B-MAJOR-4）：`_db_guard` 导入时把 stdout/stderr 改成
  `line_buffering=True` —— stdout 进管道（Render 日志、`> log`、cron）时是块缓冲，而 alembic 走
  stderr，"我要动哪个库"那一行会排到它承诺领先的那件事后面，进程被杀时一个字都看不见。
- **`数据源` / 残渣 / 报告日期三处小账**（B-MINOR-2/6、A-MINOR-7/8）：
  ① 两份 L3 报告的"日期"以前是模板字面量（重跑一遍数字全变了、日期还盖着两个月前）⇒ `_today_beijing()`；
  **第 44 轮 A 指出这句话当时只是一条承诺、没有判据**（把日期改回字面量，全套件仍然全绿）⇒
  现在两腿都有闸：子进程真问一次 `_today_beijing()` 必须等于今天的北京日期，
  且脚本里除 docstring 外不许出现写死的 `YYYY-MM-DD`（并当场把日期抄回去证明尺子会响）；
  ② 真镜像里躺着第 41 轮旧探针留下的 `_db_guard_probe2`（0 行、全仓 0 处引用），
  新增 `python scripts/drop_probe_residue.py`（默认只报告退 3；`--apply --confirm DROP-PROBE` 才删；
  只碰 SQLite、只删"0 行 + 模型没声明"的探针名字），2026-09-24 已用它清掉镜像那张；
  ③ 104 处变异的原始日志以前只在 `.gitignore` 的 `data/` 里 ⇒ 干净克隆上没人能复核，
  现在随仓库走：`docs/迭代计划/run-20260924-mutation/round43-frontend-mutations.txt`。
  （**第一版我把它存成 `.log` 就直接写进文档了** —— `.gitignore:47` 有一条全局 `*.log`，
  那句"随仓库走"当场是假的；改名之后用 `git ls-files` 核过才算。教训：**说"入库了"要拿
  `git ls-files` 核，不是看文件在不在磁盘上**。）
- **"报哪个库"这把尺子自己有两个出口，第 44 轮一起堵上**（A-m6 / B-m1 / B-m2 / B-m6）：
  ① 两份 `machine_name()` / `target_name()` 对**没有 `://` 的连接串**是"原样返回"，
  而 libpq 允许 `host=db.example.com user=u password=真口令 dbname=proddb` 这种 key=value 写法
  ⇒ 自报行会把口令整条印进 stdout / Render 日志 / `docs/` 报告（认不出 scheme 的那一支
  也是回显原串）。现在这种串只留 `host`/`port`/`dbname` 三个不涉密的键，
  scheme 只在"长得像 scheme"时才印；样品已进笛卡尔积（`test_a_key_value_dsn_is_reported_without_its_credentials`
  两头都钉：口令不许出现，**主机与库名必须还在** —— 只测"没泄露"会退化成"什么都不报"）。
  ② 只读门的 SQLite 腿以前只有 `mode=ro`，而 **`mode=ro` 只锁主库**：同一条连接
  `ATTACH` 一个可写文件、往**那个库**建表照样成功（当场跑通）。现在探针通过之后再补
  `PRAGMA query_only=ON` 并 `engine.dispose()`。**顺序不许反**：先开 pragma，
  可写连接上的探针也会被它拒绝 ⇒ 那道"数据库自己说不许写"的 fail-closed 校验变成恒真自检。
  ③ 类别词不许硬写：`purge_junk_funds.py` 的 `[target]` 改印 `db_kind()`
  （`LOCAL_DB_URL` 指到副本时它以前仍自称"本地镜像库"）；
  ④ `push_sector_mappings_to_prod.py` 认生产改成**逐字等于**已知主机：旧的是子串匹配
  （`onrender.com.attacker.example`、`notonrender.com` 都能自称"经 HTTP 写线上生产库"），
  而我第一版改成"或它的子域"是**另一个方向的过头** —— Render 一个共享后缀压着别人的应用，
  那不是我的生产。判据用 `hostname`（自动去端口/userinfo/转小写），显示仍用 netloc。
- **"这是哪个库"今天说四档，认不出主机就必须说认不出**（第 46 轮 B-M6 / A-m1）：
  第 45 轮的三档（本机 / 本项目 Supabase / 别的远程）有个致命的默认 —— **看不见主机就当本机**。
  而 `postgresql:///postgres?host=aws-0-x.pooler.supabase.co` 是 SQLAlchemy 的正规写法之一
  （主机只出现在 query 里），两把尺子于是**一致地**把线上库印成
  `本机 PostgreSQL（…，不是线上生产库）`；"两把尺子逐条相等"那条判据对这种错永远绿 ——
  **一致地错比不一致更危险**，因为唯一盯着它的用例正好是那条相等检查。
  现在：`_db_hosts()` 同时看 netloc 与 `host=`/`hosts=` 参数，并按 `,` / 空格切成**候选列表**
  （多主机列表是 libpq 的故障转移写法，整串当一个名字 ⇒ `a.supabase.co,b.backup` 混不进生产档）；
  任一候选命中生产域 ⇒ 生产；全部候选都是回环/私网 ⇒ 本机；**一个候选都没有 ⇒ 第四档
  "认不出主机，不敢说它是本机还是线上生产库"**。键值写法（没有 `://`）也进这四档（libpq conninfo
  按定义就是 Postgres）。
  同一轮补两条口令面：`sqlite:///u:S3cr3tPW@/db` 原样回显（sqlite 那一支以前不剥 userinfo）、
  `postgres:///password=S3cr3tPW`（口令落在**路径段**、没有 `?` 分隔）也原样进日志 ——
  现在两边都过 `_redact_secrets`：**"口令一个字符都不出现"这条不变式不分位置、不分方言**。
  反向边界也钉了：`data/mail@copy.db` 这种合法文件名不许被剥坏。
- **`--production` 这把旗子要问"是不是那台"，不是"远不远"**（第 46 轮 A-m3 / B-M12）：
  `purge_junk_funds.py`（S6 那把**硬删** `fund_info` 的脚本）的方向闸上一版只要求
  "非 SQLite 且非私网" ⇒ MySQL、别人暂存环境里的一台公网 Postgres 都满足 `--production`，
  而同一批刚写好的 `_is_the_production_host()` 就在同一个模块里没被调用。现在必须命中
  那张已知生产域表，否则退 4，并把"要怎么改"说清（加进尺子的域名，而不是在删数据的脚本上调串）。
- **`sweep_sector_mappings.py --restore-from` 今天才有门**（第 46 轮 B-M8 / B-M9，我上一轮刚动过这条函数）：
  文件头第 8 行一直写着"默认 dry-run，`--apply` 才写库"，可还原那条支路是
  `if args.restore_from: return restore(...)` —— 绕过 `--apply`、没有确认词，直接 `setattr`
  之后 `db.commit()`，末尾还 `FundHistory.delete()`。现在：默认 dry-run 只报"将写几行、将清几行"，
  真写要 `--apply --confirm RESTORE-SWEEP`，且这道检查排在 `pin_local_sqlite()`/`SessionLocal()`
  **之前**（用法错不该先连一次库）。另一半更贵：`created_at` 缺失/被截断时以前是
  `if since is not None: 加过滤器` ⇒ "我不知道从哪天起"被翻译成"全删"，
  而净值历史**不可再生**（每日同步只回补最近 30 天）；现在拿不到下界就一行都不清并说出来。
  清单是外部输入 ⇒ 字段名过 `MANIFEST_FIELDS` 白名单（以前写什么就 `setattr` 什么，含 `id`）。
- **钉库守卫在钉之前先问"进程里还有谁连着别处"**（第 44 轮 B-MAJOR-6）：
  只查 `sys.modules['src.models.database']` 是被 `del sys.modules[...]` 绕过的 ——
  调用方手里那个 `engine` 对象不会因此松开。现在 `pin_local_sqlite()` 之前把所有
  **本仓库的**模块（名字以 `src` 开头，或 `__file__` 落在仓库内）的 `engine`/`SessionLocal`/
  `async_engine` 属性都问一遍，绑在非 SQLite 上就 `[abort]` 退 4 并印出那台目标（口令不泄露）。
  **两处克制是被自己的故障教出来的**：① 只认 `type(x) is ModuleType` 并把 `vars()` 包进 try ——
  Windows 上 `sys.modules` 里混着 `ctypes` 的 `kernel32.dll`，对它取 `vars()` 直接抛
  `ffi.error: symbol not found`，第一版因此把 **12 条** in-process 用例一起打死（守卫自己成了故障源）；
  ② 先按 `type(obj).__module__` 是 sqlalchemy 才问 url —— `sqlalchemy` 包自己有个叫 `engine` 的
  **子模块**，`str(模块)` 既不含 `sqlite` 也不是连接串 ⇒ 每个导入过 sqlalchemy 的进程都被判"绑在远程"。
  边界要说明白：**函数局部变量里的 engine 引用这一道照不到**，那一半仍然只能靠
  "把钉库提到所有 src.* 导入之前"（上一条运行期守卫）与 AST 判据。
- **文档里"`test_x.py` N 条"这类当场账，有脚本对表**：`python scripts/audit_doc_claims.py`
  拿 `pytest tests --collect-only -q` 当场收集的条数去对 `AGENTS.md` / `DEPLOYMENT.md` /
  `docs/模块总览/*.md` 里的每个"N 条"承诺，不符就退码 3（`--fix` 就地改）。
  **两种数必须用两种写法**：说"这个文件现在有几条"就直接写 `N 条`（会被对账）；
  说"那一批加了几条"必须写成 `+N 条` / `新增 N 条`（脚本按写法跳过 —— 拿当场数去对增量数
  本身就是错的）。闸门：`tests/unit/test_doc_claims.py`（含"样品故意写错 ⇒ 必须判不符"的反空判）。
  本轮实测抓到两处漂掉的数：seed 那道闸文档写的数与当场数差了 3（当场以 `--collect-only` 为准），
  模块总览写的前者只有实际判据的一个零头 —— 两处都已改成绑命令的写法。
- **能写数据的脚本必须说清"连的是哪个库"**（第 28 轮 F-MINOR-6 起有用例钉）：
  `tests/unit/test_script_db_guards.py` 扫 `scripts/*.py`，判"能不能改数据"**只看 AST**（注释与
  docstring 不算；**边界**：AST 看得见的只有"形状"与**顶层** import 的顺序，函数体里的执行顺序
  它判不了 —— 那一半由守卫在运行期回答，见下面 B-MAJOR-4 那条。受检集合同样由 `git ls-files`
  定义，不跟着本机未入库的 `_tmp_*.py` 草稿变，第 42 轮 A-MINOR-3）：
  ① 直连 ORM 且代码里真 `commit/add/delete`；② CLI 带写开关
  （`--apply` / `--execute` / `--confirm …`）；③ **`from alembic import command` + 真调
  `command.upgrade(...)`（＝能改表结构，第 37 轮 B 的 M-3）**。命中任一条就必须出现
  `pin_local_sqlite` / 自设 `DATABASE_URL` / 显式 `--against-production` 且见远程就拒跑 / `database_label` 之一。
  起因：`scripts/run_three_bucket_retention.py` 以前直接 `from src.models.database import SessionLocal`
  且不设守卫 —— `.env` 的 `DATABASE_URL` 指向生产 ⇒ 它是那批无守卫脚本里**唯一带硬删**的，
  跑起来默认就在生产上算删除候选、还能 `--execute`。现在默认钉镜像、要动生产得显式说，
  并且第一行印库名。`scripts/run_scheduled_tasks.py`（Render Cron 入口，设计上就跑在生产）
  也补了"[库] …"这行日志 —— 净值停在 09-13 那 9 天之所以查不清，部分就是因为日志不说连哪儿。
  **第 37 轮把这条闸门补了两处，别再说成"任何自报都算守卫"**：
  ① `os.environ["DATABASE_URL"] = …` 这个信号**不区分方向**，所以只对"写行"的脚本算守卫；
  **发 DDL 的脚本不认它** —— `scripts/run_migrations.py` 那句 `= ALEMBIC_DATABASE_URL` 可以是生产，
  旧判据却把"赋过值"当"有守卫"，于是它每次 Render 启动（`render.yaml:11` 的 `startCommand`）
  对 `.env` 指的那个库发 `alembic upgrade head` 却全程静默。现在它第一行自报：
  `[库] 本地镜像库（sqlite）—— alembic upgrade head 会向它发 DDL`（副本库实测；
  **它仍然没有 dry-run，也没有"见远程就拒跑"**——要加得改 `render.yaml`，那是老板的决定项）。
  ② `database_label()` 以前只认 Session：SQLAlchemy 2.0 起 `Connection` 没有 `get_bind()`，
  那个 `except` 把异常吞成"未知库" ⇒ 自报行自己说谎。现在 Session / Engine / Connection 三种都认。
  **第 42 轮 B-(c) 又补了第二半**：认出"是 sqlite"不等于报出"是哪个库"——
  `data/fund_insight.db`（真镜像）、`data/copy_*.db`（回放副本）、`:memory:`（夹具）以前共用
  一句"本地镜像库"，而"拿副本的数当镜像的数"正是第 23 轮那次错最省事的复现方式。
  现在标签后面跟着文件或主机（`本地镜像库（data/fund_insight.db）` / `本地 sqlite 文件（不是镜像库）：…`）。
  它与 `scripts/_db_guard.machine_name()/db_kind()` 是同一件事的两份实现（src 不能 import scripts，
  `_db_guard` 也不能 import src），两者**逐条相等**由 `tests/unit/test_database_label_targets.py` 钉住：
  样品自第 43 轮起不再手抄，而是 scheme × 斜杠数 × 路径形状 × 口令/query **笛卡尔积生成**
  （手抄 10 条时，样品外当场量到分叉：`sqlite:////E:x.db` 两边剥不剥斜杠不一致）。
  同轮还查出**手写剥口令**（`url.split('@')[-1]`）散落多处 —— 那是同一把尺子的第 N 份分身，
  而且串里没有 `@` 时它把整串原样印出来。三把要动生产的工具（`q.py` /
  `prod_writeback_preflight_readonly.py` / `purge_test_rows_from_prod.py`）与 `src/__main__.py`
  已换成尺子，剩下的（4 个报错分支）钉在一道**棘轮**里：
  `test_no_new_hand_rolled_credential_stripping_appears` 只许名单变短，不许变长。
  **第 42 轮 B-MAJOR-4：钉库的顺序改由守卫自己在运行期管**。AST 只能可靠地比**顶层** import
  与门调用的行号，而仓库里 100 多处 `from src.…` 写在函数体里（定义处在前、执行处在后，
  按行号比大小要么冤枉一片要么干脆漏掉）。现在 `pin_local_sqlite()` 一进来就问一句
  `sys.modules`：`src.models.database` 已经导过 ⇒ 全局 engine 已按**当时**那串地址焊死，
  改环境变量救不回来，当场 `[abort]`（退码 4）并印出那个目标（口令不泄露）。
  判据 `test_the_door_refuses_to_pin_when_the_engine_is_already_built` 两路都跑真子进程：
  先导入必红、先钉库必放行（没有控制断言的判据等于没判据）。
- **守卫扫描器的判据这一轮从"文件里出现过"改成"同一条分支做到了"**（第 44 轮两席共同的主账，
  A-M-1 / B-BL-1 / B-M-4 / B-M-3 / A-M-2）：
  ① "会拒跑"以前是三个**独立**条件（出现过 `postgres` 字样 + 出现过 `[abort]` + 会 raise），
  放在同一个文件里就能买通 ⇒ 现在判的是**一个分支**：条件看的是库的方向、分支里印 `[abort]`、
  并且**停下来**（`raise` / `return 非 0` / `sys.exit(非 0)` 都算 —— 不认 `sys.exit` 会误伤
  仓库里真在用它的三个脚本，误报方向一样要修）；`if False:` 那一支算死代码不认。
  ② "自设 `DATABASE_URL`"以前只看有没有赋值过，方向不明也算守卫 ⇒
  现在只有**能证明是 SQLite**（字面量、`pin_local_sqlite`、一跳可证的本地变量/函数）才计分，
  方向不明的赋值另记一个键、只用来让"它动过连接串"这件事可见。
  `run_migrations.py` 就是被这一条从"靠赋值过关"改到"靠自报库名过关"的。
  ③ helper 返回的 `[目标]` 只有**这个 helper 被 print 过**才算自报（`push_sector_mappings_to_prod.py`
  的形状），并且现在有一条正面判据问"真脚本里它到底被印了吗"（A-m4：`_target_line` 以前零覆盖）。
  ④ 读侧触发补**一次间接**：脚本自己一句 `src.models` 都不写、只 `import src.services.x`
  也算"碰得到一个活的连接" —— 判据走 src 顶层 import 图的可达性，
  对照样品是一棵**临时造的 src 树**（不是手抄的邻接表）。
  ⑤ `alembic/versions/*.py` 从此在扫描范围里（B-m4）：每一支必须有 `upgrade` 与 `downgrade`，
  **往上走那一支**删结构要登记进 `alembic/destructive-upgrades.json`（今天为空，9 支迁移的删除全在
  `downgrade`）。`alembic/env.py` 那道方向闸只管"能不能连过去"，管不到"过去之后删什么"。
  ⑥ 受检集合"git 不可用就退回磁盘并明说"这一支今天自己跑过一次（A-m3：以前只在 docstring 里），
  并配"临时目录里现造文件必须被收进来"的空判对照。
- **判据"形状对了"还不算完：要看它能不能被普通写法走到**（第 47 轮两席共同的主账，A 68 / B 80 ⇒ 68）：
  ① **文件级 OR 是最常见的漏**：`env_written` 以前问"这篇文件里有没有一处证明方向" ⇒
  `if LOCAL: 钉 sqlite / else: 赋生产串`（与上一轮修掉的三目同一个语义，只换了语句形状）白买守卫。
  现在同文件里存在**任何一处**方向不明的赋值，整篇都不计分。
  ② **印出来 ≠ 报得出来**：`print("[目标] 本地镜像库（sqlite）")` 是一句抄死的话，
  `.env` 指向生产时它照样这么印 ⇒ 自报的那句话必须**从值算出来**（f-string 挖空 / `%` 插值 / 调函数）。
  ③ **恒空的能力类别比没有类别更坏**：`dbapi_direct` 的判据写成
  `resolved in DBAPI_MODULES and name == 'connect'`，而 `resolved` 是被调函数的**叶子名**
  （`sqlite3.connect` 的叶子是 `connect`）⇒ 永不成立、全仓 0 命中，而"每类一个合成样品逐类钉"
  那句话当时是假的：那一类的样品是靠 `raw_sql_write`（串里有 DML 动词）被抓的。
  真正的洞是 `from sqlite3 import connect` + `cur.execute(sql)`（SQL 在变量里）判"不能改数据"
  ⇒ 从不被问连哪儿。现在按"开了 DB-API 连接 **且** 要么发出一条看不见的语句、要么 `commit()`"判，
  而 `execute("select …")` 这种看得见的只读仍然不误伤。
  ④ **一条判据换到另一台机器上要重看一遍位置**：授予点棘轮的裸 SQL 那条腿挂在 `ast.Expr` 上
  （"这一整句必须正好是一次调用"）⇒ `n = db.execute(text("UPDATE … owner_locked = true"))`、
  `if db.execute(...).rowcount:`、列表推导里的全部隐身；而 `is_correct` / `fund_code`
  两条"唯一入口"以前**根本没有裸 SQL 这一腿**。现在三处共用同一个位置无关的
  `_raw_sql_write_hits`，并且只认 SET 子句 / INSERT 列清单里的列名
  （`UPDATE … SET status = 1 WHERE is_correct = true` 是**读**那一列来定位行，判成写就是建墙）。
  ⑤ **"看不见就当没事"是 fail-open**：迁移闸门对解析不出的调用（类方法里的 `op.drop_table`、
  `Legacy().wipe()` 这种属性出口、跨模块 helper、`op.rename_table`）以前既不算命中也不算看不清，
  审计回的是"干净"，而 `run_migrations.py` 每次 Render 启动**先问它**再对 `.env` 那个库发 DDL；
  钉库前的散落连接扫描整趟失败时也只印一行警告继续钉。两处都改成"答不出就拒跑"（退 4），
  `alter_column` 归"看不清"（改类型/收窄会重写既有值，但硬算删除会把 9 支正常迁移逼进登记表）。
  ⑥ **`[abort]` 必须停下来**：`sweep_sector_mappings.py --restore-from` 缺 `created_at` 那一支
  印完 `[abort]` 就把 `codes` 清空继续往下走 ⇒ 退码 0、`[dry-run]` 照印、`--apply` 还照写映射行。
  现在返回 4。（把这条旧行为钉成规矩的正是我自己上一轮写的那条用例 —— **判据写错时，
  绿色的用例会替错误行为作保**。）
  ⑦ 还有一条**关于我自己**的：本轮我（在压缩上下文之后）把 B 席的条目清单凭记忆重写成了一份
  **含不存在条目**的记录（"只读门 `PRAGMA` 侧门 + 生产写进 3 行 + 镜像里 `_load_legacy_probe` 残渣"），
  而 B 席原文只有 B-1~B-7、且明说"生产未连接"。我实测镜像 28 张表里没有任何探针残渣表、
  `drop_probe_residue.py` 回 `[ok] 没有探针残渣表` ⇒ 那三条**作废**，任务记录已按两份原文重写。
  教训：转述评审必须拿原文，凭印象重列等于再造一份未复现的事实。
- **同一条分支还不够：要"会执行、且排在危险动作之前"，方向要来自"值"**（第 45 轮两席共同的主账，
  与上面那条同源，只是又深了一层）：
  ① **死路不算守卫** —— `if False:`、`if X and False:` 那种恒假分支里的 `[abort]` 现在被认成死代码
  （判据 `test_a_guard_must_be_reachable_and_stand_in_front_of_the_danger`，样品是它自己现造的）。
  ② **顺序** —— 同一条分支里 `upgrade(...)` / `commit` / `drop` 在前、拒跑在后 ⇒ 不算守卫，
  因为那句话执行的时候事情已经做完了；触发词表就是扫描器里的 `DANGEROUS_CALLS`。
  ③ **方向要来自值** —— "自设 `DATABASE_URL`"要能证明设的是 SQLite：只认**函数自己每条 `return`
  都返回 sqlite 字面量**（`_returns_sqlite` 不钻嵌套函数）、赋的字面量、`pin_local_sqlite`，
  或一跳可证的本地调用；**按变量名猜、按 docstring 里出现过的词判，一律不算**
  （A-M-2：`def local_url()` 返回生产串也会被名字骗到）。
  ④ 危险迁移的判断从今天起**只有一份实现**：`scripts/migration_policy.py` 被 pytest 与
  `scripts/run_migrations.py` 同时读（运行期在**建连接之前**、`import src.models.database` 之前
  先问它），登记处在 `alembic/destructive-upgrades.json`（键＝迁移名、值＝为什么这不算事故）。
  上一轮这里写的 `DATA_LOSSING_UPGRADES` 是个**当时并不存在的名字** ⇒ 数与名字都要跟着命令走，
  复核：`python -c "import sys; sys.path.insert(0,'scripts'); import migration_policy as m; p,s=m.audit(); print(s,p)"`（今天印 `9 []`）。
  **第 46 轮：这四处每一处都又被买到过一次，下面才是它们现在的样子**（A-M1/M2/M3/M4 + B-M3/M4/M5）：
  ①′ "方向要来自值"当时的实现是**"这个表达式里出现过 `sqlite` 这几个字"** ⇒
  `= "sqlite:///data/copy.db" if a.local else os.environ["PROD_URL"]` 与
  `= os.environ.get("X", "sqlite:///x.db")` 都能白买到守卫。现在走 `_proves_sqlite`：
  字面量必须**以** sqlite 开头、多臂表达式（三目 / `or`）**每一臂**都要单独证明、
  式子里只要有"到运行时才决定"的子式（环境 / 配置 / `.get(k, 默认)`）就整体不证明；
  **解析顺序**也算判据 —— 先认 helper 的出口、再问有没有运行时子式，
  反过来的话 `url(bool(os.environ.get("L")))`（参数来自环境、每条出口都是 sqlite）
  这种诚实写法会被一起打死（我自己第一版就打死了，被那条对照抓红）。
  ②′ `_returns_sqlite` 的出口集合必须**含 `except` 里那几条**（`try: return sqlite / except: return os.environ[...]`
  以前判"每条出口都是 sqlite"）。同一文件里 `_guard_stmts` 反过来**不钻** `except` —— 那边问的是
  "正常路径会不会停下来"，这边问的是"这个 helper 可能交出什么值"，**两个问题共用一个开关才是 bug**。
  ③′ 死路改**求值**：`and 1 == 0`、`if not True`、`if ()`、`while 0:` 以前全算活 ⇒
  现在常量折叠（`ast.literal_eval` + 自己算常量比较），求不出来才当它是活的。
  ④′ 顺序与可达走**调用图**：危险动作抽进 helper、主流程先调用它再判方向 ⇒ 现在能认出来；
  护栏只被一个从不被调用的函数调用 ⇒ 不再算守卫。比较用的是**行号元组的字典序**
  （`(216, 133, 37)` vs `(216, 165)`），**不是累加小数** —— 累加会把顺序整个弄反
  （我把一条真护栏判红过，是那条"真护栏必须仍算守卫"的对照抓出来的）。
  ⑤′ 危险语句的识别从"必须是一整句"改成"**看所有调用**"：`r = op.execute(SQL)`、
  `if op.execute(...)`、推导式、`with` 块里的 `c.execute(VAR)` 以前全部隐身且不标"看不清"
  （审计回的是"干净"）；现在按"是不是**执行** SQL 的入口"判，
  `sa.text(默认值)` 这种只构造不执行的仍然不误伤 —— **闸门建成墙的代价与漏报同量级**。
  ⑥′ `audit()` 的三件事：递归走目录（alembic 自己用 `path_walk`＝`os.walk`，
  子目录里一支 `drop_table` 会被 apply 却从未被核对）、名单类型归一（传 set 曾直接
  `AttributeError`）、**一支都没看到不许报"干净"**。
- **写死"老板已确认"的授予点有棘轮了**（第 44 轮 A-M-3 起，第 45 轮把形状补全，第 46 轮改成"真值语义"）：AGENTS 那句
  "豁免每一条来源都要显式令牌"以前只是话 —— `is_correct` 与 `fund_code` 的"唯一入口"
  当初也只是话，后来各自多出一个入口。现在 `tests/unit/test_review_ownership_and_matching.py`
  用 AST 扫 `src/` 与 `scripts/`，凡是"把授予值写死"的形状都要认（属性赋值 / 字典字面量 /
  `setattr` 与 `object.__setattr__` / SQLAlchemy 批量 `.values(owner_locked=True)` /
  值来自模块常量 / 三目的其中一臂 / 字段名与授予值成对的元组列表 —— 完整清单就是那条用例里
  的 `shapes` 字典，**加一种拼写就在那加一个样品**），命中集合**必须逐字等于**登记名单且
  **按条数**相等（同一函数里多写一行授予 ⇒ 名单看不出"多了一处"，第 45 轮 A-m4），
  两处反向样品不许误伤（"搬运已有值"＝备份/序列化；与豁免无关的 `setattr(obj, name, v)`），
  字段名是变量的 `setattr` 既不静默放过也不硬算成授予 ⇒ 落进 `IMMUNITY_OPAQUE_SITES`
  并要求写明依据。`__init__.py` 从第 45 轮起**不再整族免检**（本仓的包 `__init__` 真放代码）。
  **第 46 轮（A-M6 / B-M10）：判的是"真值语义"，不是字面量 `True`。** 库里读的是
  `if getattr(row, 'owner_locked', None)` ⇒ 真值即免疫，于是以前只认 `True` 的清单漏掉六种
  日常写法：`= 2`、`= not False`、`= x or True`、带类型标注的赋值
  （`row.owner_locked: bool = True`）、`def grant(row, lock=True)` 这种"隔一层参数默认值"、
  `setdefault('owner_locked', True)` / `row['reviewed_by'] = 'owner'`、
  以及裸 SQL（`UPDATE … SET owner_locked = true`，列名和值都在字符串里）。现在都认，
  样品同样进 `shapes` 字典 —— **每一条都配了"搬运已有值不许误伤"的反向对照**。
  **一条边界要说明白，别以为它管全部**：值的来路看不见（跨模块常量、外层变量、`{**payload}`）
  ⇒ **不算"写死授予值"**，也不另开登记通道。第一版开了，当场把 5 处正常代码变成"待解释"，
  那张 `IMMUNITY_OPAQUE_SITES` 就从"要依据"退化成"盖章" —— 而它存在的理由正是防这个。
  这一档的真实归属是**载荷驱动**那条路（`_clean_row` 剔列 + 行为判据）。
  边界：`purge_junk_funds.py --restore-owner-immunity` 那一腿是**载荷驱动**的（值不是字面量），
  这条扫不到，由 `test_purge_junk_funds.py::test_restore_refuses_to_regrant_owner_immunity` 钉。
- **博主榜那一列"存活命中率"从此有用例了**（第 28 轮 F-M-1）：
  `tests/unit/test_blogger_hit_rate_map.py` 4条钉 `src/api/routes/bloggers.py:_hit_rate_map`。
  此前全仓对它零覆盖：把判据 `Prediction.is_deleted == False` 反向改成 `== True`
  （＝只算回收站），`pytest tests/ -q` **854 条全绿** —— 而这一列正是老板判断"谁可信"的依据。
  其中一条用例同时钉住**两个口径本就该不同**：同一批数据，命中率 3/4=75%，
  加权评分（排除 flat 与 `verify_count=0`）是 2/2=100% ⇒ 页面必须分开说明（不许只写进 `title`，
  手机没有 hover；这条**已做完**（第 29~31 轮：博主榜两个表头分开写 + 行内基数），任务 #30 已关）。
- **写侧凭据的 `now=` 必须与读侧的 `today=` 同一天**（第 27 轮 C-B1，实测踩过）：
  `backfill_proofs.record_probe(...)` 不打 `now=` 就取墙上时钟，而 `_fresh` 把"来自未来的
  时间戳"判为不可信（`age < -1`）⇒ 用例若同时注入固定 `today=date(2026,9,21)`，
  **它通过与否就取决于哪天跑**：09-22 23:39 全绿的 `tests/unit`，跨过北京零点红了 2 条。
  现在 `tests/unit/test_backfill_negative_proof.py::test_fixed_today_reads_never_stamp_proofs_with_the_wall_clock`
  用 AST 扫 `tests/unit/*.py`，同函数内"固定 `today=` 的读 + 不带 `now=` 的 `record_probe`"并存即红。
  同类教训第 17 轮就写过（`record_probe` 的 `now=` 就是那时加的参数）——**加了参数不等于加了护栏**。
- **字面这根轴现在是三态，不是两态**（第 28 轮，任务 #32）：
  `sector_identity_audit.relevance_state()` 返回 `relevant` / `alternative_exists` / **`no_literal_fund`**。
  以前 `sector_relevance()` 把"名册里压根查无含该板块核心词的基金"直接算成 `True`（为了不冤枉
  `市场→上证50`、`大盘→沪深300` 这类故意宽基代理），后果是 `区块链→云计算ETF`、
  `核聚变→红利低波ETF` 这批行**永不旗标、无人复核却仍在给新帖挑标的**（镜像实测未审查 17 行）。
  现在体检把第三态写进 `evidence.identity.relevance_state`，`build_worklist` 单独报数
  （`no_literal_fund` / `alternative_exists`），`identity_view` 把它带进 `/api/config/sector-mappings` 的 JSON。
  **页面已经在读这一列**（第 33 轮核对：`web/index.html` 里 `relevance_state` 4 处命中 ——
  顶栏"名册无对口 N"按钮、筛选图例、行内灰字、`identityStats` 计数）：
  第 28 轮那句"`web/` 里 0 处命中"到本次核对时已经过时，别再照抄旧结论。
  ⇒ **读 `sector_relevance()==True` 时不许再说成"已核对为相关"**；老行只有布尔位时
  `row_relevance_state()` 保守返回 `relevant`（不许凭空造旗标），等下次体检补全。
  **服务判据本轮没改**（那 17 行里既有该拦的 `核聚变→红利低波`，也有正确的 `北美→纳指`，
  硬拦会连对的一起掉）⇒ 拦不拦是老板的决定项。用例：`tests/unit/test_relevance_state.py`（6 条）。
- **回写清单的"能不能定价"必须由目标库回答，不能在镜像上算**（第 27 轮两份复评共同抓到，第 28 轮修）：
  `POST /api/config/sector-mappings/-/audit-import` 的回执现在逐行带 `items[].nav_priced_here`
  （`src/api/routes/config.py:_servability_by_code`：本库有无 `fund_info` 档案 / 有无净值行 / 是否 <30 行）。
  服务端**只报告不拒收**（拒收会改公共接口契约 ⇒ 老板的决定项，见检查单 §7 的 A/B）；
  真正拦下来的是 `scripts/push_sector_mappings_to_prod.py`：`--confirm` 真写前先跑一次 dry-run 预检，
  剔掉不可服务的行（硬发要显式 `--allow-unservable`），预检不通则一行都不发（退码 3）。
  为什么必须这样：清单里那两列（`is_fetchable` / `fund_info_needed`）是**在镜像上**算的，
  而镜像的档案我已补齐 ⇒ 清单说"缺 3 行"，生产实测多得多。**但"多得多"这个数跟着清单指纹走**：
  09-21 那份（`1ed8e42a`）量出**无档案 31 行**，09-23 15:14 重导那份（`230767a8`）量出**29 行**，
  两份都印"牵动 249 条活预测"（12 处标的变更里 5 个板块出桶、3 个进桶）。
  ⇒ 报这个数必须同时给**你用的是哪份清单**，别再说成一个常数（检查单 §7 / §7.4 存了两份原始回执）。
  用例条数别抄这里：`grep -c '^def test_' tests/unit/test_push_writeback_gate.py`（本批改完后它同时钉"旧构建缺列""整批被拒不许发真写""两路剔除都要计数"）。
  生产侧全量预检可重跑：`python scripts/prod_writeback_preflight_readonly.py`（只读，退码 5=有不可服务行）。
- **单测零网络现在是被强制的，不再靠自觉**：`tests/conftest.py::_block_real_http` 把
  `requests.Session.send` 换成抛 `BlockedRealHttp`。为什么必须这样：`from src.fund import fund_api`
  拿到的是 **`FundAPI` 实例**（`src/fund/__init__.py` 把包属性重绑成了实例），在它身上
  `setattr(..., 'fund_data_manager', 桩)` 是空操作 ⇒ 第 16 轮实测有 2 条用例每次跑批都
  真打东财接口，用例照样全绿、结论却取决于第三方实时数据。新增会打站的代码时会直接看到
  `测试禁止真实外呼：<url>`，请给那条取数路径注入桩（桩要打在**模块**上：
  `importlib.import_module('src.fund.fund_api')`）。
  **这个信号故意派生自 `BaseException` 而不是 `Exception`**（第 19 轮 MAJOR-3）：仓库里到处是
  "站点抖动一律按没结论处理"的 `except Exception`，用普通异常时守卫被自己吞掉 —— 桩失灵伪装成
  "接口没结论"，`test_no_conclusion_never_accuses` 那一族全绿但什么都没测。换成 BaseException
  之后当场照出 14 条漏桩用例（`test_prediction_verify_date_boundary.py` 整个文件的补拉腿与
  LLM 腿都是真打外网过的），现已显式注入桩。
- 准确率基线在 2026-09-22 被纠正过三次：① 撤掉并重判 94 条"用目标日之后的净值判出来"的
  结论（S7-1 遗留，违反防未来函数策略），本地镜像判对 608→598、判错 560→569；
  ② 第 15 轮按台账同步 11 行的标量 `verify_score`（还原脚本漏字段造成"结论与分数打脸"，
  只动分数、不动判对/判错条数）；③ 第 16 轮撤掉 4 条"端点早于目标日、又证不了那几天休市"
  的终局结论（`run_id=revert-lag-endpoint-20260922`），判对 598→597、判错 569→566。
  ④ 第 18 轮（S9）把 53 条"按改标前那只基金判出来"的结论退回未验证重判
  （`run_id=revert-bad-verdicts-20260922-120657`），判对 597→573、判错 566→537。
  **报数必须带库名**（第 23 轮 MAJOR-1：我长期把**本地镜像**的数当成"系统的数"在报）。
  2026-09-22 同一把尺子在两个库上的实测：
  - 本地镜像 `data/fund_insight.db`：已判 1110 / 判对 573 = **51.62%**，⚠ 197（17.7%），区间 43.96%~61.71%
  - 生产 Supabase：已判 1057 / 判对 591 = **55.91%**，⚠ 419（**39.6%**），区间 33.68%~73.32%
  差这么远的根因是生产数据落后（那一时刻的数：`fund_history` 9541 行 / 末次验证 09-13；镜像 10600 行 / 09-22；
  映射 118 vs 145 行）。⇒ 说准确率之前先说哪个库；拿旧截图对数之前先看落后程度。
  **落后程度今天重新量（2026-09-23 15:06，只读）**：生产末条净值仍是 **2026-09-13**（`fund_history` 9541 行，
  十天零增长）、末次验证 2026-09-13 15:23、到期未判 **141** 条；镜像今天 **17543 行**（净值回补过），
  上面那句"镜像 10600 行"是 09-22 的状态，别再拿来当今天的尺子。
  复现：`python scripts/q.py --production "select max(nav_date), count(*) from fund_history"`
  与 `python scripts/q.py "select count(*) from fund_history"`。
  **这条边界今天撤掉了**（第 48 轮 A-9 / B-8 与任务 #51）：`audit_verdict_evidence.py` 以前只能钉镜像，
  ⇒ 上面**生产那一行**的 ⚠ 419 / 区间 33.68%~73.32% 在仓库里**没有可跑命令**（第 35 轮 B 抓到，
  一直是手写 SQL 复算的）。现在它改走 `_db_guard.read_only_connect()` 那把门，
  复现命令：`python scripts/audit_verdict_evidence.py --production`（默认读镜像，要读线上必须显式给这句旗；
  引擎级只读 + 真试一次写，探针不通就 abort）。**2026-09-25 21:5x 第一次跑它**，结果与 09-22 手算的那行
  **逐字对上**：`已判 1057 / 判对 591 = 55.91%`、`⚠ 419（39.6%）`、`区间 33.68% ~ 73.32%`
  ⇒ 那三个数从"我说的"变成"命令印的"。**同时量出两件新事实**：① 419 的构成是
  `verdict_under_other_fund 220 / nav_row_missing 110 / nav_rewritten 89`（以前生产从没分过桶）；
  ② **生产体检的覆盖面比镜像差得多** —— 1057 条已判结论里 **713 条（67.5%）**挂的代码在
  `sector_fund_mapping` 里根本没有行 ⇒ 身份体检对它们没有意见（镜像那比例是 430/1110＝38.7%）。
  这条不是"门漏了"，是**生产映射只有 118 行而结论来自 20+ 批不同代码**，所以回写之前生产的 ⚠ 只会越查越多。
  判据：`tests/unit/test_read_only_door.py::test_the_verdict_audit_switches_target_only_when_the_flag_says_so`
  （两件事一起钉：没旗子时 `DATABASE_URL` 在场也不算数；递了旗子必须真的撞到"线上得是 PostgreSQL"那一句 ——
  拿一份 sqlite 自称"生产"正是第 46 轮要拦的事）。
  **准确率只能当区间报，而且区间要现算**：`python scripts/audit_verdict_evidence.py` 最后一行
  打印 `已判 1110 条 / 判对 573 条 = 51.62%`，以及把 197 条（17.7%）证据失效结论按
  "全判错/全判对"两个极端折算出的 **43.96% ~ 61.71%**（第 18 轮 MAJOR-3：我此前手算报出去
  的"≈49%~54%"既没有出处、也算错了口径，别再抄）。这 197 条 = 净值被就地改写 108 +
  当天缺行 89，列表里带 ⚠ 证据已失效标记。
  **⚠ 不会自己消解**（第 18 轮 MAJOR-6，我此前写过"补齐后自行消解"，是错的）：每日基金同步
  只回写最近 30 天的净值，而这些结论的端点大多是 2026 年上半年的旧日期；结论行也从不因为
  ⚠ 被重新排队。要真消解得显式补历史净值 + 重验，目前只做"标出来"。
  拿历史截图/旧导出的准确率数字做对比前先确认是哪一批。
- **改标的只允许一个入口**：`FundSyncManager.retag_prediction()`。它留痕（无 run_id 会自动生成，
  保证能被 `restore_prediction_batch` 整批还原）、必要时清结论、并登记受影响博主以便重算统计列。
  **它同时是"不许把预测绑到验不了的标的上"那一道门**（2026-09-26 任务 #100）：目标代码在本库的
  **最后一笔净值早于这条预测的窗口起点** ⇒ 不绑、并当场说出是哪只、停在哪天（尺子只有
  `prediction_lifecycle.nav_cannot_cover_window` 一处，验证器判"要不要关"问的是同一句话）。
  起因是生产实测：`003033`（末条净值 2020-12-08）上压着 37 条活预测，台账 57 行
  `action=maintenance_sync / source=sector_mapping` ⇒ 那批"到期永远判不出来"的行是**这条改标路**
  造出来的，不是随机坏的。**一条净值都没有 ⇒ 不拦**（新档案还没同步过是常态，拦了就是把门建成墙）。
  `tests/unit/test_verdict_evidence_badge.py::test_no_new_direct_fund_code_writes_appear`
  用 AST 扫 `src/` 里所有 `X.fund_code = ...` 赋值，新增站点会让它变红。
  为什么这么严：第 18 轮实测，`POST /api/funds/update-all`（页面上一个按钮）会按
  "与本板块最后登记的那只基金不一致就改过去"的旧规则清掉 **515/1110** 条已判结论。
  同一条规则的另一半（第 18 轮 MAJOR-5）：`verify_prediction` 若发现自带代码被体检判不可服务、
  改按板块解析出**另一个代码**，现在先走 `retag_prediction` 落库再判——以前它拿 B 判结论、
  行上还挂 A，等于持续新增上面那一族脏数据，而且因为从不回写，⚠ 徽章在新数据上永远测不到。
- **判"这个代码是不是基金"必须两根轴一起看，一把就下结论会杀错真基金**（2026-09-26 实测推翻我上一轮的说法）：
  `data/_roster_full.json`（27,905 条）是**快照不是全集** —— `000938 / 002154 / 002261 / 002413 / 003033`
  名册查无，但基金域自证（`verify_fund_fetchable` / `get_fund_domain_name`）逐个答 `ok=True` 并给出
  官方名、各带 20 行净值 ⇒ 那 5 只是**真基金顶着股票名或空名**；而 S6 那 6 个码
  （`603758/600189/152788/HYNX/SBSP76/ign`）两根轴都答不出 ⇒ 才是真非基金。
  所以"生产 9 行指向非基金"那句要拆成"6 行非基金 + 3 行名字错的真基金"；
  **09-26 又量一次，名字这一层比那句更宽**：`fund_info` 165 行里 **23 行的名字 ≠ 名册官方名**，
  其中 **5 行**（`000530 冰山冷热 / 000970 中科三环 / 001309 德明利 / 002354 天娱数科 / 002900 哈三联`，
  各带 59/27/27/53/33 行**真净值**）是"真基金码挂着深市股票名"，另 **4 行**
  （`000938 / 002154 / 002261 / 002413`）名册查无但基金域答得出 ⇒ 共 **9 行改名欠账**（任务 #86；
  工具 `--rename-to-official` 对生产**主动拒绝改名**，所以这不是"等一次跑批"，是决定项）。
  **来源侧那道门装在咽喉上，不是装在那条死路上**（第 49 轮 A-1 / B-4 返修）：
  上一批我把门写在 `LLMAnalyzer._save_fund_mapping` 里，而那个函数自 `9c598cf`（2026-09-21
  "板块→基金改 agent 闭环"）起在 `src/` 里**零调用方** —— 判据还直接 `object.__new__` 调这个
  私有方法，于是绿灯替死代码作保；页面上填一个股票代码时**档案先落库、身份门后判**。
  门现在在 `SectorFundService.ensure_fund_info_exists`：判"这码不是基金"就不建档案（`unknown` /
  探针坏了照旧放行，失败开放），已过门的调用方显式传 `identity_checked=True`（今天真值只有
  审计回写那一处），探针排在 `db.rollback()` 之后 —— 不把生产事务压在网络上（`update_mapping` 早就这么做）。
  **但它不是"所有 caller 的咽喉"，这句我上一版写过头了**（第 50 轮两席同条，实测复核过）：
  `grep -rn "ensure_fund_info_exists" src/` 只命中 `config.py:1443/1701/1747` 三处路由；
  而全仓能往 `fund_info` 写行的活路是 **9 处** —— agent 走 `sector_fund_agent.py:622` 自己
  `db.add(FundInfo(...))`、`full_sync` 走 `fund_sync_manager.py:270/628`、另有
  `fund_api.py:791`、`fund_service.py:460`、`data_portability_service.py:477`。
  那几处各自有**另一把尺子**（agent 的 T2 `verify()` 判股票即拒、`full_sync` 只在基金域答得出时才建档），
  今天没有证据显示它们在产生新垃圾档案 —— **但把别人的尺子算成这道门，下一轮就会漏**。
  `_save_fund_mapping` 连同它的三条用例已删（同 `PredictionService.verify()` 的先例）。
  **推论写给下一轮**：一条"从此有闸"的承诺，判据必须**从路由/服务层打进去**；
  直接调私有方法的用例只能证明"这函数会自检"，证明不了"有人走这条路"。

- **下结论（写 `Prediction.is_correct`）在代码里只有一个入口**：
  `PredictionVerifyService.verify_prediction`。**措辞边界**（第 26 轮被抓到说过头）：
  `/api/config/import` 的**合并模式**仍能按整行列插 `is_correct`（`config.py` 里只有
  `if req.replace:` 才检查 `ENABLE_DATABASE_IMPORT` 与 `X-Danger-Confirm`，
  合并模式既无总开关也无确认头）⇒ 要不要给合并模式也上闸是老板的决定项（任务 #25）。
  同一条 AST 用例还盯着第二个字段：`src/` 里给 `X.is_correct = ...` 赋值的地方必须只有
  `services/prediction_verify_service.py`（实测 2 处：`verify_prediction` 与
  `clear_verification_fields`）。为什么要这条：`PredictionService.verify()` 是一条
  自 `2c227c9`（2026-07-26 删 `POST /{id}/verify`）起就没有调用方的死路，而它**只写
  `is_correct`** —— 不写 `verify_score`、不追加 `verify_history`、不重算 `blogger_stats`，
  第 24 轮两份复评各自复现出"结论 False / 分数 100 / 台账 True / 博主准确率 100% /
  区间 0%"这种五处互相打脸的行。方法连同 `PredictionVerify` 请求模型已删；要恢复人工改判
  必须挂在验证服务那一侧（数据充分性门、休市证据门、退化终点门都在那里），别新开一条旁路。
  同类问题的通则：**"唯一入口"这句话必须有测试钉着**，否则下一轮就会多一个入口。
- **`reviewed_by='owner'` + `owner_locked`（＝身份体检豁免）只能由显式确认换来**。
  **页面**上三条写入路径同一口径：逐行审查、批量审查、以及第 18 轮 MAJOR-1 才补上的**编辑保存**
  （`update_mapping`／`PUT|POST /api/config/sector-mappings`）。以前一次普通保存就白送永久免疫。
  **还有第四条来源，别把上面那句念成"只有三条"**（第 36 轮 B-MAJOR-3 抓到）：
  `scripts/seed_owner_proxies.py:62-64` 直接写 `reviewed_by='owner' + owner_locked=True`，
  它靠的是**脚本级**旗子 `--owner-confirm SEED-PROXY`（同文件 33-37 行，第 20 轮 MAJOR-1 加的），
  不是页面上的 `owner_confirm`。**第 41 轮 B-MINOR-2 又数出第五条**：
  `scripts/purge_junk_funds.py --restore-owner-immunity`（`restore()` 的参数，149-174 行）
  在还原备份时可以把 `reviewed_by='owner' + owner_locked=True` 一起还原回去 ——
  它要的也是显式旗子，但以前文档只说"第四条来源"，等于承诺了一个不存在的封闭名单。
  **第 45 轮又数出一条**（我自己这批加的，先记在这里）：`scripts/sweep_sector_mappings.py
  --restore-owner-immunity` —— 按 manifest 还原映射时同样能把老板署名/锁定盖回去，
  默认**不**还原（判据 `tests/unit/test_sweep_restore_owner_immunity.py`）。
  **每一条都要显式令牌**：前三条在
  `tests/unit/test_review_ownership_and_matching.py`（逐行审查那条）与
  `tests/unit/test_sector_mapping_api.py`（批量审查与编辑保存各有一条），
  第四条在 `tests/unit/test_seed_owner_proxies_gate.py`（当场条数看
  `pytest tests/unit/test_seed_owner_proxies_gate.py --collect-only -q`：
  不给令牌退 4 / 给错令牌退 4 / `--dry-run` 不写 / 给了令牌才落 `owner` 署名 /
  "闸排在 `pin_local_sqlite` 之前"的源码顺序 / 探针说取不到 ⇒ 不写 / 探针说可抓但没官方名 ⇒ 不写 /
  桩的键必须等于真返回的键）；后两条（还原备份的两把旗子）各在
  `tests/unit/test_purge_junk_funds.py::test_restore_refuses_to_regrant_owner_immunity` 与
  `tests/unit/test_sweep_restore_owner_immunity.py`。
  **这个名单到底几条，别在这里抄**：以
  `tests/unit/test_review_ownership_and_matching.py` 的 `IMMUNITY_GRANT_SITES`
  （写死授予值的代码点，含条数）＋ `IMMUNITY_OPAQUE_SITES`（字段名是变量那一站，含依据）
  两张表为准 —— **加一处授予点不登记就红、登记了却没写依据也红**，所以数跟着表走
  （看：`python -c "import ast; t=ast.parse(open('tests/unit/test_review_ownership_and_matching.py',encoding='utf-8').read()); print([(n.targets[0].id, len(getattr(n.value,'keys',None) or n.value.elts)) for n in ast.walk(t) if isinstance(n,ast.Assign) and getattr(n.targets[0],'id','') in ('IMMUNITY_GRANT_SITES','IMMUNITY_OPAQUE_SITES')])"`
  —— 今天印 `[('IMMUNITY_GRANT_SITES', 4), ('IMMUNITY_OPAQUE_SITES', 2)]`）。
  以前这里写"豁免共五条来源"，而第 45 轮我动手加了第六条 ⇒ 那句话当场变成谎话，
  现在改成"别处不抄数"。**仍然要说清的是**：别说成"豁免只有三个入口"。
  **这句话今天还多了一道机器闸**（第 44 轮 A-M-3）：AST 扫全仓"写死授予值"的代码点，
  命中集合必须逐字等于登记名单（名单只许变短）——见上面那条棘轮。
  换了基金代码又没重新确认时，**旧标的上继承来的锁定会被一并撤掉**（`row_unservable()` 的
  owner 例外只认老板这次确认过的那只基金）。页面上区分三种状态：待审查 / 已审查（只是看过）/
  老板已确认（免疫）。
  **两条推论**：① 全库导入（`/api/config/import` 合并模式，无总开关也无确认头）同样**不认**
  清单里的 `reviewed_by='owner' / owner_locked` —— `_clean_row` 剔掉这两列并在回执里报条数
  （第 19 轮 MAJOR-2：否则一份自己盖了章的 JSON 就能给整表买到免疫，这条规则当场为假）；
  ② `owner_locked` 同时是"AI 匹配不得覆盖这一行"的唯一护栏，所以弹窗与列表文案必须两件事都说。
- **验证侧退到别的代码时先改标再判**（第 18 轮 MAJOR-5），清掉结论后要**立刻重算该博主的统计列**
  （第 19 轮 MAJOR-1：`blogger_stats` 按 `verify_count>0` 现算，本轮判完只给 `verified_delta=+1`
  ⇒ 同一行在"已验证数"里计两次）。配套用例读的是**表里的列**而不是会话对象——后者会被
  `recalculate_blogger_stats(commit=False)` 顺手改掉，比较起来永远为真（我这个写法被两份复评共同指出）。
  **这条改标腿能触发的前提是"同板块还有另一只可服务的代码"**。今天两边都只有 1 行/板块
  （镜像 145 行 = 145 板块、生产 118 = 118），而**生产多一条模型没声明的**
  `sector_fund_mapping_sector_name_key UNIQUE(sector_name)`（镜像没有，2026-09-22 直连
  `pg_constraint` 实测；`scripts/sync_db_columns.py` 现在会把这种"库里有、模型没声明"的对象报出来）。
  ⇒ 后果一：真正常见的结局是**解析不出别的标的 ⇒ 直接拒绝验证**（同样不再按不可服务的代码判），
  不是改标；两种都安全，但报数别说成"改标已生效"（今天 0 行被判不可服务 ⇒ 两条腿都是 0 命中）。
  ⇒ 后果二（S6 回写要记住）：任何"给同一板块再加一行"的写入在**生产会撞唯一约束**、在镜像却静默成功，
  所以清单在镜像演练通过不等于生产能过。撤这条约束属结构变更，要老板点头。
  姊妹入口 `rollback_invalid_verifications` 反过来：**标的已漂移的行只数不撤**
  （`data['code_diverged']`，第 19 轮 MAJOR-3：不许用"另一只基金缺净值"这种无关理由撤掉 A 的结论）。
- **准确率的"可信度"有接口也有页面**：`GET /api/stats/evidence` 返回 `span_report()`
  的现算结果（含 `database` 与 `as_of`），博主榜上方那行灰字读的就是它。
  别再往 `title` 里塞关键信息 —— 手机没有 hover，四条评审都因此判我"等于没报"。
- 准确率报表另有派生标记 `evidence_status`（不加列、不落库）：`python scripts/audit_verdict_evidence.py`
  可核对；每日跑批 `verdict_evidence_audit` 只报告不阻塞，要卡合入请手动跑该脚本（默认阈值 0 ⇒ 退码 3）。
  **那条"自带代码也过身份体检"的门有盲区**：判据是"映射表里带这个代码的行全被判不可服务"，
  所以**映射表根本没提过的代码一律放行**（保守设计：没有映射行 ≠ 这只基金有问题）。
  实测镜像 1110 条已判结论里 **430 条（38.7%）**属于这一类，它们既不会被打 ⚠ 也不会触发改标；
  审计脚本每次把这个数打出来，别把门说成覆盖了全部。
- **页面改动必须用真实浏览器看过才能说"好了"**（第 29 轮立规矩，起因是连续几轮把"接口有字段"
  当成"老板看得见"）。本地起服务的固定姿势：**`python scripts/serve_mirror.py --port 8098`**
  —— 它先 `pin_local_sqlite(use_mirror_default=True)` 再起 uvicorn、只绑 127.0.0.1、
  没给 `ACCESS_PASSWORD` 就现造一把一次性口令并打印（`.env` 里那个 `DATABASE_URL` 指向生产，
  手动 `python -m src` / uvicorn 就是往线上打）；跑完按端口找 PID 关掉（`netstat -ano -p tcp` + `taskkill`）。
  **改了 `web/*-manager.js` 之后要换一个全新端口再核验**（第 33 轮实测：同端口刷新会拿到旧 JS，
  而 `index.html` 是新的 ⇒ 新解构出来的名字是 `undefined`，页面**静默**少一块数、不报错）。
  这件事第 34 轮从根上堵住了：`CachedStaticFiles` 只给 `vue.global.prod.js` / `axios.min.js` 一天强缓存，
  页面 / `common.css` / 三个 `*-manager.js` 一律 `no-cache, must-revalidate`（三条 `FileResponse` 页面路由同）
  ⇒ 部署后不需要老板去强刷，也不会出现"新页面配旧脚本"。`TestClient` 逐条实测响应头。
  **核验失败态要把服务真停掉再看**（第 34 轮就是这么抓到新 bug 的）：起 `serve_mirror.py` → 登录 →
  `TaskStop` 掉服务进程 → 在页面上点各视图 / 点写操作按钮，读 `document.body.innerText` 与卡面值。
  只在服务健康时看过页面，等于只验了一半 —— 这一轮照出来的缺陷是：红字已经写"洞察没取到"，
  而洞察四张卡里**只有第四张**被 `insightsLoaded` 守住，另外三张还挂着上一轮的真数
  （`fetchInsights` 失败分支只改旗标、不清 `viewpointInsights`，注释却写着"不许再摆上一轮的数"）。
  现在失败三种形状（`success:false` / `success:true` 但 `data:null` / 抛错）都会把四张卡一起放下，
  判据 `test_a_failed_insights_call_takes_the_four_cards_down_with_it` 跑的是 `loadViewpoints` 真实调用路径。
  六条硬规矩：① 取数点分两类钉：**`onMounted` 真会打的**是 stats / stats+evidence / bloggers /
  predictions+verify-all+status 四笔，**进视图才打的**（funds、sector-mappings、posts/predictions/
  viewpoints 的列表）也要过 `withWakeRetry()`。**这条承诺以前只兑现了一半，2026-09-26 补齐（任务 #58）**：
  第 40 轮 A-m7 实测当时 `post-manager.js` / `viewpoint-manager.js` 里 `withWakeRetry` **0 处**、
  `prediction-manager.js` 只有 2 处且只用在 `verify-all/status`，三个列表都是裸 `axios.get`
  ⇒ 失败态虽然诚实（`viewErrors` 会报"没取到"），但 Render 睡着时这三个列表要老板自己再点一次，
  而另外五个取数点会自己等 90 秒 —— 同一件事两种待遇。现在三处都走注入的那把门，
  判据 `test_every_list_fetch_point_asks_the_wake_gate_before_giving_up` **跑真实源码数调用**
  （四次 fetch 必须记到 ≥4 次 wake，页面构造三个 manager 时都必须把门递进去；
  把任意一处改回裸 `axios.get` 当场红 —— 已实测过一次）。
  别把"首屏六个"当成事实说（第 31 轮 B 实测 `onMounted` 只触发 4 笔）。
  任务轮询（`post-manager.js` / `viewpoint-manager.js`）是另一条规矩：**只有 404 才允许丢任务号**，
  其余失败一律留着句柄、10 秒后再问、最多 15 分钟（`MAX_POLL_FAILURES`），停手也不删。
  上一版写的是"4xx 才算任务结束"，而 `restoreAnalysisJob()` 在 `onMounted` 里跑、**不等登录门** ⇒
  `ACCESS_PASSWORD` 轮换那天正在跑的批量分析会静默永久失联（第 31 轮两份复评共同抓到）。
  ② **状态码分档**：没答话与 502/503/504 算"服务不可用"（排队等醒，多个失败共用一次等待），
  401/403 才是"口令不对"（只有这一档能清 `localStorage` 里的口令），500 原样抛出
  （既不空等 90 秒也不删口令）—— 本仓库没配 `ACCESS_PASSWORD` 时回的就是 503；
  ③ **"取不到"不能渲染成 0 或"库里没有"，而且这条要覆盖每个列表页**：统计卡走 `statVal()`
  （取不到或字段缺失都是 `—`），空状态按 `serviceWaking → 失败原因 → 真的空` 排序，
  帖子/预测/观点/板块映射共用 `emptyText(view)` + `viewErrors`（镜像真值 27 博主 / 657 帖 /
  1616 预测 / 71 观点 / **222 行映射**，唤醒失败时报"暂无X数据"或"共 0 条"都是假事实）。
  **222 是"页面/接口口径"**：`GET /api/config/sector-mappings` 会把内置表里 DB 没有的板块并进来
  （2026-09-23 17:50 实测：DB 145 行 + 内置独有 77 行 = 222；库里行数用
  `python scripts/q.py "select count(*) from sector_fund_mapping"`）。别把这两个数当一个抄来抄去）；
  第 32~33 轮把同一条规矩铺到剩下的角落：分页条（`viewErrors.posts/predictions`）、
  观点洞察四张卡（`numOrDash`）、基金页三个筛选按钮的括号数（失败/加载中报 `—`，并注明那是
  **本页**不是全库）、映射表体、历史建议（`adviceError`）、板块别名 tab（`aliasError`）、  配置弹窗两个 tab（`configError` / `testDataError`）、TOP 弹窗（口径进表头文字、"已验证"不许换分母）、
  洞察卡第四张（`insightsLoaded`，初值 `pending_summary: []` 恒真 ⇒ 没取到也报 0）、
  预览失败时要说清"执行清理为什么被按住"；
  **列表页的失败态由取数点自己报**（manager 里 `options.onFetchFailure(key, msg)`，成功报空串），
  第 32 轮那三条 `watch(() => postMeta.value?.total)` 是死的 —— `postMeta` 是 `reactive()`，`postMeta.value` 恒 undefined；
  **同一根轴还有一面：取不到时不许把上一轮的旧数据当新数据**（第 34 轮把服务真停掉才照出来 ——
  服务健康时看页面永远看不见这一族）。三条列表分页条现在都要说
  "条数没取到，下面是上一次取到的"，`fetchInsights` 三种失败形状（`success:false` /
  `success:true` 但 `data:null` / 抛错）都会清空 `viewpointInsights`，四张卡一起变 `—`。
  推论（写给判据自己）：**一条判据里跑多种失败形状时，每种都要先跑一次成功再跑它** ——
  连着跑的话前一种已经把状态清了，后一种什么都不做也"看着通过"；
  这条不是猜的，是 `rejection_keeps_stale_cards` 那一处变异打出 GREEN（判据无效）之后改的。
  ④ 模板里一句文案的每个插槽都要有自己的守卫（`realigned.core`
  对 ETF 升级行是空的，无条件插值就渲染成"按板块核心词「」"）；
  ⑤ **模板读的每个标识符都必须出现在 `setup()` 的 return 名单里** —— 少一个就静默失效：
  `serviceWaited` 漏导出时"已经等过一轮唤醒"那一支永不渲染，`showApiKey` 压根没声明过，
  API Key 输入框的 `:type` 恒为 password（一个假开关）。机器闸：
  `test_everything_the_template_reads_is_actually_exported`。
  ⑥ **200 + `success:false` 也算一次失败**（第 32 轮 A 数出 16 处 `if (res.data.success)` 没有 else）：
  这个后端很多拒绝走的是 200 + `success:false`（证据门、清理开关没开、映射冲突、别名重复），
  只写 `catch` 等于漏掉一半失败 ⇒ 现在每个取数点要么有 else 分支、要么明写注释说明为何不报。
  推论：**删数据用的按钮不许活过自己的预览**（`fetchRetentionPreview` / `fetchCleanupPreview`
  取不到时 `cleanupEnabled=false`，否则放行的是上一轮的删除计划）。
  判据：`tests/unit/test_frontend_cold_start.py` + 可复跑的变异 `python scripts/mutation_proof_frontend.py`。
  里面若干条不读文本而是用 node **执行页面/manager 里那份真实源码**，喂 401/403/502/503/500/断网/叫不醒等真实形状。
  **条数与处数别抄文档**：跑 `python scripts/mutation_proof_frontend.py --list`，末行打印"共 N 处变异，覆盖 M 条判据"
  （第 32 轮 B 抓到 docstring 里那句"28 处"早就过时 ⇒ 会过时的数不留文字版）。
  体检跑完逐文件回读比对还原、校验变异真的落了盘；开头多一条 **CONTROL** 对照跑（**整份判据文件**在干净代码上必须先全绿），
  且每一处的判定只认"断言失败"那种红 —— 退码 2/4（用法错、收集错、conftest 起手就炸）记成 `HARNESS-FAIL`，
  不再混进满分（第 33 轮 A-MAJOR-4：否则子进程一坏，体检反而满分通过）。
  **体检与 pytest 现在双向互斥**（第 32 轮 B：没有闸拦着的承诺等于没有承诺；第 33 轮 A-MAJOR-3 抓到只做了一半）：
  两把操作系统级文件锁各管一个方向 —— 体检抢 `.mutation-harness.lock`（抢不到就不启动），
  `tests/conftest.py::pytest_configure` 看到它被持有就 `pytest.exit`；反过来 pytest 会话握
  `.pytest-session.lock`，体检启动前先问 `mutation_lock.harness_may_start()`。
  锁由操作系统管，进程被强杀也会自己放开；两个锁文件都在 `.gitignore` 里。
  用例：`tests/unit/test_mutation_lock.py`（当场条数看 `grep -c '^def test_' tests/unit/test_mutation_lock.py`；
  两条真起子进程验两端、一条验两头都真的接了线、第 47 轮 A4 起还验"默认 locale 下的第二会话不许崩"）。
  第 29 轮两份复评（72 / 86）就是拿这四条反过来打我的：第一版"只挡没答话"漏了 5xx、
  两条文本判据结构上不可能响、并发失败各起一轮 90 秒轮询。
  **文本判据必须配一个能把它打红的变异**，否则它只是在描述自己。
  **本机没有 Chromium**：`tests/unit/test_layout_browser_probe.py` 那 16 条常年是 skipped，
  所以"浏览器看过"目前只是我每次的手工动作 + 记录，**不是机器闸**。机器闸是
  `test_frontend_cold_start.py` 里那几跑 node 的行为判据（执行页面源码本身；
  有几条别抄这里：`grep -c "_run_page_js\|_run_chain_js" tests/unit/test_frontend_cold_start.py`）。
- **`tests/conftest.py` 里"钉 `DATABASE_URL` 到临时 SQLite"之前不许导入任何 `src.*`**（第 33 轮我自己踩的）：
  `src/__init__.py` 会拉起 `src.core.config`，而 `.env` 指向生产 ⇒ engine 一旦在那之前被创建就绑死生产，
  后面第 49 行的赋值救不回来（模块已在 `sys.modules` 里）。当时 `tests/unit` 里 4 条夹具把
  `INSERT INTO fund_info / bloggers / posts` 发到了 Supabase（被唯一约束挡住的只是运气），
  落地的 18 行测试数据见任务 #42。现在的两道样：① conftest 在赋值后立刻
  `assert 'src' not in sys.modules`（把原因说清），② `test_database_url_routing.py` 断言 engine 是 sqlite。
  推论：**任何在 conftest 顶层加的 import，先问它会不会拉起应用配置**。
- **裸 `alembic` CLI 在 `.env` 指向生产时被 `alembic/env.py` 拒跑**（第 38 轮 B 的 BLOCKER）：
  旧写法是"没给 `ALEMBIC_DATABASE_URL` 就拿 `DATABASE_URL` 顶上"，而 env.py 第 7 行
  `from src.models.database import Base` 已经把 `.env` 灌进进程 ⇒ 文档推荐的
  `alembic upgrade head` / `downgrade prediction_schema_baseline` 本地一跑就是**对生产发 DDL**
  （那支 downgrade 会 `drop_table("prediction_change_logs")`＝审计台账本体）。
  现在判在**最终解析出的那个值**上（第 39 轮 B 抓到上一版的第二半：只在 ini 等于默认镜像串时
  才检查方向 ⇒ 换一份 `alembic.ini` 或用 `-c` 指第二个 ini，整道闸连同自报静默失效）。
  走法只有三种：目标是 sqlite；调用方已把连接交进来（`scripts/run_migrations.py` = Render
  `startCommand` 走这条，实测不受影响）；或**同时**给 `ALEMBIC_DATABASE_URL` 与
  `ALEMBIC_ALLOW_REMOTE=1`（两道旗子——一个环境变量就放行太松），且这条分支必须自报
  `[库] …` 到 **stderr**（`--sql` 的 stdout 是要存成脚本文件的）。
  `tests/unit/test_alembic_target_direction.py` 钉着（当场条数看 `pytest tests/unit/test_alembic_target_direction.py --collect-only -q`；含"拒跑与放行都不许泄露口令"、"三条自报分支各是一条真的 `print("[库] …")`"）。
  **推论（写给判据自己）**：桩/夹具里用的键名必须来自**真函数返回值**（AST 读），
  不许我抄一份 —— 上一轮我写的 `/verify-fund` 用例给探针发明了 `is_fetchable`/`status` 两个键，
  而路由是纯 pass-through，于是那条判据结构上不可能红（页面读的其实是 `d.ok`）。
- **CodeGraph 为本地索引产物，改完代码跑 `codegraph sync .`。**

常用重点测试：

```bash
pytest tests/unit/test_prediction_verify_batch_task.py -v
pytest tests/unit/test_scheduler_fixes.py -v
pytest tests/unit/test_deployment_optimization.py -v
pytest tests/unit/test_production_hardening.py -v
```
