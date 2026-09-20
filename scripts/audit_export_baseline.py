# -*- coding: utf-8 -*-
"""导出文件基线审计：生成板块映射质量与数据量问题清单。

用法：
    python scripts/audit_export_baseline.py --file <export.json> --out docs/迭代计划/baseline-<date>.md
"""
import argparse
import collections
import json
import os
import io


def bigrams(s):
    return set(s[i:i + 2] for i in range(len(s) - 1))


TABLES = [
    "bloggers", "fund_info", "posts", "predictions", "prediction_change_logs",
    "batch_analysis_tasks", "analysis_logs", "verification_tasks",
    "prediction_groups", "viewpoints", "crawler_article_records",
    "fund_history", "sector_alias", "sector_fund_mapping", "investment_advice",
]


def audit(data):
    mappings = data.get("sector_fund_mapping") or []
    funds = {f.get("fund_code"): f for f in (data.get("fund_info") or [])}
    preds = data.get("predictions") or []
    pred_by_sector = collections.Counter(p.get("sector") for p in preds)

    rows = []
    for m in mappings:
        sector = m.get("sector_name") or ""
        code = m.get("fund_code") or ""
        rec = funds.get(code) or {}
        official = rec.get("fund_name") or m.get("fund_name") or ""
        overlap = len(bigrams(sector) & bigrams(official))
        rows.append({
            "sector": sector,
            "code": code,
            "name": official,
            "reviewed": bool(m.get("reviewed")),
            "overlap": overlap,
            "predictions": pred_by_sector.get(sector, 0),
            "fund_type_null": rec.get("fund_type") is None,
        })
    return rows, pred_by_sector, collections.Counter(p.get("status") for p in preds)


def render(path, data, rows, pred_by_sector, status):
    mappings = data.get("sector_fund_mapping") or []
    funds = data.get("fund_info") or []
    null_type = sum(1 for f in funds if f.get("fund_type") is None)
    susp = [r for r in rows if r["overlap"] == 0]
    pending = [r for r in rows if not r["reviewed"]]

    with io.open(path, "w", encoding="utf-8", newline="\n") as out:
        w = out.write
        w("# 数据基线审计（导入前，来自导出文件本身）\n\n")
        w("生成脚本：`python scripts/audit_export_baseline.py`\n\n")
        w("## 一、表计数（S1 导入后必须逐表相等）\n\n| 表 | 行数 |\n| --- | --- |\n")
        for t in TABLES:
            w("| %s | %d |\n" % (t, len(data.get(t) or [])))
        w("\n## 二、预测状态分布\n\n")
        for k, v in status.most_common():
            w("- %s: %d\n" % (k, v))

        w("\n## 三、板块映射现状\n\n")
        w("- 映射 %d 条，覆盖 %d 个板块名；`reviewed=false` %d 条。\n"
          % (len(mappings), len(set(m.get("sector_name") for m in mappings)), len(pending)))
        w("- `sector_alias` **%d 行**（别名机制实际空转）。\n" % len(data.get("sector_alias") or []))
        w("- `fund_info` %d 行中 `fund_type` 为 NULL 的有 **%d 行**，"
          "因此“是基金不是股票”不能靠库字段判定，只能靠接口证据。\n" % (len(funds), null_type))
        w("- 一个板块名有多条映射的行数：%d（`sector_name` 无唯一约束，靠 `cascade_cleanup_conflicts` 抑制）。\n"
          % sum(1 for _, c in collections.Counter(r["sector"] for r in rows).items() if c > 1))

        w("\n## 四、待审查（reviewed=false）清单：%d 条\n\n" % len(pending))
        w("| 板块 | 基金代码 | 基金名 | 辐射预测条数 |\n| --- | --- | --- | --- |\n")
        for r in sorted(pending, key=lambda r: -r["predictions"]):
            w("| %s | %s | %s | %d |\n" % (r["sector"], r["code"], r["name"], r["predictions"]))

        w("\n## 五、板块名与基金名零字面重合（疑似“八竿子打不着”）：%d / %d 条\n\n"
          % (len(susp), len(mappings)))
        w("| 板块 | 基金代码 | 基金名 | 已审查 | 辐射预测条数 |\n"
          "| --- | --- | --- | --- | --- |\n")
        for r in sorted(susp, key=lambda r: -r["predictions"]):
            w("| %s | %s | %s | %s | %d |\n"
              % (r["sector"], r["code"], r["name"], "是" if r["reviewed"] else "否", r["predictions"]))
        w("\n> 口径：板块名与基金名的二字滑窗求交，交集为空即“零重合”。其中既有真错配"
          "（如 债券→512000 证券ETF），也有同义不同词的正当映射（如 存储→159995 芯片ETF）。\n"
          "> 所以本表是**待复核工单**，不是判决书；最终结论由 S2 agent 的语义判定（T3）给出 suitable 与分数。\n")

        w("\n## 六、高影响面（错配板块 × 预测条数）Top 15\n\n")
        w("这些板块一旦映射错，牵动的准确率统计最多，S4 优先处理：\n\n")
        for r in sorted(susp, key=lambda r: -r["predictions"])[:15]:
            w("- %s（%d 条预测）→ %s %s\n" % (r["sector"], r["predictions"], r["code"], r["name"]))
        w("\n## 七、S4 完成判据（供回归比对）\n\n")
        w("- 待审查条数从 **%d** 降到 0；\n" % len(pending))
        w("- 零重合条数从 **%d** 降到 0（或每条都有 `verify_message` 写明保留理由）；\n" % len(susp))
        w("- `sector_alias` 从 0 行建成覆盖全部高影响板块；\n")
        w("- 受错配辐射的预测（上表合计 %d 条）完成重映射或明确保留，且全部有 change log。\n"
          % sum(r["predictions"] for r in susp))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    with open(args.file, encoding="utf-8") as f:
        data = json.load(f)
    rows, pred_by_sector, status = audit(data)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    render(args.out, data, rows, pred_by_sector, status)
    print("wrote %s (%d bytes, %d suspicious of %d mappings)"
          % (args.out, os.path.getsize(args.out),
             sum(1 for r in rows if r["overlap"] == 0), len(rows)))


if __name__ == "__main__":
    main()
