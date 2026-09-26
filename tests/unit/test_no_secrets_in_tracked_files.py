# -*- coding: utf-8 -*-
"""闸：仓库里**每一个 tracked 的非二进制文件**都不许含有密钥真值或"像真值"的凭据形状（任务 #46）。

为什么现在补：`AGENTS.md` 一直写着"口令绝不入库"，而这条从第 20 轮起只被我**手工扫过两次**
（09-22 与 09-25 推 GitHub 前）—— 手工扫过一次不等于有闸。真值进仓库只有一条路：
某个人（包括我）把 `.env` 里的值粘进文档、测试夹具或报错信息里，然后 CI 全绿。
本文件自己就是最容易出事的地方：**它绝不打印命中内容**，只打印"哪个文件、哪一类密钥"。

第 49 轮两席同条（A-5 / B-3）补掉五处"说了但没做到"：
① **受检面**以前按扩展名白名单挑 ⇒ tracked 558 只看 423，不看 `.csv` 101 / `.tsv` 4
   （其中就有本仓工具自己写进仓库的生产名单），也没有 `.env.example`。现在按内容判二进制。
② **形状规则**以前 6 条只有 3 条有样品 ⇒ 现在样品按 `GENERIC` **逐条**生成，少一条就红。
   （这条闸自己的"恒空"死法第一轮就发生在它身上：形状少一个 `?` ⇒ 整条恒空而主用例全绿。）
③ **占位符**以前判在**整段匹配**上 ⇒ 连接串里只要**用户名**含 `example`，
   后面那截真凭据就跟着隐身。现在只判凭据命名组；模板词（`password` 这类）只许"整段等于它"才放行。
④ 没有 `.env` 时（干净克隆 / CI）值扫描一条都不判 —— 以前照绿且不吭声，现在 `skip` 并说明。
⑤ 键名单不再手写（B-3 的 m-2）：按"键名像凭据"从 `.env` 现枚举，将来加一把
   `RENDER_API_KEY` 自动在扫描面里。
"""
import io
import os
import re
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# `DATABASE_URL` 特殊：只判其中的口令段（host 与库名要出现在文档里，不是泄漏）。
_CREDENTIAL_KEY = re.compile(r'(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CRED|APIKEY)$', re.I)
_VALUE_KEYS_MAX = 40
# 通用凭据**形状**（不是变量名 —— 扫名字会命中几百行文档与夹具，纯噪声）。
# 每条都要有 `cred*` 命名组：判"像不像占位符"只看那一段，不看整段匹配。
# ⚠ 两条含"关键字 + 长串"的规则必须**拼起来写**：关键字后面只要紧跟 `(?P<cred>` 这种
# 一长串非 @ 字符，**规则定义自己就会被自己匹配**（这一版第一次跑就是点自己的名响的，
# 连这段注释里都不能写出那个完整形状 —— 同族教训：字面量写全了，扫描器分不清哪句是规则、哪句是钥匙）。
GENERIC = (
    ('github 令牌', re.compile(rb'(?P<cred>ghp_[A-Za-z0-9]{20,})')),
    ('github 其它形态令牌', re.compile(rb'(?P<cred>github_pat_[A-Za-z0-9_]{20,})'
                                        rb'|(?P<cred2>gh[sour]_[A-Za-z0-9]{20,})')),
    ('带令牌的 git 远端', re.compile(rb'x-access' + rb'-token:(?P<cred>[^@\s]{10,})')),
    ('私钥块', re.compile(rb'-----BEGIN [A-Z ]*PRIVATE KEY-----'
                          rb'(?P<cred>[A-Za-z0-9+/=]{30,})')),
    ('sk- 型 API key', re.compile(rb'(?P<cred>sk-[A-Za-z0-9]{20,})')),
    # 带凭据的数据库连接串整体。那个 `?` 是第一版漏掉的字符：漏掉它 = 只认 `postgresql+driver://`，
    # 而本仓真实写法都不带 driver ⇒ 这一路恒空。同样**拼开写**（见上面那条 ⚠）。
    ('带口令的连接串', re.compile(rb'postgres' + rb'(?:ql)?(?:\+\w+)?://[A-Za-z0-9_.\-]+:'
                                   rb'(?P<cred>[^@\s]{6,})@')),
    ('JWT', re.compile(rb'(?P<cred>\beyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,})')),
)
# **明确合成**的值：任何时候出现都放行（测试夹具与假数据）。
_SYNTHETIC = re.compile(
    rb'(invalid|example|\.test|localhost|localdomain|evil|placeholder|dummy|fake|'
    rb'S3cr3tPW|SecretPass|s3cr3t|CHANGE_?ME|your-|xxxx)', re.I)
# **模板词**：只有"整段凭据就等于它"才放行 —— 那是文档里的格式模板
# （`.codeartsdoer/specs/fund_render/spec.md:271` 写的是 `postgresql://user:password@host:port/dbname`，
# 第一次跑这条闸就是它响的 —— 那是模板不是钥匙）。
# 代价说清楚：真口令若恰好**整个等于** `password` 会被放过；所以控制断言用的是一条不像模板的口令
# （`RealWd123456`），保证这条路仍有牙。
_TEMPLATE_WORDS = {b'password', b'passwd', b'pass', b'pwd', b'secret', b'token',
                   b'your_password', b'yourpassword', b'xxx', b'masked'}


def _env_secrets():
    """从 `.env` 现枚举"键名像凭据"的键值；`.env` 不在就返回空（调用方必须说"今天没判"）。"""
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
            if len(out) >= _VALUE_KEYS_MAX:
                break
            if _CREDENTIAL_KEY.search(key) and len(value) >= 6:
                out[key] = value
            if key == 'DATABASE_URL' and '://' in value and '@' in value:
                cred = value.split('://', 1)[1].split('@', 1)[0]
                if ':' in cred:
                    out['DATABASE_URL.password'] = cred.split(':', 1)[1]
    return {k: v for k, v in out.items() if len(v) >= 6}


def _tracked_text_files():
    """受检集合 = tracked 里**按内容判**不是二进制的文件。

    第 49 轮 A-5 / B-3：以前按扩展名白名单挑 ⇒ tracked 558 个只看 423 个，
    不看的 135 个里有 `.csv` 101 / `.tsv` 4，**其中就有本仓工具自己写进仓库的生产名单**
    （`docs/迭代计划/run-20260920/fund-info-identity-*.csv`），而 `.env.example` 恰恰是
    最常被贴进真值的那个文件。文件头那句"每一个 tracked 文件"当场不成立。
    """
    try:
        listed = subprocess.run(['git', 'ls-files', '-z'], cwd=ROOT, capture_output=True, timeout=60)
    except Exception:
        return None
    if listed.returncode != 0:
        return None
    out = []
    for n in listed.stdout.split(b'\x00'):
        if not n:
            continue
        rel = n.decode('utf-8', 'replace')
        try:
            with open(os.path.join(ROOT, rel), 'rb') as fh:
                if b'\x00' in fh.read(8192):      # 含 NUL 就按二进制处理（png / so / apk…）
                    continue
        except OSError:
            continue                               # 目录 / 断链：本来就不是可扫的文本
        out.append(rel)
    return out


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


def _credential_part(match):
    """取这条规则里**凭据那一段**（三目的另一臂放在 `cred2`）；都拿不到就退回整段。"""
    for name, idx in match.re.groupindex.items():
        if name.startswith('cred'):
            got = match.group(idx)
            if got:
                return got
    return match.group(0)


def _looks_synthetic(match):
    cred = _credential_part(match)
    if cred in _TEMPLATE_WORDS:
        return True
    return bool(_SYNTHETIC.search(cred))


def _shape_hits(files):
    bad = []
    for rel in files:
        blob = _read(rel)
        for label, rx in GENERIC:
            for m in rx.finditer(blob):
                if not _looks_synthetic(m):
                    bad.append((rel, label))
                    break
    return bad


def _scanned_files():
    files = _tracked_text_files()
    if files is None:
        pytest.skip('git 不可用 ⇒ 拿不到 tracked 清单（跳过，不算通过）')
    # 地板值给"这一族至少要有"，不给到贴近真值：真值会随无关整理漂移，
    # 卡太紧等于让一条密钥闸因为整理文件而变红 —— 那会逼人绕过它。
    assert len(files) > 450, '受检的 tracked 非二进制文件只有 %d 个 ⇒ 这份清单本身不可信' % len(files)
    return files


def test_no_credential_shapes_in_tracked_files():
    """形状扫描不依赖 `.env` ⇒ 干净克隆 / CI 上它也**真的在判**（不再与值扫描混在一起）。"""
    files = _scanned_files()
    shapes = _shape_hits(files)
    assert not shapes, '这些入库文件里有"不像占位符"的凭据形状，得人来判一次：%s' % sorted(set(shapes))


def test_no_secret_values_live_in_tracked_files():
    """真值扫描：`.env` 不在就说"没判"，不许照绿（同仓第 45 轮的规矩：尺子拿不到要 skip）。"""
    files = _scanned_files()
    secrets = _env_secrets()
    if not secrets:
        pytest.skip('本机没有 .env ⇒ 真值这一半今天没判（形状那一半由另一条用例判）')
    print('[密钥闸] 真值扫描判了 %d 把（只报键名）；受检文件 %d 个' % (len(secrets), len(files)))
    bad = _value_hits(files, [(k, v.encode('utf-8')) for k, v in secrets.items()])
    assert not bad, '这些入库文件里出现了 .env 密钥的真值（只报键名）：%s' % sorted(set(bad))


def test_the_value_scanner_would_catch_a_planted_secret(tmp_path):
    """控制断言①：把真值造进文件 ⇒ 必须点名；没造进去的不许点名（闸不是墙）。"""
    planted = 'Planted-Secret-Value-913'
    fake = tmp_path / 'leak.md'
    fake.write_text(u'文档里贴了一句口令：%s\n' % planted, encoding='utf-8')
    hits = _value_hits([str(fake)], [('ACCESS_PASSWORD', planted.encode('utf-8'))])
    assert hits and hits[0][1] == 'ACCESS_PASSWORD', '把真值造进文件里却没被发现 ⇒ 这把尺子是空的'
    assert _value_hits([str(fake)], [('ACCESS_PASSWORD', b'Not-The-Secret-At-All')]) == []


def test_every_shape_rule_has_its_own_planted_sample(tmp_path):
    """控制断言②：样品按 `GENERIC` **逐条**生成 —— 没有样品的规则直接红。

    第 49 轮 A-5：上一版四格样品里两格打的是同一条规则（连接串的两种写法），
    于是 `x-access-token:` / 私钥块 / `sk-` 三条**恒空也无人盯**。
    注意"带口令的连接串"这一格必须**拼出来**：本文件自己也是 tracked 文本文件，
    一条完整的凭据字面量会让这把闸在 `git add` 之后**点自己的名**（试过，先跑一遍就响）。
    """
    plants = {
        'github 令牌': u"token='ghp_%s'\n" % ('A' * 36),
        'github 其它形态令牌': u"t='github_pat_%s'\n" % ('B' * 40) + u"o='ghs_%s'\n" % ('C' * 30),
        '带令牌的 git 远端': u"remote https://x-access-token:%s@github.com/o/r\n" % ('D' * 24),
        '私钥块': u"-----BEGIN RSA PRIVATE KEY-----" + u"%s\n" % ('E' * 64),
        'sk- 型 API key': u"LLM_API_KEY='sk-%s'\n" % ('F' * 32),
        '带口令的连接串': u"URL='%s://%s:%s@%s:5432/prod'\n"
                       % ('postgres' + 'ql', 'svc', 'Real' + 'Wd123456', 'db' + '.corp'),
        'JWT': u"jwt='eyJ%s.%s.rest'\n" % ('A' * 24, 'B' * 14),
    }
    missing = {label for label, _rx in GENERIC} - set(plants)
    assert not missing, '这些形状规则没有自己的样品 ⇒ 恒空也测不出：%s' % sorted(missing)
    for i, (label, text) in enumerate(sorted(plants.items())):
        p = tmp_path / ('plant_%d.md' % i)
        p.write_text(text, encoding='utf-8')
        assert _shape_hits([str(p)]), '形状规则认不出「%s」⇒ 这一路是恒空的' % label


def test_a_template_word_passes_but_a_real_password_sneaking_next_to_it_does_not(tmp_path):
    """占位符只判**凭据那一段**（B-3 的直接反证）。

    旧写法判的是整段匹配 ⇒ `postgresql://example:S3cr3tPW…` 那种"用户名里含 example"的行
    会被一起放过，而真正该判的是凭据本身；文档当时只承认"真口令恰好等于 password"这一种代价，
    实际比那句宽得多。下面这条样品里的"口令"用拼接构造 ——
    写成完整字面量的话，本文件（也是 tracked 文本）会被自己点名的名。
    """
    tmpl = tmp_path / 'template.md'
    tmpl.write_text(u"配置形如 postgresql://user:password@host:5432/dbname\n", encoding='utf-8')
    assert not _shape_hits([str(tmpl)]), '文档里的格式模板被判成真泄漏 ⇒ 这条闸会逼人绕过它'

    sneaky = tmp_path / 'sneaky.md'
    sneaky.write_text(u"URL='postgresql://example:%s@prod-host:5432/proddb'\n" % ('Ky3' + '!zq9Xm2'),
                      encoding='utf-8')
    assert _shape_hits([str(sneaky)]), \
        '用户名含 example 就让真口令隐身 ⇒ 判的还是整段匹配，没改成只判凭据组'
