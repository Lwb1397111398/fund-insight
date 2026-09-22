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
    # 第 25 轮 B 实测：先剥注释再剥字面量会把 `like '%--%'` 拦腰截断（执行串≠输入串）
    "select fund_name from fund_info where fund_name like '%--%' limit 1",
    "select 'it''s a test' as x",
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
    # 第 25 轮 A 举出五条穿过上一版关键词表、在 PG 上有真实副作用的函数调用
    ('select pg_advisory_lock(1)', '会拿到跨会话锁'),
    ("select set_config('search_path','public', false)", '改会话参数'),
    ('select pg_switch_wal()', '强制切 WAL'),
    ('select pg_reload_conf()', '让服务端重读配置'),
    ("select pg_read_file('/etc/passwd')", '读服务器文件'),
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


class _FakeConn:
    """假连接：让"只读探针"三种结局都能在家里跑到（本机没有 PG 服务）。"""

    def __init__(self, write_error=None, write_succeeds=False):
        self.write_error = write_error
        self.write_succeeds = write_succeeds
        self.calls = []

    def exec_driver_sql(self, sql):
        self.calls.append(sql.strip().split()[0].upper())
        if 'CREATE TEMP' in sql.upper():
            if self.write_succeeds:
                return None
            raise RuntimeError(self.write_error)
        return None


def test_pg_read_only_probe_is_not_self_confirming():
    """探针必须**会因为写成功而报警**——这是它比 `SET + SHOW` 强的地方。

    第 25 轮两份复评共同判旧写法为恒真守护（MAJOR）：`SHOW` 读回的是刚设进去的会话值，
    答不出"它什么情况下会红"。现在三条结局各有断言：写被只读挡下=通过、
    写居然成功=中止、写报别的错=中止。
    """
    ok = _FakeConn(write_error='cannot execute CREATE TABLE in a read-only transaction')
    assert q._pg_read_only_probe(ok) is None

    not_ro = _FakeConn(write_succeeds=True)
    why = q._pg_read_only_probe(not_ro)
    assert why and '只读' in why, '探针写成功了却不报警 ⇒ 恒真守护又回来了'

    other = _FakeConn(write_error='permission denied for database')
    why2 = q._pg_read_only_probe(other)
    assert why2, '非只读类错误也要中止，不能当"已通过"'


def test_the_tool_pins_the_mirror_not_the_env_default():
    """工具默认必须走本地镜像：这条测试就是在防我上次那个"只设变量不调守卫"的错。"""
    src = open(os.path.join(ROOT, 'scripts', 'q.py'), encoding='utf-8').read()
    assert 'pin_local_sqlite(use_mirror_default=True)' in src
    assert '--production' in src, '要查生产必须显式说出来，不能靠环境变量碰运气'
    # PG 侧只读必须由"建连接时的隔离级别 + 真写探针"两件事保证
    assert "isolation_level='READ ONLY'" in src
    assert '_pg_read_only_probe(conn)' in src
    assert 'mode=ro' in src
    # 旧那套"SET 完自己 SHOW 一遍"不能回来：核对的是自己刚设的会话值，恒真。
    # （注释里允许提到它——那是在说明为什么不能用——所以只挡真正的调用形状。）
    assert "exec_driver_sql('SET default_transaction_read_only" not in src
    assert "exec_driver_sql('SHOW default_transaction_read_only" not in src

