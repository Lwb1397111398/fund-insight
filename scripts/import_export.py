# -*- coding: utf-8 -*-
"""把线上导出的 JSON 导入本地 SQLite 镜像库，并做导入后完整性核对。

安全设计（对应迭代计划 §零 环境契约 与 §三 S1）：
- 只允许操作 SQLite；`.env` 的 DATABASE_URL 指向生产 Supabase，误连即 fail-closed。
- 默认导入**影子库**（--into），只有显式 --promote 才会替换工作库 data/fund_insight.db。
- promote 是一次性的：工作库 system_config 里已有 S1_PROMOTED 标记时要求 --force。
- import_data() 不抛异常（内部吞掉后返回 success=False），所以必须看返回值。
- 计数期望 = summary + 导入时自动补建的占位基金；另做字段级抽样比对。

用法：
    python scripts/import_export.py --file <export.json> --into data/fund_insight_baseline.db
    python scripts/import_export.py --file <export.json> --promote
    python scripts/import_export.py --file <export.json> --merge
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TABLES = [
    "bloggers", "fund_info", "posts", "predictions", "prediction_change_logs",
    "batch_analysis_tasks", "analysis_logs", "verification_tasks",
    "prediction_groups", "viewpoints", "crawler_article_records",
    "fund_history", "sector_alias", "sector_fund_mapping", "investment_advice",
]

WORK_DB = os.path.join(ROOT, "data", "fund_insight.db")


def to_url(db_path):
    return "sqlite:///" + os.path.abspath(db_path).replace("\\", "/")


def enforce_sqlite(target_db):
    """锁定目标库为 SQLite；DATABASE_URL 指向别的一律拒绝。

    为什么这么硬：覆盖导入会清空白名单表，而 PG 侧 TRUNCATE 是独立连接先提交的，
    一旦连到生产，失败后留下的就是一排空表。
    """
    url = os.environ.get("DATABASE_URL", "")
    if url and not url.lower().startswith("sqlite"):
        print("[abort] DATABASE_URL 指向非 SQLite（%s），本脚本只允许操作本地镜像库。"
              % url.split("@")[-1])
        raise SystemExit(4)
    os.environ["DATABASE_URL"] = to_url(target_db)
    print("[env] DATABASE_URL = %s" % os.environ["DATABASE_URL"])
    return target_db


def backup_db(db_path):
    if not os.path.exists(db_path):
        print("[warn] 工作库不存在，无需备份（将新建）")
        return None
    os.makedirs(os.path.join(ROOT, "data", "backups"), exist_ok=True)
    dst = os.path.join(
        ROOT, "data", "backups",
        "fund_insight_pre_import_%s.db" % datetime.now().strftime("%Y%m%d_%H%M%S"))
    shutil.copy2(db_path, dst)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(db_path + suffix):
            shutil.copy2(db_path + suffix, dst + suffix)
    if os.path.getsize(dst) < 1024:
        raise SystemExit("[abort] 备份文件过小，疑似备份失败：%s" % dst)
    sha = hashlib.sha256(open(dst, "rb").read()).hexdigest()[:16]
    print("[ok] 已备份 %s (%d bytes, sha256:%s)" % (dst, os.path.getsize(dst), sha))
    return dst


def already_promoted():
    """工作库里是否已有本次 S1 的 promote 标记。"""
    if not os.path.exists(WORK_DB):
        return False
    import sqlite3
    conn = sqlite3.connect(WORK_DB)
    try:
        row = conn.execute(
            "SELECT config_value FROM system_config WHERE config_key='S1_PROMOTED'").fetchone()
        return bool(row)
    except Exception:
        return False
    finally:
        conn.close()


def mark_promoted(db_path, note):
    """在 system_config 打一次性标记，防止覆盖导入被重复执行。"""
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO system_config(config_key, config_value, description) "
            "VALUES('S1_PROMOTED', ?, 'S1 覆盖导入已生效，勿重复执行覆盖导入')",
            (note[:500],))
        conn.commit()
    finally:
        conn.close()


def integrity_check(db_path, summary, adjust=None):
    """逐表计数核对；adjust 修正导入时自动补建的占位行。"""
    from sqlalchemy import create_engine, inspect, text

    adjust = adjust or {}
    engine = create_engine(to_url(db_path))
    ins = inspect(engine)
    rows, mismatches = [], []
    with engine.connect() as conn:
        for table in TABLES:
            if table not in ins.get_table_names():
                mismatches.append((table, summary.get(table), "表不存在"))
                continue
            actual = conn.execute(text("SELECT COUNT(*) FROM %s" % table)).scalar()
            want = summary.get(table)
            if isinstance(want, int):
                want += adjust.get(table, 0)
            rows.append((table, want, actual))
            if want is not None and want != actual:
                mismatches.append((table, want, actual))
    engine.dispose()
    print("\n%-26s %10s %10s" % ("表", "期望", "本地"))
    for table, want, actual in rows:
        print("%s %-24s %10s %10s" % ("  " if want == actual else "!!", table, want, actual))
    return rows, mismatches


def _values_equal(want, got):
    """比较导出值与库内值，忽略存储格式差异（ISO 'T' vs 空格、JSON 文本列、浮点尾差）。"""
    if want == got:
        return True
    if want is None or got is None:
        return want in (None, "", []) or got in (None, "", [])
    if isinstance(want, bool) or isinstance(got, bool):
        return bool(want) == bool(got)
    if isinstance(want, (int, float)) and isinstance(got, (int, float)):
        return abs(float(want) - float(got)) < 1e-9
    if isinstance(want, (list, dict)) and isinstance(got, str):
        try:
            return _values_equal(want, json.loads(got))
        except ValueError:
            return False
    if isinstance(want, str) and isinstance(got, (list, dict)):
        return _values_equal(got, want)
    if isinstance(want, str) and isinstance(got, str):
        w, g = want.strip(), got.strip()
        if w == g:
            return True
        if ("T" in w or "T" in g) and w.replace("T", " ")[:19] == g.replace("T", " ")[:19]:
            return True
        try:
            return _values_equal(json.loads(w), g)
        except ValueError:
            return False
    if isinstance(want, (int, float)) and isinstance(got, str):
        try:
            return abs(float(want) - float(got)) < 1e-9
        except ValueError:
            return False
    return False


def sample_field_check(db_path, data, tables=("predictions", "sector_fund_mapping"), n=20):
    """字段级抽样比对：计数相等不代表内容没丢。

    盯 _clean_row 静默丢弃未知键、_coerce_value 把空串变 NULL 这类字段级损失。
    """
    import sqlite3

    diffs = []
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for table in tables:
            for row in (data.get(table) or [])[:n]:
                pk = row.get("id")
                if pk is None:
                    continue
                live = conn.execute("SELECT * FROM %s WHERE id=?" % table, (pk,)).fetchone()
                if live is None:
                    diffs.append((table, pk, "行缺失", None, None))
                    continue
                for key, want in row.items():
                    if key not in live.keys() or want in (None, "", [], {}):
                        continue
                    if not _values_equal(want, live[key]):
                        diffs.append((table, pk, key, want, live[key]))
    finally:
        conn.close()
    if diffs:
        print("\n[warn] 字段级差异 %d 处（前 10 条）：" % len(diffs))
        for d in diffs[:10]:
            print("  %s#%s %s: 导出=%r 本地=%r"
                  % (d[0], d[1], d[2], str(d[3])[:90], str(d[4])[:90]))
    else:
        print("\n[ok] 字段级抽样比对无差异")
    return diffs


def do_import(target_db, data, replace, relax_fk=True):
    """导入到 target_db。

    relax_fk=True 时先关掉本次连接的 `PRAGMA foreign_keys`：导出自生产的
    `analysis_logs`/`fund_history` 里有指向已被清理数据的引用（生产侧不报错是因为
    PG 侧这些行本就存在或约束未生效），逐行 FK 报错会让整笔导入静默回滚、什么都进不去。
    关约束不是掩盖问题——导入后立刻用 `PRAGMA foreign_key_check` 把违规行全量列出来。
    """
    from sqlalchemy import text
    from src.core import config  # noqa: F401  触发 load_dotenv（不覆盖已设的 DATABASE_URL）
    from src.models.database import SessionLocal, init_db
    from src.services.data_portability_service import DataPortabilityService

    init_db()

    def progress(table_key, done, total):
        if done == total or done % 1000 == 0:
            print("  [%s] %d/%d" % (table_key, done, total))

    db = SessionLocal()
    try:
        if relax_fk:
            db.execute(text("PRAGMA foreign_keys = OFF"))
        result = DataPortabilityService(db).import_data(
            data, replace=replace, progress_cb=progress)
        db.commit()
    except Exception as exc:
        db.rollback()
        print("[fail] 导入抛异常：%s" % exc)
        raise SystemExit(2)
    finally:
        db.close()

    # import_data 内部吞异常并返回 success=False，所以必须看返回值
    if not isinstance(result, dict) or result.get("success") is not True:
        print("[fail] 导入未成功，返回体：%s"
              % json.dumps({k: v for k, v in (result or {}).items()
                            if k not in ("data",)}, ensure_ascii=False)[:1500])
        raise SystemExit(2)
    return result


def fk_violations(db_path):
    """列出所有外键违规行，写进报告，供 S1 决定如何清理。"""
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        try:
            bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        except sqlite3.Error as exc:
            print("[warn] foreign_key_check 不可用：%s" % exc)
            return []
    finally:
        conn.close()
    from collections import Counter
    counter = Counter("%s -> %s" % (b[0], b[1]) for b in bad)
    print("\n[info] 外键违规 %d 行，按父子表分布：" % len(bad))
    for key, n in counter.most_common():
        print("  %-46s %d" % (key, n))
    return [dict(zip(("table", "rowid", "parent", "fkid"), b)) for b in bad]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--into", default=None,
                    help="导入到指定 SQLite 文件（默认影子库 data/fund_insight_baseline.db）")
    ap.add_argument("--promote", action="store_true",
                    help="导入影子库校验通过后，用影子库替换工作库（一次性）")
    ap.add_argument("--merge", action="store_true", help="合并导入（不清空）")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--strict-fk", action="store_true",
                    help="外键违规即失败（默认只列出违规清单）")
    ap.add_argument("--skip-audit", action="store_true")
    ap.add_argument("--fingerprint", default=os.path.join(
        ROOT, "docs", "迭代计划", "run-2026-09-20", "s1-counts.json"))
    args = ap.parse_args()

    with io.open(args.file, encoding="utf-8") as f:
        data = json.load(f)
    summary = data.get("summary") or {}
    print("[info] 来源 %s / export_version=%s / export_date=%s"
          % (os.path.basename(args.file), data.get("export_version"), data.get("export_date")))

    shadow = args.into or os.path.join(ROOT, "data", "fund_insight_baseline.db")
    if args.promote and already_promoted() and not args.force:
        print("[abort] 工作库已带 S1_PROMOTED 标记。覆盖导入会抹掉后续所有修复成果"
              "（别名/映射/被纠正的预测），确认要重来请加 --force。")
        raise SystemExit(5)

    enforce_sqlite(shadow)
    result = do_import(shadow, data, replace=not args.merge)

    adjust = {}
    deps = (result.get("created_dependencies") or {})
    if deps.get("fund_info"):
        adjust["fund_info"] = deps["fund_info"]
        print("[info] 导入自动补建占位基金 %d 只（计入期望值）" % deps["fund_info"])
    for key in ("imported", "skipped", "failed"):
        print("[info] %s = %s" % (key, json.dumps(result.get(key) or {}, ensure_ascii=False)))
    for warning in result.get("warnings") or []:
        print("[warn] %s" % warning)

    rows, mismatches = integrity_check(shadow, summary, adjust)
    bad = fk_violations(shadow)
    if args.strict_fk and bad:
        print("[abort] --strict-fk：存在 %d 条外键违规" % len(bad))
        raise SystemExit(3)
    diffs = sample_field_check(shadow, data)
    if diffs and not args.force:
        print("[abort] 字段级抽样有差异，先查清再 promote（--force 可跳过）")
        raise SystemExit(3)

    os.makedirs(os.path.dirname(args.fingerprint), exist_ok=True)
    with io.open(args.fingerprint, "w", encoding="utf-8") as f:
        json.dump({
            "source_file": os.path.basename(args.file),
            "source_sha256": hashlib.sha256(open(args.file, "rb").read()).hexdigest(),
            "shadow_db": os.path.abspath(shadow),
            "counts": {t: a for t, w, a in rows},
            "expected": {t: w for t, w, a in rows},
            "created_dependencies": deps,
            "checked_at": datetime.now().isoformat(),
        }, f, ensure_ascii=False, indent=2)
    print("[ok] 指纹写入 %s（S1 之后不得再跑覆盖导入）" % args.fingerprint)

    if not args.skip_audit:
        out_md = os.path.join(ROOT, "docs", "迭代计划", "baseline-post-import.md")
        subprocess.run(
            [sys.executable, os.path.join(ROOT, "scripts", "audit_export_baseline.py"),
             "--file", args.file, "--out", out_md], check=False)

    if mismatches:
        print("\n[FAIL] 以下表与期望不一致：")
        for t, w, a in mismatches:
            print("  %s: 期望 %s，本地 %s" % (t, w, a))
        raise SystemExit(3)
    print("\n[PASS] 15 张表计数与 summary(+占位补建) 完全一致：%s" % shadow)

    if args.promote:
        backup = backup_db(WORK_DB)
        shutil.copy2(shadow, WORK_DB)
        mark_promoted(WORK_DB, "S1 promote %s from %s"
                      % (datetime.now().isoformat(), os.path.basename(args.file)))
        print("[ok] 已用影子库替换工作库；备份：%s" % backup)


if __name__ == "__main__":
    main()
