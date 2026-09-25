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
        sweep.restore(test_db, path)
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
    sweep.restore(test_db, path, restore_owner_immunity=True)
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
        sweep.restore(test_db, path)
    joined = ''.join(out)
    assert '默认不还原' not in joined, '什么都没剔掉却报了"免疫未还原"：%s' % joined
    test_db.expire_all()
    after = test_db.query(SectorFundMapping).filter_by(sector_name='军工').first()
    assert after.reviewed_by == 'agent', '非授予值（`agent`）被一起吞了 ⇒ 默认拒绝做过头了'
