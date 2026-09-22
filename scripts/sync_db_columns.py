# -*- coding: utf-8 -*-
"""按 ORM 模型给目标库补"模型里有、库里没有"的列与索引（幂等，默认只出计划）。

为什么不走 `alembic upgrade head`：生产库**不是 alembic 建的**（2026-09-22 实测没有
`alembic_version` 表，0001~0006 的对象是应用 `create_all` 建的）。从 base 重跑会去
重复建已存在的表；而直接 stamp 到 head 等于在没核对前置对象的情况下撒谎。
所以这里的唯一真值是 `src/models/database.py` 的元数据 —— 补列这件事不再有第二份清单
（本仓库为"两份清单"付过太多次账）。

安全边界：
- 默认 **dry-run**；真执行必须同时给 `--apply` 与 `--confirm SYNC-COLUMNS`。
- 只发 `ADD COLUMN` / `CREATE INDEX IF NOT EXISTS`，**不改任何数据、不删任何对象**。
- URL 指向 PostgreSQL 之外时直接退出，除非显式 `--allow-sqlite`（用来在镜像副本上演练）。

用法：
    python scripts/sync_db_columns.py                          # 连 .env 指向的库，只出计划
    python scripts/sync_db_columns.py --apply --confirm SYNC-COLUMNS
    python scripts/sync_db_columns.py --allow-sqlite           # 在本地镜像副本上演练
"""
import argparse
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for _st in (sys.stdout, sys.stderr):
    try:
        _st.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

CONFIRM_TOKEN = 'SYNC-COLUMNS'


def plan(engine):
    """返回 (缺列 [(table, column, ddl)], 缺索引 [(name, table, [cols])], 缺表 [name])。"""
    import sqlalchemy as sa
    from sqlalchemy.schema import CreateColumn
    from src.models.database import Base

    insp = sa.inspect(engine)
    db_tables = set(insp.get_table_names())
    missing_cols, missing_idx, missing_tables = [], [], []
    for table in Base.metadata.sorted_tables:
        if table.name not in db_tables:
            # 整表不存在**必须说出来**：静默跳过的话，调用方会以为"没有缺项"，
            # 而 `--stamp-head` 正是在这个判断上决定要不要把版本记成 head。
            # 建表交给 create_all，不在本脚本职责内。
            missing_tables.append(table.name)
            continue
        have = {c['name'] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in have:
                continue
            ddl = str(CreateColumn(col).compile(dialect=engine.dialect)).strip()
            # CreateColumn 编译出来是 `name TYPE [DEFAULT ...] [NOT NULL]`
            missing_cols.append((table.name, col.name, ddl))
        have_idx = {i['name'] for i in insp.get_indexes(table.name)}
        for idx in table.indexes:
            if idx.name in have_idx:
                continue
            cols = [c.name for c in idx.columns]
            missing_idx.append((idx.name, table.name, cols))
    return missing_cols, missing_idx, missing_tables


def extra_objects(engine):
    """库里有、而 ORM 模型**没声明**的列/索引 —— 只报不动（第 21 轮 MAJOR-3）。

    为什么不假装元数据是双向真值：`SectorFundMapping.__table_args__` 里根本没写
    `ix_sector_fund_mapping_owner_locked`（那是 0008 迁移建的），反过来模型声明的
    `ix_prediction_change_logs_run_id` 镜像里又可能没有。所以"本脚本没报缺项"
    只等于"模型要的东西都在"，不等于"迁移历史都跑过" —— 后者正是 --stamp-head 在宣称的事。
    """
    import sqlalchemy as sa
    from src.models.database import Base

    insp = sa.inspect(engine)
    db_tables = set(insp.get_table_names())
    extras = []
    for table in Base.metadata.sorted_tables:
        if table.name not in db_tables:
            continue
        model_cols = {c.name for c in table.columns}
        for col in insp.get_columns(table.name):
            if col['name'] not in model_cols:
                extras.append(('column', '%s.%s' % (table.name, col['name'])))
        model_idx = {i.name for i in table.indexes}
        declared = {c.get('name') for c in insp.get_unique_constraints(table.name)}
        for idx in insp.get_indexes(table.name):
            name = idx['name']
            if name in model_idx or name in declared:
                continue
            # 主键与唯一约束在主库里的**实现形态就是索引**（`<表>_pkey` / `*_key`），
            # 模型侧用 primary_key=True / UniqueConstraint 声明，不进 `table.indexes`。
            # 不过滤的话这里每次都报出一堆"可疑对象"、`--stamp-head` 的闸门永远拒绝
            # —— 一个从不放行的闸门和没有闸门一样没用（第 21 轮 MAJOR-3 的自查）。
            if idx.get('unique') or name.endswith(('_key', '_pkey', '_constraint')):
                continue
            extras.append(('index', '%s.%s' % (table.name, name)))
    return extras


def main():
    ap = argparse.ArgumentParser(description='按 ORM 元数据补列/补索引（只加不减，默认 dry-run）')
    ap.add_argument('--apply', action='store_true', help='真执行（默认只出计划）')
    ap.add_argument('--confirm', help='必须等于 %s' % CONFIRM_TOKEN)
    ap.add_argument('--allow-sqlite', action='store_true',
                    help='允许对 SQLite 跑（在镜像副本上演练用）')
    ap.add_argument('--stamp-confirm', metavar='TOKEN',
                    help='--stamp-head 单独要的词：STAMP-HEAD（它等于宣称迁移都跑过了）')
    ap.add_argument('--stamp-head', action='store_true',
                    help='补完后把 alembic_version 记成当前 head（让后续 0010+ 能正常 upgrade）')
    args = ap.parse_args()

    from src.models.database import Base, engine
    url = str(engine.url)
    scheme = url.split('://')[0]
    print('[target] %s%s' % (url.split('@')[-1] if '@' in url else url,
                             '' if scheme.startswith('postgres') else '（非生产）'))
    if not scheme.startswith('postgres') and not args.allow_sqlite:
        print('[abort] 目标不是 PostgreSQL（%s）。演练请加 --allow-sqlite，'
              '真补生产库请确认 .env 的 DATABASE_URL。' % scheme)
        return 4
    if args.apply and args.confirm != CONFIRM_TOKEN:
        print('[abort] --apply 需要 --confirm %s（防手滑）' % CONFIRM_TOKEN)
        return 4

    cols, idx, tables = plan(engine)
    extras = extra_objects(engine)
    print('[元数据] 模型 %d 张表；库里缺 %d 张：%s'
          % (len(Base.metadata.sorted_tables), len(tables), tables or '无'))
    if extras:
        print('[!] 库里有、模型没声明的对象 %d 个（本脚本不动它们）：%s'
              % (len(extras), ', '.join('%s.%s' % (k, n) for k, n in extras[:8])
                 + ('…' if len(extras) > 8 else '')))
    for t in tables:
        print('   ! 整表缺失 %-22s（交给 create_all，本脚本不建表）' % t)
    if not cols and not idx and not tables:
        print('[ok] 列与索引都已存在，无需改动')
        return 0
    for t, c, ddl in cols:
        print('   + column  %-22s %s' % (t, ddl))
    for name, t, ccols in idx:
        print('   + index   %-22s %s(%s)' % (t, name, ', '.join(ccols)))
    if not args.apply:
        print('\ndry-run：未执行任何 DDL。要真补：--apply --confirm %s' % CONFIRM_TOKEN)
        return 2

    import sqlalchemy as sa
    is_pg = scheme.startswith('postgres')
    with engine.begin() as conn:
        for t, c, ddl in cols:
            if is_pg:
                conn.execute(sa.text('ALTER TABLE %s ADD COLUMN IF NOT EXISTS %s' % (t, ddl)))
            else:
                # SQLite 没有 `ADD COLUMN IF NOT EXISTS`（会直接语法报错），
                # 而演练就是在 SQLite 副本上跑的 —— 两条路都得走通才叫幂等。
                try:
                    conn.execute(sa.text('ALTER TABLE %s ADD COLUMN %s' % (t, ddl)))
                except Exception as exc:
                    if 'already exists' not in str(exc).lower():
                        raise
        for name, t, ccols in idx:
            conn.execute(sa.text('CREATE INDEX IF NOT EXISTS %s ON %s (%s)'
                                 % (name, t, ', '.join(ccols))))
    print('[ok] 已补 %d 列 / %d 索引' % (len(cols), len(idx)))

    after_cols, after_idx, after_tables = plan(engine)
    if after_cols or after_idx:
        print('[fail] 复查仍有缺项：%s %s' % (after_cols, after_idx))
        return 3
    print('[ok] 复查：列与索引都已存在')

    if args.stamp_head and args.stamp_confirm != 'STAMP-HEAD':
        print('[abort] --stamp-head 是在宣称"到 head 的迁移都已生效"，'
              '必须再给 --stamp-confirm STAMP-HEAD')
        return 4
    if args.stamp_head:
        if extras:
            # 有模型没声明的东西 ⇒ "没报缺项"不能推出"迁移都跑过"，别写版本号
            print('[skip] 库里有 %d 个模型未声明的对象 ⇒ 不写 alembic_version，'
                  '请人工核对迁移历史' % len(extras))
            return 0
        if after_tables:
            # 记版本＝宣称"0001~0009 都跑过了"，而整表还缺着就是撒谎
            print('[skip] 库里仍缺整表 %s ⇒ 不写 alembic_version' % after_tables)
            return 0
        with engine.begin() as conn:
            conn.execute(sa.text('CREATE TABLE IF NOT EXISTS alembic_version '
                                 '(version_num VARCHAR(64) NOT NULL, '
                                 'CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))'))
            n = conn.execute(sa.text('select count(*) from alembic_version')).scalar()
            if n:
                print('[skip] alembic_version 已有 %d 行，不动它' % n)
            else:
                from alembic.config import Config
                from alembic.script import ScriptDirectory
                cfg = Config(os.path.join(ROOT, 'alembic.ini'))
                head = ScriptDirectory.from_config(cfg).get_current_head()
                conn.execute(sa.text('insert into alembic_version (version_num) values (:v)'),
                             {'v': head})
                print('[ok] alembic_version 记为 %s（此后新增迁移可正常 upgrade）' % head)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
