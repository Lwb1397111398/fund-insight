# -*- coding: utf-8 -*-
"""文档里"`test_xxx.py` N 条"这类枚举承诺，必须等于 pytest 当场收集到的条数。

为什么存在（第 41 轮两份复评共同指出的**结构性**扣分，不再逐处手改）：
文档爱写"这条闸有 5 条用例钉着"，而用例只会越加越多 —— 本轮实测 `AGENTS.md` 两处
（seed 那道闸写 5 条、当场 8 条；模块总览写 `test_frontend_cold_start.py` 6 条、当场 41 条）
已经漂了。漂掉的不是数字本身，是"这句话还能不能信"。同一条教训第 35 轮也栽过（手抄的准确率）。

**这把尺子管得到什么、管不到什么**（第 42 轮两份评审都追问过"你是不是又在说满话"，写死在这里）：
- **判红**：`test_x.py` 与"N 条/个/项/种/处"在 12 字内相邻的**当场账**（中文数字也认 ——
  第 41 轮 B-MINOR-4 指出只认 ASCII 的话，"三条钉着""那两条行为判据"这类结构性看不见）。
  扫描单位是**段**不是行（第 45 轮 A-m6）：文件名落在行末、数落在行首的软换行排版，
  旧写法压根读不到；跨行的间隔只许一处换行且不许跨过句末标点，否则就是把两句拼成一句。
- **跳过**：增量账 —— 写作 `+N 条` / `新增 N 条` / `16→23`，或整**段**是基线流水（含"上一基线""本批"）。
- **只报不判**（打出来请人改写成上面两种形状，但不判红）：
  ① 数字在文件名**之前**且隔得远（"当场照出 14 条漏桩用例（`test_x.py` 整个文件…"）——
     这种句子多半在讲历史，硬判就是把对的数改错；
  ② "五条硬规矩：① …" 这种编号列表账 —— 本仓库正文在一条长 bullet 链上反复续用 ①~⑥，
     截段分不清归属，第一版就误报过 9 处 ⇒ 数错了的尺子比没有尺子更坏。

**第二把账：`docs/*.md` 里每一行 `数据源：…`**（第 42 轮 B-MINOR-6）。
写变量名（`DATABASE_URL`）不算出处 —— 它在本地、Render 上都指生产，在 CI 里可能指测试库。
放行"具体目标"（`.db` / `://` / sqlite / postgres / supabase），或明写"未记录"且给出复现命令；
其余一律判红。生成这些报告的脚本本轮已经改成印机器名，剩下的是旧版留下的历史。

用法：
    python scripts/audit_doc_claims.py            # 报告不一致，退码 3
    python scripts/audit_doc_claims.py --list     # 看哪些当场账、哪些增量账、哪些只报不判
    python scripts/audit_doc_claims.py --fix      # 就地改成当场条数（改完请复跑）

条数的唯一尺子：`python -m pytest tests --collect-only -q`（按文件前缀分组计数）。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / 'scripts') not in sys.path:
    sys.path.insert(0, str(ROOT / 'scripts'))
import _db_guard    # noqa: E402,F401  （只为把 stdout 切成 utf-8，本脚本不连任何库）

DOCS = ['AGENTS.md', 'DEPLOYMENT.md', 'ARCHITECTURE.md', 'PRODUCT.md']
DOC_DIRS = ['docs/模块总览']
# `docs/迭代计划/**` 是带日期的轮次快照（历史账），不进对账范围。

# 两种数必须分开写，否则没人判断得出它是哪一种：
#   **当场账** = "这个文件现在有几条" ⇒ 对账对象；
#   **增量账** = "那一批加了几条" ⇒ 写作 `+N 条` / `新增 N 条` / `16→23`，或整行是基线流水
#                （含"上一基线""本批"）⇒ 跳过。
# 数字**必须认中文**（第 41 轮 B-MINOR-4：只认 ASCII 的话，`AGENTS.md` 里"三条钉着""那两条
# 行为判据"这种当场账结构上看不见，而它们正是漂得最狠的一类）。
NUM = r'(\d{1,3}|[一二两三四五六七八九十]{1,3})'
CN = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8,
      '九': 9, '十': 10, '十一': 11, '十二': 12, '十三': 13, '十四': 14, '十五': 15,
      '十六': 16, '十七': 17, '十八': 18, '十九': 19, '二十': 20}
# 名字前面必须是"非标识符字符"：`backtest_l1_weighting.py` 里含 `test_l1_weighting.py`，
# 不加这道边界的话，脚本侧的名字会被当成一条测试文件承诺，然后永远"收集不到"（第 42 轮实测误报）。
_TEST_NAME = r'(?<![A-Za-z0-9_-])(test_[a-z0-9_]+\.py)'
# 间隔允许跨过**一处软换行**（第 45 轮 A-m6）：markdown 里"文件名写在一行末尾、
# `（16 条判据…）`写在下一行开头"是常见排版，逐行扫的旧写法让它压根进不了尺子 ——
# `docs/模块总览/前端与接口层.md` 那条早就过时的"16 条判据、35 处变异"就是这么躲过去的。
CLAIM = re.compile(_TEST_NAME + r'([^0-9]{0,12}?)' + NUM + r'\s*(?:条|个|项|种|处)')
CLAIM_BEFORE = re.compile(NUM + r'\s*(?:条|个|项|种|处)([^。]{0,24}?)' + _TEST_NAME)
# 顶格的列表/标题/引用/表格行 = 新的一段（兄弟 bullet 各算一段，免得把上一句的数配到下一句的名字上）
_BLOCK_BREAK = re.compile(r'^[-*#>|]')
# 数完之后还要看"编号列表自己有几条"：`五条硬规矩：① … ⑥` 这类当场就能自相矛盾
CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮'
LIST_CLAIM = re.compile(NUM + r'\s*(?:条|处|个|种|步|项)\s*'
                       r'[^。\n]{0,14}?(?:硬规矩|规矩|地方|地方|形状|结局|桶|判据|用例)[^。\n]{0,10}[：:]')
DELTA_MARK = re.compile(r'[+＋→~]|新增|拆出|另有')
NARRATIVE_MARK = re.compile(r'上一基线|本批')

# 第二类账（第 42 轮 B-MINOR-6）：报告文档里的"数据源"那一行。
# 起因：`docs/L3_VAGUE_LABEL_ESTIMATE.md` 等 4 行写着 `数据源：\`DATABASE_URL\`` ——
# 那是**变量名**，不是库。本地 `.env` 里它指生产，Render 上也指生产，CI 里可能指测试库，
# 于是"这份报告出自哪个库"在文档里根本没有答案，而第 23 轮那次错（拿镜像的数当系统的数）
# 恰恰就是靠这一格蒙过去的。生成这些报告的脚本本轮已改成印机器名；
# 剩下 4 份是旧版跑出来的，只能标"未记录"并给复现命令 —— 所以判据也认这种形状。
SOURCE_LINE = re.compile(r'数据源[:：]\s*(.+)$')
CONCRETE_TARGET = re.compile(r'\.db\b|://|\bsqlite\b|\bpostgres|supabase|\bhost\b', re.I)
UNRECORDED = re.compile(r'未记录|无法判定|无从判定')
RERUN_COMMAND = re.compile(r'scripts/[\w\-]+\.py')
VARIABLE_ONLY = re.compile(r'DATABASE_URL|LOCAL_DB_URL|DB_URL')


def _to_int(token):
    token = token.strip()
    if token.isdigit():
        return int(token)
    return CN.get(token)


def _doc_files():
    seen = []
    for name in DOCS:
        p = ROOT / name
        if p.is_file():
            seen.append(p)
    for sub in DOC_DIRS:
        seen.extend(sorted((ROOT / sub).glob('*.md')))
    return [p for p in seen if p.is_file()]


def _collected_counts():
    """每个测试文件**当场**收集到几条。尺子只有一把：pytest --collect-only。"""
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', 'tests', '--collect-only', '-q', '--no-header'],
        cwd=str(ROOT), capture_output=True, text=True, encoding='utf-8',
        errors='replace', timeout=900, env=env)
    counts = defaultdict(set)
    for line in (result.stdout or '').splitlines():
        m = re.match(r'^(tests/\S+\.py)::', line.strip())
        if m:
            counts[Path(m.group(1)).name].add(line.strip())
    total = sum(len(v) for v in counts.values())
    if total < 500:
        # 收集本身坏了（conftest 起手就炸 / 互斥锁挡下）⇒ 绝不能把"没收到"说成"条数不符"
        raise SystemExit('[abort] pytest 只收集到 %d 条 ⇒ 尺子本身不可用，'
                         '不做对账。stderr 尾部：%s' % (total, (result.stderr or '')[-300:]))
    return {name: len(ids) for name, ids in counts.items()}


def _blocks(lines):
    """把软换行拼成段：`[(首行行号, [每行在段内的起始偏移], 段文本)]`。

    为什么必须拼段（第 45 轮 A-m6）：旧写法逐行扫，而 markdown 里"文件名落在一行末尾、
    `（16 条判据…）`写在下一行开头"是常见排版 ⇒ `docs/模块总览/前端与接口层.md`
    那句早就过时的"16 条判据、35 处变异"就这样躲在尺子的盲区里。
    断段条件 = 空行，或**顶格**的列表/标题/表格行 —— 兄弟 bullet 各算一段，
    免得把上一句承诺的数配到下一句的名字上（误报的尺子会逼人把对的数改错）。
    """
    out, buf, start = [], [], 1
    for i, line in enumerate(lines, 1):
        if not line.strip():
            if buf:
                out.append((start, buf))
                buf = []
            continue
        if _BLOCK_BREAK.match(line) or not buf:
            if buf:
                out.append((start, buf))
            buf, start = [line], i
        else:
            buf.append(line)
    if buf:
        out.append((start, buf))
    res = []
    for num, buf in out:
        offs, acc = [], 0
        for ln in buf:
            offs.append(acc)
            acc += len(ln) + 1
        res.append((num, offs, '\n'.join(buf)))
    return res


def _wrap_ok(gap):
    """跨行的间隔只允许**一处换行**，且不许跨过句末标点。

    放开 `\n` 之后新出现的风险是"把上一句的数配到下一句的名字"（样品：
    "…14 条漏桩用例（`test_far.py` 整个文件的补拉腿）。" 换行 "五条硬规矩：①…" ⇒
    间隔里带 `。` 又带换行，一眼就是两句）。第 42 轮的教训在这里同样成立：
    **数错了的尺子比没有尺子更坏**，所以宁可少收，不可误伤。
    """
    if gap.count('\n') > 1:
        return False
    return not ('\n' in gap and ('。' in gap or '；' in gap))


def _locate(offs, pos):
    """段内偏移 → `(行号偏移, 行内列)`；行号 = 段首行号 + 返回的第一个值。"""
    idx = len(offs) - 1
    while idx > 0 and offs[idx] > pos:
        idx -= 1
    return idx, pos - offs[idx]


def _claims():
    """返回 `(当场账, 增量账, 看得见但不判)` 三组承诺。

    两种形状都收：① "`test_x.py` N 条"（名字在前）；② "N 条 …… `test_x.py`"（名字在后，
    中间隔不超过 24 个字）。第 41 轮 B-MINOR-4 抓的就是②加中文数字那一族 —— 只认①的话，
    `AGENTS.md` 里"三条钉着""那两条行为判据"这类当场账结构上看不见。
    扫描单位是**段**（见 `_blocks`），这样换行排版不再藏得住话；每处命中再换算回
    `(行号, 行内列)`，`--fix` 才能继续按行改写。
    """
    now, delta, unbound = [], [], []
    for path in _doc_files():
        try:
            rel = str(path.relative_to(ROOT))
        except ValueError:
            rel = str(path)        # 用例会把样品文档放在 tmp 目录里，不许因此炸
        text = path.read_text(encoding='utf-8', errors='replace')
        lines = text.splitlines()
        for num, offs, para in _blocks(lines):
            # 基线流水按**段**判。第 46 轮 B-M2 提议改按句判，我试了，实测驳回：
            # `AGENTS.md` 的"当前测试基线"是一整条 `（上一基线 X → 本批 Y：+N 条，
            # 分布在 test_a.py 新增 3 条（…）` 的链，切句之后那句"新增 3 条"就脱离流水语境
            # 被当成当场账 ⇒ 当场量到一处假红（文档写 3 条、pytest 收到 35 条），
            # 而"把对的数改成错的"正是这把尺子最不该做的事。
            # 代价讲明白：**混写在流水段里的真承诺不会被对账** ⇒ 那一条数必须印出来
            # （见 `main()` 里"其中 N 条只因那一段是基线流水"），尺子管不到的范围要可见。

            for m in CLAIM.finditer(para):
                gap = m.group(2)
                stated = _to_int(m.group(3))
                if stated is None:
                    continue
                if not _wrap_ok(gap):
                    unbound.append({'file': rel, 'line': num, 'test': m.group(1),
                                    'stated': stated, 'why': '跨行超过一处或跨过句末标点',
                                    'span': (0, 0), 'text': lines[num - 1].strip()[:120]})
                    continue
                di, dcol = _locate(offs, m.start(3))
                line_no = num + di
                narr = bool(NARRATIVE_MARK.search(para))
                item = {'file': rel, 'line': line_no, 'test': m.group(1), 'stated': stated,
                        'span': (dcol, dcol + (m.end(3) - m.start(3))),
                        'text': lines[line_no - 1].strip()[:120]}
                if narr and not DELTA_MARK.search(gap):
                    # 只有"本来会被 judged、却因为所在段落是流水而被放过"的那些条才计数；
                    # 写成 `+N 条`/`新增 N 条` 的增量账本来就是跳过，不该混进这个数字里
                    # （否则"18 条只因流水"这种话就把两种账说成了一种，等于换了个说法继续骗人）。
                    item['why'] = '所在那一段是基线流水（`上一基线`/`本批`）'
                (delta if (narr or DELTA_MARK.search(gap)) else now).append(item)
            for m in CLAIM_BEFORE.finditer(para):
                stated = _to_int(m.group(1))
                gap = m.group(2)
                if stated is None:
                    continue
                if not _wrap_ok(gap):
                    unbound.append({'file': rel, 'line': num, 'test': m.group(3),
                                    'stated': stated, 'why': '数在名字之前且跨行不合规',
                                    'span': (0, 0), 'text': lines[num - 1].strip()[:120]})
                    continue
                si, scol = _locate(offs, m.start(1))
                line_no = num + si
                blob = lines[line_no - 1]
                if NARRATIVE_MARK.search(para) or DELTA_MARK.search(blob) \
                        or DELTA_MARK.search(gap):
                    continue
                unbound.append({'file': rel, 'line': line_no, 'test': m.group(3),
                                'stated': stated,
                                'span': (scol, scol + (m.end(1) - m.start(1))),
                                'text': blob.strip()[:120]})
        for num, line in enumerate(lines, 1):
            # ③ "五条硬规矩：① … ⑥" 这种：承诺的数与**同一行往下的编号个数**当场就能对上
            lm = LIST_CLAIM.search(line)
            if lm:
                stated = _to_int(lm.group(1))
                block = line[lm.end():]
                i = num
                while i < len(lines) and lines[i].strip():     # 到空行为止都算这一段
                    block += '\n' + lines[i]
                    i += 1
                # 只数**连续的**编号：① 必须紧跟在冒号后（≤40 字内），然后 ②③… 依次出现。
                # 第一版数的是"这一段里出现过的最大编号"，结果九句毫不相等的账全被数成 6 条
                # —— 数错了的尺子比没有尺子更坏，所以先降成"看得见但不判"，再改准了升回来。
                if CIRCLED[0] in block[:40]:
                    pos = block.index(CIRCLED[0])
                    actual = 1
                    while actual < len(CIRCLED) and CIRCLED[actual] in block[pos:]:
                        pos = block.index(CIRCLED[actual], pos)
                        actual += 1
                    if stated is not None:
                        # 仍然只当**提示**，不判红：这个仓库的正文在一条长 bullet 链上反复续用
                        # ①②③④⑤⑥（`AGENTS.md:295` 的"三条硬规矩"与 12 行之后的"三处别再说满话"
                        # 共用同一串标记），"顺着空白行截段"分不清哪一段属于哪句承诺 ⇒
                        # 硬判就会误报。误报的尺子会逼人把对的数改错，比不判更坏。
                        unbound.append({'file': rel, 'line': num, 'test': None, 'stated': stated,
                                        'listed': actual, 'span': (lm.start(1), lm.end(1)),
                                        'text': line.strip()[:120]})
    return now, delta, unbound


def _rel(path):
    """给报告用的相对路径；判据在临时目录里造的样品不在仓库内，那种就直接印全路径。"""
    try:
        return str(Path(path).relative_to(ROOT)).replace('\\', '/')
    except ValueError:
        return str(path).replace('\\', '/')


def _report_docs():
    """报告类文档：`docs/*.md`（不在 `_doc_files()` 的条数对账范围内）。"""
    return sorted(p for p in (ROOT / 'docs').glob('*.md') if p.is_file())


def _source_lines():
    """每一行 `数据源：X` 到底认不认得出**是哪个库**（第 42 轮 B-MINOR-6）。

    放行三种：① 写得出具体目标（`.db` / `://` / sqlite / postgres / supabase）；
    ② 明写"未记录/无法判定"**并且**给出复现命令（`scripts/xxx.py`）——
       旧版脚本跑出来的报告改不了历史，但必须承认自己改不了；
    ③ 两者都没有 ⇒ 报出来。
    只看这一行开头的冒号往后，续行的缩进说明也算进同一段（那 4 份"未记录"就是分两行写的）。
    """
    bad = []
    seen = 0
    for path in _report_docs():
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        for num, line in enumerate(lines, 1):
            m = SOURCE_LINE.search(line)
            if not m:
                continue
            seen += 1
            block = m.group(1)
            i = num
            while i < len(lines) and lines[i].strip() and not lines[i].lstrip().startswith(
                    ('- ', '* ', '# ', '|')):
                block += ' ' + lines[i].strip()
                i += 1
                if i - num > 6:
                    break
            if CONCRETE_TARGET.search(block):
                continue
            if UNRECORDED.search(block) and RERUN_COMMAND.search(block):
                continue
            bad.append({'file': _rel(path), 'line': num,
                        'text': line.strip()[:120],
                        'why': ('只写了变量名 ⇒ 认不出哪个库' if VARIABLE_ONLY.search(block)
                                else '没写具体目标，也没承认"未记录"并给复现命令')})
    return bad, seen


def main():
    ap = argparse.ArgumentParser(description='对账文档里的"N 条用例"承诺')
    ap.add_argument('--fix', action='store_true', help='就地改成实测条数')
    ap.add_argument('--list', action='store_true', help='只列出认出的承诺')
    args = ap.parse_args()

    sources, source_total = _source_lines()

    now, delta, unbound = _claims()
    if not now and not delta:
        print('[abort] 一条"N 条用例"承诺都没认出 ⇒ 文档写法变了，这条审计已经空转')
        return 4
    if args.list:
        for c in now:
            print('[当场账]       %-42s %s:%s 说 %d 条' % (c['test'], c['file'], c['line'], c['stated']))
        for c in delta:
            print('[增量账-跳过] %-43s %s:%s 说 %d 条  %s'
                  % (c['test'], c['file'], c['line'], c['stated'],
                     c.get('why') or '（写成 `+N 条`/`新增 N 条` 的增量账）'))
        for c in unbound:
            # 这一行以前印"编号列表实际到 N" —— 第 43 轮 A 席量到那个 N 是错的：
            # 本仓库正文把 ①~⑥ 在一条长 bullet 链上反复续用，计数器把两句承诺数成了一句。
            # 既然数不准，就不许印一个数（印错的尺子会逼人把对的数改错）。
            print('[看得见但不判] %-40s %s:%s 说 %d 条（这一族的计数本脚本自认数不准，请改写成当场账写法）'
                  % (c['test'] or '编号列表账', c['file'], c['line'], c['stated']))
        print('[数据源账] 认出 %d 行 `数据源：…`，其中 %d 行认不出是哪个库' % (source_total, len(sources)))
        for c in sources:
            print('   [%s:%s] %s ← %s' % (c['file'], c['line'], c['why'], c['text']))
        print('共 %d 条当场账、%d 条增量账（其中 %d 条是因为那一句是基线流水）、%d 条不判账'
              % (len(now), len(delta),
                 sum(1 for c in delta if c.get('why')), len(unbound)))
        return 0

    counts = _collected_counts()
    bad, missing = [], []
    for c in now:
        # "编号列表自己数出来几条"这类承诺（`test` 为 None）不查 pytest，只跟列表比
        actual = c['listed'] if c.get('test') is None else counts.get(c['test'])
        if actual is None:
            missing.append(c)
        elif actual != c['stated']:
            bad.append((c, actual))
    print('[对账] 当场承诺 %d 条（另有 %d 条增量账：其中 %d 条只因所在那一段是基线流水、'
          '%d 条写成 `+N 条`/`新增 N 条`；还有 %d 条"看得见但不判"）；'
          'pytest 当场收集到 %d 个测试文件'
          % (len(now), len(delta), sum(1 for c in delta if c.get('why')),
             sum(1 for c in delta if not c.get('why')), len(unbound), len(counts)))
    for c in unbound:
        print('   [不判-请改写] %-38s %s:%s 说 %d 条 ← %s'
              % (c['test'] or '编号列表账', c['file'], c['line'], c['stated'], c['text']))
    for c in missing:
        print('   [收集不到这个文件] %-42s %s:%s 写着 %d 条'
              % (c['test'], c['file'], c['line'], c['stated']))
    for c, actual in bad:
        print('   [%s:%s] %s 文档写 %d 条，当场 %d 条 ← %s'
              % (c['file'], c['line'], c['test'], c['stated'], actual, c['text']))
    print('[数据源账] 认出 %d 行 `数据源：…`，其中 %d 行认不出是哪个库' % (source_total, len(sources)))
    for c in sources:
        print('   [%s:%s] %s ← %s' % (c['file'], c['line'], c['why'], c['text']))
    if args.fix:
        by_file = defaultdict(list)
        for c, actual in bad:
            by_file[c['file']].append((c, actual))
        for rel, items in by_file.items():
            path = ROOT / rel
            lines = path.read_text(encoding='utf-8').splitlines(keepends=True)
            for c, actual in sorted(items, key=lambda x: x[0]['span'][0], reverse=True):
                s, e = c['span']
                lines[c['line'] - 1] = (lines[c['line'] - 1][:s] + str(actual)
                                        + lines[c['line'] - 1][e:])
            path.write_text(''.join(lines), encoding='utf-8')
        print('[fix] 已改写 %d 处条数' % len(bad))
        # `--fix` 只代改**条数**这一种账。剩下的它一句都不碰，也不许假装"修完了"：
        # 数据源那一行要的是"当时连的哪个库"，机器答不出来（重跑才有）；
        # "看得见但不判"的编号列表账要的是人改写句子。
        print('[fix] 不代改：%d 行数据源账、%d 条指向收不到的文件、%d 条不判账'
              % (len(sources), len(missing), len(unbound)))
        return 3 if sources or missing else 0
    if bad or missing or sources:
        print('[结论] %d 处条数不符、%d 处指向收不到的文件、%d 行数据源认不出库'
              ' ⇒ 文档在说没跑过的话' % (len(bad), len(missing), len(sources)))
        return 3
    print('[结论] 全部对得上（条数 %d 条、数据源 %d 行都认得出来自哪个库）'
          '；另有 %d 条"看得见但不判"（编号列表账、基线流水），逐条列在上面'
          % (len(now), source_total, len(unbound)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
