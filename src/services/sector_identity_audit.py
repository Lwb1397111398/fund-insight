"""板块映射的身份体检：证明"这个代码就是名字里那只基金"，而不是同码的别的品种。

为什么需要这个模块（实测，2026-09-21）：6 位代码在 A 股与场外基金之间**码段重叠**，
所以"能不能抓到净值"完全不能用来证明"抓到的是我们要的那只基金"：

    000725 映射表存"京东方Ａ"（深市股票），基金域同码是"大成添利宝货币B"（货基，能抓到净值）
    000938 映射表存"紫光股份"（深市股票），基金域同码是"华商稳固添利债券C"
    000530 映射表存"冰山冷热"（深市股票），基金域同码是"招商丰盛稳定增长混合A"

旧的 `verify_fund_fetchable` 只看基金域有没有数据（`if ok: return 'fund'`），于是上面这些
行全部"验证通过"，预测拿货基净值去验证"京东方看涨"，得到的准确率毫无意义——这正是
老板说的"识别出来的基金离板块应该对应的基金差十万八千里"的最底层机制。

本模块只用**基金域自证**，不碰股票域接口（push2 在本机 ProxyError，永远拿不到结果），
也不依赖 LLM（账号欠费）。判定顺序即安全性，见 `arbitrate_mapping` 的 docstring。
"""

import logging
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy import or_

from src.fund.fund_api import (
    fund_api,
    jaccard_name,
    normalize_for_identity,
)

logger = logging.getLogger(__name__)

# 同一只产品的正名与别名语序不同（华宝中证医疗ETF ↔ 医疗ETF华宝），字符集 Jaccard
# 实测 0.58~0.78；而"股票名撞上同码货基"实测 0.0。0.35 之下就是完全不同的两组字符。
IDENTITY_OK_JACCARD = 0.35
# "存名确实是某只基金，只是代码填错了"——命中名必须与存名足够像，否则
# 拓维信息→招商安拓债券A、中药→建信中证红利潜力指数A 这类垃圾命中会驱动误降。
OWNERSHIP_FLOOR_JACCARD = 0.60

# 判据结论。`UNSERVABLE_VERDICTS` 里的结论会让该映射从所有读路径消失。
VERDICT_OK = 'ok'
VERDICT_NOT_A_FUND = 'not_a_fund'                  # 名字根本不是基金（股票/杂词）
VERDICT_CODE_IS_OTHER_FUND = 'code_is_other_fund'  # 码是只基金，但和板块毫无关系
VERDICT_WRONG_CODE = 'wrong_code'                  # 名字是只真基金，代码填错了
VERDICT_NOT_FETCHABLE = 'not_fetchable'            # 站点明确回答基金域没有这个码
# 下面两类**永不降级**：结论是"没查到"，不是"查到了且不对"。
VERDICT_UNKNOWN = 'unknown'
VERDICT_PROBE_UNAVAILABLE = 'probe_unavailable'

UNSERVABLE_VERDICTS = frozenset({
    VERDICT_NOT_A_FUND, VERDICT_CODE_IS_OTHER_FUND,
    VERDICT_WRONG_CODE, VERDICT_NOT_FETCHABLE,
})

# 板块核心词里没有中文（5G / CPO / SpaceX / AI）时，"与基金名零汉字交集"这件事
# 毫无信息量——任何 ETF 名字都不会含"5G"两个汉字。这类板块只能判 unknown，不许硬拒。
_CJK = re.compile(r'[\u4e00-\u9fff]')

# 泛市场类板块本来就该配宽基，用"名字里不含板块词"判它不相关是错的
GENERIC_MARKET_CORES = frozenset({
    '市场', '大盘', '资源', '宽基', '指数', '权重', '龙头', '板块', '主线',
})

# ---------- 板块核心词（相关性判据与候选 ETF 匹配共用） ----------

# 场内码段的**唯一定义**：深市 15xxxx，沪市 5xxxxx（50/51/52/53/54/55/56/57/58/59 全收）。
# 原来 agent 与体检脚本各写一套，脚本写的 `code[:2] in ('15','5')` 让 986 只沪市 ETF
# 全部进不了候选池（'512170'[:2]=='51'），现在 sector_fund_agent 从这里转发。
EXCHANGE_LISTED_CODE = re.compile(r'^(?:15\d{4}|5\d{5})$')

# 泛指尾缀。必须剥在"含数字/拉丁字母→取整串"**之前**，否则
# `SpaceX概念` 会整串成为核心词（正确是 `SpaceX`）、`全A指数` 同理；
# 「股」也要剥，否则 韩股/台股/A股 会被当成有效主题词去配基金。
_SECTOR_TAIL = re.compile(r'(?:板块行情|主题|概念|指数|方向|行情|板块|股)+$')

# 剥完还是泛指词 → 没有"对口标的"这回事，不许 realign
GENERIC_SECTOR_STOP = frozenset(GENERIC_MARKET_CORES) | {
    '全球', '精选', '应用', '综合', '设备', '中报', '业绩', '老登',
}

# 基金公司名（前缀或后缀挂在 ETF 名上）。指数段里混进公司名会让"核心词命中"变成假命中
# （板块「华夏」会命中"华夏沪深300ETF"的每一只），所以算指数段时先剔掉。
FUND_COMPANY_TOKENS = frozenset({
    '华夏', '易方达', '南方', '广发', '富国', '嘉实', '华安', '国泰', '博时', '招商',
    '天弘', '鹏华', '汇添富', '华泰柏瑞', '平安', '大成', '中欧', '景顺长城', '工银',
    '建信', '交银', '兴业', '银华', '泰康', '东财', '东吴', '中银', '永赢', '万家',
    '海富通', '华宝', '华商', '金鹰', '浦银安盛', '国寿', '国联', '长江', '摩根',
})


def is_exchange_listed(code: str) -> bool:
    """是否场内（交易所）上市基金码段。"""
    return bool(EXCHANGE_LISTED_CODE.match((code or '').strip()))


# 拉丁缩写板块名与其**中文等价说法**：不展开的话 `5G`↔「通信ETF华夏」、
# `AI`↔「人工智能ETF易方达」会被判成"字面无关"，于是把本来就是正确答案的行
# 换成一只更窄的 ETF（实测 id 121/122 就是这种假阳性）。
SECTOR_SYNONYMS: Dict[str, Tuple[str, ...]] = {
    '5G': ('通信',),
    'AI': ('人工智能',),
    'CPO': ('光模块', '光通信'),
    'EDA': ('电子设计',),
    '芯片': ('半导体',),
}


# 双向边：只写 `5G→通信` 的单向表会得出"5G 板块配通信ETF=相关、
# 通信板块配 5GETF=不相关"，后者于是被 realign 换掉一只**本来就对**的标的（实测）。
_SYNONYM_EDGES = {}
for _abbr, _zh in SECTOR_SYNONYMS.items():
    _SYNONYM_EDGES.setdefault(_abbr.upper(), set()).update(_zh)
    for _z in _zh:
        _SYNONYM_EDGES.setdefault(_z, set()).add(_abbr.upper())


def core_variants(core: str) -> Tuple[str, ...]:
    """核心词及其**双向**等价写法，只用于相关性旗标（不用于候选匹配）。

    候选必须"官方名字面含核心词"（v7.1 §一.4 的字面保证）：同义词是语义等价，
    拿它选标的就不再是"确定性"操作。
    """
    if not core:
        return ()
    out = [core] + sorted(_SYNONYM_EDGES.get(core.upper(), ()))
    return tuple(dict.fromkeys(out))


def normalize_sector_text(sector: str) -> str:
    """板块名标准化（别名→标准名）。名册/离线测试拿不到别名表时退回原串。"""
    try:
        from src.constants.sector_fund_map import normalize_sector_name
        return (normalize_sector_name((sector or '').strip()) or '').strip()
    except Exception:
        return (sector or '').strip()


def sector_core(sector: str) -> str:
    """板块的**主题核心词**，用于相关性旗标与候选 ETF 匹配。

    规则（顺序即语义，S4a-v7.3 定稿）：
    1. 标准化板块名（别名→标准名）；
    2. 剥泛指尾缀（板块/概念/主题/指数/方向/行情/股）；
    3. 剥完**整串**就是主题词：`5G`/`全A`/`SpaceX`/`中证500` 一律不做汉字抽取
       （`cjk_core('全A指数')` 会得到"全指数"，是错的）——所以代码里没有第三条分支，
       "不抽取"就是默认行为；
    4. 结果在泛指 stop-list 里、或**汉字部分**正好是这些泛指词（`Ai应用`→'应用'）、
       或长度 < 2 → 返回 ''（无核心词，不许 realign）。
    """
    norm = normalize_sector_text(sector)
    if not norm:
        return ''
    core = _SECTOR_TAIL.sub('', norm).strip()
    generic = core in GENERIC_SECTOR_STOP or cjk_core(core) in GENERIC_SECTOR_STOP
    return '' if (generic or len(core) < 2) else core


def etf_index_segment(name: str) -> str:
    """ETF 官方名里的"指数段"：`ETF` 之前的部分，剔掉基金公司名。

    储能电池ETF广发 → 储能电池；华泰柏瑞沪深300ETF → 沪深300。
    只取 ETF 之前而不看之后，是因为结尾那段几乎都是公司名。
    公司名要**按长度降序**剔：`华泰` 排在 `华泰柏瑞` 前面会先把长名剔成 `柏瑞`。
    """
    upper = (name or '').upper()
    idx = upper.find('ETF')
    seg = (name or '')[:idx] if idx >= 0 else (name or '')
    for token in sorted(FUND_COMPANY_TOKENS, key=len, reverse=True):
        seg = seg.replace(token, '')
    return seg.strip()


def servable_predicate():
    """SQL 谓词：NULL 必须视为可服务。

    119 条 reviewed 映射里 105 条 `is_fetchable IS NULL`（历史人工审查行，从没被
    体检写过）。写成 `is_fetchable != False` 在 SQL 里会把 NULL 判成不匹配，
    一次改动能让历史映射集体从改标源里消失，而且看起来像"体检很成功"。
    """
    from src.models.database import SectorFundMapping
    return or_(
        # 老板锁定的行按"老板说了算"放行，与 `row_unservable()` 的 owner 例外同一条规则。
        # 少这一句就会两侧打架：体检结论否定 + is_fetchable 被 owner 守卫跳过 =
        # SQL 在服务、Python 在藏（实测 债券/512000）。
        SectorFundMapping.owner_locked == True,          # noqa: E712
        SectorFundMapping.reviewed_by == 'owner',
        SectorFundMapping.is_fetchable.is_(None),
        SectorFundMapping.is_fetchable == True,   # noqa: E712
    )


def identity_verdict_of(row) -> Optional[str]:
    """从 evidence JSON 里取体检结论（None = 从未体检或解析不了）。"""
    import json
    raw = getattr(row, 'evidence', None)
    if not raw:
        return None
    try:
        return ((json.loads(raw) or {}).get('identity') or {}).get('verdict')
    except Exception:
        return None


def row_unservable(row) -> bool:
    """唯一的行级真值判据：**两处事实源都要看**。

    - `is_fetchable` 列：能用 SQL 过滤，但只有体检会写；
    - `evidence.identity.verdict`：批量读路径过滤不了，只能在这里补一刀。

    任何读者自己写判据迟早与别处不一致；SQL 侧的同一判据是 `servable_predicate()`。
    """
    if row is None:
        return False
    if getattr(row, 'owner_locked', None) or \
            getattr(row, 'reviewed_by', None) == 'owner':
        # 老板锁定的行是**显式例外**，不是"体检没做"：他手定的有意代理（债券→512100 类）
        # 永远按老板的结论服务。体检结论照样写进 evidence 当前端提示，但不当门。
        # 两边（列 + verdict）都跳过，才不会和 `servable_predicate()` 判出两个答案。
        return False
    if getattr(row, 'is_fetchable', None) is False:
        return True
    return identity_verdict_of(row) in UNSERVABLE_VERDICTS


MACHINE_SWAP_KEYS = ('identity_realign', 'etf_upgrade')


def machine_swap_of(row) -> Optional[Dict]:
    """机器换过标的、且老板**还没确认**的溯源记录（None = 没换过或已确认）。

    判据只能取自 `evidence`，不能取 `match_source`：后者是普通可写字段，之后任何一次
    agent/人工写入都会把它盖掉。实测有 2 行带着 `etf_upgrade` 记录，却已经是
    `match_source='agent' + reviewed=True`，于是前端"机器已纠正"分桶归 0、
    agent 也以为自己可以自由盖审查章。
    换到的代码仍等于当前代码才算"未确认"——老板一旦改成别的标的就是已经表态。
    """
    import json
    try:
        payload = json.loads(getattr(row, 'evidence', None) or '{}')
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if getattr(row, 'reviewed_by', None) == 'owner' or getattr(row, 'owner_locked', None):
        return None
    code = getattr(row, 'fund_code', None)
    for key in MACHINE_SWAP_KEYS:
        rec = payload.get(key)
        if isinstance(rec, dict) and rec.get('code') == code and code:
            return dict(rec, kind=key)
    return None


def cjk_core(text) -> str:
    return ''.join(_CJK.findall(text or ''))


def exact_name_hits(keyword: str) -> Optional[List[Tuple[str, str, bool]]]:
    """返回 [(code, name, is_fund)]，只保留 NFKC 后与查询名**完全同名**的命中。

    查询必须用**原始名**：'京东方Ａ' 原样查得到 1 条（深市），NFKC 归一后反而 0 条。
    返回 None 表示接口失败（不能据此判"不是基金"），[] 表示查无同名。
    """
    results = fund_api.search_fund(keyword)
    if results is None:
        return None
    want = normalize_for_identity(keyword)
    hits = []
    for item in results:
        name = item.get('fund_name') or ''
        if normalize_for_identity(name) == want:
            hits.append((str(item.get('fund_code') or ''), name, bool(item.get('is_fund'))))
    return hits


def name_is_known_fund(name: str) -> Optional[bool]:
    """名册名字倒排的**第二证据**（离线，零请求）。

    None = 名册没加载成功，不能下结论；False = 名册里没有这个名字（基金域不认）；
    True = 名册里有同名基金。

    `not_a_fund` 需要"搜索说是股票" + "名册里没有这个名字"两条同时成立：搜索接口
    实测同一次查询会 10 条↔1 条地抖，只靠一条就降级等于把数据交给网络抖动。
    """
    try:
        roster = fund_api.load_fund_roster()
    except Exception as exc:
        logger.warning('[identity] 名册不可用，无法二次确认：%s', exc)
        return None
    index = roster.get('codes_by_name') or {}
    if name in index or normalize_for_identity(name) in index:
        return True
    return False


def arbitrate_mapping(code: str, stored_name: str, sector: str = '',
                      _domain: Optional[Callable] = None,
                      _hits: Optional[Callable] = None,
                      _name_in_roster: Optional[Callable] = None) -> Dict:
    """判定"映射表这行的代码与名字是否指同一只基金"，以及标的与板块是否毫无关系。

    顺序就是安全性，任何一步调换都会放过股票或误杀正确行：

    1. 站点请求失败 → `probe_unavailable`（**永不降级**）。
    2. 搜索接口里存在与存名**完全同名**的命中，且其域是股票市场，**并且**名册里没有
       这个名字 → `not_a_fund`。必须在"自命中即 ok"之前判：实测 10 只股票行的搜索
       命中码**恰好等于本行码**（德明利→001309），先认自命中就等于全部放过。
    3. 基金域有该码且 jaccard(存名, 官方名) >= 0.35 → `ok`。
    4. 基金域没有该码（站点 200 + "页面未"签名，确定负证据）：存名能对上某只真基金
       → `wrong_code`（给建议码）；对不上 → `not_fetchable`。
    5. 基金域有该码但名字对不上，且**板块核心词含中文**、与该基金官方名的汉字**零交集**
       → `code_is_other_fund`（核聚变→机械ETF、中药→纯债C 这种"八竿子打不着"）。
    6. 存名与某只真基金同名（jaccard >= 0.60）但代码不同 → `wrong_code`。
    7. 其余 → `unknown`。

    Args:
        _domain/_hits/_name_in_roster: 注入点，离线测试用（不注则打真实接口）。
    """
    code = (code or '').strip()
    stored = (stored_name or '').strip()
    domain = (_domain or fund_api.get_fund_domain_name)(code)
    status = domain.get('status')
    official = domain.get('name')
    j_official = jaccard_name(stored, official) if (stored and official) else None

    result = {
        'verdict': VERDICT_UNKNOWN, 'code': code, 'stored_name': stored,
        'official_name': official, 'jaccard': j_official, 'status': status,
        'suggested_code': None, 'suggested_name': None,
        'evidence': {'domain_source': domain.get('source')},
    }
    if not code or not stored:
        result['verdict'] = VERDICT_UNKNOWN
        result['reason'] = '缺少代码或名称，无法做身份判定'
        return result

    # ---- 1. 站点没答上来：不下结论 ----
    if status == 'error':
        result['verdict'] = VERDICT_PROBE_UNAVAILABLE
        result['reason'] = '基金域接口本次不可用，未做任何判定'
        return result

    # ---- 2. 同名命中落在股票市场域（须名册二次确认）----
    hits = (_hits or exact_name_hits)(stored)
    result['evidence']['exact_hits'] = hits if hits is not None else 'probe_failed'
    if hits is None:
        result['verdict'] = VERDICT_PROBE_UNAVAILABLE
        result['reason'] = '同名检索接口本次失败，未做任何判定'
        return result

    stock_hit = next((h for h in hits if not h[2]), None)
    fund_hit_same_code = next((h for h in hits if h[2] and h[0] == code), None)
    if stock_hit and not fund_hit_same_code:
        in_roster = (_name_in_roster or name_is_known_fund)(stored)
        result['evidence']['roster_has_name'] = in_roster
        if in_roster is False:
            result['verdict'] = VERDICT_NOT_A_FUND
            result['reason'] = ('「%s」在基金域查不到同名产品，检索到的是股票市场品种'
                                % stored)
            result['evidence']['stock_category'] = True
            return result
        # 名册里居然有这个名字：不能仅凭搜索就说是股票（搜索接口会抖）
        result['evidence']['stock_hit_unconfirmed'] = stock_hit[0]

    # ---- 3. 代码与名字互相印证 ----
    if status == 'ok' and j_official is not None and j_official >= IDENTITY_OK_JACCARD:
        result['verdict'] = VERDICT_OK
        result['reason'] = '代码在基金域的品种名与映射名一致（Jaccard %.3f）' % j_official
        return result
    if fund_hit_same_code:
        result['verdict'] = VERDICT_OK
        result['reason'] = '基金域检索到与本行代码完全同名的产品'
        return result

    # ---- 4. 基金域没有这个码 ----
    if status == 'absent':
        twin = _find_fund_twin(stored, code, hits)
        if twin:
            result['suggested_code'], result['suggested_name'] = twin
            result['verdict'] = VERDICT_WRONG_CODE
            result['reason'] = ('基金域不存在代码 %s，但「%s」确实是基金 %s'
                                % (code, stored, twin[0]))
        else:
            result['verdict'] = VERDICT_NOT_FETCHABLE
            result['reason'] = '基金域查不到该代码，也查不到同名基金'
        return result

    # ---- 5. 码是只基金，但与板块毫无关系 ----
    # 这里要的是**汉字交集**判据（板块含汉字却与基金官方名零交集），
    # 与相关性/候选用的 `sector_core()` 不是同一个量，变量名必须区分开
    sector_cjk = cjk_core(sector)
    if (sector_cjk and sector_cjk not in GENERIC_MARKET_CORES
            and official and not (set(sector_cjk) & set(cjk_core(official)))):
        result['verdict'] = VERDICT_CODE_IS_OTHER_FUND
        result['reason'] = ('代码 %s 实为「%s」，与板块「%s」无任何汉字关联'
                            % (code, official, sector))
        twin = _find_fund_twin(stored, code, hits)
        if twin:
            result['suggested_code'], result['suggested_name'] = twin
        return result

    # ---- 6. 名字是只真基金，代码填错 ----
    twin = _find_fund_twin(stored, code, hits)
    if twin:
        result['suggested_code'], result['suggested_name'] = twin
        result['verdict'] = VERDICT_WRONG_CODE
        result['reason'] = ('「%s」是基金 %s，本行却记成代码 %s（该码实为「%s」）'
                            % (stored, twin[0], code, official or '?'))
        return result

    result['reason'] = ('证据不足以判定：与代码的基金域名「%s」相似度 %.3f，'
                        '低于阈值但没有反向证据'
                        % (official or '?', j_official or 0.0))
    return result


def _find_fund_twin(stored: str, code: str, hits: List[Tuple[str, str, bool]]
                    ) -> Optional[Tuple[str, str]]:
    """找一个"确实是基金、名字与存名足够像、但不是本行代码"的命中。"""
    for hit_code, name, is_fund in (hits or []):
        if not is_fund or hit_code == code:
            continue
        if jaccard_name(stored, name) >= OWNERSHIP_FLOOR_JACCARD:
            return hit_code, name
    # 搜索接口只回 top-10，且对拼写敏感；再用离线名册名字倒排兜一次
    try:
        roster = fund_api.load_fund_roster()
        for name in (stored, normalize_for_identity(stored)):
            for hit_code in (roster.get('codes_by_name') or {}).get(name, []):
                if hit_code != code:
                    entry = roster['by_code'].get(hit_code) or {}
                    return hit_code, entry.get('name') or name
    except Exception:
        return None
    return None


def sector_relevance(sector: str, official_name: str,
                     has_alternative: Optional[Callable[[str], bool]] = None) -> bool:
    """板块与标的是否"字面相关"（只用于报告，不用于降级）。

    判据必须带第二个条件："名册里存在名字含该板块核心词的基金"，否则会把
    市场→上证50、大盘→沪深300、资源股→有色 这类**故意的宽基代理**全判成不相关
    （实测单判据命中 102/145 行，毫无用处）。

    核心词用 `sector_core` 而不是 `cjk_core`：后者把 `全A指数` 抽成"全指数"，
    于是把合理的宽基代理误报成存疑（v7.1 实测 9 条里有 3 条假阳性）。
    """
    core = sector_core(sector)
    if not core:
        return True
    variants = core_variants(core)
    # 只要共享一个汉字就当作相关：宁可漏报（少报几条给老板看），
    # 也不要误报——误报会让老板以为一堆正确映射有问题。
    if any(contains_core(official_name or '', v) for v in variants):
        return True
    if set(cjk_core(core)) & set(cjk_core(official_name)):
        return True
    checker = has_alternative or _roster_has_fund_containing
    return not any(checker(v) for v in variants)


def contains_core(name: str, core: str) -> bool:
    """板块核心词是否出现在基金官方名（或指数段）里。

    刻意用朴素子串，不做整词边界：中文没有词边界，"建信创新驱动"确实会子串命中"信创"，
    但要求边界会把"富国中证核电ETF"里的"核电"也判成没命中——那是更坏的假阴性。
    相关性旗标只用于报告，允许的方向是**少报**，不是误报。
    大小写统一后才比：`Ai应用`/`AI 应用`、`5g`/`5G` 必须等价。
    """
    if not core:
        return False
    return core.upper() in (name or '').upper()


def _roster_has_fund_containing(core: str) -> bool:
    """名册里是否存在"官方名成词含该板块核心词"的基金。"""
    if len(core) < 2:
        return False
    try:
        roster = fund_api.load_fund_roster()
    except Exception:
        return True   # 判不了就说"相关"，宁可漏报不误报
    for entry in (roster.get('by_code') or {}).values():
        if contains_core(entry.get('name') or '', core):
            return True
    return False


def sector_core_names(db=None) -> List[str]:
    """返回全量**板块名**（映射表在册 ∪ 静态表键）；核心词由调用方再算。"""
    names: List[str] = []
    if db is not None:
        try:
            from src.models.database import SectorFundMapping
            names = [r[0] for r in db.query(SectorFundMapping.sector_name).distinct().all()
                     if r[0]]
        except Exception as exc:
            logger.warning('[identity] 板块名取不到，只用静态表：%s', exc)
    try:
        from src.constants.sector_fund_map import SECTOR_FUND_MAP
        names += list(SECTOR_FUND_MAP.keys())
    except Exception:
        pass
    return list(dict.fromkeys(names))


def etf_candidates(sector: str, db=None, roster: Optional[Dict] = None,
                   all_cores: Optional[List[str]] = None,
                   limit: int = 20) -> List[Dict]:
    """板块核心词在基金域名册里能对上哪些**场内 ETF**（零请求、纯离线）。

    四条硬条件（v7.2/v7.3 实测每条都对应一个真实踩坑）：
    - 码段 `is_exchange_listed`：`code[:2] in ('15','5')` 会丢掉全部沪市 ETF；
    - 官方名含 ETF **且不含「联接」**：162412「华宝医疗ETF联接A」名字带 ETF 却是场外联接；
    - 命中位置在**指数段**（`储能电池ETF广发` → `储能电池`），公司名先剔掉；
    - **最长认领**：一只候选只归"能命中它的最长核心词"。旧写法"候选名含别的板块名就作废"
      实测方向完全错（既把 159687 从"亚太"里排掉，也把 4 只从"中概"里排掉）。
      等长的多个核心词可以同时认领同一只（512070「证券保险ETF」本就该同时服务 证券 与 保险，
      撞车由调用方的"本轮已占用"消解，不在这里作废）。
    """
    core = sector_core(sector)
    if not core:
        return []
    try:
        roster = roster or fund_api.load_fund_roster()
    except Exception as exc:
        logger.warning('[identity] 名册不可用，无法给「%s」找场内 ETF 候选：%s', sector, exc)
        return []
    by_code = roster.get('by_code') or {}
    segments = {}
    for code, entry in by_code.items():
        name = entry.get('name') or ''
        if not is_exchange_listed(code) or 'ETF' not in name.upper() or '联接' in name:
            continue
        segments[code] = (name, etf_index_segment(name))
    if all_cores is None:
        all_cores = sector_core_names(db)
    # 必须把**本板块自己的核心词**并进认领宇宙：调用方传的 all_cores 可能只来自
    # 静态表（122 个键），109/145 个在册板块不在里面，漏掉自己就等于全部候选
    # 被判"归更长的核心词认领"而返回空（`--upgrade-etf` 实测静默变 no-op）。
    cores = sorted(({core} if core else set()) |
                   {c for c in (sector_core(s) for s in (all_cores or [])) if len(c) > 1},
                   key=lambda c: (-len(c), c))
    # 先用本板块核心词粗筛候选（1~2 个子串扫描 × 3000 只），再只对粗筛命中的
    # 指数段算全量认领表。反过来做会让"最长认领"这一判据在每次调用里
    # 重跑 3000 段 × 129 核心词 × 36 公司名，整轮体检拖到十几分钟。
    # 字面匹配，不用 `core_variants`：同义词是语义等价，用它选标的就不"确定性"了
    hit = [(code, name, seg) for code, (name, seg) in segments.items()
           if contains_core(seg, core)]
    variants_by_core = {c: (c,) for c in cores}      # 字面，不用同义词
    claim_cache = {}
    for seg in {item[2] for item in hit}:
        hits = []
        for candidate_core, candidate_variants in variants_by_core.items():
            matched = max((len(v) for v in candidate_variants
                           if contains_core(seg, v)), default=0)
            if matched:
                hits.append((candidate_core, matched))
        hits.sort(key=lambda t: (-t[1], t[0]))
        claim_cache[seg] = hits

    out = []
    # 指数段短的优先：`养殖ETF`（段=养殖）比 `农业养殖ETF` 更纯粹，截断时先留下它
    for code, name, seg in sorted(hit, key=lambda t: (len(t[2]), t[0])):
        claims = claim_cache[seg]
        mine = max((length for c, length in claims if c == core), default=0)
        # 只允许"本板块核心词是最长认领者之一"：更长的那只说明标的主题更窄
        # （`证券保险` 认领 512070 时不该让 `保险` 抢走，但 `保险` 与 `证券` 等长时
        #  两只都算命中——行业复合指数本来就同时覆盖两个板块）
        if not mine or (claims and claims[0][1] > mine):
            continue
        out.append({'code': code, 'name': name, 'segment': seg, 'core': core})
        if len(out) >= limit:
            break
    return out


def identity_view(row) -> Dict:
    """把体检结论整理成给前端/接口用的视图（老板必须看得见为什么某行被降级）。

    结论存在 `evidence` JSON 里（本轮零新列、零迁移），所以这里只做解析。
    """
    import json

    view = {
        'identity_verdict': None,
        'identity_reason': getattr(row, 'verify_message', None) or None,
        'identity_official_name': None,
        'identity_suggestion': None,
        'identity_suggestions': [],
        'is_fetchable': getattr(row, 'is_fetchable', None),
        # 前端筛选必须与读路径同一判据：只看列会漏掉"仅 verdict 被否"的行
        'servable': not row_unservable(row),
        'realigned': None,
    }
    raw = getattr(row, 'evidence', None)
    if not raw:
        return view
    try:
        payload = json.loads(raw) or {}
    except Exception:
        return view
    if not isinstance(payload, dict):
        payload = {'tiers': payload}      # 旧数据是候选轨迹数组，没有身份结论可解析
    identity = payload.get('identity') or {}
    view['identity_verdict'] = identity.get('verdict') or view['identity_verdict']
    view['identity_official_name'] = identity.get('official_name')
    view['identity_reason'] = identity.get('reason') or view['identity_reason']
    if identity.get('suggested_code'):
        view['identity_suggestion'] = {
            'code': identity.get('suggested_code'),
            'name': identity.get('suggested_name'),
        }
    view['identity_suggestions'] = identity.get('suggestions') or []
    # 相关性旗标由体检算好写进 evidence：判据要扫全量名册（2.7 万条），
    # 放在接口路径里会让每次 GET /sector-mappings 变成几百次全表扫描。
    view['relevance_low'] = bool(identity.get('relevance_low'))
    # 被机器换过标的的行（确定性纠正 / ETF 升级）：老板要能看出这是机器改的、原来是什么。
    # 读 `machine_swap_of` 而不是只看 `identity_realign`：只看 realign 会让 ETF 升级行
    # 在分桶里隐身；只看 `match_source` 会被后续 agent 写入洗掉（v7.4 §十四 实测 2 行）。
    swap = machine_swap_of(row)
    view['realigned'] = ({
        'from_code': swap.get('from_code'), 'from_name': swap.get('from_name'),
        'core': swap.get('core'), 'reason': swap.get('reason'), 'kind': swap.get('kind'),
    } if swap else None)
    return view


def build_worklist(rows: List[Dict]) -> Dict:
    """把体检结果整理成给老板看的一次性批量审查清单。"""
    demote = [r for r in rows if r['verdict'] in UNSERVABLE_VERDICTS and r.get('reviewed')]
    buckets: Dict[str, int] = {}
    for r in demote:
        buckets[r['verdict']] = buckets.get(r['verdict'], 0) + 1
    return {
        'total': len(rows),
        'reviewed': sum(1 for r in rows if r.get('reviewed')),
        'verdicts': {v: sum(1 for r in rows if r['verdict'] == v)
                     for v in (VERDICT_OK, VERDICT_NOT_A_FUND, VERDICT_CODE_IS_OTHER_FUND,
                               VERDICT_WRONG_CODE, VERDICT_NOT_FETCHABLE,
                               VERDICT_UNKNOWN, VERDICT_PROBE_UNAVAILABLE)},
        'demote_buckets': buckets,
        'demote_count': len(demote),
        'unreviewable_no_conclusion': sum(
            1 for r in rows
            if r['verdict'] in (VERDICT_UNKNOWN, VERDICT_PROBE_UNAVAILABLE)),
        'items': [{
            'id': r.get('id'), 'sector': r.get('sector'), 'code': r['code'],
            'stored_name': r['stored_name'], 'official_name': r.get('official_name'),
            'verdict': r['verdict'], 'reason': r.get('reason'),
            'suggested_code': r.get('suggested_code'),
            'suggested_name': r.get('suggested_name'),
            'reviewed': r.get('reviewed'), 'owner_locked': r.get('owner_locked'),
        } for r in rows],
    }




# ---------- 静态表回落的共用拒绝集 ----------

_DENY_TTL = 60.0
# loaded=False 表示"从未成功取到拒绝集"。此时静态表按空处理：
# 拿不到拒绝集就放行全部内置映射，等于体检白做（而且是静默的）。
_deny_state = {'at': 0.0, 'map': {}, 'loaded': False}
_deny_lock = threading.Lock()


def denied_code_map(refresh: bool = False, db=None) -> Dict[str, set]:
    """`{板块名: {体检判不可服务的代码}}`——静态表 `SECTOR_FUND_MAP` 没有 is_fetchable 列，
    映射行被降级后板块会悄悄回落到它，所以拒绝集必须也能挡住静态表。

    进程内 TTL 缓存：145 行一次查询，读路径每次分析都调，不能每回都打库。
    体检写完库要调 `invalidate_denied_cache()`。
    """
    from src.models.database import SectorFundMapping
    if db is not None:
        # 调用方给了 session 就直接查：测试用内存库、批量任务用临时库时，
        # 进程级 TTL 缓存会指向另一个数据库，拒绝集就形同虚设。
        # 判据必须走 `row_unservable`：以前这里只筛 `is_fetchable == False`，
        # 于是"列没写、但 evidence 里的 verdict 已是否定"的行在静态表这一步又能溜过去
        # —— 同一个"不可服务"库里存在第四把尺子（第 21 轮 MAJOR-2）。
        # 镜像今天背离 0 行，所以这是个潜伏口，不是今天的故障。
        rows = db.query(SectorFundMapping).filter(
            SectorFundMapping.is_active == True).all()               # noqa: E712
        mapping: Dict[str, set] = {}
        for row in rows:
            if row.sector_name and row.fund_code and row_unservable(row):
                mapping.setdefault(row.sector_name, set()).add(row.fund_code)
        return mapping

    from src.models.database import SessionLocal
    now = time.time()
    with _deny_lock:
        if refresh or now - _deny_state['at'] > _DENY_TTL:
            try:
                session = SessionLocal()
            except Exception as exc:      # 拿不到连接也不能抛给读路径
                logger.warning('[identity] 拒绝集：无法建立会话，沿用上一次结果：%s', exc)
                _deny_state['at'] = now
                return _deny_state['map']
            try:
                _deny_state['map'] = denied_code_map(db=session)
                _deny_state['loaded'] = True
                _deny_state['at'] = now
            except Exception as exc:
                # 读路径不能因为拒绝集拉不到就整体失败：沿用上一次的结论
                logger.warning('[identity] 拒绝集加载失败，沿用上一次结果：%s', exc)
                _deny_state['at'] = now
            finally:
                session.close()
        return _deny_state['map']


def denied_map_available() -> bool:
    """拒绝集是否至少成功加载过一次。"""
    denied_code_map()
    return _deny_state['loaded']


def invalidate_denied_cache():
    with _deny_lock:
        _deny_state['at'] = 0.0
        _deny_state['loaded'] = False


def rejected_codes(sector: str, db=None) -> set:
    """该板块被体检否掉的代码集合（按板块取，**不按代码全局拉黑**：
    159819 对"应用"是错的、对"AI"是对的）。"""
    if not sector:
        return set()
    out = set(denied_code_map(db=db).get(sector) or ())
    try:
        from src.constants.sector_fund_map import normalize_sector_name
        norm = normalize_sector_name(sector)
    except Exception:
        norm = sector
    if norm and norm != sector:
        # 归一后的键必须查**同一个库**：漏传 db 会让这一支去查进程级缓存指向的
        # 另一个数据库（测试/临时库里直接失效）
        out |= set(denied_code_map(db=db).get(norm) or ())
    return out


def static_fund_for_sector(sector: str, db=None) -> Optional[Dict]:
    """静态表查板块对应基金，挡掉体检已否掉的码。"""
    from src.constants.sector_fund_map import get_fund_for_sector, normalize_sector_name
    hit = get_fund_for_sector(sector) or get_fund_for_sector(normalize_sector_name(sector or ''))
    if hit and hit.get('code') in rejected_codes(sector, db=db):
        logger.info('[identity] 静态表回落被拒：%s %s（体检判定不可服务）',
                    sector, hit.get('code'))
        return None
    return hit
