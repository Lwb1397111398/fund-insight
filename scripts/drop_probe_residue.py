# -*- coding: utf-8 -*-
"""清掉只读探针在**本地镜像**里留下的残渣表（第 43 轮 B-MAJOR-6）。

为什么存在：第 41 轮 B-MAJOR-2 之前，`_db_guard._write_probe()` 的 sqlite 腿只发一条
`CREATE TABLE`，而 pysqlite 对 DDL 是隐式提交 —— 于是"只读不成立"那种情况
（正是要报警的那种）**由探针把表建进了被检查的那个库**。修完之后残渣不会自己消失：
2026-09-24 实测 `data/fund_insight.db` 里躺着 `_db_guard_probe2`（0 行、全仓 0 处引用）。

它不是"难看"而已：`scripts/sync_db_columns.py` 会把"库里有、模型没声明的对象"报出来，
而它 `--stamp-head` 那段在 extras 非空时**拒绝记版本号** ⇒ 一个空壳表能让一次结构对齐卡住。

规矩：
  · 只动**本地 SQLite**（先 `pin_local_sqlite(use_mirror_default=True)`，远程直接 abort）；
  · 只删同时满足三条的表：名字像探针残渣、**0 行**、**不在 ORM 模型里**；
  · 默认只报告（退码 3 = 有残渣），要真删必须 `--apply --confirm DROP-PROBE`。

用法：
    python scripts/drop_probe_residue.py                 # 报告（退码 0 干净 / 3 有残渣）
    python scripts/drop_probe_residue.py --apply --confirm DROP-PROBE
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import _db_guard    # noqa: E402  钉库 + 机器名

RESIDUE = re.compile(r'^_db_guard_probe\d*$')
CONFIRM = 'DROP-PROBE'


def declared_tables():
    """ORM 模型声明的表名 —— 残渣判据的一半："模型压根不认识它"。"""
    from src.models.database import Base
    return set(Base.metadata.tables)


def find_residue(conn, declared):
    import sqlalchemy as sa
    rows = conn.execute(sa.text("select name from sqlite_master where type='table'")).fetchall()
    out = []
    for (name,) in rows:
        if not RESIDUE.match(name or ''):
            continue
        if name in declared:
            out.append({'name': name, 'rows': -1, 'safe': False,
                        'why': '名字像探针残渣，但**模型里声明了这张表** ⇒ 绝不许当残渣删'})
            continue
        n = conn.execute(sa.text('select count(*) from "%s"' % name)).scalar()
        out.append({'name': name, 'rows': int(n or 0), 'safe': int(n or 0) == 0,
                    'why': '可删' if int(n or 0) == 0 else '表里还有行 ⇒ 不是探针残渣，拒绝删'})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='清掉只读探针在本地镜像里留下的空壳残渣表（默认只报告）')
    ap.add_argument('--apply', action='store_true', help='真删（默认只报告）')
    ap.add_argument('--confirm', metavar='TOKEN', help='真删必须等于 %s' % CONFIRM)
    ap.add_argument('--db', metavar='PATH', help='换一个 sqlite 文件跑（默认本地镜像）')
    args = ap.parse_args(argv if argv is not None else sys.argv[1:])

    if args.db:
        os.environ['LOCAL_DB_URL'] = args.db
    url = _db_guard.pin_local_sqlite(use_mirror_default=True)
    if not url.lower().startswith('sqlite'):
        print('[abort] 这个工具只允许动 SQLite，当前 %s' % _db_guard.machine_name(url))
        return 4
    if args.apply and args.confirm != CONFIRM:
        print('[abort] 真删表必须带 --confirm %s（哪怕目标是镜像：删表没有回退路径）' % CONFIRM)
        return 4

    import sqlalchemy as sa
    engine = sa.create_engine(url)
    declared = declared_tables()
    with engine.connect() as conn:
        residue = find_residue(conn, declared)
        print('[库] %s' % _db_guard.db_kind(url))
        if not residue:
            print('[ok] 没有探针残渣表')
            return 0
        for r in residue:
            print('   [%s] %s（%s 行）：%s' % ('可删' if r['safe'] else '拒绝', r['name'],
                                                 r['rows'], r['why']))
        if not args.apply:
            print('[dry-run] 一行都没删。要删：python scripts/drop_probe_residue.py '
                  '--apply --confirm %s' % CONFIRM)
            return 3
        unsafe = [r for r in residue if not r['safe']]
        if unsafe:
            print('[abort] 有 %d 张表不满足"0 行且模型没声明"，整批不删' % len(unsafe))
            return 4
        for r in residue:
            conn.execute(sa.text('DROP TABLE "%s"' % r['name']))
        conn.commit()
        print('[回执] 已删 %d 张：%s' % (len(residue), '、'.join(r['name'] for r in residue)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
