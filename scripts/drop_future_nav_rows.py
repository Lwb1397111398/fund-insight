# -*- coding: utf-8 -*-
"""删掉**晚于今天（北京时间）**的净值行，并把被它们带歪的档案头日期倒回真实末条。

为什么要有这把工具（2026-09-25 实测）：东财 lsjz 会给货币基金**预签未来行** ——
`000725`（大成添利宝货币B）在 09-25 那天给了 09-26 与 09-27 两条 `FSRQ`，一次「更新基金净值」
就把它们写进生产 `fund_history`，并把 `fund_info.nav_date` 也写成 09-27。
写入侧的门已经补在取数入口（`src/fund/fund_api.py:usable_history_rows`，判据
`tests/unit/test_nav_future_row_gate.py`），**这把工具管的是门补上之前已经躺在库里的那些行**。

用法（默认只出计划，一行都不删）：
    python scripts/drop_future_nav_rows.py                              # 本地镜像：看要删谁
    python scripts/drop_future_nav_rows.py --production                 # 生产：只出计划
    python scripts/drop_future_nav_rows.py --production --apply --confirm DROP-FUTURE-NAV \\
           --json data/nav_future_backup.json                           # 真删（先落备份）

`--production` 有牙（沿用 `purge_junk_funds.py` 那把方向闸，**不另写第二份**）：
给了它却解析出 SQLite、或没给却解析出非 SQLite，都直接退 4。
"""
import argparse
import io
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

CONFIRM_TOKEN = 'DROP-FUTURE-NAV'


def plan(db, today, extra_dates=()):
    """返回 (要删的净值行, 要倒回的档案头, 全部条数)。不写任何东西。

    默认只点"晚于今天（北京）"的行 —— 这与入库侧那道门（`fund_api.usable_history_rows`）
    是同一个谓词，不另写一份。**但门补上之前已经躺在库里的"提前签发行"，日期追平后就不再
    满足这条判据**（2026-09-26 实测生产有 4 条这样的行，全是 `000725`：09-25 那天写进 09-26/09-27，
    07-31 那天写进 08-01/08-02），所以留一条 `--dates` 显式名单：点名删，理由进回执，
    不悄悄扩大打击面。
    """
    from sqlalchemy import or_

    from src.models.database import FundHistory, FundInfo

    conds = [FundHistory.nav_date > today]
    if extra_dates:
        conds.append(FundHistory.nav_date.in_(list(extra_dates)))
    rows = db.query(FundHistory).filter(or_(*conds)).order_by(
        FundHistory.fund_code, FundHistory.nav_date).all()
    affected = sorted({r.fund_code for r in rows})
    archives = []
    for code in affected:
        info = db.query(FundInfo).filter(FundInfo.fund_code == code).first()
        if info is None:
            continue
        keep = [FundHistory.nav_date <= today]
        if extra_dates:
            keep.append(FundHistory.nav_date.notin_(list(extra_dates)))
        remaining = db.query(FundHistory.nav_date).filter(
            FundHistory.fund_code == code, *keep).order_by(
            FundHistory.nav_date.desc()).limit(1).first()
        archives.append({
            'fund_code': code, 'nav_date_now': str(info.nav_date), 'latest_nav_now': info.latest_nav,
            'nav_date_should_be': str(remaining[0]) if remaining else None,
            'archive_ahead': bool(info.nav_date and (
                info.nav_date > today or (extra_dates and info.nav_date in extra_dates))),
        })
    return rows, archives, len(rows)


def drop(db, today, apply_changes, backup_path, extra_dates=()):
    rows, archives, n = plan(db, today, extra_dates)
    print('[计划] 要删的净值行 %d 条（判据：晚于 %s%s），涉及基金 %d 只' % (
        n, today,
        '，另加点名日期 ' + '/'.join(str(d) for d in extra_dates) if extra_dates else '',
        len({r.fund_code for r in rows})))
    for r in rows[:40]:
        print('   [待删] %s  %s  nav=%s  写入于 %s' % (
            r.fund_code, r.nav_date, r.nav, r.created_at))
    if n > 40:
        print('   …其余 %d 条见备份 JSON' % (n - 40))
    for a in archives:
        print('   [档案头] %s 现为 %s%s ⇒ 倒回 %s' % (
            a['fund_code'], a['nav_date_now'],
            '（晚于今天，本身也是被未来行带歪的）' if a['archive_ahead'] else '',
            a['nav_date_should_be'] or '（库里再没有该码的净值行了）'))
    if not apply_changes:
        print('[dry-run] 以上只是计划，一行都没删。要删请补 --apply --confirm %s，'
              '并带 --json <备份路径>。' % CONFIRM_TOKEN)
        return 2, 0
    if not backup_path:
        print('[abort] 真删必须先给 --json <备份路径>：这 2 类行是"照上游重问就能再拿到"的，'
              '但没有备份就等于不可回滚。' )
        return 4, 0

    payload = {'today': str(today), 'created_at': datetime.now().isoformat(timespec='seconds'),
               'rows': [{'id': r.id, 'fund_code': r.fund_code, 'nav_date': str(r.nav_date),
                         'nav': r.nav, 'day_growth': r.day_growth, 'fund_name': r.fund_name}
                        for r in rows],
               'archives': archives}
    with io.open(backup_path, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1)
    print('[备份] %d 行 + %d 个档案头已写入 %s（回滚照它逐行插回）' % (n, len(archives), backup_path))

    for a in archives:
        from src.models.database import FundHistory, FundInfo
        info = db.query(FundInfo).filter(FundInfo.fund_code == a['fund_code']).first()
        if info is None or not a['archive_ahead']:
            continue
        target = datetime.strptime(a['nav_date_should_be'], '%Y-%m-%d').date() \
            if a['nav_date_should_be'] else None
        head = db.query(FundHistory).filter(
            FundHistory.fund_code == a['fund_code'],
            FundHistory.nav_date == target).first() if target else None
        info.nav_date = target
        if head is not None:
            info.latest_nav = head.nav
        print('   [已倒回] %s nav_date %s → %s，latest_nav → %s' % (
            a['fund_code'], a['nav_date_now'], target, info.latest_nav))

    ids = [r.id for r in rows]
    deleted = db.query(FundHistory).filter(FundHistory.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    print('[回执] 删除 %d 行净值（逐行见备份 JSON），档案头倒回 %d 只' % (deleted, len(archives)))
    return 0, deleted


def main():
    ap = argparse.ArgumentParser(description='删除晚于今天的净值行（默认只出计划）')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--confirm', help='必须等于 %s' % CONFIRM_TOKEN)
    ap.add_argument('--production', action='store_true', help='显式对线上生产库执行（默认本地镜像）')
    ap.add_argument('--json', dest='backup', help='备份/逐行清单落盘路径（--apply 时必填）')
    ap.add_argument('--today', help='覆盖"今天"（YYYY-MM-DD，仅供回放下界核对）')
    ap.add_argument('--dates', help='点名要删的日期（逗号分隔 YYYY-MM-DD）：'
                                    '用于门补上之前已入库、如今日期已被追平的"提前签发行"')
    args = ap.parse_args()

    if args.apply and args.confirm != CONFIRM_TOKEN:
        # 检查排在连库之前：用法错不该先连一次库（第 46 轮 B-M8 同条规矩）
        print('[abort] --apply 需要 --confirm %s' % CONFIRM_TOKEN)
        return 4

    if args.production:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(ROOT, '.env'))
        url = os.environ.get('DATABASE_URL', '')
        if not url.lower().startswith(('postgres', 'postgresql')):
            print('[abort] --production 要求 .env 指向 PostgreSQL')
            return 4
        from _db_guard import db_kind
        print('[target] %s' % db_kind(url))
    else:
        from _db_guard import db_kind, pin_local_sqlite
        print('[target] %s' % db_kind(pin_local_sqlite(use_mirror_default=True)))

    from src.models.database import SessionLocal
    db = SessionLocal()
    try:
        # 方向闸复用 `purge_junk_funds` 那一把，不在这儿重写第二份（同一件事两份实现
        # 迟早分叉，这个项目已经为"两份清单"付过四次账）
        from purge_junk_funds import _target_agrees_with_the_flag
        from _db_guard import db_kind as _kind
        actual = str(db.get_bind().url)
        why = _target_agrees_with_the_flag(args.production, actual)
        if why:
            print(why)
            return 4
        print('[target] 复核：连接实际落在 %s（--production=%s）' % (_kind(actual), args.production))

        if args.today:
            today = datetime.strptime(args.today, '%Y-%m-%d').date()
        else:
            from src.services.prediction_lifecycle import current_as_of
            today = current_as_of()
        extra = []
        for raw in (args.dates or '').split(','):
            raw = raw.strip()
            if not raw:
                continue
            try:
                extra.append(datetime.strptime(raw, '%Y-%m-%d').date())
            except ValueError:
                print('[abort] --dates 里的 %r 不是 YYYY-MM-DD（不猜日期）' % raw)
                return 4
        if extra:
            from src.models.database import FundHistory
            missing = [str(d) for d in extra
                       if not db.query(FundHistory.id).filter(
                           FundHistory.nav_date == d).first()]
            if missing:
                print('[abort] 点名的日期在库里没有行：%s ⇒ 名单写错了，先看计划再说' % '、'.join(missing))
                return 4
        rc, _ = drop(db, today, args.apply, args.backup, tuple(extra))
        return rc
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
