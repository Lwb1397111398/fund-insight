# -*- coding: utf-8 -*-
"""「向数据源要过这段历史、源端确实没有」的凭据（负结果缓存）。

为什么要有（S7-2 / 计划案 S7-b）：要把一条预测归成"结构性不可验"，前提必须是
**真的向数据源请求过那个区间**。以前这句话是没证明过的断言 ——
`FundDataManager.backfill_history_range` 在"本地最早净值 ≤ 起点且窗口够密"时会
early-return，压根没发请求；反过来若不设缓存，Cron 又会每天为同样的窗口重发请求。
两头都要顾，所以按基金存一条"已问过的区间 + 源端给了几条 + 核验时间"：
请求前先看它能不能盖住本次窗口，请求后把结果记下（给了几条也记，见下）。

实现约束：
- 存进已有的 `system_config`（键 `nav_backfill_proof:<基金代码>`），**不加新表新列**，
  所以不牵扯生产迁移；
- 写入用**调用方的会话且只 flush**：缓存不该把调用方事务里没写完的东西一起提交；
  提交时机由调用方决定（验证侧补拉那一步会立刻提交，否则本次验证一失败回滚，
  凭据就跟着丢了，明天又重问一遍）；
- 源端"一条都没给"和"只给了几条但填不满窗口"都要记 —— 两者都是"再问一次也一样"。
"""
import json
from datetime import date, datetime, timedelta

KEY_PREFIX = 'nav_backfill_proof:'
# 多久之后允许再问一次数据源：基金历史可能被补录，缓存不能是永久免检
TTL_DAYS = 7


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else None


def proof_key(fund_code: str) -> str:
    return KEY_PREFIX + (fund_code or '')


def read_proof(db, fund_code: str):
    """返回 `{'start': date, 'end': date, 'checked_at': str, 'source_rows': int}` 或 None。"""
    from src.models.database import SystemConfig

    if db is None or not fund_code:
        return None
    row = db.query(SystemConfig).filter(
        SystemConfig.config_key == proof_key(fund_code)).first()
    if not row or not row.config_value:
        return None
    try:
        data = json.loads(row.config_value)
        start = _as_date(date.fromisoformat(str(data.get('start'))))
        end = _as_date(date.fromisoformat(str(data.get('end'))))
    except Exception:
        return None
    if start is None or end is None:
        return None
    return {'start': start, 'end': end,
            'checked_at': str(data.get('checked_at') or ''),
            'source_rows': int(data.get('source_rows') or 0)}


def covers(proof, start_date: date, end_date: date) -> bool:
    return bool(proof) and proof['start'] <= start_date and proof['end'] >= end_date


def fresh(db, fund_code: str, start_date: date, end_date: date,
          today: date = None, ttl_days: int = TTL_DAYS):
    """盖得住本次窗口、且没过期的负凭据；否则 None（该去问一次数据源）。"""
    proof = read_proof(db, fund_code)
    if not proof or not covers(proof, start_date, end_date):
        return None
    checked_at = proof.get('checked_at')
    try:
        checked = datetime.fromisoformat(checked_at).date()
    except Exception:
        return None
    if (_as_date(today) or date.today()) - checked > timedelta(days=ttl_days):
        return None
    return proof


def record_probe(db, fund_code: str, start_date: date, end_date: date,
                 source_rows: int = 0):
    """记下"已经向数据源要过 [start,end]，它给出 `source_rows` 条"，区间按**并集**放宽。

    源端给了几条都要记：给了 3 条而窗口需要 5 条时，再问一次也还是那 3 条
    （158038 实测就是这种：源端只提供 09-07 那一条）。不记就会每天重问一遍、
    每天还是"数据不足"。历史日期是不可变的，所以要过的区间在 TTL 内不必再问。

    并集而不是覆盖，是因为同一天里多条预测问的是互不包含的窗口
    （实测 515440：07-27~07-28、07-23~07-30、08-05~08-12……）；
    只存最大区间，第二次之后基本就不必再打接口了。

    **只用调用方的会话、只 flush 不 commit**：库代码擅自 commit 会把调用方事务里
    没写完的东西一起提交（S4 就为此踩过一次）；要不要提交由调用方决定
    （验证侧现在就会立刻提交，否则这次验证一失败回滚，凭据就跟着丢了）。
    """
    from src.models.database import SystemConfig

    if db is None or not fund_code or start_date is None or end_date is None:
        return None
    start_date, end_date = _as_date(start_date), _as_date(end_date)
    try:
        existing = read_proof(db, fund_code)
        wide_start = min(start_date, existing['start']) if existing else start_date
        wide_end = max(end_date, existing['end']) if existing else end_date
        payload = json.dumps({'start': wide_start.isoformat(), 'end': wide_end.isoformat(),
                              'checked_at': datetime.now().isoformat(timespec='seconds'),
                              'source_rows': int(source_rows or 0)}, ensure_ascii=False)
        row = db.query(SystemConfig).filter(
            SystemConfig.config_key == proof_key(fund_code)).first()
        if row is None:
            row = SystemConfig(config_key=proof_key(fund_code), config_value=payload,
                               description='数据源历史净值负凭据（S7-2 结构性不可验归因）')
            db.add(row)
        else:
            row.config_value = payload
            row.description = row.description or '数据源历史净值负凭据（S7-2 结构性不可验归因）'
        db.flush()
        return {'start': wide_start, 'end': wide_end, 'source_rows': source_rows}
    except Exception:
        return None


def describe(proof) -> str:
    """给验证提示语用的一句话：把"证明过"这件事说清楚，顺带交代核验时间。

    `source_rows` 是**末次**核验那条窗口的条数（区间按并集放宽过），所以措辞用
    "只给到 …（末次核验于 …）"，不含糊成"整个区间只有这么多"。
    """
    if not proof:
        return ''
    return ('数据源在 %s~%s 内只给到 %d 条净值（末次核验于 %s）'
            % (proof['start'], proof['end'], proof.get('source_rows', 0),
               (proof.get('checked_at') or '?')[:10]))
