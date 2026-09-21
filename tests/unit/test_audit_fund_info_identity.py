# -*- coding: utf-8 -*-
"""`scripts/audit_fund_info_identity.py` 的行为用例（第 9 轮补）。

这个脚本查的是老板那句"注意是基金而不能是股票"最下游的一层：
`fund_info`（基金列表本身）里既有**空名字**的行，也有**股票名挂在别人的基金码**的行
（001309 存「德明利」，可这个码在基金域其实是「东方红睿逸定开混合」）。
两种修法的代价完全不同，所以这里钉住四件事：
1. 补空名 / 改真名都是**加性**的，净值与预测一行都不动；
2. 有未删除预测引用的行**不许改名**（名字是博主原话的唯一线索）；
3. 不带 `--apply` 时一个字都不写；
4. 体检通过的行绝不被碰。

注意：脚本自己开 `SessionLocal`，所以这里必须种进**同一个引擎**（conftest 已把它
钉到 pid 命名的临时 SQLite），用 in-memory 的 `test_db` 是看不见的。
"""
import csv
import glob
import io
import os
from datetime import date, datetime

import pytest

from src.models.database import (Blogger, FundHistory, FundInfo, Prediction, Post,
                                 SessionLocal)

OUT_DIR = os.path.join('docs', '迭代计划', 'run-2026-09-20')
CODES = ('001309', '002354', '000725', '159995')
OFFICIAL = {'001309': '东方红睿逸定开混合', '002354': '博时裕腾纯债债券A'}


def _verdict_for(code, stored_name):
    """替网络探测做决定（单测零网络）：股票名挂着基金码 = not_a_fund。"""
    if code in OFFICIAL and stored_name and stored_name != OFFICIAL[code]:
        return {'verdict': 'not_a_fund', 'official_name': OFFICIAL[code],
                'reason': '基金域这个码是另一只基金'}
    if not (stored_name or '').strip():
        return {'verdict': 'unknown', 'official_name': '大成添利宝货币B',
                'reason': '存的名字是空的，没法比对'}
    return {'verdict': 'ok', 'official_name': stored_name, 'reason': '一致'}


@pytest.fixture
def seeded():
    """种进脚本会用的那个 SessionLocal，用完按主键删干净（共享库不能留垃圾）。"""
    db = SessionLocal()
    blogger = Blogger(name='T-基金列表体检博主')
    db.add(blogger)
    db.commit()
    post = Post(blogger_id=blogger.id, title='T-基金列表体检帖', content='看多德明利',
                post_date=datetime(2026, 9, 1))
    db.add(post)
    db.commit()
    infos = [
        FundInfo(fund_code='001309', fund_name='德明利'),        # 可改名
        FundInfo(fund_code='002354', fund_name='天娱数科'),      # 被活预测引用 → 跳过
        FundInfo(fund_code='000725', fund_name=''),              # 空名 → 补
        FundInfo(fund_code='159995', fund_name='芯片ETF'),       # ok → 不动
    ]
    hist = [FundHistory(fund_code=c, nav_date=date(2026, 9, 18), nav=1.0)
            for c in ('001309', '002354')]
    pred = Prediction(blogger_id=blogger.id, post_id=post.id, sector='德明利',
                      fund_code='002354', fund_name='天娱数科',
                      prediction_type='看涨', prediction_content='德明利要涨',
                      prediction_date=date(2026, 8, 30),
                      target_date=date(2026, 9, 30), is_deleted=False)
    db.add_all(infos + hist + [pred])
    db.commit()
    try:
        yield db
    finally:
        pid = post.id
        db.query(Prediction).filter(Prediction.post_id == pid).delete()
        db.query(FundHistory).filter(FundHistory.fund_code.in_(CODES)).delete()
        db.query(FundInfo).filter(FundInfo.fund_code.in_(CODES)).delete()
        db.query(Post).filter(Post.id == pid).delete()
        db.query(Blogger).filter(Blogger.name == 'T-基金列表体检博主').delete()
        db.commit()
        db.close()


def _run(monkeypatch, db, *argv):
    from scripts import audit_fund_info_identity as tool
    monkeypatch.setattr('src.services.sector_identity_audit.arbitrate_mapping',
                        lambda code, stored_name, sector='', **kw: _verdict_for(
                            code, stored_name))
    monkeypatch.setattr(tool.sys, 'argv', ['audit_fund_info_identity.py', *argv])
    rc = tool.main()
    db.expire_all()
    return rc


def _names(db):
    return {r.fund_code: r.fund_name for r in db.query(FundInfo).filter(
        FundInfo.fund_code.in_(CODES)).all()}


def test_dry_run_writes_nothing(seeded, monkeypatch):
    before = _names(seeded)
    _run(monkeypatch, seeded, '--rename-to-official')
    assert _names(seeded) == before, '没带 --apply 就一个字都不该写'


def test_empty_name_is_filled_and_unreferenced_stock_is_renamed(seeded, monkeypatch):
    assert _run(monkeypatch, seeded, '--rename-to-official', '--apply') == 0
    names = _names(seeded)
    assert names['000725'] == '大成添利宝货币B', '空名要补上基金域自证到的品种名'
    assert names['001309'] == '东方红睿逸定开混合', '股票名要改回这个码真正的基金'
    assert names['159995'] == '芯片ETF', '体检通过的行不该被动'
    # 改名不许牵连净值：001309 那行属于同码的真基金，删了就不可再生
    assert seeded.query(FundHistory).filter(
        FundHistory.fund_code == '001309').count() == 1


def test_a_row_with_live_predictions_is_never_renamed(seeded, monkeypatch):
    """002354 有未删除预测引用：改名会把博主原话的唯一线索抹掉。"""
    _run(monkeypatch, seeded, '--rename-to-official', '--apply')
    assert _names(seeded)['002354'] == '天娱数科'


def test_report_lists_the_stock_named_rows(seeded, monkeypatch, capsys):
    """体检清单要真能落地并说清"哪几行是股票名"。

    文件名只到秒：同一秒内跑两次会**覆盖**同一个文件，所以不能靠"目录里多了个文件"
    来找产物，直接读脚本自己打出来的路径。
    """
    _run(monkeypatch, seeded, '--rename-to-official')
    printed = [ln for ln in capsys.readouterr().out.splitlines() if '清单' in ln]
    assert printed, '脚本没打出清单路径'
    path = printed[-1].split('清单')[-1].strip()
    assert os.path.exists(path), path
    with io.open(path, encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    assert {'001309', '002354'} <= {r['fund_code'] for r in rows
                                    if r['verdict'] == 'not_a_fund'}
    os.remove(path)                  # 用例产物不留在仓库里
