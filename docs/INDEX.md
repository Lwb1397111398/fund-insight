# Fund Insight 文档索引

## 新模型接手优先读

1. `AGENTS.md` 或 `CLAUDE.md`：AI 接手项目的第一入口，包含命令、架构、风险文件和工作规则。
2. `ARCHITECTURE.md`：当前系统架构、数据流、后台任务、部署关系。
3. `PRODUCT.md`：产品目标、用户、工作流和前端体验原则。
4. `DEPLOYMENT.md`：Render、Supabase、GitHub Actions、环境变量和运维排查。
5. `README.md`：人类维护者快速启动和功能总览。

## 设计和计划

- `docs/superpowers/specs/`：功能设计文档。
- `docs/superpowers/plans/`：实施计划文档。
- `docs/REFACTOR_HANDOFF_2026-07-10.md`：本轮重构改动、验证结果和后续接手建议。

## 历史报告

以下文档保留为历史记录，不能当成当前完整架构：

- `CRAWLER_SUMMARY.md`
- `SECURITY_FIXES.md`
- `HIGH_BUG_FIXES_SUMMARY.md`
- `docs/BUG_FIX_REPORT_*.md`
- `docs/*OPTIMIZATION*.md`
- `docs/*FEATURE*.md`

## 模块记录

模块级说明在各目录的 `MODULE_RECORD.md`：

- `src/api/MODULE_RECORD.md`
- `src/services/MODULE_RECORD.md`
- `src/models/MODULE_RECORD.md`
- `src/analyzer/MODULE_RECORD.md`
- `src/crawler/MODULE_RECORD.md`
- `src/fund/MODULE_RECORD.md`
- `src/tasks/MODULE_RECORD.md`
- `src/core/MODULE_RECORD.md`

## 索引和搜索

- `.codegraph/` 是本地 CodeGraph 索引产物，不手工编辑。
- 改文档或代码后运行：

```bash
codegraph sync .
codegraph status .
```
