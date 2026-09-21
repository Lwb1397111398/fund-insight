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
- 写入用**调用方的会话 + savepoint，只 flush 不 commit**：库代码不该替调用方提交事务，
  而 flush 后对象就不在 `Session.new/dirty` 里了，所以额外在 `db.info['proof_writes']`
  记一笔，让调用方知道"这次真的写了东西"（第 11 轮 M-A）；
- 合并只发生在**相交/首尾相接且两边都是空答复**的区间之间；非空答复永不合并（M-B）；
- 空答复（0 条）用更短的 TTL：现在 `None` 已经把"没问到"分出去了，但"合法信封 + 空列表"
  仍可能是限流页，2 天后允许再问一次。
"""
import json
import logging
from datetime import date, datetime, timedelta

logger = logging.getLogger(__name__)

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
    if not isinstance(items, list):
        return []          # `{"probes": 5}` 这种脏值不能让整批验证崩掉（第 11 轮 MINOR-3）
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
    if start_date is None or end_date is None or start_date > end_date:
        return None        # 倒挂窗口（脏数据）不许被任何凭据"盖住"（第 11 轮 MINOR-4）
    today = _as_date(today) or date.today()
    hits = [p for p in probes or []
            if p['start'] <= start_date and p['end'] >= end_date and _fresh(p, today, _ttl_for(p))]
    return max(hits, key=lambda p: p['checked_at']) if hits else None


def fresh(db, fund_code: str, start_date: date, end_date: date, today: date = None):
    if db is None or not fund_code:
        return None
    return covering_probe(read_probes(db, fund_code), _as_date(start_date),
                          _as_date(end_date), today)


def _merge_adjacent(probes):
    """合并**首尾相接或相交**的区间，但只合并"两边都是空答复"的。

    非空答复绝不并：`[07-01,08-31] 21 条` + `[08-31,09-30] 21 条` 并成
    `[07-01,09-30] 21 条` 会直接破掉"单次探测完整包含"这条不变量
    （第 11 轮 M-B：等于把 BLOCKER-2 的口子留了一半，条数还变成编的）。
    合并后的 `checked_at` 取**较旧**的那个，让老的那半段不能借新探测的 TTL 续命。
    """
    out = []
    for p in sorted(probes, key=lambda x: (x['start'], x['end'])):
        last = out[-1] if out else None
        touching = last and p['start'] <= last['end'] + timedelta(days=1)
        both_empty = last and not (last.get('source_rows') or 0) and not (p.get('source_rows') or 0)
        if touching and both_empty:
            last['end'] = max(last['end'], p['end'])
            if p['checked_at'] < last['checked_at']:
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
                   # 微秒精度：同一秒内连写多条探测时，"最近问过"的排序不能有并列
                   # （跑批/单测里一次就写几十条，秒级时间戳会让上限裁错人）
                   'checked_at': now.isoformat()})
    probes = _merge_adjacent(probes)
    # 超上限时保留**最近问过**的那些：按 start 排序再截尾会把老窗口的探测丢掉，
    # 而那些窗口正是每天要重问的（单只 515000 就有 77 个互不相交的窗口）
    # —— 第 11 轮 M-C。落盘前再按 start 排序，读侧遍历与展示更直观。
    if len(probes) > MAX_PROBES:
        dropped = len(probes) - MAX_PROBES
        probes = sorted(probes, key=lambda p: (p['checked_at'], p['start']),
                        reverse=True)[:MAX_PROBES]
        logger.warning('基金 %s 凭据超出 %d 条上限，丢掉最久的 %d 条（这些窗口会再问一次）'
                       % (fund_code, MAX_PROBES, dropped))
    probes = sorted(probes, key=lambda p: (p['start'], p['end']))
    payload = json.dumps({'version': 2,
                          'probes': [{k: (v.isoformat() if isinstance(v, date) else v)
                                      for k, v in p.items()} for p in probes]},
                         ensure_ascii=False)
    try:
        row = db.query(SystemConfig).filter(
            SystemConfig.config_key == proof_key(fund_code)).first()
        # 写进调用方的会话、只 flush：调用方 rollback 时凭据一起消失（两条路径都在
        # 同一事务里，SQLite/PG 语义一致）。第 12 轮 MAJOR-5 指出用 SAVEPOINT 反而破这个
        # 一致性 —— SQLite 上"没有外层事务时 SAVEPOINT 自己就是事务"，RELEASE 等于提交，
        # 于是回滚后凭据留下、会话认知与库不一致，所以这里不使用 savepoint。
        #
        # 已知遗留（待 PG 实测）：并发首次插入会撞 config_key 唯一约束，PG 下事务会进入
        # aborted。现存的并发保护是 `prediction_verify_task` 的进程锁 + advisory xact lock，
        # 同一时刻只有一个批量验证进程；真要放开并发，这里得换成 ON CONFLICT DO UPDATE。
        if row is None:
            row = SystemConfig(config_key=proof_key(fund_code), config_value=payload,
                               description='数据源历史净值探测凭据（S7-2 结构性不可验归因）')
            db.add(row)
        else:
            row.config_value = payload
        db.flush()
        # 让调用方知道"这次真的写了东西"：flush 会把对象从 Session.new/dirty 里清掉，
        # 只看那两个集合会漏提交（第 11 轮 M-A）
        db.info['proof_writes'] = int(db.info.get('proof_writes') or 0) + 1
        return probes
    except Exception:
        return None


def describe(probe) -> str:
    """提示语用：只描述**这一次**探测盖住的区间，不再拿并集糊弄。"""
    if not probe:
        return ''
    return ('数据源在 %s~%s 内给到 %d 条净值（核验于 %s）'
            % (probe['start'], probe['end'], probe.get('source_rows', 0),
               (probe.get('checked_at') or '?')[:10]))
