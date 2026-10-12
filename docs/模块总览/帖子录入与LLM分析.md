# 帖子录入与 LLM 分析

## 职责一句话
把博主的帖子/文章交给 LLM，抽出标题、方向性预测（板块、周期、目标日期）与观点，落库并留下可排查的日志。

## 运作流程
1. 入口：`POST /api/posts`（手工录入）、`POST /api/posts/{id}/analyze`（单帖分析）、
   批量分析（`batch_analysis_tasks`）、爬虫（`src/crawler/*` → `Viewpoint`）。
2. `PostService` / `post_analysis_service.py` 调 `src/analyzer/llm_analyzer.py`：
   `_call_llm()` 带缓存、指数退避、熔断器、并发信号量；解析用
   `_parse_json_with_fallback / _extract_top_level_json / _fix_json_errors` 兜住脏 JSON。
3. 预测补基金：`_fill_fund_from_sector()` → 已审查映射 → 内置字典精确键 →
   **`sector_fund_agent`**（原来是 FundInfo 模糊匹配 + `search_fund()[0]`，已删）。
   匹配层级写进 `analysis_logs.fund_match_level`（**Integer**：1=确定性 2=agent 判定通过 3=降级）。
4. 产出：`posts`、`predictions`、`viewpoints`、`analysis_logs`、`batch_analysis_tasks`。
5. 影响谁：预测验证与博主统计、板块匹配工单（新板块名会进入 AI 批量匹配的缺口清单）。

## 入口文件
`src/analyzer/llm_analyzer.py`、`src/services/post_service.py`、`src/services/post_analysis_service.py`、
`src/services/viewpoint_service.py`、`src/api/routes/posts.py`、`src/crawler/*`、
`src/constants/sector_fund_map.py`（板块别名与内置映射）。

## 对外接口与被谁调用
- `POST /api/posts`、`POST /api/posts/{id}/analyze`、`POST /api/posts/batch-analyze`、
  `POST /api/viewpoints/*`、`GET/POST /api/crawler/*`
- 被 `src/tasks/scheduler.py`（本地常驻）与 `scripts/run_scheduled_tasks.py`（Render Cron）调度。

## 上游依赖
LLM 服务：**主动渠道**（`LLM_PROVIDER/LLM_BASE_URL/LLM_MODEL/LLM_LIGHT_MODEL/LLM_API_KEY`，
火山引擎用 `VOLCENGINE_*`，选 `custom` 时同样走 `LLM_BASE_URL/LLM_MODEL`，只是不预设服务商）+
**辅助渠道**（`LLM_SECONDARY_API_KEY/LLM_SECONDARY_BASE_URL/LLM_SECONDARY_MODEL/LLM_SECONDARY_LIGHT_MODEL`，
四个全配才启用；主动渠道重试耗尽或熔断时整体切过去，换 client 也换模型；
主选轻量模型那档时辅助也用它自己的轻量模型）。
配置可在页面「系统设置 → LLM 配置」改（落 `system_config`/`llm_config.json`），
「测试辅助 API」按钮测辅助渠道连通；熔断按渠道各挂一把（`circuit_breaker` / `secondary_circuit_breaker`）。
另有 `sector_fund_agent` + `fund_api`（补基金时要抓站）、`system_config`（线上持久化配置）。

## 关键数据结构
`posts`（`analysis_result` JSON、`analyzed`）、`predictions`（`sector`/`fund_code`/`prediction_type`/
`target_date`/`status ∈ pending|success|failed`/`is_correct`/`is_deleted`）、
`analysis_logs`（模型、token、`llm_response`、`parse_success/method/error`、`fund_match_level`）、
`batch_analysis_tasks`（进度与失败 id 列表）。

## 已知坑与约束
- **`.env` 的 `DATABASE_URL` 指向生产 Supabase**：任何直接 `SessionLocal()` 的脚本默认在动线上库，
  跑脚本前必须确认连接串（见 `docs/迭代计划/…-v3.md` §零 环境契约）。
- LLM 被 prompt 明确要求**不要**输出基金代码（代码由匹配层决定），别指望模型给可靠代码。
- `llm_analyzer.py` 是高风险文件：改动前先跑 `tests/unit/test_post_analysis_flow.py` 等既有用例。
- 分析链路对延迟敏感：agent 的 LLM 调用默认由 `SECTOR_AGENT_LLM_IN_POST`（默认 false）关闭，
  重活交给板块匹配页的批量任务。
- 低质量帖子有清理任务（`/api/test-data`、`test_data_cleanup_service`），批量分析前先确认不是测试数据。

## 最后更新
2026-10-12：LLM 改「主动 + 辅助」双渠道：新增 `LLM_SECONDARY_*` 四变量（OpenAI 兼容），
`LLM_PROVIDER` 新增 `custom`；`_call_llm_internal` 改为逐渠道尝试（主动失败/熔断整体切辅助，
不可重试错误如 401 也立即换渠道），每渠道独立熔断器；config 路由与设置页透出辅助渠道并新增
`POST /api/config/test-secondary-llm`；`render.yaml` 把 `LLM_PROVIDER` 与 `LLM_MODEL` 等改为
`sync: false`（渠道选择归 Render 仪表盘管，不再被蓝图写回 volcengine）。
判据 `tests/unit/test_llm_secondary_fallback.py`（7 条）。
2026-09-20：`_fill_fund_from_sector` 第 4/5 层改走 `sector_fund_agent`；删除
`_find_fund_in_fundinfo` 的 `.contains()`/反向子串匹配及其自动写库（错配自我固化的主路径）；
`LLMAnalyzer.get_fund_for_sector` 改为只做精确匹配；`fund_match_level` 落点更正为
`analysis_logs.fund_match_level`（Integer 枚举 1/2/3）。
