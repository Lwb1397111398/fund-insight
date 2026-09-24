# -*- coding: utf-8 -*-
"""`database_label()` 必须认得 Session / Engine / Connection 三种"库"的形状。

起因（第 37 轮 B 的 M-3 顺带照出来）：`scripts/run_migrations.py` 为了自报"我要往哪个库发
DDL"而调用它，传的是 `engine.connect()` 得到的 **Connection** —— SQLAlchemy 2.0 起
`Connection` 没有 `get_bind()`，函数里那个 `except Exception: return '未知库'` 把异常吞了，
于是打印出 `[库] 未知库（sqlite）`：一个看起来不像在说谎的谎。
报"哪个库"这件事的全部意义就是**不许说 Unknown**，所以这条得有用例钉着。

第 42 轮 B-(c) 补的第二半：认出"是 sqlite"不等于报出"是哪个库"。
`sqlite:///data/fund_insight.db`（真镜像）、`sqlite:///data/copy_20260920.db`（回放副本）、
`sqlite:///:memory:`（测试夹具）以前都印同一句"本地镜像库"，而拿副本的数当镜像的数
正是第 23 轮那次错的最省事的复现方式。现在标签后面必须跟着文件或主机。
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src.services.verdict_evidence import database_label, target_name

ROOT = Path(__file__).resolve().parents[2]

# 一把尺子的两端都要喂同一批样品：`scripts/_db_guard.machine_name()` 与
# `src/services/verdict_evidence.target_name()` 是**同一件事的两份实现**
# （src 不能 import scripts；`_db_guard` 必须在"连哪个库还没定"之前就能被 import，
# 所以它也不能 import src）。两份实现漂了没人发现 = 两条自报各说各话，
# 所以这里逐条比对，而不是各测各的。
TARGET_SAMPLES = [
    'sqlite:///data/fund_insight.db',
    'sqlite:////E:/AI Agent/data/f.db',
    'sqlite:///E:/AI Agent/data/f.db',
    'sqlite:////home/me/f.db',
    'sqlite:///:memory:',
    'sqlite:///data/copy_20260920.db',
    'postgresql://u:S3cr3tPW@db.example.com:5432/proddb',
    'mysql://u:p@h:3306/app',
    '',
    'not-a-url',
]


class _UrlOnly:
    """只想让 `url` 有值的最小对象（不用真连一个 Postgres）。"""

    def __init__(self, url):
        self.url = url


def test_an_engine_tells_which_database_it_points_at():
    """直接在 2.0 上跑过的形状：`create_engine('sqlite:///...')` 自己就是"库"。"""
    assert database_label(create_engine('sqlite:///data/fund_insight.db')) \
        == '本地镜像库（data/fund_insight.db）'
    assert database_label(create_engine('postgresql://u:p@h/db')) \
        == '线上生产库（postgresql://h/db）'


def test_a_raw_connection_tells_which_database_it_points_at():
    """`with engine.connect() as conn:` 里那一行自报，传的就是这个形状。"""
    engine = create_engine('sqlite:///:memory:')
    with engine.connect() as connection:
        assert database_label(connection) == '内存 sqlite（不落盘，通常是测试夹具）'


def test_a_session_still_works():
    """原有的调用方（`span_report`、各脚本）传的是 Session，别为了修新的把旧的改坏。"""
    session = Session(bind=create_engine('sqlite:///data/fund_insight.db'))
    try:
        assert database_label(session) == '本地镜像库（data/fund_insight.db）'
    finally:
        session.close()


def test_other_and_unrecognisable_targets_are_reported_as_they_are():
    assert database_label(_UrlOnly('mysql://u:p@h/db')) == 'MySQL 库（mysql://h/db）'
    # 真认不出来的时候才许说"未知库"——而不是"认得出、但代码路径写错了"。
    assert database_label(object()) == '未知库'


def test_a_copy_is_never_reported_as_the_mirror():
    """"本地镜像库"这四个字必须**真的**落在那个文件上，副本与夹具不许共用同一个标签。

    为什么单独立一条：整个"报库名"的机制存在的唯一理由就是第 23 轮那次错
    （拿镜像的数当系统的数）。标签本身如果不区分镜像/副本/内存，它就只是把错误
    说得更自信了。
    """
    copy = database_label(create_engine('sqlite:///data/copy_20260920.db'))
    assert '不是镜像库' in copy and 'copy_20260920.db' in copy, copy
    assert not copy.startswith('本地镜像库'), copy
    assert not database_label(create_engine('sqlite:///:memory:')).startswith('本地镜像库')


def test_the_two_self_report_rulers_stay_identical():
    """两份 `machine_name` 实现必须逐条给出同一个名字（漂了就让这条变红）。"""
    sys.path.insert(0, str(ROOT / 'scripts'))
    try:
        import _db_guard
    except Exception as exc:                              # noqa: BLE001
        pytest.skip('这台机器导入不了 `_db_guard`：%s' % exc)
    finally:
        sys.path.remove(str(ROOT / 'scripts'))
    for url in TARGET_SAMPLES:
        assert target_name(url) == _db_guard.machine_name(url), (
            '同一个连接串，src 侧报成 %r、守卫侧报成 %r（样品 %r）'
            % (target_name(url), _db_guard.machine_name(url), url))
