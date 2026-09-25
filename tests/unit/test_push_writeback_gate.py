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


def test_the_password_never_leaves_for_an_unknown_base(monkeypatch, tmp_path, capsys):
    """`--base` 指到陌生主机时，**一次请求都不许发**（第 45 轮 B-M-7）。

    第 43 轮 B-MAJOR-5 只改了那一行的措辞：`--base http://127.0.0.1:9/` 不再自称"线上生产库"，
    但 `main()` 照旧把 `X-Access-Password` 与整份映射清单 POST 过去 —— 一行文案是给人看的，
    挡不住任何东西。S6 的首次真回写就用这个工具，所以这条要钉在**动作**上：
    ① 陌生主机 + 明文 http ⇒ 退 2 且零请求；
    ② 同一台主机加了 `--allow-any-base` ⇒ 照发（拒绝必须是显式的）；
    ③ 本机回环（对着 `serve_mirror.py` 核验）不加旗也放行；
    ④ 生产域名但用 `http://` ⇒ 拦（口令不许走明文）。
    """
    code, calls = _run(monkeypatch, tmp_path, ['--base', 'http://evil.example/'])
    assert code == 2, '把口令发给陌生主机居然退 %s' % code
    assert calls == [], '一行都没该发出去：%s' % calls
    out = capsys.readouterr().out
    assert '[abort]' in out and ('明文' in out or '不敢把' in out),         '拦下来了却没说清为什么 ⇒ 操作者只会去加重试：%s' % out

    code, calls = _run(monkeypatch, tmp_path, ['--base', 'https://staging.example/'])
    assert code == 2 and calls == [], 'https 但不是我那台生产，也不该把口令交出去：%s' % calls
    assert '不敢把' in capsys.readouterr().out, '陌生 https 主机那条拒绝理由没说清是谁'
    code, calls = _run(monkeypatch, tmp_path,
                       ['--base', 'https://staging.example/', '--allow-any-base'])
    assert code == 0 and calls, '显式同意之后仍被拦 ⇒ 这道闸没有出口（退 %s）' % code

    code, calls = _run(monkeypatch, tmp_path, ['--base', 'http://127.0.0.1:8098/'])
    assert code == 0 and calls, '本机演练也被拦 ⇒ 本地核验这条常规路被焊死了'

    code, calls = _run(monkeypatch, tmp_path,
                       ['--base', 'http://fund-insight.onrender.com/'])
    assert code == 2 and calls == [], '生产域名走 http 明文也要拦：%s / %s' % (code, calls)

    # 控制：把闸拆掉（恒放行）时上面几条必须变绿 —— 证明这几条判据真的在看 `_send_allowed`
    original = push._send_allowed
    monkeypatch.setattr(push, '_send_allowed', lambda base, allow_any: None)
    code, calls = _run(monkeypatch, tmp_path, ['--base', 'http://evil.example/'])
    assert code == 0 and calls, '把 `_send_allowed` 换成恒放行后仍然拦 ⇒ 上面几条测的不是它'
    monkeypatch.setattr(push, '_send_allowed', original)


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


def test_a_row_the_server_refused_early_is_dropped_not_mislabelled(monkeypatch, tmp_path):
    """服务端在写 `nav_priced_here` **之前**就 `continue` 的那些行（空板块名、字段超长、
    evidence 坏 JSON…）天生不带这一列。第 39 轮两份同点：旧写法把"被拒收"误诊成
    "对端是没升级的旧构建" ⇒ 整批 145 行退码 3，还把消息指向"去升级生产"。

    现在：带 `reason` ⇒ 剔掉那一行继续；不带 `reason` 才算旧构建（上一条用例钉那一半）。
    """
    calls = _setup(monkeypatch, tmp_path)

    def mixed_server(base, path, password, payload=None, method='GET', timeout=180):
        calls.append(payload)
        items = []
        for r in payload['mappings']:
            if r['fund_code'] == BAD['fund_code']:
                items.append({'sector_name': r['sector_name'], 'fund_code': r['fund_code'],
                              'reason': 'evidence_too_large'})      # 早期拒收，没走到定价那一列
            else:
                items.append({'sector_name': r['sector_name'], 'fund_code': r['fund_code'],
                              'nav_priced_here': True, 'nav_priced_here_note': ''})
        return 200, {'message': 'ok', 'data': {'items': items}}

    monkeypatch.setattr(push, 'request', mixed_server)
    monkeypatch.setattr('sys.argv', ['push_sector_mappings_to_prod.py', '--confirm', push.CONFIRM])
    code = push.main()
    assert code == 0, '被拒收的一行不该把整批锁死（退码 %s）' % code
    sent = [r['fund_code'] for r in calls[-1]['mappings']]
    assert sent == [GOOD['fund_code']], '那行服务端已经拒了，不该再发第二次：%s' % sent


def test_both_dropping_reasons_are_counted_together(monkeypatch, tmp_path, capsys):
    """两格剔除同时亮时，回执必须报**总数**（第 40 轮 B 的 M-3：`dropped = len(drop)` 覆盖掉了
    前一批，3 行清单只发 1 行却说"另有 1 行没发"，与同批改过的模块总览那句"计入剔除数"打脸）。"""
    rows = [{'sector_name': '半导体', 'fund_code': '512480', 'fund_name': '半导体ETF'},
            {'sector_name': '卫星', 'fund_code': '159206', 'fund_name': '卫星ETF'},
            {'sector_name': '创新药', 'fund_code': '159992', 'fund_name': '创新药ETF'}]
    io.open(str(tmp_path / 'manifest.json'), 'w', encoding='utf-8').write(json.dumps(
        {'sha256': 'x', 'generated_at': '2026-09-23T15:14:00', 'mappings': rows}, ensure_ascii=False))
    monkeypatch.setattr(push, 'MANIFEST', str(tmp_path / 'manifest.json'))
    monkeypatch.setenv('ACCESS_PASSWORD', 'gate-test')
    calls = []

    def server(base, path, password, payload=None, method='GET', timeout=180):
        calls.append(payload)
        items = []
        for r in payload['mappings']:
            if r['fund_code'] == '159206':
                items.append(dict(r, reason='evidence_too_large'))            # 早期拒收，无该列
            elif r['fund_code'] == '159992':
                items.append(dict(r, nav_priced_here=False, nav_priced_here_note='本库没有档案'))
            else:
                items.append(dict(r, nav_priced_here=True, nav_priced_here_note=''))
        return 200, {'message': 'ok', 'data': {'items': items}}

    monkeypatch.setattr(push, 'request', server)
    monkeypatch.setattr('sys.argv', ['x', '--confirm', push.CONFIRM])
    code = push.main()
    out = capsys.readouterr().out
    assert code == 0, code
    assert [r['fund_code'] for r in calls[-1]['mappings']] == ['512480'], '两批各剔各的才发得对'
    assert calls[-1]['dry_run'] is False
    # 回执里那句"另有 N 行没发"必须是 **2**（早期拒收 1 + 定不了价 1）。
    # 旧写法 `dropped = len(drop)` 把前一批覆盖掉 ⇒ 只报 1，把"少写了两行"说成"少写一行"。
    assert '另有 2 行**没发**' in out, '剔除总数被覆盖了（回执：%s）' % out[-400:]


def test_a_batch_refused_wholesale_never_sends_a_real_write(monkeypatch, tmp_path):
    """全部行都被早期拒收 ⇒ 不许再发那个 0 行的真写请求、不许打印"[完成]"、不许退 0。

    第 40 轮 B 的 M-2：`nav_priced_here is False` 那一格有"剔光就停"，上一批新加的 `refused`
    那一格没有 ⇒ 一次什么都没写进去的生产回写以"完成 + 退码 0"收场，
    任何按退码判断的人会记成"已回写"。
    """
    calls = _setup(monkeypatch, tmp_path)

    def all_refused(base, path, password, payload=None, method='GET', timeout=180):
        calls.append(payload)
        items = [dict(r, reason='too_long:fund_name') for r in payload['mappings']]
        return 200, {'message': 'ok', 'data': {'items': items}}

    monkeypatch.setattr(push, 'request', all_refused)
    monkeypatch.setattr('sys.argv', ['x', '--confirm', push.CONFIRM])
    code = push.main()
    assert code == 5, '整批被拒却按成功收场（退码 %s）' % code
    assert calls and all(c['dry_run'] is True for c in calls), '不许发出真写请求（哪怕是空批次）'


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


def test_the_target_line_only_claims_production_for_the_production_host(capsys):
    """`--base` 指到别处时，那一行不许再说"线上生产库"（第 43 轮 B-MAJOR-5）。

    B 席实测：`--base http://127.0.0.1:9/` 打出的是
    `[目标] 127.0.0.1:9 —— 经 HTTP 写**线上生产库**的写入口` ——
    与它上一行"目标=http://127.0.0.1:9/"自相矛盾。这一行是操作者按不按 `--confirm` 的依据，
    方向说反比不说更糟：它会把"往本机镜像试写"读成"已经在动线上"。
    """
    prod = push._target_line('https://fund-insight.onrender.com')
    assert '线上生产库' in prod, prod
    for base in ('http://127.0.0.1:9/', 'http://localhost:8098/', 'https://evil.example/'):
        line = push._target_line(base)
        assert '这不是已知的生产域名' in line, line
        assert '线上生产库' not in line, '%s 仍被说成生产：%s' % (base, line)

    # 第 44 轮 B-m6：上一版的判据是 `mark in host` 的**子串**匹配 ⇒
    # `onrender.com.attacker.example` 与 `notonrender.com` 都能自称"经 HTTP 写线上生产库"。
    # 这一行是操作者要不要按 `--confirm` 的唯一依据，被一个买得到的假串买通最坏。
    # 反向样品也要钉：同后缀上**别人的** Render 应用同样不是我的生产（第 44 轮 B-m6 的
    # 另一半 —— 我第一版改成"或它的子域"，正好把这一族放进了"线上生产库"）。
    for not_mine in ('https://onrender.com.attacker.example/', 'https://notonrender.com/',
                     'https://fund-insight.onrender.com.evil.test/',
                     'https://somebody-else.onrender.com/', 'https://onrender.com/'):
        line = push._target_line(not_mine)
        assert '这不是已知的生产域名' in line, '假域名/别人的应用自称生产：%s ⇒ %s' % (not_mine, line)
    # 真生产那一支仍然要说得出"生产"（否则这条闸只是把话全说了）：大小写与端口都不许它改口
    for real in ('https://FUND-INSIGHT.ONRENDER.COM/', 'https://fund-insight.onrender.com:443/'):
        assert '线上生产库' in push._target_line(real), push._target_line(real)


def _prints_the_target(tree):
    """这棵树上有没有一句 `print(...)` 里**含着** `_target_line(...)` 调用。"""
    import ast
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'print'):
            continue
        if any(isinstance(s, ast.Call) and isinstance(s.func, ast.Name)
               and s.func.id == '_target_line' for s in ast.walk(node)):
            return True
    return False


def test_the_target_line_is_actually_said_out_loud():
    """`_target_line()` 定义了却没人印 = 屏幕上仍然什么都没有（第 44 轮 A-m4）。

    上一轮把守卫扫描改成"helper 返回值里的 `[目标]` 只有被印过才算自报"，
    但没有一条用例正面回答"真脚本里它到底被印了吗"。这条读 AST 里那句 `print`，
    并**当场造一个"算了但没说"的反面样品**证明判据会响 —— 只测仓库现状的判据，
    在 helper 被改成"赋值给变量"的那天就退化成空判。
    """
    import ast
    src = io.open(spec.origin, encoding='utf-8').read()
    assert _prints_the_target(ast.parse(src)), \
        '回写工具算出了目标却没把它印出来 ⇒ 操作者按 `--confirm` 时无从判断自己在动谁'
    silenced = src.replace('print(_target_line(args.base))', '_line = _target_line(args.base)')
    assert silenced != src, '替换没生效（那句 print 的形状变了）⇒ 这条控制断言是空判'
    assert not _prints_the_target(ast.parse(silenced)), \
        '"算了但没印"仍被判成自报 ⇒ 上面那条判据只是在看字符串在不在文件里"存在"'
