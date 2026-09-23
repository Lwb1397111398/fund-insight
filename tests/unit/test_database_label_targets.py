# -*- coding: utf-8 -*-
"""`database_label()` 必须认得 Session / Engine / Connection 三种"库"的形状。

起因（第 37 轮 B 的 M-3 顺带照出来）：`scripts/run_migrations.py` 为了自报"我要往哪个库发
DDL"而调用它，传的是 `engine.connect()` 得到的 **Connection** —— SQLAlchemy 2.0 起
`Connection` 没有 `get_bind()`，函数里那个 `except Exception: return '未知库'` 把异常吞了，
于是打印出 `[库] 未知库（sqlite）`：一个看起来不像在说谎的谎。
报"哪个库"这件事的全部意义就是**不许说 Unknown**，所以这条得有用例钉着。
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.services.verdict_evidence import database_label


class _UrlOnly:
    """只想让 `url` 有值的最小对象（不用真连一个 Postgres）。"""

    def __init__(self, url):
        self.url = url


def test_an_engine_tells_which_database_it_points_at():
    """直接在 2.0 上跑过的形状：`create_engine('sqlite:///...')` 自己就是"库"。"""
    assert database_label(create_engine('sqlite:///data/fund_insight.db')) == '本地镜像库'
    assert database_label(create_engine('postgresql://u:p@h/db')) == '线上生产库'


def test_a_raw_connection_tells_which_database_it_points_at():
    """`with engine.connect() as conn:` 里那一行自报，传的就是这个形状。"""
    engine = create_engine('sqlite:///:memory:')
    with engine.connect() as connection:
        assert database_label(connection) == '本地镜像库'


def test_a_session_still_works():
    """原有的调用方（`span_report`、各脚本）传的是 Session，别为了修新的把旧的改坏。"""
    session = Session(bind=create_engine('sqlite:///:memory:'))
    try:
        assert database_label(session) == '本地镜像库'
    finally:
        session.close()


def test_other_and_unrecognisable_targets_are_reported_as_they_are():
    assert database_label(_UrlOnly('mysql://u:p@h/db')) == 'MySQL 库'
    # 真认不出来的时候才许说"未知库"——而不是"认得出、但代码路径写错了"。
    assert database_label(object()) == '未知库'
