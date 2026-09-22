# -*- coding: utf-8 -*-
"""`scripts/q.py` 的只读闸门。

为什么要给它写测试：这个工具存在的唯一理由就是我绕过守卫把只读查询打到了生产
（2026-09-22）。闸门要是自己会漏，那就等于没有闸门 —— 而"看着像只读"的语句
（`select 1; drop table`、带 `pragma =` 的写）恰恰是最容易漏的。
"""
import importlib.util
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location('q_tool', os.path.join(ROOT, 'scripts', 'q.py'))
q = importlib.util.module_from_spec(spec)
spec.loader.exec_module(q)


@pytest.mark.parametrize('sql', [
    'select count(*) from predictions',
    '  SELECT 1 ',
    'with t as (select id from predictions) select * from t',
    'explain select 1',
    'select count(*) from predictions;',                 # 结尾分号允许
    'select 1 -- ; 这只是注释里的分号',
    # 第 24 轮两份复评都点到：把 `replace` 这种读函数也拉黑，结果是逼人退回
    # `python -c`——那才是真正绕开守卫的路径。
    "select replace(fund_name,'A','B') n from fund_info limit 1",
    "select fund_name from posts where content like '%delete from%' limit 1",
    "select fund_name from fund_info where fund_name = 'a;b' limit 1",
])
def test_read_only_statements_pass(sql):
    assert q._assert_read_only(sql)


@pytest.mark.parametrize('sql,why', [
    ('update bloggers set grade=%s', '写语句'),
    ('delete from predictions', '写语句'),
    ('drop table bloggers', '写语句'),
    ('alter table bloggers add column x int', 'DDL'),
    ('select 1; drop table bloggers', '多语句藏写操作'),
    ('pragma table_info=x', '带值的 pragma 是写'),
    ('insert into bloggers values (1)', '写语句'),
    ('', '空语句'),
    # 以下四条在改前全部**通过**闸门（两份复评各自复现）。它们在 PostgreSQL 上
    # 都是合法的写：`select ... into` 建表、`nextval` 推进序列、`copy ... to` 落文件。
    ('select 1 into junk', 'PG 的 SELECT INTO 会真建表'),
    ('select a into t from fund_info', '同上，带来源表'),
    ("select nextval('bloggers_id_seq')", '序列会被真的推进'),
    ("select setval('x', 1)", '同上'),
    ('explain analyze copy predictions to \'/tmp/x.csv\'', 'COPY 是写'),
    ('select pg_sleep(1); drop table bloggers', '多语句'),
])
def test_write_or_multi_statement_is_refused(sql, why):
    with pytest.raises(ValueError):
        q._assert_read_only(sql)


def test_old_gate_would_have_let_those_four_through():
    """把"改动前会红"钉成断言，而不是我在提交说明里说一句"已修"。

    这条用例的意义：如果哪天有人把闸门改回上一版的关键词表，它会立刻红给他看。
    """
    import re
    old = re.compile(r'\b(insert|update|delete|drop|alter|truncate|create|replace|'
                     r'vacuum|attach|pragma\s*=\s*|call|execute|grant|revoke|set)\b', re.I)
    for sql in ('select 1 into junk', "select nextval('s')",
                "select setval('x',1)", "explain analyze copy predictions to '/tmp/x'"):
        assert not old.search(sql), '这条老闸门本该漏掉它：%s' % sql
        with pytest.raises(ValueError):
            q._assert_read_only(sql)


def test_sqlite_side_is_read_only_at_engine_level(tmp_path):
    """正则挡不住方言，`mode=ro` 才挡得住：真写一次必须被数据库拒绝。

    为什么单独测这个：只测 `_assert_read_only` 的话，"闸门"和"能不能写"是两件事——
    上一版的实际保护恰好来自"本地恰好是 SQLite、那些 PG 写法语法不通"，而不是设计。
    """
    import sqlite3
    db = tmp_path / 'probe.sqlite3'
    seed = sqlite3.connect(str(db))
    seed.execute('create table t (a int)')
    seed.execute('insert into t values (1)')
    seed.commit()
    seed.close()

    conn = q._connect_sqlite_ro('sqlite:///' + str(db).replace('\\', '/'))
    try:
        assert [r[0] for r in conn.execute('select a from t')] == [1]
        with pytest.raises(sqlite3.OperationalError) as exc:
            conn.execute('insert into t values (2)')
            conn.commit()
        assert 'readonly' in str(exc.value).lower()
    finally:
        conn.close()


def test_the_tool_pins_the_mirror_not_the_env_default():
    """工具默认必须走本地镜像：这条测试就是在防我上次那个"只设变量不调守卫"的错。"""
    src = open(os.path.join(ROOT, 'scripts', 'q.py'), encoding='utf-8').read()
    assert 'pin_local_sqlite(use_mirror_default=True)' in src
    assert '--production' in src, '要查生产必须显式说出来，不能靠环境变量碰运气'
    # 生产侧的只读必须"设完再读回来核对"，不能只发一条 SET 就算数
    assert 'SHOW default_transaction_read_only' in src
    assert 'mode=ro' in src

