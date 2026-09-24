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

最近一次核对（2026-09-24 15:4x（北京），第 43 轮返修（A 81 / B 66，取低分 66）之后，
最后一次改用例后立刻重跑两个口径；**默认 locale（cp936，不设 `PYTHONIOENCODING`）下跑**，
子进程一律显式 `PYTHONIOENCODING=utf-8`）：

- `pytest tests/unit -q` → **1029 passed / 16 skipped / 0 failed**（380.75 秒）。
- `pytest tests/ -q`（含 integration/services）→ **1038 passed / 16 skipped / 0 failed**（386.81 秒）。
  （上一基线 1019/1028 → 本批 1029/1038：+10 条，分布在 `test_drop_probe_residue.py` 新增 4 条
  （探针残渣工具：报告模式不改库 / 没口令不删 / 有行的表让整批不删、改完名才删且不动别的表 /
  模型声明过的名字绝不许当残渣删）、`test_script_db_guards.py` 新增 3 条
  （说明文与样板句买不到守卫信号（含两个真护栏对照组）/ 落笔能力逐类有牙（含"只读不许被判成能写"的
  过宽对照）/ 解析失败兜底 dict 的键集合不许与 `_facts` 漂开）、
  `test_database_label_targets.py` 新增 1 条（手写剥口令的**只许变短**棘轮，自带"现造一处违规"控制）、
  `test_doc_claims.py` 新增 1 条（"指向收不到的文件"必须算进退码 + 同一形状收得到时必须退 0）、
  `test_push_writeback_gate.py` 新增 1 条（`--base` 指到非生产域名时不许出现"线上生产库"那四个字）。
  **本批没动 `web/`**，所以前端 104 处变异不需要重跑（上一批的原始日志已改成随仓库走：
  `docs/迭代计划/run-20260924-mutation/round43-frontend-mutations.txt`）。
  **两条自己被抓出来的判据缺陷（记下来，因为它们正是这一族的标准死法）**：
  ① 那道棘轮的第一版按文本 grep ⇒ 我被"解释这句的 docstring"自己点红；改成判 AST 后第二版仍然**恒空**
  —— `x[-1]` 的 slice 既不是 `ast.Slice` 也不是 `Constant(-1)`，而是 `UnaryOp(USub, Constant(1))`；
  两次都是同一条"现造一处违规必须被点名"的控制断言抓出来的 ⇒ **没有控制断言的判据等于没有判据**。
  ② 上一轮我在 `scripts/_db_guard.py` 与文档里手抄的"一律 abort 会打死 8 条正经用例"，
  数是对的（当场实测 8 条）但**归错了文件**（我把它们记成了 `test_seed_owner_proxies_gate.py` 那 8 条，
  实为 `test_audit_fund_info_identity.py` ×4 + `test_sector_seed_route_honesty.py` ×2 +
  `test_snapshot_prod_mappings.py` ×2）⇒ 已在两处更正并写明复核方式。）
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
  **全套 104 处本轮逐条跑过**（`data/_mutation_round43.log` 是这一批的原始输出：
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
  ② 真镜像里躺着第 41 轮旧探针留下的 `_db_guard_probe2`（0 行、全仓 0 处引用），
  新增 `python scripts/drop_probe_residue.py`（默认只报告退 3；`--apply --confirm DROP-PROBE` 才删；
  只碰 SQLite、只删"0 行 + 模型没声明"的探针名字），2026-09-24 已用它清掉镜像那张；
  ③ 104 处变异的原始日志以前只在 `.gitignore` 的 `data/` 里 ⇒ 干净克隆上没人能复核，
  现在随仓库走：`docs/迭代计划/run-20260924-mutation/round43-frontend-mutations.txt`。
  （**第一版我把它存成 `.log` 就直接写进文档了** —— `.gitignore:47` 有一条全局 `*.log`，
  那句"随仓库走"当场是假的；改名之后用 `git ls-files` 核过才算。教训：**说"入库了"要拿
  `git ls-files` 核，不是看文件在不在磁盘上**。）
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
- **博主榜那一列"存活命中率"从此有用例了**（第 28 轮 F-M-1）：
  `tests/unit/test_blogger_hit_rate_map.py` 三条钉 `src/api/routes/bloggers.py:_hit_rate_map`。
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
  **另外一条今天要说白的边界**：`audit_verdict_evidence.py` 钉的是镜像 ⇒ 上面**生产那一行**的
  ⚠ 419 / 区间 33.68%~73.32% 在仓库里**没有可跑命令**（第 35 轮 B 抓到，已列任务 #51）——
  在那条命令补上之前，报生产准确率区间只能标"手写 SQL 复算过、非工具产出"。
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
  **现在把话说全：豁免共五条来源**（页面逐行审查 / 页面批量审查 / 页面编辑保存 /
  seed 脚本的 `--owner-confirm SEED-PROXY` / 还原备份的 `--restore-owner-immunity`），
  每一条都要显式令牌，且**都有用例钉着**：前三条在
  `tests/unit/test_review_ownership_and_matching.py`（逐行审查那条）与
  `tests/unit/test_sector_mapping_api.py`（批量审查与编辑保存各有一条），
  第四条在 `tests/unit/test_seed_owner_proxies_gate.py`（当场 8 条判据，数看
  `pytest tests/unit/test_seed_owner_proxies_gate.py --collect-only -q`：
  不给令牌退 4 / 给错令牌退 4 / `--dry-run` 不写 / 给了令牌才落 `owner` 署名 /
  "闸排在 `pin_local_sqlite` 之前"的源码顺序 / 探针说取不到 ⇒ 不写 / 探针说可抓但没官方名 ⇒ 不写 /
  桩的键必须等于真返回的键）；第五条在
  `tests/unit/test_purge_junk_funds.py::test_restore_refuses_to_regrant_owner_immunity`）。
  但**别说成"豁免只有三个入口"**，也别再说成"只有四条"——数要跟着命令跑。
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
  viewpoints 的列表）也要过 `withWakeRetry()` —— **这条承诺今天只兑现了一半**（第 40 轮 A-m7 实测：
  `post-manager.js` 与 `viewpoint-manager.js` 里 `withWakeRetry` **0 处**，`prediction-manager.js` 只有 2 处
  且只用在 `verify-all/status`；`fetchPosts` / `fetchPredictions` / `fetchViewpoints` 都是裸 `axios.get`）。
  失败态本身是诚实的（`viewErrors` 会报"没取到"），但后果是** Render 睡着时这几个列表要老板自己再点一次**，
  而另外五个取数点会自己等 90 秒 ⇒ 见任务 #58。别把"首屏六个"当成事实说（第 31 轮 B 实测 `onMounted` 只触发 4 笔）。
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
  用例：`tests/unit/test_mutation_lock.py`（7 条：两条真起子进程验两端、一条验两头都真的接了线）。
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
