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
import json
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


def _listing(path):
    """目录不存在回空列表：没带 `--apply` 的那一趟连目录都不该被建出来。"""
    return sorted(os.listdir(path)) if os.path.isdir(path) else []


@pytest.fixture(autouse=True)
def _artifacts_go_to_tmp(tmp_path, monkeypatch):
    """用例产物不许堆进仓库（第 43 轮那条"用例产物不许堆进 docs/"的同一把尺子）。

    这条脚本**每一趟**都在 `OUT_DIR` 落一份带秒数的 CSV，真改名还要在 `BACKUP_DIR`
    落一份 JSON —— 断言中途红的话这两个目录就留着残渣，而 `.gitignore` 只护住 `backup/`。
    两个目录必须**分开指**：都指 `tmp_path` 的话，那条"备份只落了一个文件"的断言
    会把 CSV 也数进去，永远红。
    """
    from scripts import audit_fund_info_identity as tool
    monkeypatch.setattr(tool, 'OUT_DIR', str(tmp_path / 'report'))
    monkeypatch.setattr(tool, 'BACKUP_DIR', str(tmp_path / 'backup'))
    return tmp_path


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


def test_the_production_flag_has_teeth_and_refuses_every_but_the_additive_fill(monkeypatch, seeded, capsys):
    """`--production` 的四道拒，全部必须排在第一次 commit 之前。

    ① 目标不是 PostgreSQL ⇒ 拒；② 要改名 ⇒ 拒（生产只允许"补空名"这种没有旧值可丢的写）；
    ③ 要写却没给确认词 ⇒ 拒；④ **给了旗子而进程里那个连接实际是 sqlite** ⇒ 拒。
    第 ④ 条才是关键：环境变量可以骗人（这台机器的 `.env` 就写着生产），
    实际 engine 绑在哪台骗不了 —— 上一版只查环境变量，等于把判据建在能撒谎的那一列上。
    """
    PROD_URL = 'postgresql+psycopg2://svc@db.example.com:5432/proddb'

    monkeypatch.setenv('DATABASE_URL', 'sqlite:///:memory:')
    assert _run(monkeypatch, seeded, '--production', '--apply',
                '--confirm', 'FUND-INFO-FILL') == 4
    assert 'PostgreSQL' in capsys.readouterr().out

    monkeypatch.setenv('DATABASE_URL', PROD_URL)
    assert _run(monkeypatch, seeded, '--production', '--rename-to-official',
                '--apply', '--confirm', 'FUND-INFO-FILL') == 4
    assert '只允许补空' in capsys.readouterr().out

    assert _run(monkeypatch, seeded, '--production', '--apply') == 4
    assert 'FUND-INFO-FILL' in capsys.readouterr().out

    assert _run(monkeypatch, seeded, '--production', '--apply',
                '--confirm', 'FUND-INFO-FILL') == 4
    out = capsys.readouterr().out
    assert '不一致' in out, out
    assert seeded.query(FundInfo).filter_by(fund_code='000725').first().fund_name == '',         '四道拒里有任何一道先写了库，这条就会红（000725 的空名被补上了）'


def test_a_dry_run_counts_the_rows_it_listed(monkeypatch, seeded, capsys):
    """dry-run 列了 1 条 `[可补]` 却印"补上 0 行" ⇒ 这句回执在生产上会误导人下结论。

    2026-09-26 实测：`--production` 那趟列出 10 条可补，末行写"空 fund_name 补上 0 行"。
    计数必须跟着"列出来的条数"走，而不是只跟"写完的条数"。
    """
    assert _run(monkeypatch, seeded) == 0     # 不给旗子：本地镜像姿势，纯计划
    out = capsys.readouterr().out
    assert '[可补] 000725 → 大成添利宝货币B' in out, out
    assert '空 fund_name：可补（未写库） 1 行' in out, out
    assert seeded.query(FundInfo).filter_by(fund_code='000725').first().fund_name == ''


# ---------------------------------------------------------------------------
# 生产改名那条通道（任务 #86）：老板逐行点名 + 自己的确认词 + 写之前先备份
# ---------------------------------------------------------------------------

def test_the_production_rename_channel_needs_named_codes_and_its_own_token(
        monkeypatch, seeded, capsys):
    """`--production` 的改名有两道新拒，且都排在第一次 commit 之前。

    ① 没有 `--only-codes` ⇒ 拒：生产的改名必须由老板逐行点名，"批量把股票名换成官方名"
      这一键永远不许存在（名字是博主原话的唯一线索）。
    ② 点了名却拿补空名那个词（`FUND-INFO-FILL`）⇒ 拒：两种写的代价不同 ——
      补空名没有旧值可丢，改名会覆盖已有名字，所以各要各的确认词。
    """
    PROD_URL = 'postgresql+psycopg2://svc@db.example.com:5432/proddb'
    monkeypatch.setenv('DATABASE_URL', PROD_URL)

    assert _run(monkeypatch, seeded, '--production', '--rename-to-official',
                '--apply', '--confirm', 'FUND-INFO-RENAME') == 4
    assert '点名' in capsys.readouterr().out

    assert _run(monkeypatch, seeded, '--production', '--rename-to-official',
                '--only-codes', '001309', '--apply',
                '--confirm', 'FUND-INFO-FILL') == 4
    out = capsys.readouterr().out
    assert 'FUND-INFO-RENAME' in out, out
    assert _names(seeded)['001309'] == '德明利', '两道拒有任何一道先写了库，这条就会红'


def test_only_codes_chooses_which_rows_may_be_renamed(seeded, monkeypatch):
    """`--only-codes` 是白名单不是黑名单：没点名的行一个字都不许动。

    这里点的是 002354（有活预测引用 ⇒ 本来就该跳过），故意不点 001309（可改名）。
    要是实现成"点名只是多一道检查、别的行照改"，001309 会被顺手改名 ⇒ 当场红。
    """
    assert _run(monkeypatch, seeded, '--rename-to-official', '--apply',
                '--only-codes', '002354') == 0
    names = _names(seeded)
    assert names['001309'] == '德明利', '没点名的行不该被顺手改名'
    assert names['002354'] == '天娱数科', '点名了但被活预测挡住 ⇒ 仍然不动'


def test_a_named_code_missing_from_the_library_is_said_out_loud(seeded, monkeypatch,
                                                                capsys):
    """打错一个代码 ⇒ 必须点名说"库里没有这一行"，不许安静地返回"改名 0 行"。"""
    assert _run(monkeypatch, seeded, '--rename-to-official',
                '--only-codes', '001309,999999') == 0
    out = capsys.readouterr().out
    assert '[点名未找到] 999999' in out, out


def test_a_real_rename_backs_up_the_old_names_before_it_commits(seeded, monkeypatch,
                                                                tmp_path, capsys):
    """真改名的那一次必须在 commit 之前把旧名字落成 JSON，并且回执逐行说出旧 → 新。

    备份默认落在仓库里的 `backup/`（`.gitignore` 有一条 `backup/`），所以 autouse 夹具
    把 `BACKUP_DIR` 指到 `tmp_path/backup`；这里再对着**默认那个目录**核一次
    "用例一个字节都没多写进去" —— 上一版这里把路径拼成了 `dirname(ROOT)`（仓库的**父目录**），
    而末尾那句断言写成 `A if cond else [] == before` 这种三元表达式，两种情况下都可能为真，
    等于一条没有牙的判据（本仓反复扣分的同一族）。
    """
    from scripts import audit_fund_info_identity as tool
    repo_backup_before = _listing(tool.BACKUP_DIR_DEFAULT)

    assert _run(monkeypatch, seeded, '--rename-to-official', '--apply',
                '--only-codes', '001309') == 0
    assert _names(seeded)['001309'] == '东方红睿逸定开混合'
    out = capsys.readouterr().out
    assert '[已改名] 001309 德明利 → 东方红睿逸定开混合' in out, out

    backup_dir = str(tmp_path / 'backup')
    files = sorted(os.listdir(backup_dir))
    assert len(files) == 1, files
    with io.open(os.path.join(backup_dir, files[0]), encoding='utf-8') as f:
        data = json.load(f)
    entry = [e for e in data['entries'] if e['fund_code'] == '001309']
    assert entry and entry[0]['old_name'] == '德明利', data
    assert entry[0]['new_name'] == '东方红睿逸定开混合', data
    # 这条断言排在跑完之后、且拿同一个函数取前后：仓库那个目录多一个文件就红
    assert _listing(tool.BACKUP_DIR_DEFAULT) == repo_backup_before, \
        '用例把备份写进了仓库里的 backup/'


def test_a_plan_writes_no_backup(seeded, monkeypatch, tmp_path):
    """没带 `--apply` 的那一趟一个字都不写 —— 备份文件也不算"写了没事"的副产品。"""
    assert _run(monkeypatch, seeded, '--rename-to-official',
                '--only-codes', '001309') == 0
    assert _listing(str(tmp_path / 'backup')) == []
    assert _names(seeded)['001309'] == '德明利'
