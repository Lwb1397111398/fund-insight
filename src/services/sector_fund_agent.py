# -*- coding: utf-8 -*-
"""板块→基金 agent：LLM 提案 + 抓站验证 + 语义复判 + 关键词搜索回环。

为什么要有这个模块：以前"板块该对应哪只基金"由三套模糊规则决定（板块名子串、
FundInfo.sector_type 反向包含、`search_fund()[0]`），结果就是"识别出来的基金离
板块差十万八千里"。这里把决策改成可复核的闭环：

    T0 确定性候选（精确匹配，零网络）
    T1 LLM 提案（3-5 个候选 + 关键词 + 别名）
    T2 抓站验证（是不是基金、抓不抓得到、LLM 声称的名字与官方名对不对得上）
    T3 LLM 语义复判（该基金能否代表这个板块；无对口时是否为"关联度最大的替代"）
    T4 关键词搜索回环（T1-T3 全灭时用关键词搜真基金，再回 T2/T3）
    T5 收口（拿不准就 needs_review/no_fund，绝不随机填一只）

两条老板亲口定的规则写进代码：
1. 品种优先级 ETF 最高（"最纯粹"），其次 LOF/场外指数，最后才是主动型；
2. 有意代理不是错配：`债券→512000 证券ETF`、`SpaceX→159206 军工ETF` 这类"压根没有
   对口基金"的板块，允许 `match_kind='proxy'`，且 `owner_locked` 的行 agent 不得覆盖。
"""
import json
import logging
import re
import time
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# 判定阈值：由 scripts/calibrate_sector_agent.py 在金标集上标定，改动要同步标定报告
T3_PASS_SCORE = 70          # 语义相关性及格线
AUTO_REVIEW_CONFIDENCE = 0.80   # direct 自动置 reviewed 的置信度（金标集标定，勿拍脑袋改）
AUTO_REVIEW_PROXY_CONFIDENCE = 0.68  # proxy 只到 0.68（覆盖率口径按此计）
CLAIM_SIM_REJECT = 0.35     # LLM 声称名 vs 官方名：低于此判"代码存在但是另一只基金"
MAX_CANDIDATES_PER_SECTOR = 8
MAX_LLM_CALLS_PER_SECTOR = 6   # T1 提案 1 次 + 每轮 T3 复判 1 次（最多 3 轮）+ 兜底 1 次
BONDED_TYPES = ('债券', '债', '货币', '理财', '短债', '纯债')
BOND_SECTOR_HINTS = ('债', '货币', '理财', '同业存单')

COMPANY_PREFIXES = (
    '华夏', '易方达', '南方', '国泰', '华宝', '招商', '广发', '嘉实', '富国', '天弘',
    '银华', '博时', '鹏华', '汇添富', '华安', '建信', '工银', '兴业', '永赢', '摩根',
    '平安', '中欧', '交银', '东方红', '景顺', '长城', '中银', '中信保诚', '创金合信',
    '信澳', '大成', '民生加银', '国联', '英大', '东海', '金鹰', '诺安', '华泰柏瑞',
)
STRIP_TOKENS = ('交易型开放式指数证券投资基金', '指数型', '发起式', '联接', 'LOF', 'ETF',
                '份额', '基金', '指数', 'A类', 'C类', 'A', 'C', 'E', '(LOF)', '（LOF）')


def normalize_fund_name(name: str) -> str:
    """剥离公司名前缀与品种后缀，留下可用于比较的核心词。"""
    s = re.sub(r"[\s\(\)（）\-—·、,，。'\"!！]", '', (name or ''))
    changed = True
    while changed:
        changed = False
        for prefix in COMPANY_PREFIXES:
            if s.startswith(prefix) and len(s) > len(prefix):
                s = s[len(prefix):]
                changed = True
    for token in STRIP_TOKENS:
        if token in s and len(s) > len(token):
            s = s.replace(token, '')
    return s


def bigrams(text: str) -> set:
    text = normalize_fund_name(text)
    return set(text[i:i + 2] for i in range(len(text) - 1)) or set(text)


def similarity(a: str, b: str) -> float:
    """二字滑窗 Dice 相似度（与 scripts/audit_export_baseline.py 的口径一致）。"""
    ga, gb = bigrams(a), bigrams(b)
    if not ga or not gb:
        return 0.0
    return round(2 * len(ga & gb) / (len(ga) + len(gb)), 4)


def is_etf(code: str, name: str = "") -> bool:
    """场内 ETF 判定：名字里有 ETF 最可靠，其次看场内代码段。"""
    if 'ETF' in (name or '').upper():
        return True
    code = (code or '').strip()
    return bool(re.fullmatch(r'(15[0-9]{3}|5[0-9]{5})', code))


def fund_kind_label(code: str, name: str = "") -> str:
    if is_etf(code, name):
        return 'etf'
    if re.fullmatch(r'(16[0-9]{4}|50[0-9]{4}|51[0-9]{4}|52[0-9]{4})', (code or '').strip()):
        return 'lof'
    if re.fullmatch(r'(0[0-9]{5}|1[0-9]{5})', (code or '').strip()):
        return 'otc'
    return 'other'


KIND_PRIOR = {'etf': 1.0, 'lof': 0.75, 'otc': 0.6, 'other': 0.4}
SOURCE_PRIOR = {'t0_reviewed_db': 1.0, 'search': 0.85, 'llm': 0.75, 't0_static': 0.6}


@dataclass
class FundCandidate:
    code: str
    name: str = ''
    source: str = 'llm'
    official_name: str = ''
    fund_type: str = ''
    kind: str = ''
    reason: str = ''
    claim_sim: float = 0.0        # LLM 声称名 vs 官方名（防"代码存在但是别的基金"）
    sector_overlap: float = 0.0   # 板块 vs 官方名：只做排序，不做淘汰
    t3_score: float = 0.0
    t3_suitable: Optional[bool] = None
    t3_proxy: Optional[bool] = None
    verify: Optional[dict] = None
    domain_kind: str = ''       # 基金域判定结果：fund|stock|unknown（与品种 kind 区分）
    rejected: str = ''
    claim_mismatch: str = ''    # LLM 记错代码↔名称，官方名为准，仅打折不再淘汰
    confidence: float = 0.0

    @property
    def display_name(self) -> str:
        return self.official_name or self.name


@dataclass
class SectorDecision:
    sector: str
    chosen: Optional[FundCandidate] = None
    alternatives: List[FundCandidate] = field(default_factory=list)
    status: str = 'needs_review'   # matched | proxy | needs_review | conflict | no_fund | locked
    confidence: float = 0.0
    rounds: int = 0
    evidence: List[dict] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    direct_exhausted: bool = False
    elapsed_ms: int = 0
    degraded: bool = False      # LLM 完全没跑成（熔断/无密钥/解析失败）
    timed_out: bool = False     # 超出预算被截断，与"判定不可用"分开看

    def to_dict(self) -> dict:
        data = asdict(self)
        data['chosen'] = asdict(self.chosen) if self.chosen else None
        return data

    @property
    def auto_reviewable(self) -> bool:
        """是否允许把 reviewed 置真（老板确认过的有意代理走 proxy 阈值）。"""
        if not self.chosen or self.status not in ('matched', 'proxy'):
            return False
        if self.chosen.t3_suitable is not True:
            return False
        need = AUTO_REVIEW_PROXY_CONFIDENCE if self.chosen.t3_proxy else AUTO_REVIEW_CONFIDENCE
        if self.chosen.t3_proxy and not self.direct_exhausted:
            return False
        return self.confidence >= need


def compute_confidence(cand: FundCandidate, direct_exhausted: bool = False) -> float:
    t3 = max(0.0, min(1.0, cand.t3_score / 100.0))
    overlap = min(1.0, cand.sector_overlap / 0.5) if cand.sector_overlap else 0.0
    kind_prior = KIND_PRIOR.get(cand.kind or fund_kind_label(cand.code, cand.official_name), 0.4)
    source_prior = SOURCE_PRIOR.get(cand.source, 0.6)
    strict = 1.0 if (cand.verify or {}).get('is_strict_ok') else 0.0
    conf = 0.55 * t3 + 0.15 * overlap + 0.15 * kind_prior + 0.10 * source_prior + 0.05 * strict
    if cand.t3_proxy:
        conf *= 0.95
    if cand.claim_mismatch:
        conf *= 0.90
    return round(min(1.0, conf), 4)


class SectorFundAgent:
    """单板块决策 agent。llm/verify/search 三个出口可注入，便于离线测试。"""

    def __init__(self, llm_call: Optional[Callable] = None,
                 verify_call: Optional[Callable] = None,
                 search_call: Optional[Callable] = None):
        self._llm_call = llm_call
        self._verify_call = verify_call
        self._search_call = search_call
        self._llm_calls = 0

    # ---------- 外部依赖 ----------
    def llm(self, prompt: str, max_tokens: int = 900) -> Optional[dict]:
        if self._llm_calls >= MAX_LLM_CALLS_PER_SECTOR:
            logger.info('[agent] LLM 调用预算用尽，走降级')
            return None
        self._llm_calls += 1
        try:
            if self._llm_call is None:
                from src.analyzer.llm_analyzer import get_analyzer
                raw = get_analyzer()._call_llm(
                    prompt, task_type='extraction', max_tokens=max_tokens, temperature=0.2)
                return get_analyzer()._parse_json_with_fallback(raw)
            return self._llm_call(prompt)
        except Exception as exc:
            logger.warning('[agent] LLM 调用失败：%s', exc)
            return None

    def verify(self, code: str, claimed_name: str = '') -> dict:
        if self._verify_call is not None:
            return self._verify_call(code, claimed_name)
        from src.fund.fund_api import fund_api
        return fund_api.verify_fund_fetchable(code, claimed_name, probe_stock=True)

    def search(self, keyword: str) -> List[dict]:
        if self._search_call is not None:
            return self._search_call(keyword)
        from src.fund.fund_api import fund_api
        return fund_api.search_fund(keyword) or []

    # ---------- T0 确定性候选 ----------
    def tier0(self, sector: str, db=None) -> List[FundCandidate]:
        """只取精确匹配的候选；已审查行优先，但同样要过 T2/T3（不再无条件采信）。"""
        from src.services.sector_fund_service import get_sector_fund_service
        out: List[FundCandidate] = []
        norm = self.normalize_sector(sector)
        try:
            cached = get_sector_fund_service().get_all_mappings() or {}
            for key in (norm, sector):
                row = cached.get(key)
                if not row:
                    continue
                code = row.get('code')
                if code and all(c.code != code for c in out):
                    out.append(FundCandidate(
                        code=code, name=row.get('name') or '',
                        source='t0_reviewed_db' if row.get('reviewed') else 't0_static'))
        except Exception as exc:
            logger.debug('[agent] T0 读库失败：%s', exc)
        try:
            from src.constants import SECTOR_FUND_MAP
            hit = SECTOR_FUND_MAP.get(norm) or SECTOR_FUND_MAP.get(sector)
            if hit and all(c.code != hit.get('code') for c in out):
                out.append(FundCandidate(code=hit.get('code'), name=hit.get('name', ''),
                                         source='t0_static'))
        except Exception:
            pass
        return out[:MAX_CANDIDATES_PER_SECTOR]

    def normalize_sector(self, sector: str) -> str:
        try:
            from src.constants.sector_fund_map import normalize_sector_name
            return normalize_sector_name((sector or '').strip())
        except Exception:
            return (sector or '').strip()

    # ---------- T1 LLM 提案 ----------
    def tier1_propose(self, sector: str, hint: Optional[str], existing: List[FundCandidate]) -> Dict:
        avoid = '、'.join(sorted({c.code for c in existing})) or '无'
        prompt = f"""板块「{sector}」需要挑一只可验证的公募基金作为跟踪标的。{hint or ''}

要求：
1. 只能选中国大陆公募基金（场内 ETF 优先，其次 LOF，最后场外指数基金），禁止股票、禁止债券型/货币型（除非板块本身就是债市）。
2. 给 3-5 个不同基金公司跟踪同一方向的候选，代码必须是真实存在的 6 位基金代码。
3. 如果这个板块确实没有对口基金，请给出"关联度最大的替代标的"，并把 proxy 标为 true。
4. keywords 用来在天天基金搜索框里找基金，给 2-4 个词；aliases 是该板块的常见叫法。

已作为候选出现过的代码（不要重复）：{avoid}

只返回 JSON：
{{"candidates":[{{"code":"159995","name":"芯片ETF","is_etf":true,"reason":"跟踪国证半导体芯片指数","proxy":false}}],
 "keywords":["半导体","芯片"],"aliases":["半导体芯片"],"best_proxy":false}}"""
        data = self.llm(prompt) or {}
        cands: List[FundCandidate] = []
        for item in (data.get('candidates') or [])[:MAX_CANDIDATES_PER_SECTOR]:
            code = str((item or {}).get('code') or '').strip()
            if not re.fullmatch(r'\d{6}', code):
                continue
            cands.append(FundCandidate(
                code=code, name=(item.get('name') or '').strip(), source='llm',
                reason=(item.get('reason') or '')[:200]))
        return {
            'candidates': cands,
            'keywords': [str(k).strip() for k in (data.get('keywords') or []) if str(k).strip()][:4],
            'aliases': [str(a).strip() for a in (data.get('aliases') or []) if str(a).strip()][:6],
            'raw': {k: v for k, v in data.items() if k in ('best_proxy',)},
        }

    # ---------- T2 抓站验证 ----------
    def tier2_verify(self, sector: str, cands: List[FundCandidate],
                     decision: SectorDecision) -> List[FundCandidate]:
        kept: List[FundCandidate] = []
        for cand in cands:
            res = self.verify(cand.code, cand.name) or {}
            cand.verify = res
            cand.official_name = res.get('official_name') or res.get('api_name') or ''
            cand.fund_type = res.get('fund_type') or ''
            # 注意两个 "kind" 不是一回事：verify 结果里的 kind 是"基金/股票"域判定，
            # cand.kind 是"ETF/LOF/场外"品种优先级。混用会让 ETF 优先规则失效。
            cand.domain_kind = res.get('kind') or 'unknown'
            cand.kind = fund_kind_label(cand.code, cand.official_name or cand.name)
            cand.claim_sim = (similarity(cand.name, cand.official_name)
                              if (cand.name and cand.official_name) else 1.0)
            cand.sector_overlap = similarity(sector, cand.official_name or cand.name)
            if cand.domain_kind == 'stock':
                cand.rejected = '该代码是股票，不是基金'
            elif not res.get('ok') and not res.get('is_strict_ok'):
                cand.rejected = '抓不到数据（可能已清盘/代码不存在）'
            elif cand.official_name and cand.claim_sim < CLAIM_SIM_REJECT:
                # LLM 记错"代码↔名称"极其常见。此时**官方名才是权威**：基金真实存在、
                # 抓得到净值，就不该淘汰掉，只是要标记出来让 T3 用官方名判断，并在
                # 置信度上打折。淘汰它反而会把好候选扔掉（实测 512660 被误丢）。
                cand.claim_mismatch = f'LLM 称「{cand.name}」，官方名实为「{cand.official_name}」'
            if cand.rejected:
                decision.evidence.append({'stage': 'T2', 'code': cand.code, 'verdict': 'reject',
                                          'reason': cand.rejected, 'fund_type': cand.fund_type})
                continue
            decision.evidence.append({'stage': 'T2', 'code': cand.code, 'verdict': 'pass',
                                      'official_name': cand.official_name,
                                      'is_strict_ok': res.get('is_strict_ok'),
                                      'claim_sim': cand.claim_sim})
            kept.append(cand)
        return kept

    # ---------- T3 语义复判 ----------
    def tier3_judge(self, sector: str, cands: List[FundCandidate],
                    decision: SectorDecision) -> List[FundCandidate]:
        if not cands:
            return []
        lines = []
        for i, c in enumerate(cands, start=1):
            lines.append(f'{i}. {c.code} {c.official_name or c.name}'
                         f'（类型={c.fund_type or "未知"}/品种={c.kind}；'
                         f'与板块字面重合={c.sector_overlap}；理由={c.reason}）')
        prompt = f"""板块「{sector}」，下面是候选公募基金（均为真实存在、可抓到净值的基金）：

{chr(10).join(lines)}

逐只判断它能否代表该板块：
- suitable：true/false
- proxy：该板块没有对口基金、这只只是"关联度最大的替代"时为 true
- score：0-100，它作为该板块跟踪标的的贴切程度
字面重合低不代表不贴切（例如 半导体→芯片ETF、债券→证券ETF 都属合理替代），
但如果候选跟踪的是别的行业，就必须给低分。

只返回 JSON：{{"judgements":[{{"code":"159995","suitable":true,"proxy":false,"score":88,"reason":"跟踪同指数"}}]}}"""
        data = self.llm(prompt, max_tokens=1200) or {}
        table = {str(j.get('code')): j for j in (data.get('judgements') or [])}
        if not table:
            # LLM 熔断/超预算/解析失败时，不能把"没判定"当成"判定不合格"，
            # 否则一次网络抖动就会让整批板块被误判为无解。
            decision.degraded = True
            decision.evidence.append({'stage': 'T3', 'verdict': 'unavailable'})
            for cand in cands:
                cand.rejected = '语义判定不可用（LLM 无响应或超预算）'
            return []
        for cand in cands:
            j = table.get(cand.code) or {}
            cand.t3_score = float(j.get('score') or 0)
            cand.t3_suitable = bool(j.get('suitable')) if j else None
            cand.t3_proxy = bool(j.get('proxy')) if j else None
            if j.get('reason'):
                cand.reason = str(j['reason'])[:200]
            decision.evidence.append({'stage': 'T3', 'code': cand.code,
                                      'suitable': cand.t3_suitable, 'proxy': cand.t3_proxy,
                                      'score': cand.t3_score})
            if cand.t3_suitable is not True or cand.t3_score < T3_PASS_SCORE:
                cand.rejected = f'语义判定不通过（score={cand.t3_score:g}）'
        decision.rounds += 1
        return [c for c in cands if not c.rejected]

    # ---------- T4 关键词搜索回环 ----------
    def tier4_search(self, sector: str, keywords: List[str], seen: set,
                     decision: SectorDecision) -> List[FundCandidate]:
        found: List[FundCandidate] = []
        for kw in keywords:
            for item in self.search(kw)[:MAX_CANDIDATES_PER_SECTOR]:
                code = str(item.get('fund_code') or '').strip()
                if not re.fullmatch(r'\d{6}', code) or code in seen:
                    continue
                seen.add(code)
                found.append(FundCandidate(code=code, name=item.get('fund_name') or '',
                                           source='search', fund_type=item.get('fund_type') or ''))
            decision.evidence.append({'stage': 'T4', 'keyword': kw,
                                      'hits': [c.code for c in found][-10:]})
            if len(found) >= MAX_CANDIDATES_PER_SECTOR:
                break
        return found[:MAX_CANDIDATES_PER_SECTOR]

    # ---------- T5 有意代理（老板规则：没有对口基金时取关联度最大的替代）----------
    def tier5_proxy(self, sector: str, pool: List[FundCandidate],
                    decision: SectorDecision) -> Optional[FundCandidate]:
        ranked = sorted(pool, key=lambda c: (-c.sector_overlap, -c.t3_score))[:5]
        if not ranked:
            return None
        lines = [f'{i}. {c.code} {c.official_name or c.name}' for i, c in enumerate(ranked, 1)]
        prompt = f"""板块「{sector}」在中国大陆公募基金里**没有对口产品**（已搜过关键词：{ '、'.join(decision.keywords or [sector]) }）。

下面都是真实存在、可抓到净值的基金，请从中挑一只"关联度最大的替代标的"：
{chr(10).join(lines)}

替代逻辑要能讲清楚（例如：SpaceX→军工/卫星产业 ETF，债券→国债 ETF，存储→芯片 ETF）。
实在没有合理替代就返回 none。

只返回 JSON：{{"code":"512660","score":0-100,"reason":"<20字替代逻辑"}}"""
        data = self.llm(prompt, max_tokens=400) or {}
        code = str(data.get('code') or '').strip()
        if not code or code.lower() == 'none':
            return None
        for cand in ranked:
            if cand.code == code:
                cand.t3_suitable = True
                cand.t3_proxy = True
                cand.t3_score = float(data.get('score') or 60)
                cand.reason = str(data.get('reason') or '')[:200]
                cand.rejected = ''
                decision.evidence.append({'stage': 'T5', 'code': cand.code,
                                          'proxy': True, 'score': cand.t3_score,
                                          'reason': cand.reason})
                return cand
        return None

    # ---------- 主流程 ----------
    def resolve(self, sector: str, hint: Optional[str] = None, max_rounds: int = 3,
                allow_llm: bool = True, budget_ms: int = 30000, db=None) -> SectorDecision:
        """`budget_ms` 默认 30s：一次真实跑批实测 T1+T2(4只)+T3 就要 10-15s，
        12s 会让 SpaceX/存储这类需要走关键词/代理回环的板块在预算内被截断。
        帖子分析热路径请显式传小值（如 6000）并配合 allow_llm=False。"""
        sector = (sector or '').strip()
        decision = SectorDecision(sector=sector)
        if not sector:
            decision.status = 'no_fund'
            return decision
        started = time.monotonic()
        self._llm_calls = 0
        seen = set()
        pool: List[FundCandidate] = []

        def elapsed():
            return int((time.monotonic() - started) * 1000)

        def over_budget():
            return elapsed() >= budget_ms

        for cand in self.tier0(sector, db=db):
            if cand.code not in seen:
                seen.add(cand.code)
                pool.append(cand)
        decision.evidence.append({'stage': 'T0', 'candidates': [c.code for c in pool]})

        if allow_llm and not over_budget():
            proposal = self.tier1_propose(sector, hint, pool)
            decision.keywords = proposal['keywords']
            decision.aliases = proposal['aliases']
            for cand in proposal['candidates']:
                if cand.code not in seen:
                    seen.add(cand.code)
                    pool.append(cand)
            decision.evidence.append({'stage': 'T1', 'candidates': [c.code for c in pool],
                                      'keywords': decision.keywords})

        passed = self.tier2_verify(sector, pool, decision)
        winners = self.tier3_judge(sector, passed, decision)

        rounds = 1
        while not winners and allow_llm and rounds < max_rounds and not over_budget():
            keywords = list(dict.fromkeys((decision.keywords or []) + [sector]))[:5]
            more = self.tier4_search(sector, keywords, seen, decision)
            if not more:
                break
            pool.extend(more)
            verified = self.tier2_verify(sector, more, decision)
            winners = self.tier3_judge(sector, verified, decision)
            rounds += 1
            decision.direct_exhausted = True

        if not winners and allow_llm and not over_budget():
            # direct 路径全灭才允许考虑代理；代理必须先证明"确无对口基金"
            decision.direct_exhausted = True
            proxy = self.tier5_proxy(sector, pool, decision)
            if proxy:
                winners = [proxy]

        decision.rounds = max(decision.rounds, rounds)
        if winners:
            winners.sort(key=lambda c: (
                0 if not c.t3_proxy else 1,
                0 if is_etf(c.code, c.official_name or c.name) else 1,
                -c.t3_score, -c.sector_overlap, -c.claim_sim))
            best = winners[0]
            best.confidence = compute_confidence(best, decision.direct_exhausted)
            decision.chosen = best
            decision.alternatives = winners[1:5]
            decision.confidence = best.confidence
            decision.status = 'proxy' if best.t3_proxy else 'matched'
        else:
            decision.alternatives = sorted(pool, key=lambda c: -c.t3_score)[:5]
            decision.status = 'no_fund' if not pool else 'needs_review'

        decision.elapsed_ms = elapsed()
        decision.timed_out = over_budget()
        decision.degraded = bool(allow_llm and self._llm_calls == 0)
        return decision


def apply_decision(db, decision: SectorDecision, mapping_id: Optional[int] = None,
                   allow_review_toggle: bool = True) -> Dict:
    """把 agent 结论落到 sector_fund_mapping，带完整证据。

    四条硬规则：
    1. `owner_locked` 的行不覆盖（老板手工挑定的有意代理不能被"修"掉）；
    2. `reviewed` 只有在证据齐（来源/时间/理由/严格可抓 + 置信度达标）时才置真；
    3. 写库后刷新两处进程内缓存，否则"自己刚写的映射自己读不到"；
    4. 低置信只更新证据，不改基金。
    """
    from src.models.database import SectorFundMapping
    from src.services.sector_fund_service import get_sector_fund_service

    cand = decision.chosen
    if cand is None:
        return {'applied': False, 'reason': '无可用结论', 'status': decision.status}

    q = db.query(SectorFundMapping)
    row = q.filter(SectorFundMapping.id == mapping_id).first() if mapping_id else \
        q.filter(SectorFundMapping.sector_name == decision.sector,
                 SectorFundMapping.is_active == True).order_by(  # noqa: E712
            SectorFundMapping.reviewed.desc(), SectorFundMapping.id.asc()).first()

    if row is not None and row.owner_locked:
        return {'applied': False, 'reason': '该映射已被老板锁定（有意代理），agent 不覆盖',
                'mapping_id': row.id, 'status': 'locked'}

    verify = cand.verify or {}
    now = datetime.now()
    evidence_json = json.dumps(decision.evidence, ensure_ascii=False)[:60000]
    message = ('直接对应' if not cand.t3_proxy else '无对口基金，取关联度最大的替代') \
        + (f'：{cand.reason}' if cand.reason else '')
    if cand.claim_mismatch:
        message += f'（{cand.claim_mismatch}）'

    should_review = allow_review_toggle and decision.auto_reviewable
    if row is None:
        from src.models.database import FundInfo
        if not db.query(FundInfo).filter_by(fund_code=cand.code).first():
            # 用传进来的 session 补档案，避免另开一个 session：单测里别的用例可能已把
            # 模块级 engine 换成临时库，新开 session 会 "no such table"。
            db.add(FundInfo(fund_code=cand.code, fund_name=cand.display_name,
                           sector_type=decision.sector))
        row = SectorFundMapping(sector_name=decision.sector,
                                fund_code=cand.code,
                                fund_name=cand.display_name)
        db.add(row)
    changed_fund = row.fund_code != cand.code
    row.fund_code = cand.code
    row.fund_name = cand.display_name
    row.is_active = True
    row.match_source = 'agent'
    row.match_kind = 'proxy' if cand.t3_proxy else 'direct'
    row.confidence = cand.confidence
    row.verified_at = now
    row.verify_message = message[:500]
    row.llm_reason = (cand.reason or '')[:500]
    row.is_fetchable = bool(verify.get('is_strict_ok'))
    row.evidence = evidence_json
    if should_review:
        row.reviewed = True
        row.reviewed_by = 'agent'
    db.commit()

    try:
        get_sector_fund_service().refresh_cache()
        from src.constants.sector_fund_map import refresh_db_aliases_cache
        refresh_db_aliases_cache()
    except Exception as exc:
        # 缓存刷新失败不能让整个操作回滚（数据已提交）；记日志，下次读库自然纠正
        logger.warning('[agent] 缓存刷新失败（不影响已写入的数据）：%s', exc)
    return {'applied': True, 'mapping_id': row.id, 'changed_fund': changed_fund,
            'reviewed': bool(row.reviewed), 'status': decision.status,
            'confidence': cand.confidence}


_AGENT: Optional[SectorFundAgent] = None


def get_sector_fund_agent() -> SectorFundAgent:
    global _AGENT
    if _AGENT is None:
        _AGENT = SectorFundAgent()
    return _AGENT


def resolve_sector_fund(sector: str, hint: Optional[str] = None, **kwargs) -> SectorDecision:
    """全项目唯一入口：给板块名，拿回带证据的基金决策。"""
    return get_sector_fund_agent().resolve(sector, hint=hint, **kwargs)
