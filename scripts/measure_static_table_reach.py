# -*- coding: utf-8 -*-
"""量一件事：静态板块表今天**实际**管着多少条活预测（只读，不写库、不打网）。

为什么要单独一个脚本：这个数被写进过 AGENTS.md、模块总览、任务标题和好几份汇报，
却始终没绑住口径 —— 第 27 轮两份复评各自把它复现成 925 / 911（差别只在"要不要同时
要求板块在静态表内"）。同一个"916"被三个口径共用，就是没有口径。

口径（本脚本唯一的一种，改了请连文档一起改）：
    活预测 = `predictions.is_deleted = 0`
    只被静态表覆盖 = 该预测的 `sector` **在** `SECTOR_FUND_MAP` 的键里，
                     **且** `sector_fund_mapping` 里没有该板块的已审查行（`reviewed = 1`）
    —— 后者才是"这张写死的表说了算"的意思；只要映射表有已审查行，静态表就只是兜底。

用法：
    python scripts/measure_static_table_reach.py
    # 再量"这一轮改表动了哪些板块、各牵动多少条活预测"（对照改表前的那份表）：
    git show c7cbc25:src/constants/sector_fund_map.py > data/_old_map.py
    python scripts/measure_static_table_reach.py --impact-against data/_old_map.py
      ↑ 这张表第 27 轮被我写成"124 条 / 7.7%"，两份复评各自算成 78 条 / 4.8%：
        我当时手工数板块、把"只登记了代理但代码没动"的行也算进"改过码"，两个方向都错。
        所以这个数现在由脚本产，不再由我手抄。
"""
import argparse
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                'scripts'))

from _db_guard import pin_local_sqlite          # noqa: E402  必须先钉再碰 ORM

pin_local_sqlite(use_mirror_default=True)

import sqlalchemy as sa                         # noqa: E402

from src.constants.sector_fund_map import SECTOR_FUND_MAP   # noqa: E402
from src.models.database import Prediction, SectorFundMapping, SessionLocal  # noqa: E402


def measure(db):
    """返回 (活预测总数, 只被静态表覆盖的条数, 板块数)。"""
    total = db.query(sa.func.count(Prediction.id)).filter(
        Prediction.is_deleted == False).scalar() or 0        # noqa: E712
    reviewed = {r[0] for r in db.query(SectorFundMapping.sector_name).filter(
        SectorFundMapping.reviewed == True).all()}           # noqa: E712
    rows = db.query(Prediction.sector, sa.func.count(Prediction.id)).filter(
        Prediction.is_deleted == False).group_by(Prediction.sector).all()  # noqa: E712
    only_static = [(s, n) for s, n in rows if s in SECTOR_FUND_MAP and s not in reviewed]
    return total, sum(n for _s, n in only_static), len(only_static)


ROW_RE = re.compile(r"'(?P<sector>[^']+)':\s*\{'code':\s*'(?P<code>[^']+)'")


def parse_table(path):
    """从某个版本的 `sector_fund_map.py` 里读出 {板块: 代码}（只认 SECTOR_FUND_MAP 那一段）。"""
    text = io.open(path, encoding='utf-8').read()
    body = text.split('SECTOR_FUND_MAP = {', 1)
    if len(body) < 2:
        raise SystemExit('[abort] %s 里找不到 SECTOR_FUND_MAP = {' % path)
    body = body[1].split('\n}', 1)[0]
    out = {}
    for m in ROW_RE.finditer(body):
        out.setdefault(m.group('sector'), m.group('code'))
    return out


def impact(db, old_path):
    """对照改表前的那份表：改码/删键各涉及哪些板块、各牵动多少条活预测。"""
    old = parse_table(old_path)
    live = dict(db.query(Prediction.sector, sa.func.count(Prediction.id)).filter(
        Prediction.is_deleted == False).group_by(Prediction.sector).all())  # noqa: E712
    changed = sorted(s for s in set(old) & set(SECTOR_FUND_MAP)
                     if old[s] != (SECTOR_FUND_MAP[s] or {}).get('code'))
    removed = sorted(set(old) - set(SECTOR_FUND_MAP))
    rows = [(s, old.get(s, ''), (SECTOR_FUND_MAP.get(s) or {}).get('code') or '（已删）',
             live.get(s) or 0) for s in changed + removed]
    return sum(live.values()), len(changed), len(removed), rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--impact-against', metavar='OLD_PY',
                    help='改表前那份 sector_fund_map.py 的副本（git show 出来），'
                         '用来量"这一轮动了哪些板块、各牵动多少条活预测"')
    args = ap.parse_args()

    db = SessionLocal()
    try:
        if args.impact_against:
            total, n_changed, n_removed, rows = impact(db, args.impact_against)
            touched = sum(n for *_x, n in rows)
            print('[影响面] 对照 %s：动了 %d 个板块（改码 %d / 删键 %d），'
                  '牵动活预测 %d 条 = %.1f%%（活预测共 %d 条）'
                  % (args.impact_against, len(rows), n_changed, n_removed,
                     touched, 100.0 * touched / total, total))
            print('板块\t旧码\t新码\t活预测')
            for sector, old_code, new_code, n in sorted(rows, key=lambda r: -r[3]):
                print('%s\t%s\t%s\t%d' % (sector, old_code, new_code, n))
            print('[口径] 库=本地镜像 data/fund_insight.db；"动过"只看 code 变了或键被删；'
                  '"只登记代理、代码没动"的板块不算动过（上一版把两者混着数：漏了 算力/中药/储能/石油，'
                  '又多算了 大盘/港股/小金属 ⇒ 净差 46 条，两个方向都错）')
            return 0
        total, covered, sectors = measure(db)
    finally:
        db.close()
    print('[静态表覆盖面] 活预测 %d 条；只被静态表覆盖 %d 条（%d 个板块）'
          % (total, covered, sectors))
    print('[口径] 库=本地镜像 data/fund_insight.db；活预测按 is_deleted=0；'
          '"只被静态表覆盖"= 板块在 SECTOR_FUND_MAP 键里 且 映射表无该板块的已审查行')
    print('[注意] 换库或改表后这个数就会变；引用它时请带上本脚本与日期，别抄旧截图')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
