# -*- coding: utf-8 -*-
"""S6 预检（**只读**）：回写清单里的每一行，放到生产上到底能不能被服务。

为什么要它：第 27 轮两份复评共同指出一件事——清单里的 `is_fetchable` 是**在镜像上算的**，
所以"生产取不到净值"这种行会在客户端闸门里被放成"可服务"（D 用它自己那份数证明了我的闸会全放）。
本脚本把判据搬到**生产侧**跑，只读，输出逐行回执。

只读怎么保证：直接复用 `scripts/q.py` 的那一套——
`create_engine(url, execution_options={'postgresql_readonly': True})`
+ `_pg_read_only_probe()` **真试一次写临时表**，只有数据库自己拒绝才算只读成立。
不打印连接串（只印 host 段）。
"""
import argparse
import importlib.util
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

MANIFEST = os.path.join(ROOT, 'docs', '迭代计划', 'run-2026-09-20',
                        'prod-writeback-sector-mappings.json')
MIN_NAV_ROWS = 30          # 与 sync_sector_map_funds.py 同一把尺子

spec = importlib.util.spec_from_file_location('q_guard', os.path.join(ROOT, 'scripts', 'q.py'))
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)


def prod_conn():
    from dotenv import load_dotenv
    load_dotenv(os.path.join(ROOT, '.env'))
    url = os.environ.get('DATABASE_URL', '')
    if not url.lower().startswith(('postgres', 'postgresql')):
        raise SystemExit('[abort] .env 的 DATABASE_URL 不是 PostgreSQL，拒绝继续')
    import sqlalchemy as sa
    import _db_guard
    print('[target] %s（只读）' % _db_guard.db_kind(url))
    engine = sa.create_engine(url, execution_options={'postgresql_readonly': True})
    conn = engine.connect()
    why = q._pg_read_only_probe(conn)
    if why:
        conn.close()
        engine.dispose()
        raise SystemExit('[abort] 只读没成立：%s' % why)
    print('[guard] 探针写临时表被数据库拒绝 ⇒ 这条连接确实只读')
    return conn


def quoted(codes):
    return ','.join("'%s'" % c.replace("'", "''") for c in sorted(codes))


def relevance_breakdown(rows):
    """用体检脚本自己的判据，把清单按"板块↔名册官方名"分档（离线名册，不打网）。

    这一档**不是**"82 行都错了"：`存储→芯片ETF`、`CPO→通信ETF`、`面板→消费电子ETF` 语义上
    是对的，只是**字面这根轴说不出话**——那正是 T3 语义判据要管的东西（而它还没标定，见任务 #22）。
    把它量出来是为了：写生产之前知道这批行里有多少是"字面看不出关系"的，别把它当已核对。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'audit_probe', os.path.join(ROOT, 'scripts', 'audit_static_sector_map.py'))
    audit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(audit)
    roster_path = os.path.join(ROOT, 'data', '_roster_full.json')
    if not os.path.exists(roster_path):
        return None
    raw = json.load(io.open(roster_path, encoding='utf-8'))
    by_code = raw.get('by_code') if 'by_code' in raw else raw
    buckets = {'core': [], 'char': [], 'none': []}
    for r in rows:
        sector = (r.get('sector_name') or r.get('sector') or '').strip()
        code = r.get('fund_code')
        official = ((by_code.get(code) or {}).get('name') or '').strip()
        kind = audit.relevance_kind(sector, official) or 'none'
        buckets[kind].append((sector, code, official, bool(r.get('reviewed'))))
    return buckets


def identity_audit(conn):
    """生产侧身份体检回执（只读）：已有多少行带着身份结论、分布如何、多少行压根没证据。

    为什么单独一个模式（任务 #29）：`sweep_sector_mappings.py` 钉死本地镜像，
    服务端也没有"跑一次体检"的路由 ⇒ S6 第 3 步"生产身份体检跑一次并看回执"以前**没有工具**。
    注意这里不重算判据（重算要扫 2.7 万名册并把结论写进 `evidence`，那是写操作），
    只把生产**已有**的结论解析出来 —— 如果证据列是空的，结论就是"体检在生产上没有输入"。
    """
    import sqlalchemy as sa

    from src.services.sector_identity_audit import identity_view

    rows = conn.execute(sa.text(
        'select id, sector_name, fund_code, fund_name, evidence, is_fetchable, '
        '       reviewed, reviewed_by, owner_locked, match_source, verify_message '
        '  from sector_fund_mapping order by sector_name')).fetchall()
    verdicts, no_evidence, unservable, relevance_low = {}, [], [], []
    for r in rows:
        row = type('R', (), dict(zip(
            ('id', 'sector_name', 'fund_code', 'fund_name', 'evidence', 'is_fetchable',
             'reviewed', 'reviewed_by', 'owner_locked', 'match_source', 'verify_message'),
            r)))()
        view = identity_view(row)
        if not row.evidence:
            no_evidence.append(row.sector_name)
        v = view['identity_verdict'] or '（无身份结论）'
        verdicts[v] = verdicts.get(v, 0) + 1
        if not view['servable']:
            unservable.append((row.sector_name, row.fund_code, v))
        if view.get('relevance_low'):
            relevance_low.append(row.sector_name)
    print('\n===== 生产身份体检回执（只读解析，不重算判据）=====')
    print('[行] 生产映射 %d 行；evidence 为空 %d 行；被判不可服务 %d 行；相关性旗标 %d 行'
          % (len(rows), len(no_evidence), len(unservable), len(relevance_low)))
    print('[结论分布] %s' % '、'.join('%s %d' % kv for kv in sorted(verdicts.items())))
    for sector, code, v in unservable[:20]:
        print('   [不可服务] %-14s %-8s verdict=%s' % (sector, code, v))
    if no_evidence:
        print('[读法] evidence 全空的那些行 = **生产上没跑过身份体检**：'
              '体检结论存在 evidence JSON 里（零新列），所以回写之前这一步在生产没有输入。')
    return 0


def main():
    ap = argparse.ArgumentParser(description='S6 生产侧只读预检')
    ap.add_argument('--identity-audit', action='store_true',
                    help='只解析生产已有的身份结论（不读回写清单、不发请求）')
    args = ap.parse_args()
    if args.identity_audit:
        conn = prod_conn()
        try:
            return identity_audit(conn)
        finally:
            conn.close()

    sa = __import__('sqlalchemy')
    man = json.load(io.open(MANIFEST, encoding='utf-8'))
    rows = man['mappings']
    codes = {r.get('fund_code') for r in rows if r.get('fund_code')}
    sectors = {r.get('sector_name') or r.get('sector') for r in rows}
    print('[清单] %d 行、%d 个代码、%d 个板块；generated_at=%s sha256=%s'
          % (len(rows), len(codes), len(sectors), man.get('generated_at'),
             (man.get('sha256') or '')[:16]))

    raw = json.load(io.open(os.path.join(ROOT, 'data', '_roster_full.json'), encoding='utf-8'))
    by_code_all = raw.get('by_code') if 'by_code' in raw else raw
    not_fund = sorted(c for c in codes if c not in by_code_all)
    print('[只允许基金] %d 个代码里，全量基金名册（%d 只）查无此码的：%d 个 %s'
          % (len(codes), len(by_code_all), len(not_fund), '、'.join(not_fund)))
    bd = relevance_breakdown(rows)
    if bd:
        print('[字面这根轴] 核心词命中 %d / 只共一个汉字 %d / 字面看不出关系 %d（后者里 reviewed=True %d 行）'
              % (len(bd['core']), len(bd['char']), len(bd['none']),
                 sum(1 for x in bd['none'] if x[3])))
        print('   注：字面看不出关系 ≠ 错配（存储→芯片ETF、CPO→通信ETF 语义是对的），'
              '它说的是"这根判据说不出话"，要的是 T3 语义判据＋金标集（任务 #22）')
        for sector, code, official, rev in bd['none'][:8]:
            print('   %-10s %-8s 官方名=%-24s reviewed=%s' % (sector, code, official, rev))

    conn = prod_conn()
    try:
        info = {r[0]: r[1] for r in conn.execute(sa.text(
            'select fund_code, fund_name from fund_info where fund_code in (%s)' % quoted(codes)))}
        hist = {r[0]: (r[1], r[2]) for r in conn.execute(sa.text(
            'select fund_code, count(*), max(nav_date) from fund_history '
            'where fund_code in (%s) group by fund_code' % quoted(codes)))}
        existing = {r[0]: (r[1], r[2], r[3]) for r in conn.execute(sa.text(
            'select sector_name, fund_code, reviewed, owner_locked from sector_fund_mapping'))}
        live = {r[0]: r[1] for r in conn.execute(sa.text(
            'select sector, count(*) from predictions where is_deleted = false group by sector'))}
    finally:
        conn.close()

    no_info, no_nav, thin_nav, ok = [], [], [], []
    for r in rows:
        code = r.get('fund_code')
        sector = r.get('sector_name') or r.get('sector')
        n, latest = hist.get(code, (0, None))
        rec = (sector, code, info.get(code) or '（无档案）', n, latest, live.get(sector) or 0)
        if code not in info:
            no_info.append(rec)
        elif n == 0:
            no_nav.append(rec)
        elif n < MIN_NAV_ROWS:
            thin_nav.append(rec)
        else:
            ok.append(rec)

    def dump(title, items):
        print('\n== %s：%d 行' % (title, len(items)))
        for sector, code, name, n, latest, nlive in sorted(items, key=lambda x: -x[5]):
            print('   %-14s %-8s %-26s 净值%-4d 最新%-12s 生产该板块活预测%d'
                  % (sector, code, name, n, latest or '-', nlive))

    dump('生产查无此基金档案（推过去就是定价不了的映射）', no_info)
    dump('有档案但生产一行净值都没有', no_nav)
    dump('有档案、净值不足 %d 行' % MIN_NAV_ROWS, thin_nav)
    print('\n[回执合计] 可服务 %d / 净值偏薄 %d / 无净值 %d / 无档案 %d（共 %d 行）'
          % (len(ok), len(thin_nav), len(no_nav), len(no_info), len(rows)))
    blocked = no_info + no_nav
    touched = sum(nlive for *_x, nlive in blocked)
    print('[闸门前结论] 生产侧不可服务的 %d 行，牵动生产活预测 %d 条'
          % (len(blocked), touched))
    print('[对照] 生产现有映射 %d 行；清单里已有同名板块的行 %d；owner_locked %d'
          % (len(existing),
             sum(1 for r in rows if (r.get('sector_name') or r.get('sector')) in existing),
             sum(1 for v in existing.values() if v[2])))
    print('[口径] 生产库只读；净值行数按 fund_history 全量 count；"活预测"按 is_deleted=false 计')
    return 5 if blocked else 0


if __name__ == '__main__':
    raise SystemExit(main())
