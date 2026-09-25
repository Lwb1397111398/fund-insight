# -*- coding: utf-8 -*-
"""`scripts/sweep_sector_mappings.py --restore-from` 默认**不还原老板免疫**（第 45 轮）。

起因不是猜测，是我把"谁能盖老板已确认"做成 AST 棘轮之后，它自己点出来的：
`restore()` 以前按清单逐字段 `setattr`，而清单的字段表里躺着 `reviewed_by` 与 `owner_locked`
⇒ 一份 manifest 文件就能把"身份体检豁免 + 老板署名"发回库里，**不需要任何令牌**。
那正是页面侧第 18 轮已经堵掉的那条路（一次普通保存白送永久免疫），只不过换了个入口 ——
AGENTS 里那句"豁免共五条来源，每条都要显式令牌"当时就是错的：实际有六条，第六条没人管。

修法照 `purge_junk_funds.py` 的先例：默认剔掉这两列并**报剔了几行**，
要连它们一起还原得显式 `--restore-owner-immunity`。
"""
import importlib.util
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.models.database import SectorFundMapping    # noqa: E402


def _load_sweep():
    spec = importlib.util.spec_from_file_location(
        'sweep_under_test', os.path.join(ROOT, 'scripts', 'sweep_sector_mappings.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _manifest(tmp_path, rows, fields):
    path = tmp_path / 'manifest.json'
    with io.open(str(path), 'w', encoding='utf-8') as fh:
        json.dump({'created_at': '2026-09-20T10:00:00', 'created_fund_codes': [],
                   'fields': list(fields), 'rows': rows}, fh, ensure_ascii=False)
    return str(path)


def _mapping(db, sector, code, reviewed_by=None, owner_locked=False):
    row = SectorFundMapping(sector_name=sector, fund_code=code, fund_name=code,
                            reviewed=True, reviewed_by=reviewed_by, owner_locked=owner_locked,
                            is_active=True)
    db.add(row)
    db.commit()
    return row


def _capture(into):
    """把 stdout 收进列表（`restore` 只往屏幕上说话，判据得听见它说了什么）。"""
    import contextlib
    import io as _io

    class _Capture(_io.StringIO):
        def write(self, s):
            into.append(s)
            return _io.StringIO.write(self, s)

    return contextlib.redirect_stdout(_Capture())


def test_restore_refuses_to_hand_back_owner_immunity_by_default(test_db, tmp_path):
    """默认还原：标的前后一致，但老板署名/锁**不跟着回去**，且回执报得出剔了几行。"""
    sweep = _load_sweep()
    row = _mapping(test_db, '半导体', '512480')          # 库里现在**没有**免疫
    path = _manifest(tmp_path, [{'id': row.id, 'sector_name': '半导体', 'fund_code': '599999',
                                 'reviewed_by': 'owner', 'owner_locked': True}],
                     ('fund_code', 'reviewed_by', 'owner_locked'))

    out = []
    with _capture(out):
        sweep.restore(test_db, path, apply=True)
    test_db.expire_all()
    after = test_db.query(SectorFundMapping).filter_by(sector_name='半导体').first()
    assert after.fund_code == '599999', '标的没还原 ⇒ 这条用例没在测还原动作本身'
    assert not after.owner_locked and after.reviewed_by != 'owner', (
        '一份清单就把老板免疫发回去了（owner_locked=%r / reviewed_by=%r）⇒ 默认拒绝没生效'
        % (after.owner_locked, after.reviewed_by))
    joined = ''.join(out)
    assert '默认不还原' in joined and '--restore-owner-immunity' in joined, \
        '剔掉了两列却不告诉操作者 ⇒ 他以为"还原=回到原样"：%s' % joined
    assert '1 行' in joined, '剔了几行没报数：%s' % joined


def test_the_token_does_hand_it_back(test_db, tmp_path):
    """控制：显式令牌必须**真的有用**（否则上一条只是"永远不还原"，测不到那两列）。"""
    sweep = _load_sweep()
    row = _mapping(test_db, '债券', '511260')
    path = _manifest(tmp_path, [{'id': row.id, 'sector_name': '债券', 'fund_code': '511260',
                                 'reviewed_by': 'owner', 'owner_locked': True}],
                     ('reviewed_by', 'owner_locked'))
    sweep.restore(test_db, path, apply=True, restore_owner_immunity=True)
    test_db.expire_all()
    after = test_db.query(SectorFundMapping).filter_by(sector_name='债券').first()
    assert after.owner_locked is True and after.reviewed_by == 'owner', (
        '加了 --restore-owner-immunity 还是不还原 ⇒ 上面那条"默认拒绝"可能只是恒假')


def test_nothing_is_held_back_when_the_manifest_grants_nothing(test_db, tmp_path):
    """第二半控制：清单里**没有**老板列时，不许谎报"剔了几行"（否则回执在编数字）。"""
    sweep = _load_sweep()
    row = _mapping(test_db, '军工', '512660', reviewed_by='agent', owner_locked=False)
    path = _manifest(tmp_path, [{'id': row.id, 'reviewed_by': 'agent', 'owner_locked': False}],
                     ('reviewed_by', 'owner_locked'))
    out = []
    with _capture(out):
        sweep.restore(test_db, path, apply=True)
    joined = ''.join(out)
    assert '默认不还原' not in joined, '什么都没剔掉却报了"免疫未还原"：%s' % joined
    test_db.expire_all()
    after = test_db.query(SectorFundMapping).filter_by(sector_name='军工').first()
    assert after.reviewed_by == 'agent', '非授予值（`agent`）被一起吞了 ⇒ 默认拒绝做过头了'


def test_restore_is_dry_run_until_apply(test_db, tmp_path):
    """B-M8：`--restore-from` 以前**没有门** —— 不加 `--apply` 也照样写库、删净值。

    文件头第 8 行那时写着"默认 dry-run，`--apply` 才写库"，可那句话对还原这条路是假的。
    两头都要钉：① 默认一支笔都不动（映射行与净值原样、回执里说得出"将要写几行/删几行"）；
    ② `apply=True` 必须**真的有用**（否则①只是"永远不写"，测不到门本身）。
    """
    sweep = _load_sweep()
    from src.models.database import FundInfo, FundHistory
    from datetime import date
    row = _mapping(test_db, '红利', '510880')
    test_db.add(FundInfo(fund_code='999001', fund_name='本轮新建'))
    test_db.add(FundHistory(fund_code='999001', nav_date=date(2026, 9, 20), nav=1.0))
    test_db.commit()
    path = tmp_path / 'm.json'
    import json
    json.dump({'created_at': '2026-09-19T10:00:00', 'created_fund_codes': ['999001'],
               'fields': ['fund_code'],
               'rows': [{'id': row.id, 'fund_code': '510889'}]},
              open(str(path), 'w', encoding='utf-8'))

    out = []
    with _capture(out):
        assert sweep.restore(test_db, str(path)) == 0
    test_db.expire_all()
    joined = ''.join(out)
    assert test_db.query(SectorFundMapping).filter_by(id=row.id).first().fund_code == '510880', \
        '没给 --apply 却改了映射表 ⇒ dry-run 是假的'
    assert test_db.query(FundHistory).filter_by(fund_code='999001').count() == 1, \
        '没给 --apply 却删了净值（那是不可再生数据）'
    assert '[dry-run]' in joined and '1 行' in joined, joined
    assert '--apply --confirm' in joined, '回执没告诉操作者真写要怎么按：%s' % joined

    with _capture(out):
        assert sweep.restore(test_db, str(path), apply=True) == 0
    test_db.expire_all()
    assert test_db.query(SectorFundMapping).filter_by(id=row.id).first().fund_code == '510889', \
        'apply=True 还是不写 ⇒ 上面那条"默认不写"可能只是恒假'
    assert test_db.query(FundHistory).filter_by(fund_code='999001').count() == 0


def test_a_manifest_without_created_at_refuses_to_delete_nav(test_db, tmp_path):
    """B-M9：缺 `created_at` 时"只删本轮新建的净值"退化成"删这只基金的全部净值"。

    旧写法 `if since is not None: hist = hist.filter(...)` ⇒ 日期看不见就**不加过滤器**，
    把"我不知道从哪天起"翻译成"全都删"，而它上面 4 行注释写的正是这个风险。
    现在要反过来：看不见下界 ⇒ 一行都不删，并且说清为什么。
    """
    sweep = _load_sweep()
    from src.models.database import FundInfo, FundHistory
    from datetime import date
    row = _mapping(test_db, '煤炭', '515220')
    test_db.add(FundInfo(fund_code='999002', fund_name='有历史的基金'))
    for day in (1, 2, 3):
        test_db.add(FundHistory(fund_code='999002', nav_date=date(2026, 9, day), nav=1.0))
    test_db.commit()
    import json
    path = tmp_path / 'no-date.json'
    json.dump({'created_fund_codes': ['999002'], 'fields': ['fund_code'],
               'rows': [{'id': row.id, 'fund_code': '510300'}]},
              open(str(path), 'w', encoding='utf-8'))
    out = []
    with _capture(out):
        # 第 47 轮 B-5：`[abort]` 必须**停下来**。上一版这里返回 0（这条用例当时就是把
        # "印了拒跑却继续跑"钉成了规矩）—— 屏幕上是一句 `[abort]`，退码是成功，
        # 而 `--apply` 那一支还会把映射行照写回去：操作者读到 `[abort]` 会以为这一次没动。
        rc = sweep.restore(test_db, str(path), apply=True)
    test_db.expire_all()
    joined = ''.join(out)
    assert rc == 4, '印了 `[abort]` 却不挡流程（退码 %s）⇒ 本仓自己的定义是"[abort] + 停下来"' % rc
    assert 'dry-run' not in joined, '一边说拒跑一边照印 dry-run 的计划 ⇒ 两句话互相打脸：%s' % joined
    assert test_db.query(FundHistory).filter_by(fund_code='999002').count() == 3, \
        '缺 created_at 却删光了净值 ⇒ "%s"' % [l for l in out if 'abort' in l]
    test_db.refresh(row)
    assert row.fund_code == '515220', \
        '拒跑那一支还是把映射行写回去了（现在是 %s）⇒ "只拒绝删净值、照写行"不是不一致的一半，' \
        '是替操作者做了他没同意的动作' % row.fund_code
    assert '[abort]' in joined and 'created_at' in joined, \
        '拒删却不说明理由（操作者会以为"已经还原干净了"）：%s' % joined


def test_the_manifest_cannot_ask_for_fields_outside_the_whitelist(test_db, tmp_path):
    """清单是**外部输入**：以前 `data.get('fields', …)` 里写什么就 setattr 什么（含 `id`）。

    一份改过的 manifest 于是能改任意 ORM 属性 ⇒ 还原动作本身成了写入通道。
    现在白名单外的名字要**报出来并不写**，而白名单内的照常还原（否则这只是把功能关掉）。
    """
    sweep = _load_sweep()
    row = _mapping(test_db, '传媒', '512980')
    other = _mapping(test_db, '别的板块', '510300')
    import json
    path = tmp_path / 'evil.json'
    json.dump({'created_at': '2026-09-19T10:00:00', 'created_fund_codes': [],
               'fields': ['fund_code', 'id', 'sector_name'],
               'rows': [{'id': row.id, 'fund_code': '512980', 'id': 99999,
                         'sector_name': '被抢来的板块'}]},
              open(str(path), 'w', encoding='utf-8'))
    out = []
    with _capture(out):
        assert sweep.restore(test_db, str(path), apply=True) == 0
    joined = ''.join(out)
    test_db.expire_all()
    mine = test_db.query(SectorFundMapping).filter_by(id=row.id).first()
    assert mine.id == row.id, '清单点名要改 `id` 就真的改了 ⇒ 白名单没生效'
    assert 'id' in joined and '[skip]' in joined, '剔掉了字段却不报告：%s' % joined
    assert mine.fund_code == '512980'


def test_the_apply_gate_is_said_before_the_database_is_touched():
    """`--restore-from --apply` 缺确认词时必须**在连库之前**退出。

    钉库/建会话排在检查之后的话，一次打错字的命令行就已经连上库了 ——
    与本仓 seed 那道闸（`--owner-confirm SEED-PROXY`）同一姿势，用 AST 判顺序。
    """
    import ast
    src = open(os.path.join(ROOT, 'scripts', 'sweep_sector_mappings.py'), encoding='utf-8').read()
    tree = ast.parse(src)
    main = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == 'main')
    token_check = [n.lineno for n in ast.walk(main) if isinstance(n, ast.Compare)
                   and 'RESTORE_CONFIRM_TOKEN' in ast.dump(n)]
    connects = [n.lineno for n in ast.walk(main) if isinstance(n, ast.Call)
                and getattr(n.func, 'id', '') in ('SessionLocal', 'pin_local_sqlite')]
    assert token_check, 'main() 里没有确认词检查 ⇒ 门只写在 restore() 里，`--apply` 照旧直通'
    assert connects, 'main() 里没有建库动作 ⇒ 这条顺序判据是空判'
    assert min(token_check) < min(connects), \
        '确认词检查排在连库之后（%s vs %s）⇒ 用法错也会先连一次库' % (token_check, connects)


def test_a_stale_created_at_is_refused_and_the_plan_is_said_before_the_delete(test_db, tmp_path):
    """第 48 轮 B-1：`created_at` **写成过去的日期**与"没有 `created_at`"是同一个后果。

    上一轮堵的是"看不见下界"，但清单是外部输入：把它写成 `2020-01-01` ⇒
    `nav_date >= 那天` 命中这只基金**全部**历史（实测副本 6 行删剩 0，其中 4 行是
    老板本来就有的）。现在下界必须与清单自身的时间戳自洽（不早于 90 天、不晚于 1 天），
    而且"要清掉几行不可再生的净值"必须印在**动手之前**——以前只有事后那一句。
    """
    sweep = _load_sweep()
    from src.models.database import FundInfo, FundHistory
    from datetime import date, timedelta
    row = _mapping(test_db, '煤炭', '515220')
    test_db.add(FundInfo(fund_code='999003', fund_name='老历史基金'))
    back = date.today() - timedelta(days=400)
    for day in range(6):
        test_db.add(FundHistory(fund_code='999003', nav_date=back + timedelta(days=day), nav=1.0))
    test_db.commit()
    path = tmp_path / 'stale.json'
    import json
    json.dump({'created_at': '2020-01-01', 'created_fund_codes': ['999003'],
               'fields': ['fund_code'], 'rows': [{'id': row.id, 'fund_code': '510300'}]},
              open(str(path), 'w', encoding='utf-8'))
    out = []
    with _capture(out):
        rc = sweep.restore(test_db, str(path), apply=True)
    joined = ''.join(out)
    assert rc == 4, '下界与清单时间戳差 6 年却照写照删（退码 %s）：%s' % (rc, joined)
    assert '[abort]' in joined and '净值下界' in joined, joined
    test_db.expire_all()
    assert test_db.query(FundHistory).filter_by(fund_code='999003').count() == 6, \
        '一份日期写错的清单就把不可再生的 6 行净值清光了'
    # 同一份清单换成自洽的日期：必须**先报计划再动手**
    json.dump({'created_at': str(date.today()), 'created_fund_codes': ['999003'],
               'fields': ['fund_code'], 'rows': [{'id': row.id, 'fund_code': '510300'}]},
              open(str(path), 'w', encoding='utf-8'))
    out2 = []
    with _capture(out2):
        sweep.restore(test_db, str(path), apply=True)
    lines = ''.join(out2).splitlines()
    plan = [i for i, l in enumerate(lines) if l.startswith('[计划]')]
    done = [i for i, l in enumerate(lines) if l.startswith('[还原]')]
    assert plan and done and plan[0] < done[0], \
        '"要清几行"没排在动手之前印出来（操作者读到事后账＝来不及停）：%s' % lines


def test_the_immunity_grant_ratchet_rejects_a_read_of_the_same_column(tmp_path):
    """第 48 轮 A-4：授予那台扫描器以前用**自己那份**正则 ⇒
    `SELECT … WHERE owner_locked = true` 被算成三处授予（把墙建起来，将来没人信它），
    而 `SET reviewed_by = :who, owner_locked = :ok` 配参数字典一处都不算（新来源全绿）。
    现在三处棘轮共用 `scripts/sql_write_policy.py`，两个方向各有样品。
    """
    import importlib.util
    import os
    import sys
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    spec = importlib.util.spec_from_file_location(
        'ratchet', os.path.join(root, 'tests', 'unit', 'test_review_ownership_and_matching.py'))
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault('ratchet', mod)
    spec.loader.exec_module(mod)
    pkg = tmp_path / 'pkg'
    pkg.mkdir()
    with open(str(pkg / '_read_only_where.py'), 'w', encoding='utf-8') as fh:
        fh.write('import sqlalchemy as sa\n\n\ndef stat(db):\n'
                 '    return db.execute(sa.text("SELECT id FROM sector_fund_mapping '
                 'WHERE owner_locked = true")).fetchall()\n')
    with open(str(pkg / '_bind_param_grant.py'), 'w', encoding='utf-8') as fh:
        fh.write('import sqlalchemy as sa\n\n\ndef grant(db):\n'
                 '    return db.execute(sa.text("UPDATE sector_fund_mapping '
                 "SET reviewed_by = :who, owner_locked = :ok\"), "
                 '{\"who\": \"owner\", \"ok\": True})\n')
    found, _opaque = mod._grants_immunity(pkg)
    files = {k[0].split('/')[-1] for k in found}
    assert '_bind_param_grant.py' in files, \
        '绑定参数式授予仍然看不见 ⇒ 加一条豁免来源不必登记：%s' % sorted(files)
    assert '_read_only_where.py' not in files, \
        '只读的一列定位被算成授予 ⇒ 判据过宽（第 44 轮"闸门建成墙"同一课）：%s' % sorted(files)
