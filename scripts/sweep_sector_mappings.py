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
    python scripts/sweep_sector_mappings.py --restore-from docs/.../manifest.json
退出码：0 正常 / 4 连接串不是本地 SQLite / 7 超过降级上限（未写库）
        5 站点不可用或无结论过多（未写库）
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
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import _db_guard  # noqa: E402

OUT_DIR = os.path.join(ROOT, "docs", "迭代计划", "run-2026-09-20")
CACHE_PATH = os.path.join(OUT_DIR, 'sweep-cache.json')

MANIFEST_FIELDS = ('sector_name', 'fund_code', 'fund_name', 'reviewed', 'reviewed_by',
                   'owner_locked', 'is_fetchable', 'match_source', 'match_kind',
                   'confidence', 'verified_at', 'verify_message', 'evidence')

# 已知一定可服务的好码：整轮请求里周期性重探，用来区分"这只基金有问题"与
# "站点/网络此刻不可用"。后者绝不允许变成批量降级。
CANARIES = ('510300', '159915', '512170', '159995', '515880', '006105')
CANARY_EVERY = 15
PACE_SECONDS = 0.12
MAX_UNKNOWN_RATIO = 0.20
PER_BUCKET_CAP = 16   # 单一原因降这么多条，通常意味着判据或站点出了问题


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


def suggest_exchange_etf(sector):
    """从基金域名册里找"官方名逐字含板块核心词"的场内 ETF，只作建议不自动采信。"""
    import re
    from src.fund.fund_api import fund_api
    from src.services.sector_identity_audit import cjk_core, GENERIC_MARKET_CORES

    core = cjk_core(sector)
    if len(core) < 2 or core in GENERIC_MARKET_CORES:
        return []
    try:
        roster = fund_api.load_fund_roster()
    except Exception:
        return []
    pattern = re.compile(r'(?<![\u4e00-\u9fff])' + re.escape(core) + r'(?![\u4e00-\u9fff])')
    out = []
    for code, entry in (roster.get('by_code') or {}).items():
        name = entry.get('name') or ''
        if not pattern.search(name):
            continue
        if not (code[:2] in ('15', '5') and 'ETF' in name.upper()):
            continue
        out.append({'code': code, 'name': name})
    out.sort(key=lambda x: (len(x['name']), x['code']))
    return out[:5]


def pick_demotions(results):
    """只降"身份不通过 + 原本已审查 + 不是老板手定"的行，分桶互斥。

    老板用 `owner_locked` 标过的有意代理（债券→512000、SpaceX→159206）永远不降——
    那是他说的"没有专门基金，只能选关联性最大的"。
    """
    from src.services.sector_identity_audit import UNSERVABLE_VERDICTS
    return [r for r in results
            if r['verdict'] in UNSERVABLE_VERDICTS and r.get('reviewed')
            and not r.get('owner_row')]


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
    args = ap.parse_args()

    db_url = _db_guard.pin_local_sqlite()
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
                r['suggestions'] = suggest_exchange_etf(m.sector_name)
            r['relevance_low'] = bool(
                r['verdict'] == VERDICT_OK
                and not sector_relevance(m.sector_name, r.get('official_name') or ''))
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

        write_report(args.tag, work, results, buckets, len(demote))
        work['relevance_low'] = sum(1 for r in results if r.get('relevance_low'))
        print('[相关性] 身份没问题但标的与板块字面无关：%d 条（只报告，不降级）'
              % work['relevance_low'])
        print(json.dumps({'verdicts': work['verdicts'], 'demote_buckets': buckets,
                          'demote_count': len(demote),
                          'no_conclusion': unknown}, ensure_ascii=False))

        if not args.apply:
            print('\n未写库。确认 CSV/清单后加 --apply 执行。')
            return 0
        if len(demote) > args.max_flip:
            print('[FAIL] 需降级 %d 条 > 上限 %d，未写库' % (len(demote), args.max_flip))
            return 7

        snap_path, digest = backup_db(db_path)
        manifest = write_manifest(args.tag, db, rows)
        print('[备份] %s sha256=%s' % (snap_path, digest[:16]))
        print('[清单] %s（--restore-from 用它还原）' % manifest)

        from src.services.sector_fund_service import get_sector_fund_service
        applied = apply_results(db, results, demote, args.evidence_only)
        get_sector_fund_service(db).refresh_cache()
        from src.services.sector_identity_audit import invalidate_denied_cache
        invalidate_denied_cache()      # 静态表回落的拒绝集也要立刻认账
        print('[写入] 证据列 %d 行；降级 reviewed %d 行；旧语义标记复位 %d 行%s'
              % (applied['evidence'], applied['demoted'], applied['legacy_repaired'],
                 '（--evidence-only，未降级）' if args.evidence_only else ''))
        print('[提示] 缓存是进程内的：Web 服务如果在跑，需重启才会读到新结论')
        return 0
    finally:
        db.close()


def apply_results(db, results, demote, evidence_only):
    """写证据列（`is_fetchable` 语义 = 可服务）与降级；绝不写 match_source/match_kind。"""
    from src.models.database import SectorFundMapping
    from src.services.sector_identity_audit import (
        UNSERVABLE_VERDICTS, VERDICT_UNKNOWN, VERDICT_PROBE_UNAVAILABLE)
    demote_ids = {r['id'] for r in demote}
    repaired = repair_legacy_fetchable_flag(db)
    by_id = {r['id']: r for r in results}
    now = datetime.now()
    evidence_n = demoted_n = 0
    for row in db.query(SectorFundMapping).filter(
            SectorFundMapping.id.in_(list(by_id))).all():
        r = by_id[row.id]
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
            'evidence': r.get('evidence'),
            'checked_at': now.isoformat(timespec='seconds'),
        }
        row.evidence = json.dumps(evidence, ensure_ascii=False)
        row.verify_message = r.get('reason') or ''
        row.verified_at = now
        if r['verdict'] not in (VERDICT_UNKNOWN, VERDICT_PROBE_UNAVAILABLE):
            # "没查到"不等于"查到了且不对"：无结论时保持原值，不把好端端的历史行踢出服务
            row.is_fetchable = r['verdict'] not in UNSERVABLE_VERDICTS
        evidence_n += 1
        if not evidence_only and row.id in demote_ids:
            row.reviewed = False
            row.owner_locked = False
            row.reviewed_by = None
            demoted_n += 1
    db.commit()
    return {'evidence': evidence_n, 'demoted': demoted_n, 'legacy_repaired': repaired}


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
                         '结论', '相关性存疑', '原本已审查', '老板锁定', '理由', '建议标的'])
        for it in results:
            writer.writerow([it['id'], it['sector'], it['code'], it['stored_name'],
                             it.get('official_name'), it.get('jaccard'), it['verdict'],
                             '是' if it.get('relevance_low') else '',
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


def write_manifest(tag, db, rows):
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


def restore(db, manifest_path):
    """按 manifest 逐字段还原：体检写坏任何东西都能退回原样。"""
    from src.models.database import SectorFundMapping
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
            value = item[field]
            if field == 'verified_at' and value:
                value = datetime.fromisoformat(value)
            setattr(row, field, value)
        restored += 1
    db.commit()
    print('[还原] %d 行已按 %s 恢复' % (restored, manifest_path))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
