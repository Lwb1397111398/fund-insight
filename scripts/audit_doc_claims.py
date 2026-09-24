# -*- coding: utf-8 -*-
"""文档里"`test_xxx.py` N 条"这类枚举承诺，必须等于 pytest 当场收集到的条数。

为什么存在（第 41 轮两份复评共同指出的**结构性**扣分，不再逐处手改）：
文档爱写"这条闸有 5 条用例钉着"，而用例只会越加越多 —— 本轮实测 `AGENTS.md` 两处
（seed 那道闸写 5 条、实际 8 条；冷启动写"八条形状"、实际 9 条）已经漂了。
漂掉的不是数字本身，是"这句话还能不能信"。同一条教训在第 35 轮也栽过（手抄的准确率）。

用法：
    python scripts/audit_doc_claims.py            # 报告不一致，退码 3
    python scripts/audit_doc_claims.py --fix      # 就地改成实测条数（改完请复跑）
    python scripts/audit_doc_claims.py --list     # 只看它认出了哪些承诺

条数的唯一尺子：`python -m pytest --collect-only -q <那个文件>` 的 `::` 行数。
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

# `test_xxx.py` 之后 ≤10 个字符内出现"N 条"就算一条承诺。
CLAIM = re.compile(r'(test_[a-z0-9_]+\.py)([^0-9\n]{0,10}?)(\d+)\s*条')
# 两种数必须分开写，否则没人判断得出它是哪一种：
#   **当场账** = "这个文件现在有几条" ⇒ 对账对象；
#   **增量账** = "那一批加了几条" ⇒ 写作 `+N 条` / `新增 N 条` / `16→23`，或整行是基线流水
#                （含"上一基线""本批"）⇒ 跳过。
DELTA_MARK = re.compile(r'[+＋→~]|新增')
NARRATIVE_MARK = re.compile(r'上一基线|本批')


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


def _claims():
    """返回 `(当场账, 增量账)` 两组承诺。"""
    now, delta = [], []
    for path in _doc_files():
        try:
            rel = str(path.relative_to(ROOT))
        except ValueError:
            rel = str(path)        # 用例会把样品文档放在 tmp 目录里，不许因此炸
        text = path.read_text(encoding='utf-8', errors='replace')
        for num, line in enumerate(text.splitlines(), 1):
            narrative = bool(NARRATIVE_MARK.search(line))
            for m in CLAIM.finditer(line):
                item = {'file': rel, 'line': num, 'test': m.group(1),
                        'stated': int(m.group(3)), 'span': (m.start(3), m.end(3)),
                        'text': line.strip()[:120]}
                (delta if (narrative or DELTA_MARK.search(m.group(2))) else now).append(item)
    return now, delta


def main():
    ap = argparse.ArgumentParser(description='对账文档里的"N 条用例"承诺')
    ap.add_argument('--fix', action='store_true', help='就地改成实测条数')
    ap.add_argument('--list', action='store_true', help='只列出认出的承诺')
    args = ap.parse_args()

    now, delta = _claims()
    if not now and not delta:
        print('[abort] 一条"N 条用例"承诺都没认出 ⇒ 文档写法变了，这条审计已经空转')
        return 4
    if args.list:
        for c in now:
            print('[当场账]       %-42s %s:%s 说 %d 条' % (c['test'], c['file'], c['line'], c['stated']))
        for c in delta:
            print('[增量账-跳过] %-43s %s:%s 说 %d 条' % (c['test'], c['file'], c['line'], c['stated']))
        print('共 %d 条当场账、%d 条增量账' % (len(now), len(delta)))
        return 0

    counts = _collected_counts()
    bad, missing = [], []
    for c in now:
        actual = counts.get(c['test'])
        if actual is None:
            missing.append(c)
        elif actual != c['stated']:
            bad.append((c, actual))
    print('[对账] 当场承诺 %d 条（另有 %d 条按增量账跳过）；pytest 当场收集到 %d 个测试文件'
          % (len(now), len(delta), len(counts)))
    for c in missing:
        print('   [收集不到这个文件] %-42s %s:%s 写着 %d 条'
              % (c['test'], c['file'], c['line'], c['stated']))
    for c, actual in bad:
        print('   [%s:%s] %s 文档写 %d 条，当场 %d 条 ← %s'
              % (c['file'], c['line'], c['test'], c['stated'], actual, c['text']))
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
        print('[fix] 已改写 %d 处' % len(bad))
        return 0
    if bad or missing:
        print('[结论] %d 处条数不符、%d 处指向收不到的文件 ⇒ 文档在说没跑过的话'
              % (len(bad), len(missing)))
        return 3
    print('[结论] 全部对得上')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
