"""
基金数据模块 - 支持每日自动抓取和历史存储
"""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import re
import threading
import time
import unicodedata
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional
from sqlalchemy.orm import Session
import sys
import os
import logging

if __name__ == "__main__":
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

from src.core.config import config
from src.models.database import FundInfo, FundHistory, SessionLocal

logger = logging.getLogger(__name__)

# pingzhongdata 的"页面未找到"页是 HTTP 200 + 固定正文，只能按字节匹配：
# 正文 title 是 GBK 乱码，`'页面未找到' in response.text` 永远不成立。
PINGZHONG_NOT_FOUND_SIGNATURE = b'\xe9\xa1\xb5\xe9\x9d\xa2\xe6\x9c\xaa'  # 页面未

_FUND_ROSTER = None
_ROSTER_LOCK = threading.Lock()

_IDENTITY_STRIP = re.compile(r"[\s\-—·、,，。'\"!！()\[\]（）【】]")


def _probe_clock(today):
    """凭据时间戳与注入的 `today` 用同一个时钟（第 17 轮 MINOR-1）。

    不传 `today` 时返回 None ⇒ `record_probe` 自己打墙上时钟（生产行为不变）；
    固定日期的回放/用例里则把戳记落在那一天，免得种下一条"相对今天永远对不上"的凭据。
    """
    if today is None:
        return None
    if isinstance(today, datetime):
        return today
    return datetime.combine(today, datetime.min.time())


def normalize_for_identity(name) -> str:
    """把名称折成可比较形式：全角转半角（京东方Ａ→京东方A）、去空白与分隔符（报 喜 鸟→报喜鸟）。

    归一细节直接决定阈值是否安全：id 25 j=0.467、id 79 j=0.40 都贴着 0.35/0.45 边界，
    任何一处归一改动都可能把正确行翻成硬拒，所以由
    tests/fixtures/name_identity_golden.json 逐项钉死。
    """
    if not name:
        return ''
    return _IDENTITY_STRIP.sub('', unicodedata.normalize('NFKC', str(name)).lower())


def jaccard_name(a, b) -> float:
    """字符集 Jaccard：语序无关，用来判"这两个名字指的是不是同一只产品"。

    不用二字 Dice——`华宝中证医疗ETF` vs `医疗ETF华宝` 是同一只基金，Dice 只有
    0.33 会把正确映射判成错行；Jaccard 给 0.78，而"京东方Ａ vs 大成添利宝货币B"
    （股票名撞上同码货基）仍是 0.0，分离度足够。
    """
    ca, cb = set(normalize_for_identity(a)), set(normalize_for_identity(b))
    if not ca or not cb:
        return 0.0
    return round(len(ca & cb) / len(ca | cb), 4)


def is_future_nav(day, today=None) -> bool:
    """这一行的净值日期是不是"还没到"（按北京时间自然日，与 `current_as_of()` 同一把尺子）。"""
    if not isinstance(day, date):
        return False
    if today is None:
        from src.services.prediction_lifecycle import current_as_of
        today = current_as_of()
    return day > today


def usable_history_rows(fund_code: str, rows: List[Dict], today=None) -> List[Dict]:
    """上游给的净值行进库前的"防未来函数"门 —— **只在取数入口这一处实现**。

    为什么必须有（2026-09-25 实测）：`000725`（大成添利宝货币B，货币型）的东财 lsjz 直接把
    `FSRQ` 签成 09-26 / 09-27，而那天是 09-25 ⇒ 一次「更新基金净值」就往生产 `fund_history`
    写了 2 条晚于当天的行，并把 `fund_info.nav_date` 也写成 09-27。验证侧从第 13 轮起就有
    "不取目标日之后的行情"这道门，**入库侧一直没有** —— 于是脏数据是从写入那一刻进来的，
    而不是从判定那一刻。

    三个 `FundHistory(...)` 写入点（`fund_api.update_fund_history`、`fund_api` 的回填腿、
    `fund_sync_manager._update_fund_history`）都在两个取数入口下游，所以门加在入口，
    不复制三份。丢掉几条必须说出来：静默丢弃和不丢一样难查。
    """
    kept = [r for r in rows if not is_future_row(r, today)]
    dropped = len(rows) - len(kept)
    if dropped:
        logger.warning('[净值门] %s 上游给了 %d 条晚于 %s 的净值行，已丢弃不入库：%s'
                       % (fund_code, dropped,
                          today if today is not None else '今天（北京）',
                          ', '.join(sorted({str(r.get('date')) for r in rows if is_future_row(r, today)}))))
    return kept


def is_future_row(row: Dict, today=None) -> bool:
    day = row.get('date') if isinstance(row, dict) else None
    if isinstance(day, datetime):
        day = day.date()
    return is_future_nav(day, today)


class FundAPI:
    """天天基金API封装"""
    
    def __init__(self):
        self.base_url = "http://fundgz.1234567.com.cn"
        self.search_url = "http://fundsuggest.eastmoney.com/FundSearch/api/FundSearchAPI.ashx"
        self.history_url = "http://api.fund.eastmoney.com/f10/lsjz"
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Referer': 'https://fund.eastmoney.com/'
        }
        self.timeout = config.FUND_API_TIMEOUT

        self.session = requests.Session()
        retry_strategy = Retry(
            total=config.FUND_API_MAX_RETRIES,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def close(self):
        """关闭 Session，释放连接池资源"""
        if self.session:
            self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
    
    def _history_fallback_info(self, fund_code: str) -> Optional[Dict]:
        """fundgz 实时接口失效时，用最近一天历史净值兜底。

        名称/类型刻意返回 None，避免覆盖库内已有字段。
        """
        history = self.get_fund_history(fund_code, days=1)
        if not history:
            return None

        latest = history[0]
        nav_date = latest.get('date')
        return {
            'fund_code': fund_code,
            'fund_name': None,
            'fund_type': None,
            'nav': latest.get('nav'),
            'nav_date': (
                nav_date.strftime('%Y-%m-%d')
                if hasattr(nav_date, 'strftime')
                else (str(nav_date) if nav_date else None)
            ),
            'estimate_nav': None,
            'estimate_date': None,
            'day_growth': latest.get('growth'),
        }

    def get_fund_info(self, fund_code: str, allow_fallback: bool = True) -> Optional[Dict]:
        """获取基金实时信息

        返回数据包含：
        - nav/nav_date: 实际净值和净值日期（来自 jzrq/dwjz）
        - estimate_nav/estimate_date: 估值和估值时间（来自 gsz/gztime）
        - day_growth: 估值涨跌幅

        fundgz 实时接口失效（404 HTML / 超时 / 网络错误）时，
        自动用历史净值接口兜底，保证基金更新链路可用。

        allow_fallback=False 时仅尝试实时接口，失效直接返回 None，
        供抓取验证等场景复用，避免一次验证触发多次历史接口调用（防限流）。
        """
        try:
            url = f"{self.base_url}/js/{fund_code}.js"
            response = self.session.get(url, headers=self.headers, timeout=self.timeout)
            response.encoding = 'utf-8'

            text = response.text
            if 'jsonpgz' in text:
                match = re.search(r'jsonpgz\((.+)\)', text)
                if match:
                    data = json.loads(match.group(1))

                    # 实际净值日期（如 "2026-06-04"）
                    actual_nav_date = data.get('jzrq', '')
                    # 估值时间（如 "2026-06-05 15:00"）
                    estimate_time = data.get('gztime', '').split(' ')[0]

                    return {
                        'fund_code': data.get('fundcode'),
                        'fund_name': data.get('name'),
                        # 实际净值（收盘后确认的净值）
                        'nav': float(data.get('dwjz', 0) or 0),
                        'nav_date': actual_nav_date,
                        # 估值数据（盘中实时）
                        'estimate_nav': float(data.get('gsz', 0) or 0),
                        'estimate_date': estimate_time,
                        # 涨跌幅使用估值涨跌幅
                        'day_growth': float(data.get('gszzl', 0) or 0),
                        'fund_type': data.get('fundtype', '')
                    }

            # fundgz 接口失效时（返回 404 页面等），用历史净值兜底
            logger.warning(f"基金{fund_code}实时接口无有效数据，尝试历史净值兜底")
            return self._history_fallback_info(fund_code) if allow_fallback else None
        except requests.exceptions.Timeout:
            logger.warning(f"获取基金{fund_code}信息超时，尝试历史净值兜底")
            return self._history_fallback_info(fund_code) if allow_fallback else None
        except requests.exceptions.RequestException as e:
            logger.warning(f"获取基金{fund_code}网络错误: {e}，尝试历史净值兜底")
            return self._history_fallback_info(fund_code) if allow_fallback else None
        except Exception as e:
            logger.error(f"获取基金{fund_code}信息失败: {e}，尝试历史净值兜底")
            if not allow_fallback:
                return None
            try:
                return self._history_fallback_info(fund_code)
            except Exception:
                return None
    
    def get_fund_history(self, fund_code: str, days: int = 30) -> List[Dict]:
        """获取基金历史净值"""
        try:
            params = {
                'fundCode': fund_code,
                'pageIndex': 1,
                'pageSize': min(days, 60),
                'startDate': '',
                'endDate': '',
                'perFundType': ''
            }
            
            headers = self.headers.copy()
            headers['Referer'] = f'https://fund.eastmoney.com/f10/jjjz_{fund_code}.html'
            
            response = self.session.get(
                self.history_url,
                params=params,
                headers=headers,
                timeout=self.timeout
            )
            response.encoding = 'utf-8'
            data = response.json()
            
            results = []
            if 'Data' in data and 'LSJZList' in data['Data']:
                lsjz_list = data['Data']['LSJZList']
                
                if not lsjz_list:
                    logger.debug(f"基金 {fund_code} 历史净值列表为空")
                    return []
                
                for item in lsjz_list:
                    try:
                        nav_date = datetime.strptime(item.get('FSRQ'), '%Y-%m-%d').date()
                        results.append({
                            'date': nav_date,
                            'nav': float(item.get('DWJZ', 0) or 0),
                            'growth': float(item.get('JZZZL', 0) or 0)
                        })
                    except Exception as e:
                        logger.warning(f"解析基金 {fund_code} 历史净值数据失败: {e}, 数据项: {item}")
                        continue
            else:
                logger.warning(f"基金 {fund_code} API返回数据格式异常")
            
            return usable_history_rows(fund_code, results)

        except Exception as e:
            logger.error(f"获取基金{fund_code}历史数据失败: {e}")
            return []

    def get_fund_history_range(
        self,
        fund_code: str,
        start_date: date,
        end_date: date,
        max_pages: int = 40,
    ) -> Optional[List[Dict]]:
        """按日期区间分页获取基金历史净值（用于补拉数据库缺失的早期数据）。

        与 get_fund_history 不同：这里显式传 startDate/endDate 并自动翻页，
        直到取完区间内全部数据（或达到 max_pages 上限，防止异常情况下死循环）。

        Args:
            fund_code: 基金代码
            start_date: 区间起始日（含）
            end_date: 区间结束日（含）
            max_pages: 最大翻页数兜底（实测接口每页最多 20 条，40 页约 800 个交易日）

        Returns:
            [{'date': date, 'nav': float, 'growth': float}, ...]，按接口返回顺序；
            **返回 `None` 表示"这次没问到"**（超时、非 JSON、`ErrCode≠0`、`Data`/`LSJZList`
            形状不对、有行解析失败、翻页触顶未取满 `TotalCount`）。
            这个区分是 S7-b 第 10~12 轮评审逼出来的：以前传输失败与"源端确实没有"都返回 `[]`，
            于是 `backfill_history_range` 会把一次抖动写成"已证明该区间无净值"的凭据，
            并被后续 TTL 期内引用 —— 真数据就永远拿不回来了。
        """
        if not start_date or not end_date or start_date > end_date:
            return None

        # 注意：东财 lsjz 接口实测每页最多返回 20 条（请求更大的 pageSize 也会被截断），
        # 因此按 20 条/页翻页，并以"返回不足 20 条"作为结束标志。
        page_size = 20
        results: List[Dict] = []
        complete = False          # 只有"整段区间问完了"才允许调用方把它当证据
        for page_index in range(1, max_pages + 1):
            try:
                params = {
                    'fundCode': fund_code,
                    'pageIndex': page_index,
                    'pageSize': page_size,
                    'startDate': start_date.strftime('%Y-%m-%d'),
                    'endDate': end_date.strftime('%Y-%m-%d'),
                    'perFundType': ''
                }

                headers = self.headers.copy()
                headers['Referer'] = f'https://fund.eastmoney.com/f10/jjjz_{fund_code}.html'

                response = self.session.get(
                    self.history_url,
                    params=params,
                    headers=headers,
                    timeout=self.timeout
                )
                response.encoding = 'utf-8'
                data = response.json()

                if not isinstance(data, dict) or 'Data' not in data:
                    # 限流页/错误页也可能 200 + 可解析 JSON：这种"没有 Data 键"
                    # 不能读成"该区间没有净值"，只能读成"这次没问到"。
                    logger.error(f"基金 {fund_code} 历史净值第{page_index}页响应不是合法信封")
                    break
                payload = data.get('Data')
                err_code = data.get('ErrCode')
                if err_code not in (None, 0, '0'):
                    # 接口自己报了错（限流、参数、风控）：答案未知，不能记凭据
                    logger.error(f"基金 {fund_code} 历史净值第{page_index}页 ErrCode={err_code}")
                    break
                if not isinstance(payload, dict) or not isinstance(payload.get('LSJZList'), list):
                    # 实测（2026-09-21 打真接口三种"无数据"）真·没有数据是
                    # `Data` 为 dict + `LSJZList: []` + `ErrCode=0`。
                    # 所以"缺键 / 值为 null / 值不是列表"这一整族都只能判成"没问到"
                    # —— 第 12 轮 BLOCKER-1：上一版只查了键存在，`LSJZList: null` 照样漏过去。
                    logger.error(f"基金 {fund_code} 历史净值第{page_index}页 LSJZList 不是列表")
                    break
                lsjz_list = payload['LSJZList']
                # `TotalCount` 只在它是个非负 int 时才当总行数用；缺省**不许**回落成"当页行数"
                # （那样首页就满足 `len(results) >= total`，翻页保护整体失效，比改动前更差 ——
                # 第 12 轮 BLOCKER-2）。拿不到总数时只靠"不足一页"判完。
                total_raw = data.get('TotalCount')
                total = (total_raw if isinstance(total_raw, int) and not isinstance(total_raw, bool)
                         and total_raw >= 0 else None)
                if not lsjz_list:
                    complete = True          # 问完了，这段确实没有
                    break

                page_ok = 0
                for item in lsjz_list:
                    try:
                        nav_date = datetime.strptime(item.get('FSRQ'), '%Y-%m-%d').date()
                        results.append({
                            'date': nav_date,
                            'nav': float(item.get('DWJZ', 0) or 0),
                            'growth': float(item.get('JZZZL', 0) or 0)
                        })
                        page_ok += 1
                    except Exception as e:
                        logger.warning(f"解析基金 {fund_code} 补拉净值失败: {e}, 数据项: {item}")
                        continue

                # 有一行没解析出来就是**丢了某一天**，此时整段答案都不完整：
                # 继续翻页只会把这个洞留在凭据里（第 12 轮 BLOCKER-1 第 3 条路径）
                if page_ok < len(lsjz_list):
                    logger.error(f"基金 {fund_code} 第{page_index}页有 "
                                 f"{len(lsjz_list) - page_ok} 行解析失败，不判为问完")
                    break

                # 取够接口自报的 `TotalCount` 才算问完；没有总数时只靠"不足一页"判到底
                if (total is not None and len(results) >= total) or len(lsjz_list) < page_size:
                    complete = True
                    break
                time.sleep(0.2)
            except Exception as e:
                logger.error(f"补拉基金{fund_code}历史数据失败(第{page_index}页): {e}")
                break

        # 循环走完却没置 complete ⇒ 翻页触顶、区间没问完，同样按"没问到"处理
        # 回填腿也过同一道净值门（入口只此两处，三个写入点都在下游）
        return usable_history_rows(fund_code, results) if complete else None

    def verify_fund_fetchable(self, fund_code: str, input_name: Optional[str] = None,
                             probe_stock: bool = False, fill_name: bool = True) -> Dict:
        """验证基金代码是否能从数据源正常抓取。

        依次调用实时信息接口与历史净值接口，返回结构化验证结果。
        供"板块映射审查 / 添加基金"流程在保存前做抓取可行性检查。

        Args:
            fund_code: 6 位数字基金代码
            input_name: 用户填写的基金名称（可选，仅用于回填对比，不作为通过依据）

        Returns:
            dict 包含：
            - ok: 是否可正常抓取（信息或历史至少其一有效）
            - code/input_name/api_name/api_nav/nav_date/history_count
            - message: 面向用户的一句话结论
        """
        code = (fund_code or '').strip()
        if not re.fullmatch(r'\d{6}', code):
            return {
                'ok': False,
                'is_strict_ok': False,
                'kind': 'unknown',
                'fund_type': None,
                'official_name': None,
                'code': code,
                'input_name': input_name,
                'api_name': None,
                'api_nav': None,
                'nav_date': None,
                'history_count': 0,
                'message': '基金代码格式不正确，应为 6 位数字'
            }

        info = None
        try:
            # 仅尝试实时接口（不触发历史兜底），用于补充官方名称/实时净值
            info = self.get_fund_info(code, allow_fallback=False)
        except Exception as e:
            logger.warning(f"验证基金{code}时信息接口异常: {e}")

        history: List[Dict] = []
        try:
            # 历史净值是判断"能否抓取"的权威依据，只调一次。
            # 窗口用 30 天而不是 7 天：7 天遇上节假日只有 4-5 条，会让严格判据
            # 在周末随机翻转（同一只正常基金一会儿合格一会儿不合格）。
            history = self.get_fund_history(code, days=30)
        except Exception as e:
            logger.warning(f"验证基金{code}时历史接口异常: {e}")

        nav = (info or {}).get('nav')
        api_name = (info or {}).get('fund_name')
        nav_date = (info or {}).get('nav_date')
        # 场内 ETF 常常拿不到实时名称（jsonpgz 为空），但官方名是"验证抓取"面板和
        # 后续相关性判断的关键输入。这里必须用**纯基金域**来源：搜索接口是混合证券
        # 搜索，按代码反查会把股票名（000938→紫光股份）当成基金官方名返回。
        if not api_name and fill_name:
            domain = self.get_fund_domain_name(code)
            if domain.get('status') == 'ok':
                api_name = domain.get('name')
        # 实时接口没给净值日期时，用历史最新一条兜底展示
        if not nav_date and history:
            latest_date = history[0].get('date')
            nav_date = (
                latest_date.strftime('%Y-%m-%d')
                if hasattr(latest_date, 'strftime')
                else (str(latest_date) if latest_date else None)
            )
        ok = bool((nav and nav > 0) or history)

        if ok:
            if api_name:
                message = f'验证通过：{api_name}'
            else:
                message = '验证通过：可抓取净值数据（接口未返回名称）'
        elif info is None and not history:
            message = '验证失败：接口无有效数据，该基金可能已停牌/清盘或代码有误'
        else:
            message = '验证失败：未抓取到有效净值数据'

        return {
            'ok': ok,
            # is_strict_ok 是 agent 用的严格判据：只"接口有返回"不够，要能拿到可用净值。
            # 旧 ok 语义保持不变（tests/unit/test_fund_verify.py 断言"1 条历史也算 ok"，
            # 且前端"验证抓取"面板依赖它），新增字段而不是改老字段。
            'is_strict_ok': bool((nav and nav > 0) or len(history) >= 5),
            # kind：天天基金这两个接口都只服务基金域，有数据即基金；
            # 无数据时再去股票域探测，避免把 6 位股票代码当成"基金抓不到"。
            'kind': self._classify_code_kind(code, ok, probe_stock=probe_stock),
            'fund_type': (info or {}).get('fund_type') or None,
            'official_name': api_name,
            'code': code,
            'input_name': input_name,
            'api_name': api_name,
            'api_nav': nav,
            'nav_date': nav_date,
            'history_count': len(history),
            'message': message
        }

    def _classify_code_kind(self, code: str, ok: bool, probe_stock: bool = False) -> str:
        """判定 6 位代码属于基金还是股票。

        000001 既是深市股票（平安银行）也是场外基金（华夏成长）的代号，**不能靠码段猜**，
        只能看"基金域有没有数据"。股票域探测要多打一次外站请求，因此默认不探
        （`probe_stock=False`）——只有 agent 匹配链路需要区分"基金抓不到"与"这是股票"。
        """
        if ok:
            return 'fund'
        if not probe_stock:
            return 'unknown'
        try:
            response = self.session.get(
                'https://push2.eastmoney.com/api/qt/stock/get',
                params={
                    'secid': ('1.' if code.startswith(('5', '6', '9')) else '0.') + code,
                    'fields': 'f57,f58,f43',
                    'invt': '2',
                },
                timeout=self.timeout,
            )
            data = (response.json() or {}).get('data')
            if data and (data.get('f57') or data.get('f58')):
                return 'stock'
        except Exception as e:
            logger.debug(f"股票域探测 {code} 失败（按未知处理）: {e}")
        return 'unknown'

    def verify_funds_batch(self, items: List[Dict], delay: float = 0.3) -> Dict:
        """批量验证多只基金能否从数据源抓取，用于一键排查问题基金。

        Args:
            items: [{'sector_name','fund_code','fund_name'}, ...]
            delay: 相邻两次网络验证之间的间隔秒数，降低被数据源限流概率。

        Returns:
            dict：total / ok_count / problem_count / results / problems
            - results: 每个输入条目一条（含验证结果与所属板块）
            - problems: 仅抓取失败的条目
        相同基金代码只发起一次网络验证，结果复用到所有引用它的板块。
        """
        cache: Dict[str, Dict] = {}
        results: List[Dict] = []
        for idx, item in enumerate(items or []):
            code = (item.get('fund_code') or '').strip()
            name = item.get('fund_name') or ''
            sector = item.get('sector_name') or ''
            if code in cache:
                verify = dict(cache[code])
            else:
                # 仅在真正发起新的一次网络验证前做节流间隔
                if cache and delay > 0:
                    time.sleep(delay)
                verify = self.verify_fund_fetchable(code, name, fill_name=False)
                cache[code] = verify
            row = {
                'sector_name': sector,
                'fund_code': verify.get('code') or code,
                'fund_name': name,
                'ok': verify.get('ok', False),
                'api_name': verify.get('api_name'),
                'api_nav': verify.get('api_nav'),
                'nav_date': verify.get('nav_date'),
                'history_count': verify.get('history_count', 0),
                'message': verify.get('message', ''),
            }
            results.append(row)

        problems = [r for r in results if not r['ok']]
        return {
            'total': len(results),
            'checked_codes': len(cache),
            'ok_count': len(results) - len(problems),
            'problem_count': len(problems),
            'results': results,
            'problems': problems,
        }

    def search_fund(self, keyword: str) -> Optional[List[Dict]]:
        """搜索基金。

        注意：这个接口是**混合证券搜索**，返回值里既有基金也有股票
        （实测 `000938` 只回"紫光股份"、`液冷` 回"冰山冷热/五 粮 液"），
        所以调用方必须用 `is_fund` / `category_desc` 过滤，不能拿到结果就当基金。
        查询键要用**原始名称**：'京东方Ａ' 原始名查得到，NFKC 归一后反而 0 条命中。

        Returns:
            None 表示请求/解析失败（与"查无结果"的 [] 区分开——身份判定要靠这个区别
            决定能不能下"这不是基金"的结论，混为一谈会让站点故障静默变成批量降级）。
        """
        try:
            params = {
                'm': '1',
                'key': keyword
            }
            response = self.session.get(
                self.search_url,
                params=params,
                timeout=self.timeout
            )
            response.encoding = 'utf-8'
            data = response.json()

            results = []
            for item in (data.get('Datas') or [])[:10]:
                category = item.get('CATEGORYDESC') or ''
                results.append({
                    'fund_code': item.get('CODE'),
                    'fund_name': item.get('NAME'),
                    'fund_type': item.get('FUNDTYPE', ''),
                    'category_desc': category,
                    'is_fund': category == '基金' or bool(item.get('FundBaseInfo')),
                })
            return results
        except Exception as e:
            logger.error(f"搜索基金失败: {e}")
            return None

    def load_fund_roster(self, refresh: bool = False) -> Dict:
        """一次性拉取基金域名册（约 3.1MB / 2.7 万条），用于"码→官方名"和"名→码"。

        为什么用名册而不是逐个代码 pingzhongdata：整表 1 个请求，且**离线**就能把
        "德明利/哈三联/京东方Ａ 这些名字根本不是基金"判掉。
        名册不是全集（实测缺 000938/002154/002261/004529，这些都能由单码接口解析），
        所以**名册查不到永远不作为负证据**，只回落单码。
        """
        global _FUND_ROSTER
        with _ROSTER_LOCK:
            if _FUND_ROSTER and not refresh:
                return _FUND_ROSTER
            response = self.session.get(
                'http://fund.eastmoney.com/js/fundcode_search.js', timeout=30)
            text = response.content.decode('utf-8', errors='replace')
            start, end = text.find('['), text.rfind(']')
            if start < 0 or end <= start:
                raise ValueError('fundcode_search.js 解析失败')
            rows = json.loads(text[start:end + 1])
            by_code, codes_by_name = {}, {}
            for row in rows:
                # 行结构：[代码, 拼音缩写, 中文名称, 基金类型, 全拼]——拼音列不是名字，别取错
                if len(row) < 4 or not row[0]:
                    continue
                code, name, ftype = str(row[0]), (row[2] or ''), (row[3] or '')
                by_code[code] = {'name': name, 'fund_type': ftype}
                codes_by_name.setdefault(name, []).append(code)
                codes_by_name.setdefault(normalize_for_identity(name), []).append(code)
            _FUND_ROSTER = {'by_code': by_code, 'codes_by_name': codes_by_name,
                            'loaded_at': time.time(), 'size': len(by_code)}
            return _FUND_ROSTER

    def get_fund_domain_name(self, code: str, use_roster: bool = True) -> Dict:
        """取"这个 6 位码在基金域到底是什么品种"，返回 {status, name, fund_type, source}。

        status: ok（基金域有这只）/ absent（站点明确回答没有此码，确定负证据）/
                error（请求失败，**不能**据此下结论）。

        `use_roster=False` 走单码接口（pingzhongdata），用于"名字本来就是从名册写进去"
        的场景（realign 换标的）：拿名册写名、再拿名册自证身份，Jaccard 恒 1.0，
        是条自我背书的假证据。
        """
        code = (code or '').strip()
        if not re.fullmatch(r'\d{6}', code):
            return {'status': 'absent', 'name': None, 'fund_type': None, 'source': 'bad_code'}
        if use_roster:
            try:
                roster = self.load_fund_roster()
            except Exception as e:
                logger.warning(f"名册加载失败，回落到单码接口: {e}")
                roster = {'by_code': {}, 'codes_by_name': {}}
            hit = roster['by_code'].get(code)
            if hit and hit.get('name'):
                return {'status': 'ok', 'name': hit['name'],
                        'fund_type': hit.get('fund_type'), 'source': 'roster'}
        try:
            response = self.session.get(
                f'http://fund.eastmoney.com/pingzhongdata/{code}.js',
                timeout=self.timeout, allow_redirects=True)
        except Exception as e:
            logger.debug(f"单码基金名查询 {code} 失败: {e}")
            return {'status': 'error', 'name': None, 'fund_type': None, 'source': 'pingzhong'}
        content = response.content or b''
        match = re.search(rb'fS_name\s*=\s*"([^"]+)"', content)
        if match:
            return {'status': 'ok',
                    'name': match.group(1).decode('utf-8', errors='replace'),
                    'fund_type': None, 'source': 'pingzhong'}
        # 404 页是 HTTP 200 + 固定文案，只能按字节判：正文明明是 GBK 乱码 title，
        # 用 `'页面未找到' in response.text` 永远匹配不上，会静默退回"取不到就当没结论"。
        if PINGZHONG_NOT_FOUND_SIGNATURE in content:
            return {'status': 'absent', 'name': None, 'fund_type': None, 'source': 'pingzhong'}
        return {'status': 'error', 'name': None, 'fund_type': None, 'source': 'pingzhong'}


def weekday_capacity(start_date, end_date) -> int:
    """窗口内**最多可能**有多少条净值：只数周一~周五（法定节假日只会更少，不会更多）。

    为什么需要它（第 16 轮 MAJOR-2）：密度门槛必须夹在"窗口物理容量"之内，
    否则永不可达。上一版用**自然日**夹取（`max(1, span_days)`），于是"周六→周一"
    这种容量只有 1 天的窗口仍要求 2 条 ⇒ 永远拉不满、天天真发一次请求
    （终点在 30 天内时负凭据 TTL 只有 1 天，等于无限期日问）。
    """
    days = (end_date - start_date).days + 1
    if days <= 0:
        return 0
    full_weeks, remainder = divmod(days, 7)
    count = full_weeks * 5
    start_weekday = start_date.weekday()
    for offset in range(remainder):
        if (start_weekday + offset) % 7 < 5:
            count += 1
    return count


class FundDataManager:
    """基金数据管理器 - 处理数据库存储和查询"""
    
    def __init__(self):
        self.api = FundAPI()
    
    def update_fund_info(self, fund_code: str, db: Session = None) -> Optional[FundInfo]:
        """更新基金信息到数据库
        
        注意：日涨幅(day_growth)使用历史净值中的实际涨跌幅，而不是估值涨跌幅
        """
        info = self.api.get_fund_info(fund_code)
        if not info:
            return None
        
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True
        
        try:
            fund = db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
            nav_date = None
            if info.get('nav_date'):
                try:
                    nav_date = datetime.strptime(info['nav_date'], '%Y-%m-%d').date()
                except (ValueError, TypeError) as e:
                    logger.warning(f"日期解析失败: {info.get('nav_date')}, 错误: {e}")
            
            # 获取历史净值中的实际涨跌幅（更准确）
            history = self.api.get_fund_history(fund_code, days=1)
            actual_day_growth = None
            actual_nav_date = None
            
            if history:
                latest = history[0]
                actual_day_growth = latest.get('growth')
                try:
                    actual_nav_date = latest.get('date')
                    if isinstance(actual_nav_date, str):
                        actual_nav_date = datetime.strptime(actual_nav_date, '%Y-%m-%d').date()
                except (ValueError, TypeError) as e:
                    logger.debug(f"解析历史净值日期失败: {e}")
            
            # 优先使用历史净值中的实际数据
            day_growth = actual_day_growth if actual_day_growth is not None else info.get('day_growth')
            if actual_nav_date:
                nav_date = actual_nav_date
            # 档案头不许是"还没到的那一天"。第 49 轮 A 席量到：那道 `is_future_nav` 的门以前
            # 只挂在 `update_all_funds_info` 一个 caller 上，而这里（`scheduler.py` 每天调
            # `update_fund_info`）是同一条形状的另一条活路 —— 货币基金会给预签发的净值行
            # （000725 实测在 09-25 给出 09-26/09-27），照抄就把档案头推到将来。
            if is_future_nav(nav_date):
                logger.warning('[净值门] %s 的档案头日期 %s 晚于今天 ⇒ 不回写，保留现有档案头',
                               fund_code, nav_date)
                nav_date = fund.nav_date if fund else None
            
            if fund:
                if info.get('fund_name'):
                    fund.fund_name = info['fund_name']
                if info.get('fund_type'):
                    fund.fund_type = info['fund_type']
                fund.latest_nav = info.get('nav')
                fund.nav_date = nav_date
                fund.day_growth = day_growth
            else:
                fund = FundInfo(
                    fund_code=fund_code,
                    fund_name=info.get('fund_name'),
                    fund_type=info.get('fund_type'),
                    latest_nav=info.get('nav'),
                    nav_date=nav_date,
                    day_growth=day_growth
                )
                db.add(fund)

            # 仅当自建会话时提交；外部传入的 db 由调用方控制事务边界，
            # 避免提前提交调用方 session 上的其它 pending 改动（与 update_fund_history 一致）
            if close_db:
                db.commit()
                db.refresh(fund)
            else:
                db.flush()
            return fund

        except Exception as e:
            logger.error(f"更新基金信息失败: {e}")
            db.rollback()
            return None
        finally:
            if close_db:
                db.close()
    
    def update_fund_history(self, fund_code: str, days: int = 30, db: Session = None) -> int:
        """更新基金历史净值到数据库"""
        history = self.api.get_fund_history(fund_code, days)
        if not history:
            return 0

        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True

        try:
            # 批量查询已存在的日期，避免逐条查询
            existing_dates = set(
                r[0] for r in db.query(FundHistory.nav_date).filter(
                    FundHistory.fund_code == fund_code
                ).all()
            )

            # 批量查询已存在的日期，避免逐条查询
            existing_dates = set(
                r[0] for r in db.query(FundHistory.nav_date).filter(
                    FundHistory.fund_code == fund_code
                ).all()
            )

            # 获取基金信息（只查询一次）
            fund_info = db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
            fund_name = fund_info.fund_name if fund_info else ''

            # 一次性把已存在记录载入内存映射，消除循环内逐条查询的 N+1
            existing_map = {
                r.nav_date: r for r in db.query(FundHistory).filter(
                    FundHistory.fund_code == fund_code,
                    FundHistory.nav_date.in_([it['date'] for it in history if it['date'] in existing_dates])
                ).all()
            } if existing_dates else {}

            count = 0
            for item in history:
                if item['date'] in existing_dates:
                    # 更新已存在的记录（内存查找，无额外查询）
                    existing = existing_map.get(item['date'])
                    if existing:
                        existing.nav = item['nav']
                        existing.day_growth = item['growth']
                else:
                    # 插入新记录
                    record = FundHistory(
                        fund_code=fund_code,
                        fund_name=fund_name,
                        nav_date=item['date'],
                        nav=item['nav'],
                        day_growth=item['growth']
                    )
                    db.add(record)
                    count += 1

            # 计算周涨跌幅和月涨跌幅
            self._calculate_growth_rates(fund_code, db)

            # 仅当使用内部创建的 session 时才提交，外部 session 由调用方管理事务
            if close_db:
                db.commit()
            return count

        except Exception as e:
            logger.error(f"更新历史净值失败: {e}")
            if close_db:
                db.rollback()
            return 0
        finally:
            if close_db:
                db.close()

    def backfill_history_range(
        self,
        fund_code: str,
        start_date: date,
        end_date: date,
        db: Session = None,
        today: date = None,
    ) -> int:
        """按需补拉数据库缺失的历史净值。

        只有当**区间起点缺数据**、或**窗口内已有条数不够密**时才发起网络补拉，
        否则直接返回 0，避免对每笔验证都打数据源接口。
        （以前只看"最早一天"，中间断档永远补不上 —— 见下方注释。）

        `today` 要一路传到凭据判定：TTL 分档与"未来时间戳"防线都按天算，
        不传就让固定日期的回放拿到一个"当时还不存在"的宽限（第 16 轮 m-3 的另一半：
        上一轮只把 today 传到了验证服务那一侧，补拉这道门仍吃墙上时钟）。

        Args:
            fund_code: 基金代码
            start_date: 需要覆盖的最早日期（含）
            end_date: 需要覆盖的最晚日期（含）
            db: 可选外部会话；不传则自建会话并提交

        Returns:
            新增入库的记录数（0 表示无需补拉或补拉无结果）
        """
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True

        try:
            oldest = db.query(FundHistory.nav_date).filter(
                FundHistory.fund_code == fund_code
            ).order_by(FundHistory.nav_date.asc()).first()

            if oldest and oldest[0] is not None:
                oldest_date = oldest[0]
                if isinstance(oldest_date, datetime):
                    oldest_date = oldest_date.date()
                # 「起点已有数据」不等于「窗口够用」：只看最早一天会让**中间断档**
                # 永远补不上 —— 第 8 轮实测 69 条到期预测卡在"窗口内净值记录不足"，
                # 生产同样中招（Cron 无限重试，生命周期还报"结构性不可验 0"）。
                # 因此再要求窗口内的实际条数达到按自然日折算的最低密度，
                # 够密才免打接口（保留本方法原有的"别为每笔验证都请求数据源"意图）。
                inside = db.query(FundHistory.nav_date).filter(
                    FundHistory.fund_code == fund_code,
                    FundHistory.nav_date >= start_date,
                    FundHistory.nav_date <= end_date,
                ).count()
                # 门槛不能超过窗口**物理上可能有的净值天数**，否则永不可达（第 9 轮
                # MAJOR-2 实测预测 1709：512680，2026-07-10→07-11 周六，inside=1、门槛=2）。
                # 第 15 轮删掉了"14 天以内只看起点覆盖"这条短路（它让 inside=1 的行
                # 既不补拉也永不归因），第 16 轮把夹取从自然日改成**工作日容量**：
                # `max(1, span_days)` 挡不住"周六→周一"那种容量 1 天、门槛 2 条的窗口，
                # 而终点在 30 天内时负凭据 TTL 只有 1 天 ⇒ 那种行会天天真发一次请求。
                capacity = weekday_capacity(start_date, end_date)
                min_inside = max(1, min(capacity, max(2, int(capacity * 0.6)))) if capacity else 1
                # `end_covered` 是 S7-2 补的：短窗口只看"起点被覆盖 + 窗内有 1 条"就免问，
                # 于是"目标日那天本地缺行"的行永远问不到凭据，验证侧只能在"猜"与"无限重试"
                # 之间二选一（第 12 轮 MAJOR-3）。要求终点也被覆盖才允许免打接口。
                # 整段窗口空着的行（实测 003033：本地净值停在 2020-12-08，预测窗口在
                # 2026 年 9 月）也一律要问：不问就永远拿不到"源端到底有没有"的证据，
                # 结构性不可验的归因也就建不起来 —— 现在的 `min_inside >= 1` 正好涵盖这条。
                end_covered = db.query(FundHistory.nav_date).filter(
                    FundHistory.fund_code == fund_code,
                    FundHistory.nav_date == end_date).first() is not None
                if oldest_date <= start_date and end_covered and inside >= min_inside:
                    return 0

            # S7-2/S7-b：先问"这段历史是不是已经向数据源要过、且源端给不出"。
            # 有未过期的负凭据就直接跳过重复请求 —— 否则 Cron 每天为同样的窗口白跑一趟。
            from src.fund import backfill_proofs

            proven_empty = backfill_proofs.fresh(db, fund_code, start_date, end_date,
                                                 today=today)
            if proven_empty:
                logger.debug(f"[FundData] 基金 {fund_code} {start_date}~{end_date} "
                             f"已有负凭据，跳过重复补拉")
                return 0

            history = self.api.get_fund_history_range(fund_code, start_date, end_date)
            if history is None:
                # 这次**没问到**（超时、限流页、翻页触顶截断）：什么都不记。
                # 记了就等于把一次网络抖动写成"源端确实没有这段历史"的凭据，
                # 之后 7 天都凭它跳过请求，真数据就永远拿不回来了（第 10 轮 BLOCKER-1）。
                logger.warning(f"基金 {fund_code} 区间 {start_date}~{end_date} 未问成功，不记凭据")
                return 0
            if not history:
                # 真的问过数据源、它对这个区间一条都没给 —— 这是"结构性不可验"的合法证据
                # （没问过就下这个结论是瞎猜，见 S7-2 工单）。
                backfill_proofs.record_probe(db, fund_code, start_date, end_date, 0,
                                       now=_probe_clock(today))
                if close_db:
                    db.commit()
                return 0

            fund_info = db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
            fund_name = fund_info.fund_name if fund_info else ''

            existing_dates = set(
                r[0] for r in db.query(FundHistory.nav_date).filter(
                    FundHistory.fund_code == fund_code
                ).all()
            )

            count = 0
            in_window = 0
            for item in history:
                item_date = item['date']
                if isinstance(item_date, datetime):
                    item_date = item_date.date()
                if isinstance(item_date, date) and start_date <= item_date <= end_date:
                    in_window += 1
                if item_date in existing_dates:
                    continue
                db.add(FundHistory(
                    fund_code=fund_code,
                    fund_name=fund_name,
                    nav_date=item_date,
                    nav=item['nav'],
                    day_growth=item['growth']
                ))
                existing_dates.add(item_date)
                count += 1

            # 源端"给了几条"也要记：给了 1 条而窗口需要 2 条时，再问一次还是那 1 条
            # （158038 实测如此），不记就会每天重问、每天照旧报"数据不足"。
            # 数的是**落在请求区间内**的行数，不是 len(history)：源端偶尔会就着一个空
            # 区间回吐区间外的行，照 len 记会让凭据正文（"数据源在 start~end 内给到 N 条"）
            # 说谎，并在 TTL 内压住本该重问的窗口（第 15 轮 m-4）。
            backfill_proofs.record_probe(db, fund_code, start_date, end_date, in_window,
                                   now=_probe_clock(today))

            if close_db:
                db.commit()
            else:
                db.flush()

            if count:
                logger.info(f"[FundData] 基金 {fund_code} 补拉历史净值 {count} 条 ({start_date} ~ {end_date})")
            return count
        except Exception as e:
            logger.error(f"补拉基金{fund_code}历史净值失败: {e}")
            if close_db:
                db.rollback()
            return 0
        finally:
            if close_db:
                db.close()

    def _calculate_growth_rates(self, fund_code: str, db: Session):
        """计算周涨跌幅和月涨跌幅"""
        try:
            latest = db.query(FundHistory).filter(
                FundHistory.fund_code == fund_code
            ).order_by(FundHistory.nav_date.desc()).first()
            
            if not latest or latest.nav is None:
                return
            
            fund = db.query(FundInfo).filter(FundInfo.fund_code == fund_code).first()
            if not fund:
                return
            
            week_ago = db.query(FundHistory).filter(
                FundHistory.fund_code == fund_code,
                FundHistory.nav_date < latest.nav_date
            ).order_by(FundHistory.nav_date.desc()).offset(4).first()
            
            if week_ago and week_ago.nav and week_ago.nav > 0:
                fund.week_growth = round((latest.nav - week_ago.nav) / week_ago.nav * 100, 2)
            else:
                fund.week_growth = None
            
            month_ago = db.query(FundHistory).filter(
                FundHistory.fund_code == fund_code,
                FundHistory.nav_date < latest.nav_date
            ).order_by(FundHistory.nav_date.desc()).offset(19).first()
            
            if month_ago and month_ago.nav and month_ago.nav > 0:
                fund.month_growth = round((latest.nav - month_ago.nav) / month_ago.nav * 100, 2)
            else:
                fund.month_growth = None
            
        except Exception as e:
            logger.error(f"计算涨跌幅失败: {e}")
    
    def get_nav_by_date(self, fund_code: str, target_date: date, db: Session = None) -> Optional[float]:
        """
        获取指定日期的净值
        如果当天没有数据，返回最近的交易日净值
        """
        close_db = False
        if db is None:
            db = SessionLocal()
            close_db = True
        
        try:
            record = db.query(FundHistory).filter(
                FundHistory.fund_code == fund_code,
                FundHistory.nav_date <= target_date
            ).order_by(FundHistory.nav_date.desc()).first()
            
            if record:
                return record.nav
            
            history = self.api.get_fund_history(fund_code, days=60)
            for item in history:
                if item['date'] <= target_date:
                    return item['nav']
            
            return None
            
        except Exception as e:
            logger.error(f"获取历史净值失败: {e}")
            return None
        finally:
            if close_db:
                db.close()
    
    def calculate_change(self, fund_code: str, start_date: date, end_date: date, 
                         db: Session = None) -> Optional[Dict]:
        """
        计算两个日期之间的涨跌幅
        
        返回:
        {
            'start_nav': float,
            'end_nav': float,
            'change': float,  # 涨跌幅百分比
            'start_date': date,
            'end_date': date
        }
        """
        start_nav = self.get_nav_by_date(fund_code, start_date, db)
        end_nav = self.get_nav_by_date(fund_code, end_date, db)
        
        if start_nav is None or end_nav is None or start_nav == 0:
            return None
        
        change = (end_nav - start_nav) / start_nav * 100
        
        return {
            'start_nav': start_nav,
            'end_nav': end_nav,
            'change': round(change, 2),
            'start_date': start_date,
            'end_date': end_date
        }


fund_api = FundAPI()
fund_data_manager = FundDataManager()


if __name__ == '__main__':
    api = FundAPI()
    
    info = api.get_fund_info('000001')
    print("基金信息:", info)
    
    history = api.get_fund_history('000001', days=7)
    print("历史净值:", history)
    
    dm = FundDataManager()
    dm.update_fund_info('000001')
    dm.update_fund_history('000001', days=30)
    
    from datetime import date
    change = dm.calculate_change('000001', date(2024, 1, 1), date(2024, 1, 31))
    print("月涨跌:", change)
