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
6. 博主统计：`blogger_stats`、`BloggerService`、`StatsService` 统计准确率、等级、预测数量。

观点和建议流：

1. 爬虫或人工录入观点，保存到 `Viewpoint`。
2. `ViewpointService` 支持批量分析、汇总观点、权重和有效期。
3. `AdviceService` 与 `LLMAnalyzer.generate_investment_advice_three_stage()` 生成投资建议。

## 重点文件

| 文件 | 说明 |
| --- | --- |
| `src/api/main.py` | FastAPI 入口、中间件、路由注册、静态页面、危险的数据库导入接口 |
| `src/api/routes/` | 按领域拆分的 REST API |
| `src/models/database.py` | 30+ ORM 表模型，兼容 SQLite/PostgreSQL |
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
- **为什么不用 `scripts/run_migrations.py`**：它从 base 跑，会去重复建已存在的表；
  直接 stamp 又是在没核对前置对象的前提下撒谎。`sync_db_columns.py` 的唯一真值是
  `src/models/database.py` 的元数据（库里缺整表会被**说出来**并且拒绝 stamp）。

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

## 推荐工作流

1. 先看本文件、`ARCHITECTURE.md`、`PRODUCT.md`。
2. 查结构优先用 CodeGraph：`codegraph query`、`codegraph callers`、`codegraph context`；不可用时用 `rg`。
3. 修改前定位对应测试；能写测试就写测试，不能写则运行最小验证。
4. 改完运行相关 pytest，再运行 `python -m src --init-db` 做启动级数据库初始化检查。
5. 涉及索引或文档入口变化后运行 `codegraph sync .`。

## 当前测试基线

最近一次核对（2026-09-23 12:36（北京），第 33 轮返修（复评 62/74 驱动）之后，
最后一次改用例后立刻重跑；**这一批起在默认 locale（cp936，不设 `PYTHONIOENCODING`）下跑** ——
上一批我是在 UTF-8 环境里报的 898/907，换到默认 locale 就红一条，见下面子进程那条）：

- `pytest tests/unit -q` → **912 passed / 16 skipped / 0 failed**（229 秒）。
- `pytest tests/ -q`（含 integration/services）→ **921 passed / 16 skipped / 0 failed**（186 秒）。
  报数时要写清是哪个口径，两个数都对但常被人当成回归。
  （上一基线 885/894（红过一次：897/**1 failed**/16）→ 本批 912/921：+14 条 =
  清理脚本离线往返 6、`/api/bloggers/top` 路由形状 3、互斥闸反向 2、失败态铺满 3。
  判据数：前端 26 条 / 变异 66 处（`--list` 末行为准，别抄这里）。）
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
  两根轴上还有两处**别再说满话**的地方（第 27 轮两份复评各抓到一处）：
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
  `--impact-against data/_old_map.py` 量影响面：**31 个板块（改码 18 / 删键 13）= 78 条 = 4.8%**）。
  以前文档里写过的"916 条""124 条 / 7.7%"都是手抄没绑口径，已撤回（同一个数被复现成 925/911）。
- **能写数据的脚本必须说清"连的是哪个库"**（第 28 轮 F-MINOR-6 起有用例钉）：
  `tests/unit/test_script_db_guards.py` 扫 `scripts/*.py`，凡**带写开关**
  （`--apply` / `--execute` / `--confirm …`）**且直连 ORM** 的脚本，必须出现
  `pin_local_sqlite` / 自设 `DATABASE_URL` / 显式 `--against-production` / `database_label` 之一。
  起因：`scripts/run_three_bucket_retention.py` 以前直接 `from src.models.database import SessionLocal`
  且不设守卫 —— `.env` 的 `DATABASE_URL` 指向生产 ⇒ 它是那批无守卫脚本里**唯一带硬删**的，
  跑起来默认就在生产上算删除候选、还能 `--execute`。现在默认钉镜像、要动生产得显式说，
  并且第一行印库名。`scripts/run_scheduled_tasks.py`（Render Cron 入口，设计上就跑在生产）
  也补了"[库] …"这行日志 —— 净值停在 09-13 那 9 天之所以查不清，部分就是因为日志不说连哪儿。
- **博主榜那一列"存活命中率"从此有用例了**（第 28 轮 F-M-1）：
  `tests/unit/test_blogger_hit_rate_map.py` 三条钉 `src/api/routes/bloggers.py:_hit_rate_map`。
  此前全仓对它零覆盖：把判据 `Prediction.is_deleted == False` 反向改成 `== True`
  （＝只算回收站），`pytest tests/ -q` **854 条全绿** —— 而这一列正是老板判断"谁可信"的依据。
  其中一条用例同时钉住**两个口径本就该不同**：同一批数据，命中率 3/4=75%，
  加权评分（排除 flat 与 `verify_count=0`）是 2/2=100% ⇒ 页面必须分开说明（不许只写进 `title`，
  手机没有 hover；这条还没做，见任务 #30）。
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
  而镜像的档案我已补齐 ⇒ 清单说"缺 3 行"，生产实测缺 **31 行**（压着 249 条活预测）。
  用例：`tests/unit/test_push_writeback_gate.py`（4 条）+ `test_sector_mapping_audit_import.py`（3 条）。
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
  差这么远的根因是生产数据落后（`fund_history` 9541 行 / 末次验证 09-13；镜像 10600 行 / 09-22；
  映射 118 vs 145 行）。⇒ 说准确率之前先说哪个库；拿旧截图对数之前先看落后程度。
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
  `tests/unit/test_verdict_evidence_badge.py::test_no_new_direct_fund_code_writes_appear`
  用 AST 扫 `src/` 里所有 `X.fund_code = ...` 赋值，新增站点会让它变红。
  为什么这么严：第 18 轮实测，`POST /api/funds/update-all`（页面上一个按钮）会按
  "与本板块最后登记的那只基金不一致就改过去"的旧规则清掉 **515/1110** 条已判结论。
  同一条规则的另一半（第 18 轮 MAJOR-5）：`verify_prediction` 若发现自带代码被体检判不可服务、
  改按板块解析出**另一个代码**，现在先走 `retag_prediction` 落库再判——以前它拿 B 判结论、
  行上还挂 A，等于持续新增上面那一族脏数据，而且因为从不回写，⚠ 徽章在新数据上永远测不到。
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
- **`reviewed_by='owner'` + `owner_locked`（＝身份体检豁免）只能由显式 `owner_confirm=true` 换来**。
  三条写入路径同一口径：逐行审查、批量审查、以及第 18 轮 MAJOR-1 才补上的**编辑保存**
  （`update_mapping`／`PUT|POST /api/config/sector-mappings`）。以前一次普通保存就白送永久免疫。
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
  静态子资源没有版本戳这件事记在任务 #43。
  五条硬规矩：① 取数点分两类钉：**`onMounted` 真会打的**是 stats / stats+evidence / bloggers /
  predictions+verify-all+status 四笔，**进视图才打的**（funds、sector-mappings、posts/predictions/
  viewpoints 的列表）也要过 `withWakeRetry()` —— 子模块靠 `createPredictionManager({ withWakeRetry })`
  注入拿它。别把"首屏六个"当成事实说（第 31 轮 B 实测 `onMounted` 只触发 4 笔）。
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
  1616 预测 / 71 观点 / 222 映射，唤醒失败时报"暂无X数据"或"共 0 条"都是假事实）；
  第 32~33 轮把同一条规矩铺到剩下的角落：分页条（`viewErrors.posts/predictions`）、
  观点洞察四张卡（`numOrDash`）、基金页三个筛选按钮的括号数（失败/加载中报 `—`，并注明那是
  **本页**不是全库）、映射表体、历史建议（`adviceError`）、板块别名 tab（`aliasError`）、
  配置弹窗两个 tab（`configError` / `testDataError`）、TOP 弹窗（口径进表头文字、"已验证"不许换分母）、
  洞察卡第四张（`insightsLoaded`，初值 `pending_summary: []` 恒真 ⇒ 没取到也报 0）、
  预览失败时要说清"执行清理为什么被按住"；
  **列表页的失败态由取数点自己报**（manager 里 `options.onFetchFailure(key, msg)`，成功报空串），
  第 32 轮那三条 `watch(() => postMeta.value?.total)` 是死的 —— `postMeta` 是 `reactive()`，`postMeta.value` 恒 undefined；
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
  判据：`tests/unit/test_frontend_cold_start.py`（本批实测 23 条，其中两条用 node **执行页面里那份源码**，
  喂 401/403/502/503/500/断网/叫不醒七种真实形状）+ 可复跑的变异 `python scripts/mutation_proof_frontend.py`。
  **处数别抄文档**：跑 `python scripts/mutation_proof_frontend.py --list`，末行打印"共 N 处变异，覆盖 M 条判据"
  （第 32 轮 B 抓到 docstring 里那句"28 处"早就过时 ⇒ 会过时的数不留文字版）。
  体检跑完逐文件回读比对还原、校验变异真的落了盘；开头多一条 **CONTROL** 对照跑（**整份判据文件**在干净代码上必须先全绿），
  且每一处的判定只认"断言失败"那种红 —— 退码 2/4（用法错、收集错、conftest 起手就炸）记成 `HARNESS-FAIL`，
  不再混进满分（第 33 轮 A-MAJOR-4：否则子进程一坏，体检反而满分通过）。
  **体检与 pytest 现在双向互斥**（第 32 轮 B：没有闸拦着的承诺等于没有承诺；第 33 轮 A-MAJOR-3 抓到只做了一半）：
  两把操作系统级文件锁各管一个方向 —— 体检抢 `.mutation-harness.lock`（抢不到就不启动），
  `tests/conftest.py::pytest_configure` 看到它被持有就 `pytest.exit`；反过来 pytest 会话握
  `.pytest-session.lock`，体检启动前先问 `mutation_lock.harness_may_start()`。
  锁由操作系统管，进程被强杀也会自己放开；两个锁文件都在 `.gitignore` 里。
  用例：`tests/unit/test_mutation_lock.py`（7 条：两条真起子进程验两端、一条验两头都真的接了线）。
  第 29 轮两份复评（72 / 86）就是拿这四条反过来打我的：第一版"只挡没答话"漏了 5xx、
  两条文本判据结构上不可能响、并发失败各起一轮 90 秒轮询。
  **文本判据必须配一个能把它打红的变异**，否则它只是在描述自己。
  **本机没有 Chromium**：`tests/unit/test_layout_browser_probe.py` 那 16 条常年是 skipped，
  所以"浏览器看过"目前只是我每次的手工动作 + 记录，**不是机器闸**。机器闸是
  `test_frontend_cold_start.py` 里那两条 node 行为判据（执行页面源码本身）。
- **`tests/conftest.py` 里"钉 `DATABASE_URL` 到临时 SQLite"之前不许导入任何 `src.*`**（第 33 轮我自己踩的）：
  `src/__init__.py` 会拉起 `src.core.config`，而 `.env` 指向生产 ⇒ engine 一旦在那之前被创建就绑死生产，
  后面第 49 行的赋值救不回来（模块已在 `sys.modules` 里）。当时 `tests/unit` 里 4 条夹具把
  `INSERT INTO fund_info / bloggers / posts` 发到了 Supabase（被唯一约束挡住的只是运气），
  落地的 18 行测试数据见任务 #42。现在的两道样：① conftest 在赋值后立刻
  `assert 'src' not in sys.modules`（把原因说清），② `test_database_url_routing.py` 断言 engine 是 sqlite。
  推论：**任何在 conftest 顶层加的 import，先问它会不会拉起应用配置**。
- CodeGraph 为本地索引产物，改完代码跑 `codegraph sync .`。

常用重点测试：

```bash
pytest tests/unit/test_prediction_verify_batch_task.py -v
pytest tests/unit/test_scheduler_fixes.py -v
pytest tests/unit/test_deployment_optimization.py -v
pytest tests/unit/test_production_hardening.py -v
```
