# -*- coding: utf-8 -*-
"""`fund_info`（基金列表本身）的身份体检与名称修复。

为什么需要它：本轮迭代一直在修**板块→基金映射**，第 9 轮把同一套判据对准 `fund_info`
才发现问题的另一半在更下游 —— 基金列表里有 **29 行**通不过体检（213 行中）：

- 15 行 `not_a_fund`：存的是**股票名**挂在别人的基金码上
  （000530 冰山冷热 / 000938 紫光股份 / 000970 中科三环 / 001309 德明利 /
   002154 报喜鸟 / 002261 拓维信息 / 002354 天娱数科 / 002413 雷科防务 …）。
  这些码在基金域里各自对应一只真基金（001309 其实是「东方红睿逸定开混合」），
  于是"能抓到净值"完全正常 —— 老板要的"注意是基金而不能是股票"在**这一层**从来没被查过。
- 14 行 `unknown`：`fund_name` 是**空串**，码本身没问题，但体检没法比对名字。
  它们背后挂着 **56 条未删除预测**（006105 33 条、513520 8 条、003033 6 条…），
  前端只能显示空名字，统计页也认不出是哪只基金。

本脚本只做**加性修复**：把空的 `fund_name` 用基金域自证到的品种名补上。
股票名那 15 行**不动**：删行会牵动 `fund_history` 与历史预测口径，属于要老板点头的破坏性操作，
这里只出 CSV 清单（`docs/迭代计划/run-2026-09-20/fund-info-identity-audit.csv`）等处理。

    python scripts/audit_fund_info_identity.py            # 看清单（默认不写）
    python scripts/audit_fund_info_identity.py --apply    # 只补空名字

对**生产**只放开这一种写：`--production --apply --confirm FUND-INFO-FILL`
—— 空的 `fund_name` 没有旧值可丢（补上是纯加性），而"股票名挂在真基金码"那批要改名，
方向不可逆（老板靠这些名字回溯"我说的就是这只股票"），所以 `--rename-to-official`
对生产直接退 4，只能在本地镜像上跑。旗子与实际连接不一致也退 4（见用例）。

生产同样有这批脏数据：先在 Render 上跑同一脚本的 dry-run，再 `--apply`。
"""
import argparse
import csv
import io
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _db_guard  # noqa: E402

OUT_DIR = os.path.join(ROOT, 'docs', '迭代计划', 'run-2026-09-20')
# 对生产写库的确认词：这条脚本只做一种生产写入 —— 把空的 `fund_name` 补成基金域自证到的品种名
CONFIRM_TOKEN = 'FUND-INFO-FILL'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='只补空 fund_name，别的一律不写')
    ap.add_argument('--rename-to-official', action='store_true',
                    help='把"股票名挂在别人基金码"的行改成基金域真名（加性改名，不删数据）')
    ap.add_argument('--limit', type=int, default=None, help='只处理前 N 行（先在云上小批试）')
    ap.add_argument('--production', action='store_true',
                    help='对线上生产库执行**只有"补空 fund_name"这一种**加性写入')
    ap.add_argument('--confirm', help='对生产真写必须等于 %s' % CONFIRM_TOKEN)
    args = ap.parse_args()

    if args.production:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, '.env'))
        url = os.environ.get('DATABASE_URL', '')
        if not url.lower().startswith(('postgres', 'postgresql')):
            print('[abort] --production 要求 .env 指向 PostgreSQL')
            return 4
        from _db_guard import db_kind
        print('[target] %s（--production）' % db_kind(url))
        if args.rename_to_official:
            # 补空名没有"旧值可丢"；改名有。库里那 4 行"股票名挂在真基金码"（000938 紫光股份
            # 实为华商稳固添利债券C 等）背后可能牵着老板的历史线索 ⇒ 生产上不自动改。
            print('[abort] --production 只允许补空 fund_name；--rename-to-official 请先在'
                  '本地镜像上跑并核对 CSV（改名会覆盖已有名字，方向不可逆）')
            return 4
        if args.apply and args.confirm != CONFIRM_TOKEN:
            print('[abort] --apply 对生产写需要 --confirm %s' % CONFIRM_TOKEN)
            return 4
    else:
        _db_guard.pin_local_sqlite(use_mirror_default=True)
    from src.models.database import SessionLocal, FundInfo
    from src.services.sector_identity_audit import arbitrate_mapping

    db = SessionLocal()
    # 旗子与实际连接必须对得上（与 `purge_junk_funds` 同一把尺子，不另写一份）：
    # 说了 `--production` 却连到 sqlite、或没说却连到远程，都在第一次 commit 之前退 4。
    from _db_guard import db_kind as _kind
    from purge_junk_funds import _target_agrees_with_the_flag
    actual_url = str(db.get_bind().url)
    why = _target_agrees_with_the_flag(args.production, actual_url)
    if why:
        db.close()
        print(why)
        return 4
    print('[target] 复核：连接实际落在 %s（--production=%s）'
          % (_kind(actual_url), args.production))
    try:
        rows = db.query(FundInfo).order_by(FundInfo.fund_code).all()
        if args.limit:
            rows = rows[:args.limit]
        print('[体检] fund_info %d 行' % len(rows))
        report, filled, renamed, fillable, renameable = [], 0, 0, 0, 0
        for r in rows:
            try:
                v = arbitrate_mapping(r.fund_code, r.fund_name or '', '')
            except Exception as exc:
                v = {'verdict': 'probe_error', 'official_name': '', 'reason': str(exc)[:80]}
            verdict = v.get('verdict')
            official = (v.get('official_name') or '').strip()
            if not (r.fund_name or '').strip() and official:
                # 名字为空时体检只能判 unknown（没东西可比），但基金域已经自证到品种名 ——
                # 补名字是纯加性修复，而这批行背后挂着 56 条预测（006105 就占 33 条）：
                # 不补的话前端列表与统计页永远显示一只"无名基金"。
                if args.apply:
                    r.fund_name = official
                    filled += 1
                else:
                    fillable += 1
                    print('   [可补] %s → %s' % (r.fund_code, official))
                continue
            if verdict == 'ok':
                continue
            report.append({'fund_code': r.fund_code, 'stored_name': r.fund_name,
                           'official_name': official, 'verdict': verdict,
                           'reason': (v.get('reason') or '')[:120]})
            if verdict == 'not_a_fund' and official and args.rename_to_official:
                # 改名是加性的（净值与预测都不动），但只在**没有活预测引用**时才做：
                # 名字一改，博主那些"我说的就是这只股票"的历史线索就找不回来了。
                from src.models.database import Prediction
                live = db.query(Prediction).filter(
                    Prediction.fund_code == r.fund_code,
                    Prediction.is_deleted.is_(False)).count()
                if live:
                    print('   [跳过] %s 仍被 %d 条未删除预测引用，先不动'
                          % (r.fund_code, live))
                elif args.apply:
                    r.fund_name = official
                    renamed += 1
                else:
                    renameable += 1
                    print('   [可改名] %s %s → %s'
                          % (r.fund_code, r.fund_name, official))
        if args.apply:
            db.commit()
        os.makedirs(OUT_DIR, exist_ok=True)
        path = os.path.join(OUT_DIR, 'fund-info-identity-%s.csv'
                            % datetime.now().strftime('%Y%m%d-%H%M%S'))
        with io.open(path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=['fund_code', 'stored_name', 'official_name',
                                              'verdict', 'reason'])
            w.writeheader()
            w.writerows(report)
        stocks = [r for r in report if r['verdict'] == 'not_a_fund']
        print('[结果] 通不过体检 %d 行（其中股票名挂基金码 %d 行）；清单 %s'
              % (len(report), len(stocks), path))
        print('[结果] 空 fund_name：%s %d 行'
              % ('已补' if args.apply else '可补（未写库）',
                 filled if args.apply else fillable))
        # dry-run 以前只会印"补上 0 行"，而上面明明列了 10 条 [可补] —— 一句说反话的
        # 回执比没有回执更坏（同族教训：`--dry-run` 那行不许说"会发 DDL"）。
        if args.rename_to_official:
            print('[结果] 股票名挂基金码：%s %d 行（改名不可逆，生产一律拒绝）'
                  % ('已改' if args.apply else '可改名（未写库）',
                     renamed if args.apply else renameable))
        if stocks:
            print('[提示] 股票名那批**没有自动删**：删行会牵动 fund_history 与历史预测口径，'
                  '要老板确认后再处理（可对照 CSV 逐行看）')
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
