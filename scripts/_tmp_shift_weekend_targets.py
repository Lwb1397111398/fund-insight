# -*- coding: utf-8 -*-
"""临时脚本：把存量预测里落在周末的目标日顺延到下周一（本地镜像库）。

只改未验证（is_correct 为 NULL）且 target_date 落在周六/周日的预测，
对齐修复③ calculate_target_date 的新口径，使旧数据无需重跑分析即可验证。
"""
import os
import sys
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(ROOT, "data", "fund_insight.db").replace("\\", "/")
sys.path.insert(0, ROOT)

from src.core import config  # noqa: F401
from src.models.database import Prediction, SessionLocal, init_db  # noqa: E402


def main():
    init_db()
    db = SessionLocal()
    try:
        rows = db.query(Prediction).filter(
            Prediction.is_correct.is_(None),
            Prediction.is_deleted == False,  # noqa: E712
            Prediction.target_date.isnot(None),
        ).all()
        changed = []
        for p in rows:
            wd = p.target_date.weekday()
            if wd == 5:
                p.target_date = p.target_date + timedelta(days=2)
                changed.append((p.id, "Sat->Mon"))
            elif wd == 6:
                p.target_date = p.target_date + timedelta(days=1)
                changed.append((p.id, "Sun->Mon"))
        db.commit()
        print(f"调整了 {len(changed)} 条预测的周末目标日:", changed)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
