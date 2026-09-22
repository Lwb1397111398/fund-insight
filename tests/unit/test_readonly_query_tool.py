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
])
def test_write_or_multi_statement_is_refused(sql, why):
    with pytest.raises(ValueError):
        q._assert_read_only(sql)


def test_the_tool_pins_the_mirror_not_the_env_default():
    """工具默认必须走本地镜像：这条测试就是在防我上次那个"只设变量不调守卫"的错。"""
    src = open(os.path.join(ROOT, 'scripts', 'q.py'), encoding='utf-8').read()
    assert 'pin_local_sqlite(use_mirror_default=True)' in src
    assert '--production' in src, '要查生产必须显式说出来，不能靠环境变量碰运气'
