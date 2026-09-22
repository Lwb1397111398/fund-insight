# -*- coding: utf-8 -*-
"""给静态板块表引用的基金码补齐本地档案与净值历史（默认 dry-run）。

为什么需要：`SECTOR_FUND_MAP` 第 27 轮体检换了 17 个代码，而本地库里
**12 个既没有 `fund_info` 档案也没有一行净值**。板块换对了标的，标的却取不到净值
⇒ 落在新代码上的预测永远进不了验证队列（`due` 也查不出端点）。
`scripts/audit_static_sector_map.py` 管"代码对不对"，这个脚本管"代码能不能用"。

安全阀：
  * 先 `pin_local_sqlite` —— .env 里的 DATABASE_URL 指向**生产 Supabase**，
    任何脚本在 import ORM 之前必须钉住本地镜像，否则就是往生产写净值；
  * 默认 dry-run，只报"要补哪些、各缺什么"；真写要 `--apply --confirm SYNC-MAP-FUNDS`；
  * 一次代码一个事务：某只基金接口挂了，不影响其余已补的；
  * 只新增/刷新档案与净值行，不碰预测、不碰映射表。

用法：
    python scripts/sync_sector_map_funds.py                       # dry-run
    python scripts/sync_sector_map_funds.py --apply --confirm SYNC-MAP-FUNDS --days 400
"""
import argparse
import os
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

from _db_guard import pin_local_sqlite          # noqa: E402  必须先钉再碰 ORM

CONFIRM = 'SYNC-MAP-FUNDS'


def audit_codes(db, codes):
    """返回 {code: {'name':…, 'info': bool, 'rows': int, 'latest': str|None}}"""
    from src.models.database import FundHistory, FundInfo
    import sqlalchemy as sa
    out = {}
    infos = dict(db.query(FundInfo.fund_code, FundInfo.fund_name).filter(
        FundInfo.fund_code.in_(codes)).all())
    stats = {r[0]: (r[1], r[2]) for r in db.query(
        FundHistory.fund_code, sa.func.count(FundHistory.id), sa.func.max(FundHistory.nav_date)
    ).filter(FundHistory.fund_code.in_(codes)).group_by(FundHistory.fund_code).all()}
    for code in codes:
        rows, latest = stats.get(code, (0, None))
        out[code] = {'name': infos.get(code), 'info': code in infos,
                     'rows': rows, 'latest': str(latest or '')}
    return out


def fill_missing_names(db, codes):
    """给"档案有了但 `fund_name` 是空"的行按名册补名。

    第 27 轮实测踩到的：`update_fund_info` 对部分新代码只回一个空壳档案（接口拿不到
    名字），我上一版把它算成 `[ok]` ⇒ 一次跑完凭空留下 26 行没有名字的基金档案。
    名字是名册里的官方名（纯加法），补不上就单独报数，绝不静默。
    """
    from src.models.database import FundInfo
    from src.fund import fund_api
    rows = db.query(FundInfo).filter(FundInfo.fund_code.in_(codes)).all()
    try:
        roster = (fund_api.load_fund_roster() or {}).get('by_code') or {}
    except Exception as exc:
        # 名册取不到（离线/限流）时不整体抛：净值已经补好了，别让补名这一步把回执也吞掉。
        print('[补名] 名册取不到，这一步跳过：%s' % str(exc)[:160])
        return [], [r.fund_code for r in rows if not (r.fund_name or '').strip()]
    filled, still = [], []
    for row in rows:
        if (row.fund_name or '').strip():
            continue
        official = ((roster.get(row.fund_code) or {}).get('name') or '').strip()
        if official:
            row.fund_name = official
            filled.append((row.fund_code, official))
        else:
            still.append(row.fund_code)
    return filled, still


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='真写本地镜像（默认只报数）')
    ap.add_argument('--confirm', help='真写必须带 %s' % CONFIRM)
    ap.add_argument('--days', type=int, default=400, help='补多少天的净值（默认 400）')
    ap.add_argument('--min-rows', type=int, default=30,
                    help='净值行数少于这个数才算"要补"（默认 30）')
    ap.add_argument('--only-missing', action='store_true',
                    help='只补完全没有档案/净值的代码，已有历史的只做档案')
    args = ap.parse_args()

    if args.apply and args.confirm != CONFIRM:
        print('[abort] 真写要带 --confirm %s（口令拼错就当没看见）' % CONFIRM)
        return 2

    pin_local_sqlite(use_mirror_default=True)
    from src.constants.sector_fund_map import SECTOR_FUND_MAP
    from src.fund.fund_api import fund_data_manager
    from src.models.database import SessionLocal, engine

    url = str(engine.url)
    if not url.startswith('sqlite'):
        print('[abort] 这个脚本只允许写本地 SQLite，当前 %s' % url.split('@')[-1])
        return 2

    codes = sorted({(e or {}).get('code') for e in SECTOR_FUND_MAP.values()
                    if (e or {}).get('code')})
    db = SessionLocal()
    try:
        state = audit_codes(db, codes)
        no_info = [c for c, v in state.items() if not v['info']]
        thin = [c for c, v in state.items() if v['rows'] < args.min_rows]
        unnamed = [c for c, v in state.items() if v['info'] and not (v['name'] or '').strip()]
        todo = sorted(set(no_info) | set(thin) | set(unnamed))
        if args.only_missing:
            todo = [c for c in todo if c in no_info or state[c]['rows'] == 0]
        print('[表] %d 个代码；缺档案 %d 个、净值不足 %d 行 %d 个、档案有名无字 %d 个 ⇒ 待处理 %d 个'
              % (len(codes), len(no_info), args.min_rows, len(thin), len(unnamed), len(todo)))
        for c in todo[:60]:
            v = state[c]
            print('   %-8s 档案%s 净值%-4d行 最新%-10s %s'
                  % (c, '有' if v['info'] else '缺', v['rows'], v['latest'] or '-', v['name'] or ''))
        if len(todo) > 60:
            print('   ...其余 %d 个省略' % (len(todo) - 60))
        if not todo:
            print('[完成] 静态表引用的代码本地全部有档案且有净值，不用补')
            return 0
        if not args.apply:
            print('\ndry-run：未写库。真补：--apply --confirm %s --days %d'
                  % (CONFIRM, args.days))
            return 0

        ok, failed = [], []
        today = date.today()
        start = today - timedelta(days=args.days)
        for code in todo:
            try:
                with db.begin_nested():
                    info = fund_data_manager.update_fund_info(code, db=db)
                    # 用**分页**的区间补拉：`update_fund_history(days=400)` 其实只发
                    # pageSize=min(days,60) 的一页（实测每只回来 20 行），拿它当"补 400 天"
                    # 是句假话；`backfill_history_range` 才按 startDate/endDate 翻页到底。
                    rows = fund_data_manager.backfill_history_range(code, start, today, db=db)
                db.commit()
                if info is None and not rows:
                    failed.append((code, '接口既没给档案也没给净值'))
                    continue
                ok.append((code, getattr(info, 'fund_name', None) or state[code]['name'] or '', rows))
                print('[ok] %-8s %-22s 档案%s 新增净值 %d 行'
                      % (code, ok[-1][1], '已建/刷新' if info else '未动', rows))
            except Exception as exc:
                db.rollback()
                failed.append((code, str(exc)[:160]))
                print('[失败] %s：%s' % (code, str(exc)[:160]))
        print('\n[完成] 补好 %d 个、失败 %d 个' % (len(ok), len(failed)))
        for code, why in failed:
            print('   [仍缺] %s：%s' % (code, why))
        filled, nameless = fill_missing_names(db, codes)
        db.commit()
        print('[补名] 按名册补上 %d 行空档案%s'
              % (len(filled), ('；仍补不上 %d 行：%s' % (len(nameless), '、'.join(nameless)))
                 if nameless else ''))
        after = audit_codes(db, codes)
        still = [c for c, v in after.items() if not v['info'] or v['rows'] == 0]
        unnamed_after = [c for c, v in after.items() if not (v['name'] or '').strip()]
        if still or unnamed_after or failed:
            print('[退码 5] 没有档案或没有净值 %d 个：%s；名字仍为空 %d 个'
                  % (len(still), '、'.join(still[:20]), len(unnamed_after)))
            print('         这些板块的预测落在取不到净值/叫不出名字的标的上 ⇒ 验不了，需要人工换标的')
            return 5
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
