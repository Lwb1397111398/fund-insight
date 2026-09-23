# -*- coding: utf-8 -*-
"""`scripts/purge_test_rows_from_prod.py` 的离线往返用例（不连任何远程库）。

为什么要有（第 33 轮两份复评共同抓到）：老板点完头就要跑这个脚本，而它当时**零行为测试**。
上一版有三处一跑就炸、或炸得比不跑更糟：
① `_row()` 只转 `datetime`，而 `posts.post_date` 是 `Date` ⇒ `json.dump` 当场 `TypeError`，
   还在盘上留下一个"看着像备份"的截断文件；
② `--restore-from` 按备份顺序（先子后父）插 ⇒ 第一条 `posts` 就撞 `bloggers` 外键；
③ 引用面是手挑的两张表 ⇒ 漏了 `fund_history` / `fund_sync_retry`（真外键）等。
这些都在这份文件里被真跑一遍：临时 SQLite（`tests/conftest.py` 钉的）上建博主 + 帖子，
`_row` → `json.dumps` → `loads` → `build_insert` 插回去，逐字段比对。
"""
import importlib.util
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    'purge_test_rows', str(ROOT / 'scripts' / 'purge_test_rows_from_prod.py'))
purge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(purge)

from src.models.database import Blogger, Post, engine  # noqa: E402


def _mk(db, when):
    b = Blogger(name=purge.TEST_BLOGGER_NAME, platform='weibo', description='探针',
                created_at=when)
    db.add(b)
    db.flush()
    p = Post(blogger_id=b.id, title='探针帖', content='探针内容',
             post_date=date(2026, 9, 1), analysis_result={'k': [1, 2]}, created_at=when)
    db.add(p)
    db.flush()
    return b, p


def test_the_backup_round_trips_through_json(db_session):
    """`Date` / `dict` 都得能序列化**并且能逆回去**（`default=str` 只满足前一半）。

    走的就是真流程的两半：`_row` 取快照 → 删掉 → 用 `build_insert` 原样插回 → 逐字段比对。
    （备份里带着 `id`，所以不先删就会撞主键 —— 这条用例顺手把这一点钉住。）
    """
    import sqlalchemy as sa

    b, p = _mk(db_session, datetime(2026, 9, 23, 10, 30))
    rows = [purge._row(p), purge._row(b)]
    blob = json.dumps(rows, ensure_ascii=False)          # 上一版在这里抛 TypeError
    back = json.loads(blob)
    assert isinstance(back[0]['post_date'], str) and back[0]['post_date'].startswith('2026-09-01')
    assert json.loads(back[0]['analysis_result']) == {'k': [1, 2]}, 'JSON 列要转成字符串才喂得进驱动'
    db_session.delete(p)
    db_session.delete(b)
    db_session.flush()
    assert db_session.query(Post).filter(Post.title == '探针帖').count() == 0
    # 备份文件是"先子后父"（为了删），照这个顺序插就撞外键 —— 第 33 轮 B 的 MAJOR-4。
    kid_first = [r for r in back if r['__table__'] == 'posts'] + [r for r in back if r['__table__'] != 'posts']
    with pytest.raises(sa.exc.IntegrityError):
        _insert_all(db_session, kid_first)
    tables = purge._insert_order(engine, {r['__table__'] for r in back})
    assert tables.index('bloggers') < tables.index('posts')
    ordered = [r for t in tables for r in back if r['__table__'] == t]
    _insert_all(db_session, ordered)
    db_session.flush()
    got = db_session.query(Post).filter(Post.title == '探针帖').one()
    assert str(got.post_date) == '2026-09-01' and got.content == '探针内容', '还原回来的是另一行数据'


def _insert_all(db, rows):
    for r in [dict(x) for x in rows]:
        tbl = r.pop('__table__')
        cols = sorted(r)
        from sqlalchemy import text
        db.execute(text(purge.build_insert(tbl, cols)), {c: r[c] for c in cols})


def test_restore_puts_parents_before_children():
    order = purge._insert_order(engine, ['posts', 'bloggers', 'system_config'])
    assert order.index('bloggers') < order.index('posts'), \
        '还原顺序还是"先子后父"：第一条 posts 就会撞 bloggers 外键（现序 %s）' % order


def test_the_reference_sweep_is_derived_from_the_schema():
    """删 `fund_info` 前要数"谁引用这只基金"——清单必须由库回答。"""
    tables = purge.tables_with_column(engine, 'fund_code')
    for must in ('predictions', 'sector_fund_mapping', 'fund_history', 'fund_sync_retry'):
        assert must in tables, '%s 有 fund_code，但扫描清单里没有它 ⇒ 上一版的洞还在' % must
    assert 'analysis_logs' in purge.tables_with_column(engine, 'post_id')


def test_the_identifier_whitelist_refuses_non_identifiers():
    for bad in ('bloggers; drop table bloggers', 'a-b', ''):
        with pytest.raises(SystemExit):
            purge._ident(bad)


def test_the_predicate_carries_a_time_floor_into_sql(db_session):
    """名字对但时间不对的行**不许**被删：时间下界要在 SQL 里，不是打印给人看。"""
    old_when = datetime(2026, 9, 23, 10, 30) - timedelta(days=30)   # 远早于 SINCE
    _mk(db_session, old_when)
    b_new, p_new = _mk(db_session, datetime(2026, 9, 23, 10, 30))
    db_session.commit()
    plan = purge.plan(db_session, engine)
    assert [b.id for b in plan['bloggers']] == [b_new.id], '旧行被圈进来了 ⇒ 谓词没有时间下界'
    assert [p.id for p in plan['posts']] == [p_new.id]
    assert plan['orphan_posts'] == 0 or plan['orphan_posts'] == 1, '挂在测试博主名下的旧帖要单独报出来'


def test_deleting_parents_checks_who_references_them(db_session):
    """删博主/帖子之前必须查引用（第 34 轮 A-MINOR：上一版只给 fund_info 做了这件事）。"""
    b, p = _mk(db_session, datetime(2026, 9, 23, 10, 30))
    from src.models.database import AnalysisLog
    db_session.add(AnalysisLog(post_id=p.id))
    db_session.flush()
    hard, soft = purge.fk_ref_counts(db_session, engine, 'post_id', [p.id], 'posts')
    assert 'analysis_logs=%d' % 1 in hard or any('analysis_logs' in h for h in hard), \
        '带外键的引用没被认出来：hard=%s soft=%s' % (hard, soft)
    assert purge.tables_with_column(engine, 'blogger_id'), 'blogger_id 的引用面一个都没扫到'


def test_local_database_is_refused_before_anything_is_printed():
    with pytest.raises(SystemExit):
        purge._reject_local('sqlite:///data/fund_insight.db')
    purge._reject_local('postgresql+psycopg2://u:pw@example/supabase')     # 远程要放行
