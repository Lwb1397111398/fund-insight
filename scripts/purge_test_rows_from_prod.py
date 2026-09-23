# -*- coding: utf-8 -*-
"""清理**我的测试会话误写进生产库**的假数据（默认只预检，真删要确认词，删前逐行备份）。

发生了什么（2026-09-23，北京时间 10:17 与 10:37 两批）：`tests/conftest.py` 里我在
"钉 `DATABASE_URL` 到临时 SQLite"那行**之前**导入了一次 `src.utils.…`，而 `src/__init__.py`
会拉起 `src.core.config` ⇒ 应用 engine 绑上了 `.env` 里的生产 Supabase。
后果：几条夹具数据落在了线上（只读预检实测，均在 `>= 2026-09-23 10:00`）：
    bloggers        8 行，名字全是 `T-基金列表体检博主`（id 334-341）
    posts           8 行，挂在上面那 8 个博主下
    system_config   1 行，`config_key = nav_backfill_proof:TEST_BF`
    fund_info       1 行，`159877 / 占位基金`（id 302，updated_at 今天）
这些行会进博主榜与统计 ⇒ 老板界面上的数被我的测试污染了。conftest 的顺序已经修好，
并加了一条"钉库之前不许导入 `src`"的断言（见 `tests/conftest.py`）。

为什么单独写一个脚本而不是手敲 DELETE：这仓库为"批量删除"反复付过账 ⇒
一律 先备份被删的行、逐行 receipt、`--restore-from` 能按原样插回去。

为什么谓词按**名字/键**而不是按时间：时间窗能圈进来的可能是老板真数据。
名字/键是夹具里写死的字符串，匹配不到真博主。（时间只用来打印"我看到了什么"。）

用法：
    python scripts/purge_test_rows_from_prod.py --production              # 只读预检 + 出计划
    python scripts/purge_test_rows_from_prod.py --production --apply --confirm CLEAN-TEST-ROWS
    python scripts/purge_test_rows_from_prod.py --production --restore-from backup/purge-test-rows-*.json --confirm RESTORE-TEST-ROWS
"""
import argparse
import io
import json
import os
import re
import sys
from datetime import date, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
for _st in (sys.stdout, sys.stderr):
    try:
        _st.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

CONFIRM_TOKEN = 'CLEAN-TEST-ROWS'
RESTORE_CONFIRM_TOKEN = 'RESTORE-TEST-ROWS'
# 夹具里写死的字符串：只按这些匹配，绝不按时间窗删。
TEST_BLOGGER_NAME = 'T-基金列表体检博主'
TEST_CONFIG_KEY = 'nav_backfill_proof:TEST_BF'
TEST_FUND = ('159877', '占位基金')
SINCE = '2026-09-23 10:00'


def _reject_local(url):
    """这个脚本的靶子必须是远程库：本地镜像没有这些行，误跑只会报"干净"给人假安心。"""
    if str(url).lower().startswith('sqlite'):
        print('[abort] 当前连的是 SQLite（%s）—— 本脚本只处理**生产**误写，'
              '要查镜像请直接用 scripts/q.py。' % url)
        raise SystemExit(4)


def _referencing(db, engine, column, value, refer_target):
    """**从库的列推**有哪些表引用这个值，而不是手挑一张清单。

    第 33 轮 B 的 MAJOR-5：上一版只数了 `predictions` + `sector_fund_mapping`，
    而 `fund_history` / `user_fund_bindings` / `fund_holdings` / `fund_sync_retry`
    同样带 `fund_code`（其中 `fund_sync_retry` 还是**真外键**，撞上就整轮回滚）。
    换成"凡是有这一列的表都数一遍"，下次事故换表也不会漏。
    """
    import sqlalchemy as sa

    total, hits, fk_hits = 0, [], []
    for tbl in tables_with_column(engine, column):
        n = db.execute(sa.text('select count(*) from %s where %s = :v'
                               % (_ident(tbl), _ident(column))), {'v': value}).scalar() or 0
        total += n
        if not n:
            continue
        # 分两类：**有外键**的引用会真的让删除失败（或级联掉不该掉的行）；
        # 没外键的引用只是删除后留孤儿行 —— 孤儿要报出来给人看，但不该把删除卡死。
        # 第 33 轮：上一版把两类混成一个数字，结果 `fund_history` 的 8 行旧净值
        # 让脚本拒绝删掉"我插入的那一行基金档案"，而生产库里本来就没有它的档案行。
        fk = bool([f for f in _foreign_keys(engine, tbl)
                  if f.get('referred_table') == refer_target])
        (fk_hits if fk else hits).append('%s=%d' % (tbl, n))
    return total, hits, fk_hits


def _foreign_keys(engine, tbl):
    from sqlalchemy import inspect as sa_inspect

    try:
        return sa_inspect(engine).get_foreign_keys(tbl) or []
    except Exception:
        return []


def tables_with_column(engine, column):
    # 库里哪些表有这一列，由 schema 回答，不是作者手挑一张清单（第 33 轮 B 的 MAJOR-5）。
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(engine)
    return sorted(t for t in insp.get_table_names()
                  if column in {c['name'] for c in insp.get_columns(t)})


def build_insert(tbl, cols):
    # 还原用的 INSERT：表名/列名都过标识符白名单（MINOR-6：上一版全裸插值）。
    return 'insert into %s (%s) values (%s)' % (
        _ident(tbl), ', '.join(_ident(c) for c in cols), ', '.join(':' + c for c in cols))


_IDENT = re.compile(r'^[a-z_][a-z0-9_]*$')


def _ident(name):
    """表名/列名要插进 SQL 文本里，所以只允许"就是库里那个标识符"，别的当场拒。"""
    if not _IDENT.match(str(name or '')):
        raise SystemExit('[abort] 标识符不合法，拒绝拼进 SQL：%r' % (name,))
    return name


def plan(db, engine):
    """按名字/键 + 时间下界取出待删的行（删除顺序由调用方按外键排：先子表再父表）。"""
    from src.models.database import Blogger, FundInfo, Post, SystemConfig

    bloggers = db.query(Blogger).filter(Blogger.name == TEST_BLOGGER_NAME,
                                       Blogger.created_at >= SINCE).all()
    ids = [b.id for b in bloggers]
    posts = db.query(Post).filter(Post.blogger_id.in_(ids),
                                 Post.created_at >= SINCE).all() if ids else []
    # 挂着测试博主名下、但 created_at 早于时间窗的帖子 = 谓词与时间对不上 ⇒ 单独报出来给人看
    orphans = (db.query(Post).filter(Post.blogger_id.in_(ids)).count() - len(posts)) if ids else 0
    cfg = db.query(SystemConfig).filter(SystemConfig.config_key == TEST_CONFIG_KEY,
                                       SystemConfig.created_at >= SINCE).all()
    code, name = TEST_FUND
    funds = db.query(FundInfo).filter(FundInfo.fund_code == code, FundInfo.fund_name == name).all()
    refs, soft_hits, fk_hits = _referencing(db, engine, 'fund_code', code, 'fund_info')
    return {'bloggers': bloggers, 'posts': posts, 'config': cfg, 'funds': funds,
            'orphan_posts': orphans, 'fund_refs': refs, 'fund_ref_hits': soft_hits,
            'fund_ref_fk': fk_hits}


def _jsonable(v):
    """备份必须**可逆**：date / datetime / Decimal / JSON 都转成能 `json.dumps` 也能被
    PG 直接吃下的形态（第 33 轮 B 的 BLOCKER-3：`posts.post_date` 是 `Date`，
    上一版 `json.dump` 当场抛 TypeError，还把截断的"备份"留在盘上像真凭据）。"""
    import decimal

    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (bytes, bytearray)):
        return v.decode('utf-8', 'replace')
    return v


def _row(obj):
    from sqlalchemy import inspect as sa_inspect
    mapper = sa_inspect(type(obj))
    cols = sorted(c.key for c in mapper.columns)
    d = {c: _jsonable(getattr(obj, c)) for c in cols}
    return {'__table__': mapper.local_table.name, **d}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--production', action='store_true', required=True,
                    help='显式声明靶子是生产库（缺了就拒跑）')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--confirm', default='')
    ap.add_argument('--restore-from', default='')
    args = ap.parse_args()

    if args.restore_from:
        return restore(args)

    from src.models.database import SessionLocal, engine
    _reject_local(engine.url)
    db = SessionLocal()
    try:
        p = plan(db, engine)
        print('[target] %s' % str(engine.url).split('@')[-1])
        print('[预检] 谓词=夹具名/键 + `created_at >= %s`（两条都在 SQL 里，不是打印给人看）：'
              'posts %d → bloggers %d → system_config %d → fund_info %d'
              % (SINCE, len(p['posts']), len(p['bloggers']), len(p['config']), len(p['funds'])))
        if p['orphan_posts']:
            print('[abort] 有 %d 行帖子挂在测试博主名下但 created_at 早于 %s'
                  '⇒ 谓词与时间窗不自洽，先人工看清再说' % (p['orphan_posts'], SINCE))
            return 6
        rows = ([_row(x) for x in p['posts']] + [_row(x) for x in p['bloggers']]
                + [_row(x) for x in p['config']] + [_row(x) for x in p['funds']])
        for r in rows:
            when = r.get('created_at') or r.get('updated_at')
            print('   - %-16s id=%-6s %s  %s' % (r['__table__'], r.get('id'),
                                                 (r.get('name') or r.get('config_key')
                                                  or r.get('fund_name') or '')[:24], when))
        if p['fund_ref_hits']:
            print('[提醒] 删掉 `%s` 后，这些**没有外键**的表会留下指向它的孤儿行（%s）'
                  '—— 它们本来也是孤儿：档案行是这次测试插进来的'
                  % (TEST_FUND[0], '、'.join(p['fund_ref_hits'])))
        if p['fund_ref_fk']:
            print('[abort] `%s` 被这些**带外键**的表引用（%s）⇒ 不删这只基金档案'
                  % (TEST_FUND[0], '、'.join(p['fund_ref_fk'])))
            funds, rows = [], [r for r in rows if r['__table__'] != 'fund_info']
        else:
            funds = p['funds']
        if not args.apply:
            print('（dry-run，未改动任何数据。真删：--apply --confirm %s）' % CONFIRM_TOKEN)
            return 0
        if args.confirm != CONFIRM_TOKEN:
            print('[abort] 真删生产数据必须带 --confirm %s' % CONFIRM_TOKEN)
            return 3
        # 先整体序列化成字符串再落盘：中途抛异常不会留下一个"看着像备份"的截断文件
        blob = json.dumps(rows, ensure_ascii=False, indent=1)
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        os.makedirs(os.path.join(ROOT, 'backup'), exist_ok=True)
        path = os.path.join(ROOT, 'backup', 'purge-test-rows-%s.json' % stamp)
        with io.open(path, 'w', encoding='utf-8') as fh:
            fh.write(blob)
        print('[备份] 已落盘 %s（%d 行，%d 字节，回读可解析=%s）'
              % (path, len(rows), len(blob), json.loads(io.open(path, encoding='utf-8').read()) is not None))
        deleted = 0
        for obj in p['posts'] + p['bloggers'] + p['config'] + funds:
            tbl, oid = type(obj).__tablename__, obj.id
            db.delete(obj)
            db.flush()
            print('   [已删] %s id=%s' % (tbl, oid))
            deleted += 1
        db.commit()
        after = plan(db, engine)
        left = sum(len(after[k]) for k in ('posts', 'bloggers', 'config', 'funds'))
        print('[回执] 删除 %d 行；再跑一次预检仍匹配到 %d 行（应为 0）' % (deleted, left))
        print('[回执] 还原：python scripts/purge_test_rows_from_prod.py --production '
              '--restore-from %s --confirm %s' % (path, RESTORE_CONFIRM_TOKEN))
        return 0 if left == 0 else 5
    finally:
        db.close()


def _insert_order(engine, tables):
    """被引用表先插（父在前），否则第一条 `posts` 就撞外键（第 33 轮 B 的 MAJOR-4：
    备份按"先子后父"存是为了**删**，反过来才是**插**的顺序）。"""
    from sqlalchemy import inspect as sa_inspect

    insp = sa_inspect(engine)
    present = set(tables)
    deps = {t: {k for k in
                {fk['referred_table'] for fk in insp.get_foreign_keys(t)}
                if k in present and k != t} for t in present}
    order, done = [], set()
    while len(order) < len(present):
        ready = [t for t in present if t not in done and deps[t] <= done]
        if not ready:                      # 环（本仓库没有，但别在这里死循环）
            ready = [t for t in present if t not in done][:1]
        for t in sorted(ready):
            order.append(t)
            done.add(t)
    return order


def restore(args):
    if args.confirm != RESTORE_CONFIRM_TOKEN:
        print('[abort] 往生产插回数据必须带 --confirm %s' % RESTORE_CONFIRM_TOKEN)
        return 3
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy import text

    from src.models.database import SessionLocal, engine
    _reject_local(engine.url)
    with io.open(args.restore_from, encoding='utf-8') as fh:
        rows = json.load(fh)
    insp = sa_inspect(engine)
    real_cols = {t: {c['name'] for c in insp.get_columns(t)} for t in
                 {r['__table__'] for r in rows}}
    db = SessionLocal()
    try:
        by_table = {}
        for r in rows:
            by_table.setdefault(r.pop('__table__'), []).append(r)
        for tbl in _insert_order(engine, list(by_table)):
            cols_all = sorted({c for r in by_table[tbl] for c in r})
            # 标识符白名单：只允许"这张表在库里真有的列"，别的当场拒
            for c in cols_all:
                if c not in real_cols[tbl]:
                    raise SystemExit('[abort] %s 没有列 %s —— 备份被改过？' % (tbl, c))
            for r in by_table[tbl]:
                cols = [c for c in cols_all if c in r]
                sql = build_insert(tbl, cols)
                db.execute(text(sql), {c: r[c] for c in cols})
                print('   [已插回] %s id=%s' % (tbl, r.get('id')))
        db.commit()
        # 原样带 id 插回不会推进 PG 序列 ⇒ 老板下次建博主会撞 id=334（第 33 轮 B）
        for tbl in _insert_order(engine, list(by_table)):
            if 'id' not in real_cols[tbl]:
                continue
            mx = db.execute(text('select coalesce(max(id),0) from %s' % _ident(tbl))).scalar()
            try:
                db.execute(text("select setval(pg_get_serial_sequence('%s','id'), :m)" % _ident(tbl)),
                           {'m': mx})
            except Exception as exc:            # 非序列主键（如映射表）没有序列可回拨
                print('   [跳过序列] %s：%s' % (tbl, str(exc)[:80]))
        db.commit()
        print('[回执] 插回 %d 行，并把各表序列抬到 max(id)=%s' % (
            len(rows), {t: db.execute(text('select max(id) from %s' % _ident(t))).scalar()
                        for t in by_table}))
        return 0
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    sys.exit(main())
