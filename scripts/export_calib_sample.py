#!/usr/bin/env python
"""导出 clear 桶 50 条（seed 固定）供盲标；只读。"""
from __future__ import annotations

import json
import os
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from scripts.audit_l3_clear_labels import (
    SAMPLE_N,
    SEED,
    audit_clear_label,
    classify_bucket,
    connect,
    fetch,
)

eng, session, db_label = connect()
try:
    rows = fetch(session)
finally:
    session.close()
    eng.dispose()

clear_all = [
    r
    for r in rows
    if classify_bucket(r.get("prediction_content") or "", r.get("prediction_type") or "")
    == "clear"
]
rng = random.Random(SEED)
sample = list(clear_all)
rng.shuffle(sample)
sample = sample[: min(SAMPLE_N, len(sample))]

out = []
for r in sample:
    content = (r.get("prediction_content") or "").strip()
    a = audit_clear_label(content, r.get("prediction_type") or "")
    out.append(
        {
            "id": r["id"],
            "content": content,
            "prod": (r.get("prediction_type") or "").lower(),
            "rule_mismatch": bool(a["mismatch"]),
            "rule_reasons": a["reasons"],
        }
    )

path = ROOT / "data" / "_calib_sample_50.json"
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps({"db": db_label, "n": len(out), "rows": out}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"wrote {path} n={len(out)} db={db_label}")
for i, row in enumerate(out, 1):
    # 只打印 id+原文，遮蔽 prod，供盲标
    c = row["content"].replace("\n", " ")[:160]
    print(f"--- {i} id={row['id']} ---")
    print(c)
