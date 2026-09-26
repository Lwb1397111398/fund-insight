# -*- coding: utf-8 -*-
"""闸：仓库里**每一个 tracked 文件**都不许含有 `.env` 里那几把密钥的真值（任务 #46）。

为什么现在补：`AGENTS.md` 一直写着"口令绝不入库"，而这条从第 20 轮起只被我**手工扫过两次**
（09-22 与 09-25 推 GitHub 前）—— 手工扫过一次不等于有闸。真值进仓库只有一条路：
某个人（包括我）把 `.env` 里的值粘进文档、测试夹具或报错信息里，然后 CI 全绿。
本文件自己就是最容易出事的地方：**它绝不打印命中内容**，只打印"哪个文件、哪一类密钥"。

两条控制断言（没有"现造一处违规必须被点名"的判据等于没有判据）：
① 把 .env 真值的样子造进临时文件 ⇒ 必须命中；
② 把凭据形状（带口令的连接串 / `ghp_` / JWT）各造一条 ⇒ 必须命中。
   ② 是今天补的：第一版形状规则漏了一个 `?`，只认带 `+driver` 的连接串，
   而本仓所有真实写法都不带 driver ⇒ **整条形状扫描恒空**而主用例照绿，正是这一族标准的死法。
"""
import io
import os
import re
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 这些键的**值**是秘密；`DATABASE_URL` 特殊：只判其中的口令段（host 与库名要出现在文档里，不是泄漏）
SECRET_KEYS = ('ACCESS_PASSWORD', 'LLM_API_KEY', 'VOLCENGINE_API_KEY', 'OPENAI_API_KEY')
# 通用凭据**形状**（不是变量名 —— 扫名字会命中 379 行文档与夹具，纯噪声）
GENERIC = (
    ('github 令牌', re.compile(rb'ghp_[A-Za-z0-9]{20,}')),
    ('带令牌的 git 远端', re.compile(rb'x-access-token:[^@\s]{10,}')),
    ('私钥块', re.compile(rb'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
    ('sk- 型 API key', re.compile(rb'sk-[A-Za-z0-9]{20,}')),
    # 带凭据的数据库连接串整体。那个 `?` 是第一版漏掉的字符：漏掉它 = 只认 `postgresql+driver://`，
    # 而本仓真实写法都不带 driver ⇒ 这一路恒空。
    ('带口令的连接串', re.compile(rb'postgres(?:ql)?(?:\+\w+)?://[A-Za-z0-9_.\-]+:[^@\s]{6,}@')),
    ('JWT', re.compile(rb'\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}')),
)
# 占位符白名单：**只看被匹配到的那一小段**。不放开成"文件里有 invalid 就整文件免检" ——
# 那样一个注释就能给真口令发通行证。
# `password|passwd` 这两项是为了放过文档里的**格式模板**
# （`.codeartsdoer/specs/fund_render/spec.md:271` 写的是 `postgresql://user:password@host:port/dbname`，
# 第一次跑这条闸就是它响的 —— 那是模板不是钥匙）。代价说清楚：真口令若恰好就等于 "password" 会被放过，
# 所以控制断言②用的是一条不像模板的口令（`RealWd123456`），保证这条路仍有牙。
_PLACEHOLDER = re.compile(
    rb'(invalid|example|\.test|localhost|localdomain|evil|placeholder|dummy|fake|'
    rb'S3cr3tPW|SecretPass|s3cr3t|password|passwd|CHANGE_?ME|your-|xxxx|\bpass\b|\bpwd\b)', re.I)


def _env_secrets():
    path = os.path.join(ROOT, '.env')
    if not os.path.exists(path):
        return {}
    out = {}
    with io.open(path, encoding='utf-8', errors='replace') as fh:
        for line in fh:
            line = line.strip()
            if '=' not in line or line.startswith('#'):
                continue
            key, value = line.split('=', 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key in SECRET_KEYS and len(value) >= 6:
                out[key] = value
            if key == 'DATABASE_URL' and '://' in value and '@' in value:
                cred = value.split('://', 1)[1].split('@', 1)[0]
                if ':' in cred:
                    out['DATABASE_URL.password'] = cred.split(':', 1)[1]
    return {k: v for k, v in out.items() if len(v) >= 6}


def _tracked_text_files():
    try:
        listed = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, timeout=60)
    except Exception:
        return None
    if listed.returncode != 0:
        return None
    exts = ('.py', '.js', '.html', '.md', '.json', '.yaml', '.yml', '.txt', '.css',
            '.ini', '.cfg', '.toml', '.sh')
    return [n.decode('utf-8', 'replace') for n in listed.stdout.split(b'\x00')
            if n and os.path.splitext(n.decode('utf-8', 'replace'))[1].lower() in exts]


def _read(rel):
    try:
        with open(rel if os.path.isabs(rel) else os.path.join(ROOT, rel), 'rb') as fh:
            return fh.read()
    except OSError:
        return b''


def _value_hits(files, needles):
    """`needles` = [(键名, 真值 bytes)]；返回 (文件, 键名)，**绝不带上命中内容**。"""
    bad = []
    for rel in files:
        blob = _read(rel)
        for name, needle in needles:
            if needle in blob:
                bad.append((rel, name))
    return bad


def _shape_hits(files):
    bad = []
    for rel in files:
        blob = _read(rel)
        for label, rx in GENERIC:
            for m in rx.finditer(blob):
                if not _PLACEHOLDER.search(m.group(0)):
                    bad.append((rel, label))
                    break
    return bad


def test_no_secret_values_live_in_tracked_files():
    files = _tracked_text_files()
    if files is None:
        pytest.skip('git 不可用 ⇒ 拿不到 tracked 清单（跳过，不算通过）')
    assert len(files) > 400, 'tracked 文本文件只有 %d 个 ⇒ 这份清单本身不可信' % len(files)
    secrets = _env_secrets()
    if secrets:
        bad = _value_hits(files, [(k, v.encode('utf-8')) for k, v in secrets.items()])
        assert not bad, '这些入库文件里出现了 .env 密钥的真值（只报键名）：%s' % sorted(set(bad))
    shapes = _shape_hits(files)
    assert not shapes, '这些入库文件里有"不像占位符"的凭据形状，得人来判一次：%s' % sorted(set(shapes))


def test_the_value_scanner_would_catch_a_planted_secret(tmp_path):
    """控制断言①：把真值造进文件 ⇒ 必须点名。"""
    planted = 'Planted-Secret-Value-913'
    fake = tmp_path / 'leak.md'
    fake.write_text(u'文档里贴了一句口令：%s\n' % planted, encoding='utf-8')
    hits = _value_hits([str(fake)], [('ACCESS_PASSWORD', planted.encode('utf-8'))])
    assert hits and hits[0][1] == 'ACCESS_PASSWORD', '把真值造进文件里却没被发现 ⇒ 这把尺子是空的'


def test_the_shape_rules_are_not_vacuous(tmp_path):
    """控制断言②：四种形状各造一条都要命中；再造一条合成占位的 ⇒ 不该命中（否则闸门建成墙）。

    这两条连接串必须**拼出来**，不许整条写成字面量：本文件自己也是 tracked 文本文件，
    一条完整的凭据字面量会让这把闸在 `git add` 之后**点自己的名**（先跑一遍就响，试过一次）。
    """
    scheme, pwd, host = 'postgres' + 'ql', 'Real' + 'Wd123456', 'db' + '.corp'
    cases = {
        '不写 driver 的连接串': u"URL='%s://%s:%s@%s:5432/prod'\n" % (scheme, 'svc', pwd, host),
        '写 driver 的连接串': u"URL='%s+%s://%s:%s@%s/prod'\n" % (scheme, 'psycopg2', 'svc', pwd, host),
        'github 令牌': u"token = 'ghp_%s'\n" % ('A' * 36),
        'JWT': u"jwt = 'eyJ%s.%s.rest'\n" % ('A' * 24, 'B' * 14),
    }
    for i, (label, text) in enumerate(sorted(cases.items())):
        p = tmp_path / ('plant_%d.md' % i)
        p.write_text(text, encoding='utf-8')
        assert _shape_hits([str(p)]), '形状规则认不出「%s」⇒ 这一路是恒空的' % label
    ok = tmp_path / 'placeholder.md'
    ok.write_text(u"URL='postgresql://u:S3cr3tPW@evil.invalid/proddb'\n", encoding='utf-8')
    assert not _shape_hits([str(ok)]), '测试夹具里的合成凭据被判成真泄漏 ⇒ 这条闸会逼人绕过它'
