# -*- coding: utf-8 -*-
"""板块映射身份体检（S4a-v6 执行案）。

给 `sector_fund_mapping` 的每一行做"身份自证"：这个 6 位代码在基金域到底是不是
名字里写的那只产品。判定与降级都只认基金域自己的证据（名册 / pingzhongdata /
fundsuggest 的 CATEGORYDESC），不碰股票域接口（本机 ProxyError），也不依赖 LLM。

默认 dry-run，只出 CSV 与分桶报告；`--apply` 才写库，且写之前自动备份 + 生成
before-image 清单，`--restore-from` 可逐字段还原。

用法：
    python scripts/sweep_sector_mappings.py                        # 只体检，不写
    python scripts/sweep_sector_mappings.py --apply                # 写证据列 + 降级
    python scripts/sweep_sector_mappings.py --apply --evidence-only  # 只写证据列
    python scripts/sweep_sector_mappings.py --apply --realign-irrelevant  # 换掉八竿子打不着的标的
    python scripts/sweep_sector_mappings.py --restore-from docs/.../manifest.json
退出码：0 正常 / 4 连接串不是本地 SQLite / 5 站点不可用或无结论过多（未写库）
        7 超过降级或 realign 上限（未写库）/ 8 判据自相矛盾或拒绝集读不到（未写库）
"""
import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import sys
import time
import traceback
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import _db_guard  # noqa: E402

OUT_DIR = os.path.join(ROOT, "docs", "迭代计划", "run-2026-09-20")
CACHE_PATH = os.path.join(OUT_DIR, 'sweep-cache.json')

MANIFEST_FIELDS = ('sector_name', 'fund_code', 'fund_name', 'reviewed', 'reviewed_by',
                   'owner_locked', 'is_fetchable', 'match_source', 'match_kind',
                   'confidence', 'verified_at', 'verify_message', 'evidence',
                   # realign 会连带复位这几列，缺一项就是"回滚之后字段对不上"
                   'updated_at', 'is_active', 'keywords', 'llm_reason')

# 已知一定可服务的好码：整轮请求里周期性重探，用来区分"这只基金有问题"与
# "站点/网络此刻不可用"。后者绝不允许变成批量降级。
CANARIES = ('510300', '159915', '512170', '159995', '515880', '006105')
CANARY_EVERY = 15
PACE_SECONDS = 0.12
MAX_UNKNOWN_RATIO = 0.20
PER_BUCKET_CAP = 16   # 单一原因降这么多条，通常意味着判据或站点出了问题
NAV_STALE_DAYS = 30   # 净值停更超过这么多天的候选直接拒（宁可少修，不修只"存在"的僵尸份额）
MAX_VERIFY_PER_ROW = 6


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def backup_db(db_path):
    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    target = os.path.join(ROOT, 'data', 'backups', 'pre-sweep-%s.db' % stamp)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    _checkpoint_sqlite(db_path)        # 不 checkpoint 会漏掉 WAL 里未落盘的改动
    shutil.copy2(db_path, target)
    return target, sha256(target)


def _checkpoint_sqlite(db_path):
    import sqlite3
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            conn.close()
    except Exception as exc:
        print('[warn] WAL checkpoint 失败（继续备份，可能不含未落盘改动）：%s' % exc)


def load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            return json.load(io.open(CACHE_PATH, encoding='utf-8'))
        except Exception:
            pass
    return {'domain': {}, 'hits': {}}


def save_cache(cache):
    os.makedirs(OUT_DIR, exist_ok=True)
    with io.open(CACHE_PATH, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False)


def arbitrate_cached(code, stored, sector, cache):
    """带断点续跑缓存的身份判定：站点抖了重跑不必从头再来一遍。"""
    from src.services.sector_identity_audit import arbitrate_mapping

    def domain(c):
        hit = cache['domain'].get(c)
        if hit is None:
            from src.fund.fund_api import fund_api
            hit = fund_api.get_fund_domain_name(c)
            time.sleep(PACE_SECONDS)
            if hit.get('status') != 'error':
                # 只缓存站点确实回答过的结果。缓存 error = 一次抖动永久化，
                # 之后每次续跑都会在无结论比例上撞死，只能删缓存重来。
                cache['domain'][c] = hit
        return hit

    def hits(name):
        if name not in cache['hits']:
            from src.services.sector_identity_audit import exact_name_hits
            res = None
            for attempt in range(2):          # 检索接口会抖，拿不稳就再问一次
                res = exact_name_hits(name)
                if res is not None:
                    break
                time.sleep(0.5 * (attempt + 1))
            cache['hits'][name] = res if res is not None else 'FAILED'
            time.sleep(PACE_SECONDS)
        saved = cache['hits'][name]
        return None if saved == 'FAILED' else saved

    def in_roster(name):
        from src.services.sector_identity_audit import name_is_known_fund
        try:
            return name_is_known_fund(name)
        except Exception:
            return None

    result = arbitrate_mapping(code, stored, sector,
                              _domain=lambda c: domain(c),
                              _hits=lambda n: hits(n),
                              _name_in_roster=in_roster)
    # 检索接口本次失败：记进缓存的是 'FAILED'，重跑时会再问一次
    if cache['hits'].get(stored) == 'FAILED':
        del cache['hits'][stored]
    return result


def check_canaries():
    from src.fund.fund_api import fund_api
    dead = []
    for code in CANARIES:
        try:
            r = fund_api.verify_fund_fetchable(code, fill_name=False)
            if not r.get('ok'):
                dead.append(code)
        except Exception:
            dead.append(code)
    return dead


def suggest_exchange_etf(sector, db=None, limit=5):
    """板块核心词能对上哪些场内 ETF（纯离线名册匹配，只作建议不自动采信）。

    旧版在这里自己写 `code[:2] in ('15','5')` —— `'512170'[:2]=='51'` 永远不匹配，
    986 只沪市 ETF 全被丢掉；又用 CJK 整词正则，"储能电池ETF"里的"储能"也匹配不上。
    匹配规则已统一进 `sector_identity_audit.etf_candidates`。
    """
    from src.services.sector_identity_audit import etf_candidates
    return etf_candidates(sector, db=db, limit=limit)


def proxy_deny_codes():
    """老板手定的"没有对口标的，只能取关联度最大的替代"的代码。

    读不到就返回 None（调用方必须放弃 realign）：少了这份拒绝集，realign 会把
    债券→512000、SpaceX→159206 这类故意代理换成别的标的，等于覆盖老板的决定。
    """
    try:
        from src.services.sector_fund_agent import DELIBERATE_PROXIES
        return {code for code, _reason in DELIBERATE_PROXIES.values()}
    except Exception as exc:
        print('[FAIL] 读不到刻意代理拒绝集（%s），本轮不做 realign' % exc)
        return None


def _nav_fresh(nav_date):
    """净值日期是否为空/可解析/距今 ≤NAV_STALE_DAYS。返回 (是否新鲜, 描述)。"""
    if not nav_date:
        return False, '无净值日期'
    try:
        date = datetime.strptime(str(nav_date)[:10], '%Y-%m-%d')
    except ValueError:
        return False, '净值日期解析失败'
    days = (datetime.now() - date).days
    return days <= NAV_STALE_DAYS, '%s（%d 天前）' % (nav_date, days)


def rank_verified_candidates(db, cands, used, verify=None):
    """候选排序：①站点净值最新 ②本地历史条数 ③51/56 宽基段 ④代码升序。

    第一排序键必须是**站点新鲜度**：储能/信创的候选本地净值全是 0 条，
    只按本地深度排序等于随机选（v7.1 实测）。停更 >30 天直接拒。
    """
    from sqlalchemy import func
    from src.models.database import FundHistory
    from src.fund.fund_api import fund_api

    verify = verify or (lambda code: fund_api.verify_fund_fetchable(code, fill_name=False))

    def local_depth(code):
        return db.query(func.count(FundHistory.id)).filter(
            FundHistory.fund_code == code).scalar() or 0

    scored = []
    probed = 0
    for cand in cands:
        code = cand['code']
        if code in used:
            continue                      # 本轮已被别的板块挑走，换下一只
        if probed >= MAX_VERIFY_PER_ROW:
            break                         # 每行最多探这么多候选，其余按排序作废
        probed += 1
        try:
            res = verify(code)
        except Exception as exc:
            print('  [skip] %s 验证异常：%s' % (code, exc))
            continue
        time.sleep(PACE_SECONDS)
        if not res.get('is_strict_ok'):
            continue                      # 严格抓取没过 = 现在就不能验证，不写
        fresh, nav_desc = _nav_fresh(res.get('nav_date'))
        if not fresh:
            print('  [skip] %s %s 净值%s' % (code, cand.get('name'), nav_desc))
            continue
        depth = local_depth(code)      # 同一只基金查两次 COUNT 没有意义
        scored.append((str(res.get('nav_date') or ''), depth,
                       1 if code[:2] in ('51', '56') else 0, code, cand,
                       {'nav_date': res.get('nav_date'), 'api_name': res.get('api_name'),
                        'local_history_rows': depth}))
    # 稳定排序：先按代码升序定死同分时的次序，再按 新鲜度↓/本地深度↓/宽基段↓ 排
    scored.sort(key=lambda t: t[3])
    scored.sort(key=lambda t: (t[0], t[1], t[2]), reverse=True)
    return [{'cand': t[4], 'verify': t[5]} for t in scored]


def pick_irrelevant_replacement(db, row, verdict_row, used=None, cores=None,
                                verify=None, realign_codes=None, occupied=None):
    """给"标的与板块八竿子打不着"的行挑一只字面对口的场内 ETF（确定性，零 LLM）。

    准入（v7.1 表格 + v7.2 第 4/5 条）：
    - 老板锁定/owner 手定行**一律不动**；行内代码或**候选代码**落在刻意代理拒绝集
      （512000/159206）里都不动；板块名被别名改写过的（实测 债券→券商）一律不动；
    - `occupied` = 全表在用的代码：本轮没被搬走的行占着的标的不能再来抢一次
      （跨轮撞车：上一轮把 512070 给了 保险，这一轮别的板块就不该再拿到它）；
    - `verdict==ok` 且 `relevance_low`：身份没问题、只是标的选错方向 → 可换；
    - `verdict ∈ {code_is_other_fund, wrong_code}`：本来就是坏行，换成对口号是修复；
    - 其余（not_a_fund / not_fetchable / unknown / probe_unavailable）不换：
      名字本身是股票，或站点没给确定答案，换标的等于猜。
    - 板块核心词必须有效（泛指词如 市场/业绩/应用 直接放弃），且候选经
      `rank_verified_candidates` 的严格抓取 + 新鲜度门槛。
    """
    from src.services.sector_identity_audit import (
        VERDICT_OK, VERDICT_CODE_IS_OTHER_FUND, VERDICT_WRONG_CODE, sector_core)

    used = used if used is not None else set()
    if getattr(row, 'owner_locked', None) or getattr(row, 'reviewed_by', None) == 'owner':
        return None
    if realign_codes and row.fund_code in realign_codes:
        return None
    verdict = verdict_row.get('verdict')
    if verdict not in (VERDICT_OK, VERDICT_CODE_IS_OTHER_FUND, VERDICT_WRONG_CODE):
        return None
    if verdict == VERDICT_OK and not verdict_row.get('relevance_low'):
        return None
    core = sector_core(row.sector_name)
    if not core:
        return None
    from src.services.sector_identity_audit import normalize_sector_text
    if normalize_sector_text(row.sector_name) != (row.sector_name or '').strip():
        # 别名会把板块改写成另一个主题（债券→券商），算出来的核心词与候选都不是
        # 老板在界面上看到的那个板块，确定性换标的在这种行上等于瞎换（v7.2 第 7 条）
        return None
    cands = etf_candidates_for(row.sector_name, db, cores)
    if realign_codes:
        cands = [c for c in cands if c['code'] not in realign_codes]
    blocked = set(used) | ((occupied or set()) - {row.fund_code})
    ranked = rank_verified_candidates(db, cands, blocked, verify)
    if not ranked:
        return None
    best = ranked[0]
    cand, info = best['cand'], best['verify']
    if cand['code'] == row.fund_code:
        return None
    used.add(cand['code'])
    return {
        'id': row.id, 'sector': row.sector_name, 'core': core,
        'from_code': row.fund_code, 'from_name': row.fund_name,
        'code': cand['code'], 'name': cand['name'], 'verdict': verdict,
        'candidates': [b['cand']['code'] + ' ' + b['cand']['name'] for b in ranked[:5]],
        'local_history_rows': info['local_history_rows'],
        'nav_date': info['nav_date'],
        'reason': '板块「%s」核心词「%s」，换成字面对口的场内 ETF %s %s（原 %s %s）'
                  % (row.sector_name, core, cand['code'], cand['name'],
                     row.fund_code, row.fund_name),
    }


def etf_candidates_for(sector, db=None, cores=None, limit=MAX_VERIFY_PER_ROW * 3):
    from src.services.sector_identity_audit import etf_candidates
    return etf_candidates(sector, db=db, all_cores=cores, limit=limit)


def apply_realign(db, plans, created_codes=None, realign_codes=None, manifest_path=None,
                  written=None):
    """两阶段写入的第二阶段：只写字段，绕开 `update_mapping`。

    `update_mapping` 会把行置成 reviewed=True（第 18 轮以前还顺带白送
    owner_locked + reviewed_by='owner'），
    等于 agent 替老板批了审查（v7.2 第 5 条）。换过标的的行必须回到"待老板一键审查"：
    `reviewed/owner_locked=False`、`reviewed_by=None`，并把上一轮针对旧代码的结论
    （confidence/match_*/llm_reason/keywords/evidence.etf_upgrade）一并复位。

    准入是计划期算的，中间隔着几分钟网络验证，老板可能已经用 PUT 改过这行，
    所以**每行落笔前再判一次**（owner 锁、代码是否还是计划里的 from_code、拒绝集）。
    """
    from src.models.database import SectorFundMapping
    from src.services.sector_fund_service import SectorFundService
    rows = {r.id: r for r in db.query(SectorFundMapping).filter(
        SectorFundMapping.id.in_([p['id'] for p in plans])).all()}
    service = SectorFundService(db)
    progress = written if written is not None else []
    for plan in plans:
        row = rows.get(plan['id'])
        if row is None:
            continue
        if getattr(row, 'owner_locked', None) or getattr(row, 'reviewed_by', None) == 'owner':
            print('  [skip] %s 计划期内被老板改过，不覆盖' % row.sector_name)
            continue
        if plan['from_code'] != row.fund_code:
            print('  [skip] %s 标的已变成 %s（计划基于 %s），放弃'
                  % (row.sector_name, row.fund_code, plan['from_code']))
            continue
        if realign_codes and (row.fund_code in realign_codes
                              or plan['code'] in realign_codes):
            print('  [skip] %s 牵涉刻意代理码 %s，不动' % (row.sector_name, plan['code']))
            continue
        recheck = reaudit_new_code(plan['code'], plan['name'], row.sector_name)
        if recheck['verdict'] != 'ok':
            print('  [skip] %s → %s 重新仲裁未通过（%s），不写'
                  % (row.sector_name, plan['code'], recheck['verdict']))
            continue
        if service.ensure_fund_info_exists(plan['code'], plan['name'],
                                           sector_type=row.sector_name):
            # ensure_fund_info_exists 内部会 commit：必须**创建后立刻登记**，
            # 否则回滚漏删，这只基金从此留在基金列表里被同步任务拉历史。
            if created_codes is not None:
                created_codes.append(plan['code'])
            if manifest_path:
                try:
                    # 立刻落盘：后面任何异常/commit 失败都不会让已建档案变成孤儿
                    finalize_manifest(manifest_path, created_codes)
                except Exception as exc:
                    print('[FAIL] 清单写盘失败，本轮新建的基金档案必须手工删除：%s（%s）'
                          % (created_codes, exc))
                    raise
        now = datetime.now()
        try:
            evidence = json.loads(row.evidence) if row.evidence else {}
            if not isinstance(evidence, dict):
                evidence = {'prev': evidence}
        except Exception:
            evidence = {}
        evidence['identity_realign'] = dict(
            plan, applied_at=now.isoformat(timespec='seconds'),
            reaudit=recheck)
        evidence['identity'] = recheck['identity']
        evidence.pop('etf_upgrade', None)     # 旧升级记录讲的是被换掉的那只，留着会指错
        row.fund_code, row.fund_name = plan['code'], plan['name']
        row.confidence = None
        row.match_kind = None
        # 不能置 None：审查门要求 `match_source + verified_at` 齐备，
        # 置空等于让老板点不动"标记已审查"（S5 副本实测：batch_mark_reviewed 返回 0），
        # 预测就永远改不到新标的。旧值（agent/manual）也确实不适用，所以给新 provenance。
        row.match_source = 'identity_realign'
        row.llm_reason = None
        row.keywords = None
        row.reviewed = False
        row.owner_locked = False
        row.reviewed_by = None
        # 用户可见理由用**计划里那句**（"板块「X」核心词「Y」，换成字面对口的场内 ETF…"），
        # 不是重新仲裁的"品种名与映射名一致（Jaccard 1.000）"：老板要看的是为什么换标的，
        # 机器自证的那句已经记在 evidence.identity_realign.reaudit 里，不丢。
        row.verify_message = plan.get('reason') or recheck['reason']
        row.verified_at = now
        row.is_fetchable = True                   # 重新仲裁已确认 ok
        row.evidence = json.dumps(evidence, ensure_ascii=False)
        db.commit()              # 逐行落盘：崩溃时"已成交"与"未成交"分得清
        progress.append(plan)
    db.commit()
    return progress


def audit_relevant(sector, official_name):
    from src.services.sector_identity_audit import sector_relevance
    try:
        return sector_relevance(sector, official_name)
    except Exception:                 # 判不了就当作相关，宁可少报存疑
        return True


def audit_relevance_state(sector, official_name):
    """字面这根轴的**三态**（`relevant` / `alternative_exists` / `no_literal_fund`）。

    `relevance_low` 那一根布尔位把后两态压成了"不旗标"，于是名册里压根查无对口基金的板块
    （区块链、低空经济、核聚变、海力士…）**永远不会进待复核**却还在给新帖挑标的
    （第 27 轮任务 #32）。这里把第三态单独落进 `evidence.identity`，先让人看见。
    """
    from src.services.sector_identity_audit import RELEVANT, relevance_state
    try:
        return relevance_state(sector, official_name)
    except Exception:
        return RELEVANT               # 同 `audit_relevant`：判不了不当旗标


def reaudit_new_code(code, name, sector):
    """换标的后重新做身份判定，**只用非名册来源**。

    名册写名字、再拿名册自证 → Jaccard 恒 1.0、恒 ok，是自我背书。
    这里走 pingzhongdata（单码接口），并叠加"指数段含核心词 + 严格抓取通过 +
    净值 ≤30 天"三条非循环证据（前者已在候选生成里查过，这里只补身份）。
    """
    from src.fund.fund_api import fund_api
    from src.services.sector_identity_audit import arbitrate_mapping

    domain = fund_api.get_fund_domain_name(code, use_roster=False)
    res = arbitrate_mapping(code, name, sector,
                            _domain=lambda _c: domain,
                            _hits=lambda _n: [],
                            _name_in_roster=lambda _n: None)
    identity = {
        'verdict': res['verdict'], 'jaccard': res.get('jaccard'),
        'official_name': res.get('official_name'), 'reason': res.get('reason'),
        'suggested_code': None, 'suggestions': [],
        'relevance_low': bool(res['verdict'] != 'ok' or not audit_relevant(
            sector, res.get('official_name') or '')),
        'relevance_state': audit_relevance_state(sector, res.get('official_name') or ''),
        'evidence': res.get('evidence'),
        'checked_at': datetime.now().isoformat(timespec='seconds'),
        'reaudit_after_realign': True,
    }
    return {'verdict': res['verdict'], 'reason': res.get('reason'),
            'official_name': res.get('official_name'), 'identity': identity}


def pick_demotions(results):
    """只降"身份不通过 + 原本已审查 + 不是老板手定"的行，分桶互斥。

    老板用 `owner_locked` 标过的有意代理（债券→512000、SpaceX→159206）永远不降——
    那是他说的"没有专门基金，只能选关联性最大的"。
    """
    from src.services.sector_identity_audit import UNSERVABLE_VERDICTS
    # 本轮已被 realign 换掉标的的行不再降级：坏代码已经被对口的场内 ETF 顶替了，
    # 再盖一个"不可服务"章等于把刚修好的行踢出服务。
    return [r for r in results
            if r['verdict'] in UNSERVABLE_VERDICTS and r.get('reviewed')
            and not r.get('owner_row') and not r.get('realign')]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true', help='真正写库（默认只体检出报告）')
    ap.add_argument('--evidence-only', action='store_true', help='只写证据列，不做降级')
    ap.add_argument('--max-flip', type=int, default=20, help='允许降级条数上限')
    ap.add_argument('--limit', type=int, default=None, help='只体检前 N 行（调试用）')
    # 带时分秒：同一天二次 --apply 不能覆盖唯一的还原清单（覆盖=丢掉回滚能力）
    ap.add_argument('--tag', default=datetime.now().strftime('%Y%m%d-%H%M%S'))
    ap.add_argument('--restore-from', default=None, help='按 manifest 逐字段还原')
    ap.add_argument('--refresh-roster', action='store_true')
    ap.add_argument('--upgrade-etf', action='store_true',
                    help='把"有场内 ETF 可用却挂着联接/LOF/场外基金"的映射换成那只 ETF"')
    ap.add_argument('--realign-irrelevant', action='store_true',
                    help='把"标的与板块八竿子打不着"的行换成字面对口的场内 ETF（确定性，零 LLM）')
    ap.add_argument('--max-realign', type=int, default=8, help='允许 realign 的条数上限')
    ap.add_argument('--max-upgrade', type=int, default=8, help='允许场内 ETF 升级的条数上限')
    args = ap.parse_args()

    db_url = _db_guard.pin_local_sqlite(use_mirror_default=True)
    db_path = db_url.split('sqlite:///')[-1].replace('\\', '/')
    from src.models.database import SessionLocal, SectorFundMapping
    from src.services.sector_identity_audit import (
        UNSERVABLE_VERDICTS, VERDICT_OK, VERDICT_PROBE_UNAVAILABLE, VERDICT_UNKNOWN,
        build_worklist, sector_relevance)
    os.makedirs(OUT_DIR, exist_ok=True)
    db = SessionLocal()
    try:
        if args.restore_from:
            return restore(db, args.restore_from)

        rows = db.query(SectorFundMapping).order_by(SectorFundMapping.id).all()
        if args.limit:
            rows = rows[:args.limit]
        print('[体检] 待检 %d 行；站点不可用时不会下任何结论' % len(rows))

        dead = check_canaries()
        if dead:
            print('[abort] 已知好码 %s 本次抓不到 —— 判定为站点/网络不可用，不写库' % dead)
            return 5

        from src.fund.fund_api import fund_api
        cache = load_cache()
        try:
            fund_api.load_fund_roster(refresh=args.refresh_roster)
        except Exception as exc:
            print('[abort] 基金域名册拉取失败（%s）：没有名册就不能判"不是基金"' % exc)
            return 5

        results = []
        used_codes = set()          # 本轮已被挑走的候选码：防两个板块撞同一只基金
        # 全表在用的标的：本轮没搬走的行占着的就是"别人的持仓"，跨轮也不许抢
        # 用全表而不是 --limit 截过的行集：调试跑一半时占用保护也不能失真
        occupied_codes = {c for (c,) in db.query(SectorFundMapping.fund_code).distinct().all()
                          if c}
        realign_plans = []
        realign_cores = realign_deny = None
        if args.realign_irrelevant:
            from src.services.sector_identity_audit import sector_core_names
            realign_deny = proxy_deny_codes()
            if realign_deny is None:
                return 8            # 读不到刻意代理拒绝集就不能 realign
            realign_cores = sector_core_names(db)
        for idx, m in enumerate(rows, 1):
            r = arbitrate_cached(m.fund_code, m.fund_name, m.sector_name, cache)
            r['id'] = m.id
            r['sector'] = m.sector_name
            r['reviewed'] = bool(m.reviewed)
            r['owner_locked'] = bool(getattr(m, 'owner_locked', False))
            r['owner_row'] = bool(getattr(m, 'owner_locked', False)) or \
                getattr(m, 'reviewed_by', None) == 'owner'
            if r['verdict'] in (VERDICT_UNKNOWN, VERDICT_PROBE_UNAVAILABLE):
                r['no_conclusion'] = True
            if r['verdict'] in UNSERVABLE_VERDICTS:
                r['suggestions'] = suggest_exchange_etf(m.sector_name, db)
            r['etf_upgrade'] = (pick_etf_upgrade(db, m, r, taken=occupied_codes,
                                                 proxy_codes=realign_deny)
                                if args.upgrade_etf else None)
            r['relevance_low'] = bool(
                r['verdict'] == VERDICT_OK
                and not sector_relevance(m.sector_name, r.get('official_name') or ''))
            r['relevance_state'] = audit_relevance_state(
                m.sector_name, r.get('official_name') or '')
            if args.realign_irrelevant:
                plan = pick_irrelevant_replacement(db, m, r, used=used_codes,
                                                   cores=realign_cores,
                                                   realign_codes=realign_deny,
                                                   occupied=occupied_codes)
                if plan:
                    r['realign'] = plan
                    realign_plans.append(plan)
                    occupied_codes.discard(plan['from_code'])
                    occupied_codes.add(plan['code'])
            results.append(r)
            if idx % CANARY_EVERY == 0:
                dead = check_canaries()
                if dead:
                    print('[abort] 第 %d 行复探金丝雀失败（%s），本轮不写库' % (idx, dead))
                    save_cache(cache)
                    return 5
            if idx % 20 == 0:
                print('  ... %d/%d' % (idx, len(rows)))
                save_cache(cache)
        save_cache(cache)

        work = build_worklist(results)
        unknown = work['unreviewable_no_conclusion']
        if unknown > MAX_UNKNOWN_RATIO * len(results):
            print('[abort] 无结论行 %d/%d 超过 %.0f%%，多半是站点抖动，本轮不写库'
                  % (unknown, len(results), MAX_UNKNOWN_RATIO * 100))
            return 5

        # 分桶互斥：verdict 本身就是单值的，这里只是防止将来加规则时重复计数
        demote = pick_demotions(results)
        planned = [r for r in results if r.get('etf_upgrade')]
        print('[场内ETF升级] 可替换 %d 条（--apply --upgrade-etf 生效）' % len(planned))
        for r in planned[:25]:
            up = r['etf_upgrade']
            print('   %-12s %s → %s %s' % (r['sector'], up['replaced'],
                                           up['code'], up['name']))
            # 占用登记在 pick_etf_upgrade 内部做（能被单测覆盖），这里只打印
        buckets = {}
        for r in demote:
            buckets[r['verdict']] = buckets.get(r['verdict'], 0) + 1
        if sum(buckets.values()) != len(demote):
            # 安全不变量写成 assert 的话，`python -O` 会把它整个优化掉
            print('[FAIL] 分桶求和 != 降级数，判据出现重叠，未写库')
            return 8
        over = {v: n for v, n in buckets.items() if n > PER_BUCKET_CAP}
        if over:
            print('[FAIL] 单一原因降级 %s 超过单桶上限 %d，多半是判据或站点异常，未写库'
                  % (over, PER_BUCKET_CAP))
            return 7

        print('[不相关标的纠正] 计划 %d 条（--apply --realign-irrelevant 生效）'
              % len(realign_plans))
        for p in realign_plans[:25]:
            print('   %-12s %s %s → %s %s（候选 %d 只，净值 %s）'
                  % (p['sector'], p['from_code'], p['from_name'], p['code'], p['name'],
                     len(p['candidates']), p['nav_date']))
        exit_code = 0
        write_report(args.tag, work, results, buckets, len(demote))
        if len(planned) > args.max_upgrade:
            print('[FAIL] 场内 ETF 升级 %d 条 > 上限 %d，未写库（可加 --max-upgrade）'
                  % (len(planned), args.max_upgrade))
            return 7
        if len(realign_plans) > args.max_realign:
            print('[FAIL] 需 realign %d 条 > 上限 %d，未写库（CSV 已出，可先核对）'
                  % (len(realign_plans), args.max_realign))
            return 7
        flagged = [r for r in results if r.get('relevance_low')]
        fixable = sum(1 for r in flagged if r.get('realign'))
        print('[相关性] 身份没问题但标的与板块字面无关：%d 条，其中可确定性修复 %d 条、'
              '需要 LLM 语义判断 %d 条' % (len(flagged), fixable, len(flagged) - fixable))
        if flagged:
            print('   flagged: %s' % ', '.join(
                '%s%s' % (r['sector'], '(可修)' if r.get('realign') else '(待LLM)')
                for r in flagged))
        print(json.dumps({'verdicts': work['verdicts'], 'demote_buckets': buckets,
                          'demote_count': len(demote),
                          'relevance_low': len(flagged),
                          'realign_planned': len(realign_plans),
                          'no_conclusion': unknown}, ensure_ascii=False))

        if not args.apply:
            print('\n未写库。确认 CSV/清单后加 --apply 执行。')
            return 0
        if len(demote) > args.max_flip:
            print('[FAIL] 需降级 %d 条 > 上限 %d，未写库' % (len(demote), args.max_flip))
            return 7

        # 规划期的网络验证要花几分钟，期间老板可能通过 PUT 改过行：
        # 先 expire_all() 再写清单，否则回滚会把他的新改动当成"原值"退回去
        db.expire_all()
        snap_path, digest = backup_db(db_path)
        manifest = write_manifest(args.tag, db)
        print('[备份] %s sha256=%s' % (snap_path, digest[:16]))
        print('[清单] %s（--restore-from 用它还原）' % manifest)

        from src.services.sector_fund_service import get_sector_fund_service
        # expire_all() 已经提前到 write_manifest 之前：清单前像与写入读的是同一份真值
        created_codes = []
        realigned = []
        applied = apply_results(db, results, demote, args.evidence_only,
                                upgrade_etf=args.upgrade_etf,
                                created_codes=created_codes, manifest_path=manifest)
        if should_realign(args):
            try:
                # 成交的行**逐行提交**并即时登记进 realigned（out 参数）：
                # ensure_fund_info_exists 内部会 commit，中途抛异常时前面几行已经落盘，
                # 只靠返回值会丢掉进度，finally 就会把已修好的行反向盖成不可服务
                apply_realign(db, realign_plans, created_codes,
                              realign_codes=realign_deny, manifest_path=manifest,
                              written=realigned)
            except Exception:
                traceback.print_exc()
                print('[warn] realign 中途失败（已成交 %d 行保留，其余按体检结论处理）'
                      % len(realigned))
                exit_code = 6
            finally:
                db.rollback()      # 半路异常后 session 处于失败事务里，不 rollback 会让下一步直接抛
                if created_codes:
                    finalize_manifest(manifest, created_codes)
                # 计划了却没成交的坏行必须回到该降就降，否则它顶着 reviewed=True
                # 挂着不可服务结论活着，读者与审查门都不认它
                demote_unwritten(db, results, realign_plans, realigned)
        get_sector_fund_service(db).refresh_cache()
        from src.services.sector_identity_audit import invalidate_denied_cache
        invalidate_denied_cache()      # 静态表回落的拒绝集也要立刻认账
        print('[写入] 证据列 %d 行；降级 reviewed %d 行；旧语义标记复位 %d 行；'
              '场内 ETF 升级 %d 行；不相关标的纠正 %d 行；机器换标的待复核复位 %d 行%s'
              % (applied['evidence'], applied['demoted'], applied['legacy_repaired'],
                 applied['upgraded'], len(realigned), applied['unacknowledged_reset'],
                 '（--evidence-only，未降级）' if args.evidence_only else ''))
        print('[提示] 映射缓存是进程内的（60s TTL）：Web 服务最迟 60 秒后读到新结论，'
              '要立刻生效就重启它')
        return exit_code
    finally:
        db.close()


def pick_etf_upgrade(db, row, verdict_row, taken=None, proxy_codes=None):
    """老板规则：能配场内 ETF 就必须用 ETF（最纯粹），联接/LOF/场外只在没有 ETF 时用。

    只在**同时满足**以下条件时替换：原标的不是场内 ETF、名册里存在官方名含板块核心词
    的场内 ETF、该 ETF 抓取严格通过。换标的属于改数据，所以要留证据。
    """
    from src.services.sector_fund_agent import fund_kind_label
    from src.services.sector_identity_audit import identity_verdict_of, UNSERVABLE_VERDICTS

    if getattr(row, 'owner_locked', None) or getattr(row, 'reviewed_by', None) == 'owner':
        return None                       # 老板手定的代理不动
    # 两个真值源都要看：只读**存量** verdict 的话，第一次对没有体检证据的库
    # （生产 S6 就是这种）跑 --upgrade-etf，本轮刚判出的 not_a_fund/code_is_other_fund
    # 会被放过 → 给股票行换上 ETF、又留着"不可服务"的证据，行从此对所有读者隐身，
    # 连 owner_confirm 都拒绝它（sector_fund_service:220）。
    if (verdict_row.get('verdict') in UNSERVABLE_VERDICTS
            or identity_verdict_of(row) in UNSERVABLE_VERDICTS):
        return None                       # 先等身份体检降级，别在坏行上换标的
    official = verdict_row.get('official_name') or row.fund_name or ''
    if fund_kind_label(row.fund_code, official) == 'etf':
        return None
    from src.fund.fund_api import fund_api
    # 同一指数往往有多家公司的场内 ETF（医疗：512170 / 159828 / 158010 …）。
    # 名字长度分不出好坏，用**净值历史条数**当"年龄与规模"的代理：老基金历史更长，
    # 通常也更活跃。候选本来就限死在 5 只以内，所以每只都抓一次是可接受的。
    from sqlalchemy import func
    from src.models.database import FundHistory

    def local_depth(code):
        # 本站已经存了多少条净值 = 这只基金在本系统里"现在就能验证"到什么程度。
        # 零网络、比 30 天窗口的 history_count 有区分度（那个最多 30 条，大家都一样）。
        return db.query(func.count(FundHistory.id)).filter(
            FundHistory.fund_code == code).scalar() or 0

    best, best_score = None, -1
    probed = 0
    # 必须带 db：all_cores 缺省会退化成"只有静态表那 122 个板块"，
    # 109/145 个在册板块的核心词不在里面，候选恒空（--upgrade-etf 静默变 no-op）
    cands = verdict_row.get('suggestions') or suggest_exchange_etf(
        row.sector_name, db=db, limit=20)
    # 与 realign 同一套保护：老板刻意代理码（512000/159206）不能被"升级"顶掉，
    # 别的板块正在用的标的也不能抢（否则两个板块挂同一只 ETF）
    if proxy_codes is None:
        proxy_codes = proxy_deny_codes()
        if proxy_codes is None:
            return None                   # 读不到拒绝集就不升级（fail-closed）
    caller_taken = taken       # 成交后把占用的码登记回调用方：同轮不许第二次抢同一只
    taken = set(taken or ()) - {row.fund_code}
    cands = [c for c in cands if c['code'] not in proxy_codes and c['code'] not in taken]
    for cand in cands:
        if cand['code'] == row.fund_code:
            return None                   # 第一名就是自己：已经是最优，不必再探
        # （注意：这里 return 是对的——候选按"指数段最短优先"排过，
        #  自己排第一说明现役标的就是最纯粹的那只，换任何一只都是变差）
        probed += 1
        if probed > MAX_VERIFY_PER_ROW:
            break                         # 与 rank_verified_candidates 同一预算，别一行打 20 次站
        verify = fund_api.verify_fund_fetchable(cand['code'], fill_name=False)
        time.sleep(PACE_SECONDS)
        if not verify.get('is_strict_ok'):
            continue
        if not _nav_fresh(verify.get('nav_date'))[0]:
            continue                      # 停更 >30 天的"僵尸份额"不能当升级目标
        score = local_depth(cand['code'])
        if score > best_score:
            best, best_score = cand, score
    if best is None:
        return None
    if caller_taken is not None:
        caller_taken.add(best['code'])   # 同指数的复合板块（证券/保险）本轮只许占一次
    return {'code': best['code'], 'name': best['name'],
            'replaced': '%s %s' % (row.fund_code, row.fund_name or official),
            # 前端"机器已纠正"横幅要说得出**原来是什么**：只留 `replaced` 一句人话，
            # `identity_view.realigned.from_code` 对升级行就永远是 null（第 7 轮实测
            # id 131/145 就是这样），realign 那侧一直是带 from_code/from_name 的。
            'from_code': row.fund_code,      # 落笔前复判用（realign 同一机制）
            'from_name': row.fund_name,
            'local_history_rows': best_score,
            # 理由里写老板在册的那只（`row.fund_name`），不是站点解析出的 official_name：
            # 两者不一致时（如 162412 vs 医疗ETF华宝）报告会说"替换"一只不存在的持仓
            'reason': '板块「%s」有场内 ETF 可用，按"ETF 最纯粹"替换 %s'
                      % (row.sector_name, row.fund_name or official)}


def should_realign(args) -> bool:
    """realign 只在"要写库 + 明确开启 + 不是只写证据列"三件事同时成立时执行。"""
    return bool(args.apply and args.realign_irrelevant and not args.evidence_only)


def logger_skip(row, why):
    print('  [skip] %s 计划未成交但不降级：%s' % (row.sector_name, why))


def demote_unwritten(db, results, plans, written):
    """计划没成交的行必须回到"该降就降"。

    `pick_demotions` 会因为"本轮有 realign 计划"放过这些坏行；一旦重新仲裁把计划否掉，
    没人再补这一刀，那行就顶着 `reviewed=True` 挂着不可服务的结论活着，
    而且 CSV/分桶/worklist 里都查不到它的账。
    """
    done = {p['id'] for p in written}
    pending_by_id = {p['id']: p for p in plans if p['id'] not in done}
    pending = list(pending_by_id.values())
    if not pending:
        return 0
    from src.models.database import SectorFundMapping
    from src.services.sector_identity_audit import UNSERVABLE_VERDICTS
    by_id = {r['id']: r for r in results}
    fixed = 0
    for row in db.query(SectorFundMapping).filter(
            SectorFundMapping.id.in_([p['id'] for p in pending])).all():
        r = by_id.get(row.id) or {}
        if r.get('verdict') not in UNSERVABLE_VERDICTS or r.get('owner_row'):
            continue
        # 计划期到现在这几分钟里老板可能已经 PUT 修过这行：
        # 拿旧事实降级会把他刚设的 owner_locked 解掉、甚至把新换的好标的判成不可服务
        if getattr(row, 'owner_locked', None) or getattr(row, 'reviewed_by', None) == 'owner':
            logger_skip(row, '老板已锁定')
            continue
        from_code = (pending_by_id.get(row.id) or {}).get('from_code')
        if from_code and from_code != row.fund_code:
            logger_skip(row, '标的已改成 %s' % row.fund_code)
            continue
        row.reviewed, row.owner_locked, row.reviewed_by = False, False, None
        row.is_fetchable = False
        fixed += 1
    db.commit()
    if fixed:
        print('[回退降级] %d 行 realign 未成交，按体检结论降级' % fixed)
    return fixed


def apply_results(db, results, demote, evidence_only, upgrade_etf=False,
                  created_codes=None, manifest_path=None):
    """写证据列（`is_fetchable` 语义 = 可服务）与降级；绝不写 match_source/match_kind。"""
    from src.models.database import SectorFundMapping
    from src.services.sector_identity_audit import (
        UNSERVABLE_VERDICTS, VERDICT_UNKNOWN, VERDICT_PROBE_UNAVAILABLE)
    demote_ids = {r['id'] for r in demote}
    repaired = repair_legacy_fetchable_flag(db)
    by_id = {r['id']: r for r in results}
    from src.services.sector_identity_audit import machine_swap_of
    now = datetime.now()
    evidence_n = demoted_n = upgraded = unacked = 0
    for row in db.query(SectorFundMapping).filter(
            SectorFundMapping.id.in_(list(by_id))).all():
        r = by_id[row.id]
        swapped_reason = None
        try:
            evidence = json.loads(row.evidence) if row.evidence else {}
            if not isinstance(evidence, dict):
                evidence = {'prev': evidence}
        except Exception:
            evidence = {}
        evidence['identity'] = {
            'verdict': r['verdict'], 'jaccard': r.get('jaccard'),
            'official_name': r.get('official_name'), 'reason': r.get('reason'),
            'suggested_code': r.get('suggested_code'),
            'suggestions': r.get('suggestions') or [],
            'relevance_low': r.get('relevance_low', False),
            'relevance_state': r.get('relevance_state') or 'relevant',
            'evidence': r.get('evidence'),
            'checked_at': now.isoformat(timespec='seconds'),
        }
        up = r.get('etf_upgrade')
        upgraded_this_row = bool(upgrade_etf and not evidence_only and up
                                 and up.get('from_code') in (None, row.fund_code)
                                 and not (getattr(row, 'owner_locked', None)
                                          or getattr(row, 'reviewed_by', None) == 'owner'))
        if upgraded_this_row:
            evidence['etf_upgrade'] = dict(up, applied_at=now.isoformat(timespec='seconds'))
            # sector_fund_mapping.fund_code 有外键指向 fund_info：新标的没档案就写
            # 会 IntegrityError（实测踩到），先补一条最小档案，净值由基金同步任务补。
            from src.services.sector_fund_service import SectorFundService
            service = SectorFundService(db)
            if service.ensure_fund_info_exists(up['code'], up['name'],
                                               sector_type=row.sector_name):
                # ensure_fund_info_exists 内部会 commit：新建的档案必须登记进清单，
                # 否则 --restore-from 之后基金列表里多一只没人认领的基金、
                # 同步任务还会替它拉历史（realign 路径已经这么处理，两条要对齐）
                if created_codes is not None:
                    created_codes.append(up['code'])
                    if manifest_path:
                        # 档案已经 commit：清单写不下去就必须喊人手工删，否则 restore()
                        # 漏掉它，基金同步任务从此替一只没人认领的基金拉净值
                        # （realign 路径 `:358` 是这么处理的，两条对齐）
                        try:
                            finalize_manifest(manifest_path, created_codes)
                        except Exception:
                            traceback.print_exc()
                            print('[warn] 清单写入失败：请手工删除本轮新建基金档案 %s'
                                  % ','.join(created_codes))
            row.fund_code, row.fund_name = up['code'], up['name']
            row.is_fetchable = None       # 新标的还没体检，退回"从未体检"
            # 旧的 identity/verify_message 是**被换掉那只**的结论：留着会让
            # evidence.identity.verdict=ok 挂在一只从没体检过的新代码上（假自证）
            if 'identity' in evidence:
                evidence['identity_before_upgrade'] = evidence.pop('identity')
            # 机器换标的不能沿用老板那句"已审查"：与 realign 同一规则
            row.reviewed, row.owner_locked, row.reviewed_by = False, False, None
            row.confidence = None
            row.match_kind = None
            row.match_source = 'etf_upgrade'
            row.llm_reason = None
            row.keywords = None
            swapped_reason = up['reason']
            upgraded += 1
        row.evidence = json.dumps(evidence, ensure_ascii=False)
        # `verify_message` 是**给用户看的理由**（前端与审查门都直接引用它），不是体检日志：
        # 只在"本轮真的改了这行的结论"时写。以前无条件写体检语句，实测一次 --apply
        # 就把 143 行的用户可见理由刷成"Jaccard 1.000"，连老板手定的
        # "无债市标的时取证券/券商 ETF"都一起刷没了。
        if swapped_reason:
            row.verify_message = swapped_reason
        elif r['verdict'] in UNSERVABLE_VERDICTS and not r.get('owner_row'):
            # 只给"本轮判出问题"的行写体检理由。**不要**加 `or not row.verify_message`：
            # 那会把 ok 行也刷成"品种名一致（Jaccard 1.000）"，实测 103/145 行被这样灌满，
            # 正是刚返修过的损坏类别（理由列是给用户看的，不是体检日志）。
            row.verify_message = r.get('reason') or ''
        # 存量返修：机器换过标的、章却是"已审查"的行（D4 之前留下的，实测 id 131 传媒、
        # 145 医疗）。换标的从没人确认过，那个 reviewed 不是老板盖的 → 复位待复核，
        # 前端"机器已纠正待复核"分桶才认账。判据只能来自 evidence（见 `machine_swap_of`）。
        if not evidence_only and not upgraded_this_row and row.reviewed \
                and not r.get('owner_row') and machine_swap_of(row):
            row.reviewed, row.owner_locked, row.reviewed_by = False, False, None
            unacked += 1
        row.verified_at = now
        # 本轮刚换标的的行不写镜像章：verdict 是关于旧代码的，写 True 等于替新代码背书
        if (r['verdict'] not in (VERDICT_UNKNOWN, VERDICT_PROBE_UNAVAILABLE)
                and not r.get('owner_row')
                and not upgraded_this_row):
            # "没查到"不等于"查到了且不对"：无结论时保持原值，不把好端端的历史行踢出服务。
            # 老板锁定的行更不打不可服务章——那列会让所有读者拉黑它，而审查门禁
            # 连 owner_confirm 都拒绝，等于把老板自己选的标的永久关在门外。
            row.is_fetchable = r['verdict'] not in UNSERVABLE_VERDICTS
        evidence_n += 1
        if not evidence_only and row.id in demote_ids:
            row.reviewed = False
            row.owner_locked = False
            row.reviewed_by = None
            demoted_n += 1
    db.commit()
    return {'evidence': evidence_n, 'demoted': demoted_n, 'legacy_repaired': repaired,
            'upgraded': upgraded, 'unacknowledged_reset': unacked}


def repair_legacy_fetchable_flag(db) -> int:
    # 清掉"列被旧语义写成 False、但没有任何身份结论"的历史行：旧版 agent 把
    # is_fetchable 当"严格抓取通过"写过，而该列现在的语义是"可服务"。遗留 False
    # 会让行在所有 SQL 读者里黑洞掉，却没有任何 verdict 能解释原因。
    # 部署顺序（迁移 → 上线 → 本脚本 --apply）里这一步就是兜底。
    from src.models.database import SectorFundMapping
    from src.services.sector_identity_audit import identity_verdict_of
    fixed = 0
    for row in db.query(SectorFundMapping).filter(
            SectorFundMapping.is_fetchable == False).all():   # noqa: E712
        if identity_verdict_of(row) is None:
            row.is_fetchable = None
            fixed += 1
    return fixed


def _fmt_suggestion(item):
    """把建议标的写成一句人话：优先给"名字对应的另一只基金"，否则给场内 ETF 候选。"""
    if item.get('suggested_code'):
        return '%s %s' % (item['suggested_code'], item.get('suggested_name') or '')
    alts = item.get('suggestions') or []
    if alts:
        return ' / '.join('%s %s' % (a['code'], a['name']) for a in alts[:3])
    return ''


def write_report(tag, work, results, buckets, demote_count):
    path = os.path.join(OUT_DIR, 'sweep-%s.csv' % tag)
    with io.open(path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['id', '板块', '代码', '映射里的名字', '基金域真名', '相似度',
                         '结论', '相关性存疑', '可确定性纠正', '原本已审查', '老板锁定',
                         '理由', '建议标的'])
        for it in results:
            writer.writerow([it['id'], it['sector'], it['code'], it['stored_name'],
                             it.get('official_name'), it.get('jaccard'), it['verdict'],
                             '是' if it.get('relevance_low') else '',
                             '%s %s' % (it['realign']['code'], it['realign']['name'])
                             if it.get('realign') else '',
                             '是' if it.get('reviewed') else '',
                             '是' if it.get('owner_locked') else '',
                             it.get('reason'), _fmt_suggestion(it)])
    print('[报告] %s（降级候选 %d 条，分桶 %s）' % (path, demote_count, buckets))
    # 老板一次性批量审查清单：只列被降的，附建议标的
    review_path = os.path.join(OUT_DIR, 'sweep-%s-review-list.csv' % tag)
    with io.open(review_path, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['板块', '原标的', '为什么不能用', '建议换成（未采信，需人工确认）'])
        for it in results:
            if it['verdict'] in ('ok', 'unknown', 'probe_unavailable') or not it.get('reviewed'):
                continue
            writer.writerow([it['sector'], '%s %s' % (it['code'], it['stored_name']),
                             it.get('reason'),
                             _fmt_suggestion(it) or '（名册里没有对应的场内 ETF）'])
    print('[清单] %s' % review_path)


def write_manifest(tag, db):
    """同名清单已存在时自动加时间后缀，绝不覆盖既有回滚依据。"""
    from src.models.database import SectorFundMapping
    items = []
    for row in db.query(SectorFundMapping).order_by(SectorFundMapping.id).all():
        item = {'id': row.id}
        for field in MANIFEST_FIELDS:
            value = getattr(row, field, None)
            item[field] = value.isoformat(sep=' ') if isinstance(value, datetime) else value
        items.append(item)
    path = os.path.join(OUT_DIR, 'sweep-manifest-%s.json' % tag)
    if os.path.exists(path):
        path = path.replace('.json', '-%s.json' % datetime.now().strftime('%H%M%S'))
    with io.open(path, 'w', encoding='utf-8') as f:
        json.dump({'created_at': datetime.now().isoformat(timespec='seconds'),
                   'fields': list(MANIFEST_FIELDS), 'rows': items},
                  f, ensure_ascii=False, indent=1)
    total = db.query(SectorFundMapping).count()
    if len(items) != total:
        # 清单不全 = 回滚会静默漏行，比没有清单更危险
        raise SystemExit('[FAIL] manifest 覆盖 %d/%d 行，拒绝写库' % (len(items), total))
    return path


def finalize_manifest(path, created_fund_codes):
    """把本轮新建的 `fund_info` 代码登记进清单：不登记的话回滚后会漏删，
    这只基金永远挂在基金列表里、被同步任务拉历史。"""
    data = json.load(io.open(path, encoding='utf-8'))
    data['created_fund_codes'] = sorted(set(data.get('created_fund_codes') or []) |
                                        set(created_fund_codes))
    with io.open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return data['created_fund_codes']


DATETIME_FIELDS = ('verified_at', 'updated_at')


def _coerce(field, value):
    """清单是 JSON，日期存成 ISO 串：塞回 DateTime 列前必须还原类型，
    否则 SQLite 直接 TypeError（上一版只对 verified_at 做了这件事）。"""
    if field in DATETIME_FIELDS and isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return value


def restore(db, manifest_path):
    """按 manifest 逐字段还原：体检写坏任何东西都能退回原样。"""
    from src.models.database import SectorFundMapping, FundInfo, FundHistory
    data = json.load(io.open(manifest_path, encoding='utf-8'))
    restored = 0
    for item in data['rows']:
        row = db.query(SectorFundMapping).filter(
            SectorFundMapping.id == item['id']).first()
        if not row:
            continue
        for field in data.get('fields', MANIFEST_FIELDS):
            if field not in item:
                continue
            setattr(row, field, _coerce(field, item[field]))
        restored += 1
    removed = 0
    # 只清"清单生成之后"同步回来的净值：本地库现存 **32 只 / 209 行**是"有净值、没档案"
    # 的历史孤儿，`ensure_fund_info_exists` 对它们回报"新建"，无条件 delete 会把老板
    # 本来就有的净值一起吞掉（那是不可再生数据，同步任务不会补历史全量）。
    since_raw = (data.get('created_at') or '')[:10]
    since = datetime.strptime(since_raw, '%Y-%m-%d').date() if since_raw else None
    for code in data.get('created_fund_codes') or []:
        hist = db.query(FundHistory).filter(FundHistory.fund_code == code)
        if since is not None:
            hist = hist.filter(FundHistory.nav_date >= since)
        hist.delete(synchronize_session=False)
        db.query(FundInfo).filter(FundInfo.fund_code == code).delete(synchronize_session=False)
        removed += 1
    db.commit()
    from src.services.sector_fund_service import get_sector_fund_service
    get_sector_fund_service(db).refresh_cache()
    from src.services.sector_identity_audit import invalidate_denied_cache
    invalidate_denied_cache()
    print('[还原] %d 行已按 %s 恢复；清掉本轮新建基金档案 %d 只'
          % (restored, manifest_path, removed))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
