# -*- coding: utf-8 -*-
"""结论证据的可复现性判定（派生值，不落库、不加列）。

为什么做成派生而不是一个布尔列：这一类状态会随着净值序列的修正**自己变回去**
（补拉到位、历史被更正），写进列就要面对"谁来清、什么时候清、迁移要不要跑"
（本仓库生产加列＝一次迁移）。读的时候算一次，永远与净值表一致，也不可能出现
"列说证据有效而表里根本没有那一行"这种第二份真值。

它回答的问题是第 16/17 轮查出来的那件事：1163 条已判结论里有 250 条，
**结论存的端点净值在"当前标的"的净值表里已经复现不出来**。历轮自洽检查看不见它 ——
`verify_score` 与 `verify_history` 是同一次验证一起写的，互相吻合，
却可能整体挂着另一个标的、或一份被就地改写过的净值。

三种状态（None＝证据仍然成立）：
  - `verdict_under_other_fund` 写下结论时行上挂的是**另一个**代码，且那个代码当天的净值
    正好等于存的端点值 ⇒ 真·挂错标的（改标后没重算）。
  - `nav_row_missing`            端点那一天该标的没有净值行：周末目标日的老数据、镜像缺行、基金停更都在这一桶。
  - `nav_rewritten`              同一天有行，数值不同 ⇒ 净值被就地改写或覆盖过。
"""
import ipaddress
import os
import re
from datetime import date, datetime
from typing import Dict, Iterable, List, Optional

FLOAT_TOL = 1e-6


def _same_number(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) <= FLOAT_TOL * max(1.0, abs(float(a)))
    except (TypeError, ValueError):
        return a == b


def has_verdict(prediction) -> bool:
    return getattr(prediction, 'is_correct', None) is not None \
        and getattr(prediction, 'end_nav', None) is not None \
        and getattr(prediction, 'end_nav_date', None) is not None


def _as_date(value):
    if value is None or isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def evidence_status(prediction, nav_rows_by_key, verified_code=None) -> Optional[str]:
    """纯判据版本：`nav_rows_by_key` 是 `{(fund_code, nav_date): nav}` 批量预取的结果。

    拆成纯函数的原因与 `resync_verdict_scalars.find_desynced` 一样：
    判据要能脱离数据库被测（列表页一次 200 条，不能每条各打一次查询）。
    """
    if not has_verdict(prediction):
        return None
    code = prediction.fund_code
    end_date = _as_date(prediction.end_nav_date)
    current_nav = nav_rows_by_key.get((code, end_date))
    if verified_code and verified_code != code:
        # 两条同时成立才算"挂错标的"：结论当时是另一个代码，且那个代码当天的净值
        # 正好等于存的端点值。只看"改过标"会把"改标后又重验过"的行误判
        # （第 17 轮我第一版就这么错过：报出的 97 条里 95 条末次验证在改标之后）。
        if _same_number(nav_rows_by_key.get((verified_code, end_date)), prediction.end_nav):
            return 'verdict_under_other_fund'
    if current_nav is None:
        return 'nav_row_missing'
    if not _same_number(current_nav, prediction.end_nav):
        return 'nav_rewritten'
    return None


def _verified_codes(db, predictions: List) -> Dict[int, Optional[str]]:
    """每条预测**写下最后一条结论时**挂的 fund_code（最后一条 verified 变更日志的后像）。"""
    from src.models.database import PredictionChangeLog

    ids = [p.id for p in predictions]
    out: Dict[int, Optional[str]] = {}
    if not ids:
        return out
    # 按 id 升序遍历（id 即写入顺序）⇒ 后写覆盖前写，留下的就是"最后一次验证当时"的代码。
    # 只查需要的列：列表页一次可能带 200 条预测，日志行数在几千量级。
    rows = db.query(PredictionChangeLog.prediction_id,
                    PredictionChangeLog.after_state).filter(
        PredictionChangeLog.prediction_id.in_(ids),
        PredictionChangeLog.action == 'verified',
    ).order_by(PredictionChangeLog.prediction_id,
               PredictionChangeLog.id).all()
    for pid, after in rows:
        out[pid] = (after or {}).get('fund_code')
    return out


def evidence_statuses(db, predictions: Iterable) -> Dict[int, Optional[str]]:
    """批量算证据状态：两条查询，不给列表页加 N+1。"""
    from src.models.database import FundHistory

    rows = [p for p in predictions]
    judged = [p for p in rows if has_verdict(p)]
    if not judged:
        return {p.id: None for p in rows}

    dates = sorted({_as_date(p.end_nav_date) for p in judged if _as_date(p.end_nav_date)})
    codes = {p.fund_code for p in judged if p.fund_code}
    verified = _verified_codes(db, judged)
    codes |= {c for c in verified.values() if c}

    nav_rows = db.query(FundHistory.fund_code, FundHistory.nav_date, FundHistory.nav).filter(
        FundHistory.nav_date.in_(dates),
        # 必须按代码过滤：只按日期筛的话，一页 70 个日期就会把**全表**拉回来
        # （实测 8724/10600 行 = 82%），在 Supabase 上等于每翻一页回传一次净值全表。
        FundHistory.fund_code.in_(list(codes)),
    ).all() if codes and dates else []
    nav_by_key = {(c, _as_date(d)): nav for c, d, nav in nav_rows if c in codes}
    return {p.id: evidence_status(p, nav_by_key, verified.get(p.id)) for p in rows}


def stale_counts(db) -> Dict[str, int]:
    """全库已判结论里各失效成因的条数（每日跑批与审计脚本共用，别各写一份 SQL）。"""
    from src.models.database import Prediction

    rows = db.query(Prediction).filter(
        Prediction.is_deleted == False,                     # noqa: E712
        Prediction.is_correct.isnot(None),
        Prediction.end_nav.isnot(None),
        Prediction.end_nav_date.isnot(None)).all()
    counts: Dict[str, int] = {}
    for kind in evidence_statuses(db, rows).values():
        if kind:
            counts[kind] = counts.get(kind, 0) + 1
    return counts


EVIDENCE_LABELS = {
    'verdict_under_other_fund': '结论是按改标前的标的判的，至今没重算',
    'nav_row_missing': '端点那天该标的没有净值行（周末老数据 / 本地缺行 / 基金停更），需按区间回补后重验',
    'nav_rewritten': '端点那天的净值后来被修正过，结论用的数已复现不出来',
}


def evidence_label(status: Optional[str]) -> Optional[str]:
    return EVIDENCE_LABELS.get(status) if status else None


def judged_rows(db) -> List:
    """未删除、已按下结论的预测（唯一取法，脚本与接口共用）。"""
    from src.models.database import Prediction
    rows = db.query(Prediction).filter(
        Prediction.is_deleted == False,                      # noqa: E712
        Prediction.is_correct != None,
    ).all()
    return [r for r in rows if has_verdict(r)]


def span_report(db) -> Dict:
    """把"多少结论证据已失效"折算成一份可展示的体检报告（**唯一出处**）。

    为什么放在服务层而不是脚本里：脚本 `audit_verdict_evidence.py` 与页面都要报这几个数，
    两把尺子迟早打架（第 23 轮：我给老板报了十几轮**镜像库**的数，生产其实是另一组）。
    所以这里连 `database` 与 `as_of` 一起给出去 —— 数字必须带库名和截止日。
    """
    from src.services.prediction_lifecycle import current_as_of

    rows = judged_rows(db)
    judged = len(rows)
    correct = sum(1 for r in rows if r.is_correct)
    statuses = evidence_statuses(db, rows)
    stale_rows = [r for r in rows if statuses.get(r.id)]
    stale_correct = sum(1 for r in stale_rows if r.is_correct)
    by_kind: Dict[str, int] = {}
    for r in stale_rows:
        kind = statuses.get(r.id)
        by_kind[kind] = by_kind.get(kind, 0) + 1
    pct = lambda n: round(100.0 * n / judged, 2) if judged else 0.0
    return {
        'judged': judged,
        'correct': correct,
        'accuracy_pct': pct(correct),
        'stale_evidence': len(stale_rows),
        'stale_pct': pct(len(stale_rows)),
        # 两端假设：失效的这批"全判错" / "全判对"。真值在区间内，今天定不到小数点。
        'span_low_pct': pct(correct - stale_correct),
        'span_high_pct': pct(correct - stale_correct + len(stale_rows)),
        'by_kind': by_kind,
        # 统一走 `current_as_of()`（北京时间自然日）：第 24 轮评审指出 Render 没设 TZ，
        # `date.today()` 在 UTC 下每天会有 8 小时显示"截至昨天"，而这个字段存在的理由
        # 恰恰是"数字必须带截止日"。
        'as_of': current_as_of().isoformat(),
        'database': database_label(db),
    }


_DSN_KEEP_KEYS = ('host', 'hosts', 'port', 'dbname', 'database')
# 一个键值对：值可以是 `'带空格的'`、`"带空格的"` 或裸词。分隔符按 libpq/URI 两种写法都认
# （`&` 与 `,` 以前不切，于是 `host=h&password=X` 整串被当成**一个**值原样印出来 —— A-m2）。
_DSN_PAIR = re.compile(r"(?P<k>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*"
                       r"(?:'(?P<q1>[^']*)'|\"(?P<q2>[^\"]*)\"|(?P<p>[^\s'\",;=&]*))")
_DSN_SECRET_KEYS = ('password', 'passwd', 'pwd', 'secret')


def _conninfo_target(text: str) -> str:
    """**没有** `://` 的连接串（libpq/pgbouncer 的 `key=value` 写法）→ 只留不涉密的键。

    第 44 轮 A-m6：旧写法是"认不出 scheme 就原样返回"，于是
    `host=db.example.com user=u password=真口令 dbname=proddb` 会被自报行**整条印出来** ——
    而自报的目的恰恰是"把目标打进 stdout / Render 日志 / `docs/` 报告"。
    报库名的机制自己成了凭据泄露面，这与它要防的事同级。
    与 `scripts/_db_guard._conninfo_target()` 是同一件事的两份实现。
    """
    if '=' not in text:
        # 认不出键值写法时按"@ 之后"处理：宁可少说，不可把凭据多说出去。
        return text.split('@')[-1] or '(空)'
    # 引号不配对 ⇒ 键值切分不可信（第 45 轮 B-m1：`password='two words dbname=LEAKED'`
    # 会被按空格切分的写法当成两个键，口令的第二个词以 `dbname=` 的名义印出来）。
    # 这种情况只留 host/port。
    unbalanced = (text.count("'") % 2) or (text.count('"') % 2)
    kept, secret_seen = [], False
    for m in _DSN_PAIR.finditer(text):
        key = (m.group('k') or '').strip().lower()
        value = next((g for g in (m.group('q1'), m.group('q2'), m.group('p'))
                      if g is not None), '').strip()
        if key in _DSN_SECRET_KEYS:
            secret_seen = True
            continue
        if key in _DSN_KEEP_KEYS and value and not (unbalanced and key not in ('host', 'port')):
            kept.append('%s=%s' % (key, value))
    out = ' '.join(kept)
    if unbalanced and secret_seen:
        out += '（引号不配对，其余字段已隐去）'
    return out or '(DSN：只留下非凭据字段，其余已隐去)'


_SECRET_IN_TEXT = re.compile(r"([?;&/]|^)(password|passwd|pwd|secret|token|apikey|api_key)"
                             r"\s*=\s*[^;&/\s]*", re.I)


def _redact_secrets(text: str) -> str:
    """任何 `key=值` 形状的涉密段换成 `[隐去]`（与守卫侧 `_redact_secrets` 同文）。"""
    return _SECRET_IN_TEXT.sub(lambda m: '%s%s=[隐去]' % (m.group(1), m.group(2).lower()), text)


def target_name(url: str) -> str:
    """连接串 → **打得开的那个目标**：sqlite 给文件路径，远程给 `scheme://host/db`（不含口令）。

    与 `scripts/_db_guard.machine_name()` 是同一把尺子的两份实现（src 不能去 import scripts，
    而 `_db_guard` 必须能在"还没决定连哪个库"之前被导入 ⇒ 它也不能 import src）。
    两份实现不许各说各话：`tests/unit/test_database_label_targets.py` 拿一批同样的样品
    逐一比对两者的输出。
    """
    if not url:
        return '(空)'
    if '://' not in url:
        return _conninfo_target(url)
    scheme, rest = url.split('://', 1)
    if scheme.lower().startswith('sqlite'):
        body = url[len('sqlite:///'):] if url.lower().startswith('sqlite:///') else rest
        body = body.split('?', 1)[0]
        # 与守卫侧同一条：`user:pass@` 这种形状只有"吃原始串"时才见得到，
        # 而"口令一个字符都不出现"这条不变式不分方言（第 46 轮 B-minor-4）。
        body = re.sub(r'^[^/]*:[^/]*@', '', body)
        if body.startswith('//'):
            body = body.lstrip('/')
        if re.match(r'^/[A-Za-z]:[\\/]', body):
            body = body[1:]                    # 四斜杠 Windows 绝对路径：/E:/… → E:/…
        return body or '(当前目录里的 sqlite 文件)'
    # 剪掉 query / fragment / `;` 参数：`postgresql://h/db?password=X` 里最涉密的那一段
    # 不能跟着"这是哪个库"进日志（第 45 轮 A-m2 / B-m1，与守卫侧同一条改动）
    where = re.split(r'[?;&#]', rest.split('@')[-1], 1)[0]
    # 没有分隔符的整段也要过涉密键：`postgres:///password=X` 把口令藏在路径位（第 46 轮）
    where = _redact_secrets(where)
    # 与 `scripts/_db_guard.machine_name()` 同步（第 47 轮 B-6）：netloc 为空、主机只写在
    # `?host=` 里的那一种合法串，以前报成 `线上生产库（postgresql:///postgres）` ——
    # 档位对，可"是哪一台"没说。两份实现不能互相 import，所以补在同一处、同一条件，
    # 由 `test_the_two_self_report_rulers_stay_identical` 逐样品钉相等。
    if (rest.split('@')[-1].split('?')[0]).startswith('/'):
        params = _db_hosts(url)
        if params:
            where = '%s@%s' % (where, ','.join(params))
    return '%s://%s' % (scheme, where)


MIRROR_SUFFIX = os.path.join('data', 'fund_insight.db')
# 本项目那台线上库的**注册域**：与 `scripts/_db_guard._PROD_DB_DOMAINS` 逐字相等
# （两份实现由 `tests/unit/test_database_label_targets.py` 钉住，漂了就红）。
_PROD_DB_DOMAINS = ('supabase.com', 'supabase.co', 'supabase.in')


def is_the_mirror(name: str) -> bool:
    """这个 sqlite 目标**是不是**真镜像 —— 比的是同一个 inode/规范路径，不是后缀。

    为什么不能用后缀（第 43 轮 B-MINOR-1）：`sqlite:///C:/backup/2026-09/data/fund_insight.db`
    （某次备份）与 `sqlite:///…/data/_tmp/data/fund_insight.db` 都以 `data/fund_insight.db` 结尾，
    用 `endswith` 会把它们印成和真镜像一字不差的"本地镜像库（data/fund_insight.db）" ——
    而这正是第 23 轮那次错（拿别的库的数当系统的数）最需要被标签看穿的情形。
    """
    if not name or name == ':memory:':
        return False
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    mirror = os.path.realpath(os.path.join(root, MIRROR_SUFFIX))
    candidate = name.replace('\\', '/')
    if not os.path.isabs(candidate):
        candidate = os.path.join(root, candidate)
    try:
        return os.path.realpath(candidate) == mirror
    except OSError:
        return False


def _db_hosts(url: str) -> list:
    """连接串里能找到的**所有**候选主机（小写、去端口、不含口令）。

    与 `scripts/_db_guard._db_hosts()` 同形（两份实现，逐条相等由
    `tests/unit/test_database_label_targets.py` 钉住）。第 46 轮 B-M6 / A-m1：
    主机可以只写在 `?host=` 里（SQLAlchemy/PgBouncer 的正规写法），也可以是逗号分隔的
    多主机列表 —— 只看 netloc 会解析出**空主机**，而空主机以前被当成"本机"，
    于是真生产被两把尺子**一致地**报成 `本机 PostgreSQL（…，不是线上生产库）`。
    """
    hosts = []
    if '://' in url:
        rest = url.split('://', 1)[1]
        where = re.split(r'[?;&#]', rest.split('@')[-1], 1)[0]
        netloc = where.split('/')[0]
        if netloc.startswith('['):
            head = netloc[:netloc.find(']') + 1] if ']' in netloc else netloc
        else:
            head = netloc.split(':')[0]
            if not head and netloc:
                # 不带方括号的 IPv6：按 `:` 一切就剩空串（与守卫侧同一条修法）
                head = netloc
        hosts += [h.lower() for h in re.split(r'[,\s]+', head) if h]
    for m in re.finditer(r'(?:^|[?&;\s,])hosts?\s*=\s*(?:\'([^\']*)\'|"([^"]*)"|([^&;\s"\']+))',
                         url, re.I):
        value = m.group(1) or m.group(2) or m.group(3) or ''
        hosts += [h.lower().strip('[]') for h in re.split(r'[,\s]+', value) if h]
    return hosts


def _is_a_local_host(host: str) -> bool:
    """localhost / 回环 / 私网 ⇒ 这台**不可能**是线上生产库（第 45 轮 B-m5）。"""
    if not host or host in ('localhost', '::1', '[::1]'):
        return True
    try:
        ip = ipaddress.ip_address(host.strip('[]'))
    except ValueError:
        return False                      # 域名：不是"显然本机"，也不许被判成本机
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)


def _is_the_production_host(host: str) -> bool:
    """主机名**就是**（或挂在）Supabase 的注册域下面 ⇒ 才是本项目那台线上库。

    与 `scripts/_db_guard._is_the_production_host()` 同一张表：判据不许写成
    `'supabase' in host` —— `notsupabase.evil.example` 会被那种子串买通
    （第 43 轮 `push_sector_mappings_to_prod.py` 上的同名课程）。
    """
    host = (host or '').lower()
    return any(host == s or host.endswith('.' + s) for s in _PROD_DB_DOMAINS)


def _postgres_words(url: str, name: str) -> str:
    """"这是一个 PostgreSQL"要说到的四档（与守卫侧 `_postgres_words` 同文）。

    本机 / 本项目 Supabase / 别的远程 / **认不出主机**。最后一档是第 46 轮 B-M6 补的：
    看不见主机时旧写法落进"本机"，把线上库说成本机库 —— 认不出就只能承认认不出。
    """
    hosts = _db_hosts(url)
    if any(_is_the_production_host(h) for h in hosts):
        return '线上生产库（%s）' % name
    if hosts and all(_is_a_local_host(h) for h in hosts):
        return '本机 PostgreSQL（%s，不是线上生产库）' % name
    if not hosts:
        return 'PostgreSQL（%s）—— 认不出主机，不敢说它是本机还是线上生产库' % name
    return '远程 PostgreSQL（%s）—— 不是本项目那台 Supabase 生产库' % name


def describe_url(url: str) -> str:
    """连接串 → 一句"这是哪个库"。与 `scripts/_db_guard.db_kind()` 同一套词（两份实现，用例钉相等）。

    scheme 一律**先转小写再判**：守卫侧判的是 `url.lower()`，两边判的对象必须同一个，
    否则 `SQLITE:///x.db` 这种写法会得到两句不同的自报（第 43 轮笛卡尔积样品逼出来的）。
    """
    name = target_name(url)
    low = url.lower()
    if low.startswith('sqlite'):
        if name == ':memory:':
            return '内存 sqlite（不落盘，通常是测试夹具）'
        if is_the_mirror(name):
            return '本地镜像库（data/fund_insight.db）'
        return '本地 sqlite 文件（不是镜像库）：%s' % name
    if low.startswith(('postgres', 'postgresql')):
        return _postgres_words(url, name)
    if '://' not in url and '=' in url and _db_hosts(url):
        # libpq 的 conninfo 写法（`host=… dbname=…`）按定义就是 PostgreSQL，只是没有 scheme。
        # 第 46 轮 B-M2/A-m2：这一族以前只会落到"认不出 scheme"，于是"这是不是那台线上库"
        # 在 q.py / 预检工具的自报行里没有答案 —— 而主机名明明写在串里。
        return _postgres_words(url, name)
    if low.startswith('mysql'):
        return 'MySQL 库（%s）' % name
    # 认不出的 scheme **不许把原串回显出来**（第 44 轮 A-m6）：`low.split('://')[0]` 在没有
    # `://` 时是整个输入，于是 `host=h password=真口令 dbname=d` 里最涉密的那一段
    # 会跟着"这是哪个库"一起进日志。scheme 只在长得像 scheme 时才印。
    scheme = low.split('://', 1)[0] if '://' in low else ''
    if not re.match(r'^[a-z][a-z0-9+._-]{0,19}$', scheme):
        return '认不出 scheme 的连接串（目标：%s）' % name
    return '%s 库（%s）' % (scheme, name)


def database_label(db) -> str:
    """给老板看的库名：本地镜像 / 线上生产，别说"数据库"这种没信息量的词。

    Session / Engine / Connection 三种都得认得：SQLAlchemy 2.0 起 `Connection` 已经没有
    `get_bind()`，只认 Session 的话传引擎进来会被下面那个 except 吞掉、静默降级成"未知库"
    ——而"报出是哪个库"这件事的全部意义就是不许说 Unknown。

    光给"本地镜像库"这五个字仍然不够（第 42 轮 B-c）：`sqlite:///data/copy_x.db`、
    测试用的 `:memory:` 也都会被报成"本地镜像库"，而这三者的后果完全不同
    （副本回放 / 夹具 / 真镜像）。所以名字后面必须跟着**那个文件**或**那台主机**。
    """
    url = None
    for pick in (lambda: db.get_bind().url, lambda: db.url, lambda: db.engine.url):
        try:
            url = str(pick())
            break
        except Exception:
            continue
    if url is None:
        return '未知库'
    return describe_url(url)
