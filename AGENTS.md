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

最近一次核对（2026-09-22 23:39，第 27 轮（静态板块表守护）修复之后，最后一次改用例后立刻重跑）：

- `pytest tests/unit -q` → **835 passed / 16 skipped / 0 failed**（约 200 秒）。
- `pytest tests/ -q`（含 integration/services）→ **844 passed / 16 skipped / 0 failed**（约 180 秒）。
  报数时要写清是哪个口径，两个数都对但常被人当成回归。
  （上一基线 808/817 → 819/828 → 本批 835/844。**同一个错我连犯两轮**：写完基线数字后又加用例
  却没复测。规矩改成：最后一次改用例之后先跑数、再写文档，数字与用例改动必须在同一个提交里。）
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
  因此改写 `sector_core`；③ 表里的 `name` 必须逐字等于名册官方名（`--fix-labels` 可代改）。
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
- CodeGraph 为本地索引产物，改完代码跑 `codegraph sync .`。

常用重点测试：

```bash
pytest tests/unit/test_prediction_verify_batch_task.py -v
pytest tests/unit/test_scheduler_fixes.py -v
pytest tests/unit/test_deployment_optimization.py -v
pytest tests/unit/test_production_hardening.py -v
```
