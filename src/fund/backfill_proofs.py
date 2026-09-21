# -*- coding: utf-8 -*-
"""「已经向数据源问过这段区间、它给了几条」的凭据（负/部分结果缓存）。

为什么要有（S7-2 / 计划案 S7-b）：要把一条预测归成"结构性不可验"，前提必须是
**真的向数据源请求过那个区间**。以前这句话是没证明过的断言 ——
`FundDataManager.backfill_history_range` 在"本地最早净值 ≤ 起点且窗口够密"时会
early-return，压根没发请求；反过来若不设缓存，Cron 又会每天为同样的窗口重发请求。

第 10 轮评审把两条造假入口堵掉了，本模块的设计因此是"窄但诚实"：
- **B-1**：传输失败/限流页/翻页触顶时数据源调用返回 `None`（不是 `[]`），
  调用方**什么都不记**。一次抖动被记成"源端确实没有"＝ 之后 TTL 天内不再问，真数据永远回不来。
- **B-2**：不再把多次探测做**区间并集**。并集会把从没问过的空洞盖住，
  而 `covers()` 只看包络 ⇒ 等于谎称"这段问过且没有"。现在存的是**逐次探测的区间列表**，
  只有**单次**探测完整包含本次窗口才算数；合并只允许发生在相交/首尾相接的区间之间。

实现约束：
- 存进已有的 `system_config`（键 `nav_backfill_proof:<基金代码>`），**不加新表新列** ⇒ 不牵扯生产迁移；
- 写入用**调用方的会话且只 flush**：缓存不该把调用方事务里没写完的东西一起提交，
  提交时机由调用方决定（验证侧补拉那一步会立刻提交，否则本次验证一失败回滚，凭据就跟着丢）；
- 空答复（0 条）用更短的 TTL：现在 `None` 已经把"没问到"分出去了，但"合法信封 + 空列表"
  仍可能是限流页，2 天后允许再问一次。
"""
import json
from datetime import date, datetime, timedelta

KEY_PREFIX = 'nav_backfill_proof:'
TTL_DAYS = 7                  # 源端给过数据 ⇒ 区间内那些行是事实，一周内不必再问
EMPTY_TTL_DAYS = 2            # 源端答"没有" ⇒ 也存，但更短，防限流页被当成事实
KEEP_DAYS = 30                # 太久以前的探测不再参与判断，直接丢掉
MAX_PROBES = 40               # 单基金凭据条数上限，避免这行 JSON 无限膨胀


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def proof_key(fund_code: str) -> str:
    return KEY_PREFIX + (fund_code or '')


def _parse(text):
    """读出探测列表。老格式（区间并集那种单 dict）一律**视为不可信**并丢弃。"""
    if not text:
        return []
    try:
        data = json.loads(text)
    except Exception:
        return []
    items = data.get('probes') if isinstance(data, dict) else None
    if items is None:
        return []          # v1 的并集格式：它可能盖住从没问过的窗口，不认
    probes = []
    for raw in items:
        try:
            start = date.fromisoformat(str(raw.get('start')))
            end = date.fromisoformat(str(raw.get('end')))
            rows = int(raw.get('source_rows') or 0)
        except (TypeError, ValueError, AttributeError):
            continue       # 单条坏数据只丢这一条，不让整批验证崩掉（评审 MINOR-3）
        if start and end and start <= end:
            probes.append({'start': start, 'end': end, 'source_rows': rows,
                           'checked_at': str(raw.get('checked_at') or '')})
    return probes


def read_probes(db, fund_code: str):
    from src.models.database import SystemConfig

    if db is None or not fund_code:
        return []
    row = db.query(SystemConfig).filter(
        SystemConfig.config_key == proof_key(fund_code)).first()
    return _parse(row.config_value if row else None)


def read_proof(db, fund_code: str):
    """兼容旧调用名：返回**最近一次**探测（不做并集）。需要"盖得住"请用 `fresh`。"""
    probes = read_probes(db, fund_code)
    return max(probes, key=lambda p: p['checked_at']) if probes else None


def _fresh(probe, today: date, ttl_days: int = TTL_DAYS) -> bool:
    try:
        checked = datetime.fromisoformat(probe['checked_at']).date()
    except (TypeError, ValueError):
        return False
    return (today - checked) <= timedelta(days=ttl_days)


def _ttl_for(probe) -> int:
    return TTL_DAYS if (probe.get('source_rows') or 0) > 0 else EMPTY_TTL_DAYS


def covering_probe(probes, start_date: date, end_date: date, today: date = None):
    """存在**单次**探测完整包含 [start,end] 且未过期 ⇒ 返回它，否则 None。"""
    today = _as_date(today) or date.today()
    hits = [p for p in probes or []
            if p['start'] <= start_date and p['end'] >= end_date and _fresh(p, today, _ttl_for(p))]
    return max(hits, key=lambda p: p['checked_at']) if hits else None


def fresh(db, fund_code: str, start_date: date, end_date: date, today: date = None):
    if db is None or not fund_code or start_date is None or end_date is None:
        return None
    return covering_probe(read_probes(db, fund_code), _as_date(start_date),
                          _as_date(end_date), today)


def _merge_adjacent(probes):
    """只合并**相交或首尾相接**的区间 —— 相隔两周的两段绝不并成一段。"""
    out = []
    for p in sorted(probes, key=lambda x: (x['start'], x['end'])):
        last = out[-1] if out else None
        touching = last and p['start'] <= last['end'] + timedelta(days=1)
        both_same_day_count = last and (last.get('source_rows') or 0) == (p.get('source_rows') or 0)
        if touching and both_same_day_count:
            last['end'] = max(last['end'], p['end'])
            if p['checked_at'] > last['checked_at']:
                last['checked_at'] = p['checked_at']
        else:
            out.append(dict(p))
    return out


def record_probe(db, fund_code: str, start_date: date, end_date: date, source_rows: int = 0):
    """追加一次探测记录（区间 + 源端给了几条 + 核验时间），返回写入后的列表或 None。

    只 flush 不 commit：库代码不该替调用方提交事务（S4 为此踩过一次）。
    """
    from src.models.database import SystemConfig

    if db is None or not fund_code or start_date is None or end_date is None:
        return None
    start_date, end_date = _as_date(start_date), _as_date(end_date)
    now = datetime.now()
    probes = [p for p in read_probes(db, fund_code)
              if _fresh(p, now.date(), KEEP_DAYS)]        # 太旧的丢掉
    probes.append({'start': start_date, 'end': end_date,
                   'source_rows': int(source_rows or 0),
                   'checked_at': now.isoformat(timespec='seconds')})
    probes = _merge_adjacent(probes)[-MAX_PROBES:]
    payload = json.dumps({'version': 2,
                          'probes': [{k: (v.isoformat() if isinstance(v, date) else v)
                                      for k, v in p.items()} for p in probes]},
                         ensure_ascii=False)
    try:
        row = db.query(SystemConfig).filter(
            SystemConfig.config_key == proof_key(fund_code)).first()
        if row is None:
            row = SystemConfig(config_key=proof_key(fund_code), config_value=payload,
                               description='数据源历史净值探测凭据（S7-2 结构性不可验归因）')
            db.add(row)
        else:
            row.config_value = payload
        db.flush()
        return probes
    except Exception:
        # 并发首次写会撞 config_key 唯一约束：这一条凭据丢了无所谓（明天会再问一次），
        # 但**不能**把会话留在 aborted 状态 —— 交回调用方决定回滚，这里只标记失败。
        return None


def describe(probe) -> str:
    """提示语用：只描述**这一次**探测盖住的区间，不再拿并集糊弄。"""
    if not probe:
        return ''
    return ('数据源在 %s~%s 内给到 %d 条净值（核验于 %s）'
            % (probe['start'], probe['end'], probe.get('source_rows', 0),
               (probe.get('checked_at') or '?')[:10]))
