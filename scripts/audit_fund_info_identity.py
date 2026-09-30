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

本脚本默认只做**加性修复**：把空的 `fund_name` 用基金域自证到的品种名补上。
股票名那批行**默认不动**（不带 `--rename-to-official` 只出 CSV 清单等处理），
因为名字是老板回溯"我说的就是这只股票"的唯一线索；要改得显式给旗子，见下面"对生产"那一节。
删行更是要老板点头的破坏性操作（会牵动 `fund_history` 与历史预测口径），本脚本一律不做。

    python scripts/audit_fund_info_identity.py            # 看清单（默认不写）
    python scripts/audit_fund_info_identity.py --apply    # 只补空名字

对**生产**放开两种写，各要各的确认词：
① 补空的 `fund_name`（纯加性，没有旧值可丢）⇒ `--production --apply --confirm FUND-INFO-FILL`；
② 改名（会覆盖已有名字，方向不可逆）⇒ 必须**同时**给 `--production --rename-to-official
--only-codes 代码,代码 --apply --confirm FUND-INFO-RENAME`。
`--only-codes` 是**白名单**：只点名的那几个码会被改，其余行连体检都不进。
"一键把所有股票名换成官方名"这一键**不存在也不该存在** —— 老板靠这些名字回溯
"我说的就是这只股票"，所以每一行都要他亲自点名（逐行清单工具：`scripts/q.py` 只读数 +
`docs/迭代计划/run-2026-09-20/fund-info-identity-*.csv`）。
真写之前会把旧名字落进 `backup/fund-info-rename-*.json`（备份写不下去 ⇒ 回滚并退 4，
不允许"改了但没留档"），并逐行打 `[已改名] 代码 旧 → 新`。
"""
import argparse
import csv
import io
import json
import os
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (ROOT, os.path.join(ROOT, 'scripts')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _db_guard  # noqa: E402

OUT_DIR = os.path.join(ROOT, 'docs', '迭代计划', 'run-2026-09-20')
# 真写之前落旧名字的地方（`.gitignore` 有一条 `backup/`）。用例把 BACKUP_DIR 改指 tmp_path，
# BACKUP_DIR_DEFAULT 留着，好让"仓库那个目录一个文件都不许多"那条控制能对表。
BACKUP_DIR_DEFAULT = os.path.join(ROOT, 'backup')
BACKUP_DIR = BACKUP_DIR_DEFAULT
# 对生产写库的确认词：这条脚本对生产放开的两种写**代价不同**，所以各要各的词
CONFIRM_TOKEN = 'FUND-INFO-FILL'
# 改名会覆盖已有名字（方向不可逆），比补空名贵一档 —— 拿 FILL 那个词买不到改名
RENAME_CONFIRM_TOKEN = 'FUND-INFO-RENAME'


def _write_backup(entries):
    """把这一趟将要写掉的旧名字落成 JSON，返回路径。

    排在 `db.commit()` 之前调用；写不下去就让它抛出去，由调用方回滚并退 4 ——
    "改了名字但没留下改前的名字"是这条通道唯一不可恢复的那种失败。
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, 'fund-info-rename-%s.json'
                        % datetime.now().strftime('%Y%m%d-%H%M%S'))
    with io.open(path, 'w', encoding='utf-8') as f:
        f.write(json.dumps({'created_at': datetime.now().isoformat(timespec='seconds'),
                            'script': os.path.basename(__file__),
                            'entries': entries}, ensure_ascii=False, indent=2,
                           default=str))
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='只补空 fund_name，别的一律不写')
    ap.add_argument('--rename-to-official', action='store_true',
                    help='把"股票名挂在别人基金码"的行改成基金域真名（加性改名，不删数据）')
    ap.add_argument('--only-codes', default='',
                    help='逗号分隔的代码白名单：只处理点名的这几行（生产改名必须给）')
    ap.add_argument('--limit', type=int, default=None, help='只处理前 N 行（先在云上小批试）')
    ap.add_argument('--production', action='store_true',
                    help='对线上生产库执行加性写入：补空 fund_name，或改**老板点名的那几个码**的名字')
    ap.add_argument('--confirm',
                    help='对生产真写要的确认词：补空名 %s，改名 %s'
                         % (CONFIRM_TOKEN, RENAME_CONFIRM_TOKEN))
    args = ap.parse_args()
    named = [c.strip() for c in (args.only_codes or '').split(',') if c.strip()]

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
            # 改名会覆盖已有名字，而"这一行原来是哪个名字"是老板回溯博主原话的唯一线索
            # ⇒ 生产上只改他逐行点名的码；不给清单就没有这条通道（镜像不受此限，见用例）。
            if not named:
                print('[abort] --production 的改名必须由老板逐行点名（--only-codes 代码,代码）；'
                      '不给清单就只允许补空 fund_name（改名会覆盖已有名字，方向不可逆）')
                return 4
            if args.apply and args.confirm != RENAME_CONFIRM_TOKEN:
                print('[abort] --production 改名需要 --confirm %s'
                      '（补空名用的是 %s：一个没有旧值可丢，一个有）'
                      % (RENAME_CONFIRM_TOKEN, CONFIRM_TOKEN))
                return 4
        elif args.apply and args.confirm != CONFIRM_TOKEN:
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
        # 白名单不是黑名单：点了名就**只**看这几行，其余行连体检都不进。
        # 打成"额外检查"的话，一次误点会把没被点名的股票名一起改掉。
        entries = []
        if named:
            by_code = {r.fund_code: r for r in rows}
            for c in named:
                if c not in by_code:
                    print('[点名未找到] %s（库里没有这一行）' % c)
            rows = [by_code[c] for c in named if c in by_code]
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
                    entries.append({'fund_code': r.fund_code, 'old_name': r.fund_name,
                                    'new_name': official, 'kind': 'fill_empty_name'})
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
                    entries.append({'fund_code': r.fund_code, 'old_name': r.fund_name,
                                    'new_name': official, 'kind': 'rename_stock_name'})
                    r.fund_name = official
                    renamed += 1
                    print('   [已改名] %s %s → %s'
                          % (r.fund_code, entries[-1]['old_name'], official))
                else:
                    renameable += 1
                    print('   [可改名] %s %s → %s'
                          % (r.fund_code, r.fund_name, official))
        # 备份排在 commit 之前：这条通道唯一不可恢复的失败是"改了名字但没留下改前的名字"，
        # 而名字是老板回溯"我说的就是这只股票"的唯一线索。写不下去 ⇒ 回滚并退 4。
        if args.apply:
            if entries:
                try:
                    backup_path = _write_backup(entries)
                except OSError as exc:
                    db.rollback()
                    print('[abort] 备份写不下去（%s）⇒ 一个字都不提交' % exc)
                    return 4
                db.commit()
                print('[备份] 改前的名字落档 %s（%d 行）' % (backup_path, len(entries)))
            else:
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
            # 上一版这句写"生产一律拒绝"，而这条通道现在能改生产（代价：必须逐行点名 +
            # 改名专用的确认词 + 先把旧名字落档）⇒ 一句作废的拒绝话会让下一轮以为没这条路。
            print('[结果] 股票名挂基金码：%s %d 行（改名会覆盖已有名字：生产只改 --only-codes '
                  '点名的码，且旧名字先落档 backup/）'
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
