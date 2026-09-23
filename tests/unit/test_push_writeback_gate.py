# -*- coding: utf-8 -*-
"""回写工具的闸门：`is_fetchable` 是镜像算的，真写前必须问服务端一句。

第 27 轮两份复评共同抓到：`prod-writeback-sector-mappings.json` 里那 31 行在生产
连 `fund_info` 档案都没有（压着 249 条活预测），而清单里的 `is_fetchable` 全写着 `True` ——
因为那一列是**在本地镜像上**算的，而我已经在镜像把档案补齐了。
所以"能不能定价"这句话必须由**目标库**说；本工具在 `--confirm` 真写之前先跑一次 dry-run 预检，
把不可服务的行剔掉（要硬发得显式 `--allow-unservable`）。
"""
import importlib.util
import io
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location(
    'push_prod_gate', os.path.join(ROOT, 'scripts', 'push_sector_mappings_to_prod.py'))
push = importlib.util.module_from_spec(spec)
spec.loader.exec_module(push)

GOOD = {'sector_name': '半导体', 'fund_code': '512480'}
BAD = {'sector_name': '创新药', 'fund_code': '159992'}


def _setup(monkeypatch, tmp_path, preflight_status=200):
    manifest = tmp_path / 'manifest.json'
    mappings = [dict(GOOD, fund_name='半导体ETF'), dict(BAD, fund_name='创新药ETF')]
    io.open(str(manifest), 'w', encoding='utf-8').write(json.dumps(
        {'sha256': 'x', 'generated_at': '2026-09-21T16:44:33', 'mappings': mappings},
        ensure_ascii=False))
    monkeypatch.setattr(push, 'MANIFEST', str(manifest))
    monkeypatch.setenv('ACCESS_PASSWORD', 'gate-test')

    calls = []

    def fake_request(base, path, password, payload=None, method='GET', timeout=180):
        calls.append(payload)
        items = [{'sector_name': r['sector_name'], 'fund_code': r['fund_code'],
                  'outcome': 'created' if payload.get('dry_run') else 'updated',
                  'changed_fields': ['fund_code'], 'mapping_id': 1, 'matched_by': 'sector_name',
                  'reason': None,
                  # 只有生产没有档案的那行会被告知"定不了价"
                  'nav_priced_here': r['fund_code'] != BAD['fund_code'],
                  'nav_priced_here_note': '' if r['fund_code'] != BAD['fund_code']
                  else '本库没有这只基金的 fund_info 档案'}
                 for r in payload['mappings']]
        if payload.get('dry_run') and preflight_status != 200:
            return preflight_status, {'detail': 'boom'}
        return 200, {'message': 'ok', 'written': 0 if payload.get('dry_run') else len(items),
                     'data': {'items': items, 'counts': {'updated': 0, 'created': len(items),
                                                          'unchanged': 0, 'refused': 0},
                              'refused_reasons': {}, 'no_nav_priced_in_this_db':
                                  sum(1 for i in items if i['nav_priced_here'] is False)}}

    monkeypatch.setattr(push, 'request', fake_request)
    return calls


def _run(monkeypatch, tmp_path, argv, preflight_status=200):
    calls = _setup(monkeypatch, tmp_path, preflight_status)
    monkeypatch.setattr('sys.argv', ['push_sector_mappings_to_prod.py'] + argv)
    return push.main(), calls


def test_dry_run_asks_the_server_only_once(monkeypatch, tmp_path):
    code, calls = _run(monkeypatch, tmp_path, [])
    assert code == 0, code
    assert len(calls) == 1 and calls[0]['dry_run'] is True, calls


def test_real_write_preflights_and_drops_unpriced_rows(monkeypatch, tmp_path):
    code, calls = _run(monkeypatch, tmp_path, ['--confirm', push.CONFIRM])
    assert code == 0, code
    assert len(calls) == 2, '真写前必须先问一次服务端'
    assert calls[0]['dry_run'] is True and calls[1]['dry_run'] is False
    sent = [r['fund_code'] for r in calls[1]['mappings']]
    assert sent == [GOOD['fund_code']], '生产定不了价的行不该被发出去：%s' % sent


def test_allow_unservable_is_an_explicit_override(monkeypatch, tmp_path):
    code, calls = _run(monkeypatch, tmp_path,
                       ['--confirm', push.CONFIRM, '--allow-unservable'])
    assert code == 0, code
    sent = sorted(r['fund_code'] for r in calls[-1]['mappings'])
    assert sent == sorted([GOOD['fund_code'], BAD['fund_code']]), sent


def test_failed_preflight_sends_nothing(monkeypatch, tmp_path):
    code, calls = _run(monkeypatch, tmp_path, ['--confirm', push.CONFIRM],
                       preflight_status=500)
    assert code == 3, code
    assert len(calls) == 1 and calls[0]['dry_run'] is True, '预检没走通就不该发真写请求'


def test_an_old_server_that_never_answers_the_column_sends_nothing(monkeypatch, tmp_path):
    """对端"有 audit-import、但回执里没有 `nav_priced_here` 这一列"的旧构建 ⇒ 预检必须拒。

    第 38 轮 B 席 MAJOR：旧判据写的是 `nav_priced_here is False`，于是**字段缺失＝过了闸**，
    这条预检在最需要它的时刻（服务端比代码旧）是开着的，159992 那种没档案的行会照发。
    "没回答"与"回答不可服务"不是一回事，但两者都不许真写。
    """
    calls = _setup(monkeypatch, tmp_path)

    def old_server(base, path, password, payload=None, method='GET', timeout=180):
        calls.append(payload)
        items = [{'sector_name': r['sector_name'], 'fund_code': r['fund_code'],
                  'outcome': 'created'} for r in payload['mappings']]
        return 200, {'message': 'ok', 'data': {'items': items}}

    monkeypatch.setattr(push, 'request', old_server)
    monkeypatch.setattr('sys.argv', ['push_sector_mappings_to_prod.py', '--confirm', push.CONFIRM])
    code = push.main()
    assert code == 3, '旧版服务端没回这一列，真写却放行了（退码 %s）' % code
    assert calls[-1]['dry_run'] is True, '拒收之前就该停住，最后一个请求不许是真写'


def test_a_preflight_that_answers_fewer_rows_sends_nothing(monkeypatch, tmp_path):
    """预检只答了一半的行 ⇒ 剩下那些等于"没问过"，同样不许发。"""
    calls = _setup(monkeypatch, tmp_path)

    def short_server(base, path, password, payload=None, method='GET', timeout=180):
        calls.append(payload)
        first = payload['mappings'][0]
        items = [{'sector_name': first['sector_name'], 'fund_code': first['fund_code'],
                  'nav_priced_here': True, 'nav_priced_here_note': ''}]
        return 200, {'message': 'ok', 'data': {'items': items}}

    monkeypatch.setattr(push, 'request', short_server)
    monkeypatch.setattr('sys.argv', ['push_sector_mappings_to_prod.py', '--confirm', push.CONFIRM])
    code = push.main()
    assert code == 3, '预检答了 1 行 / 发出 2 行，剩下那行等于没问过却仍要写（退码 %s）' % code
    assert calls[-1]['dry_run'] is True
