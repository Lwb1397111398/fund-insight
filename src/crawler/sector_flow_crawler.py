"""
东方财富板块资金流向爬虫
获取板块成交额、主力资金、散户资金三项基础数据

数据源：
- API 1: push2.eastmoney.com/api/qt/clist/get （板块资金流向列表）
- API 2: push2.eastmoney.com/api/qt/stock/get （板块成交额补充）
"""
import time
import logging
from typing import Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

logger = logging.getLogger(__name__)

# 东方财富板块资金流向列表 API
SECTOR_LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
# 东方财富个股/板块详情 API（用于补充成交额）
SECTOR_DETAIL_URL = "https://push2.eastmoney.com/api/qt/stock/get"

# 板块类型对应的 fs 参数
SECTOR_TYPE_MAP = {
    "industry": "m:90+t:2",  # 行业板块
    "concept": "m:90+t:3",   # 概念板块
}

# 需要获取的字段
SECTOR_FIELDS = "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f85,f124"

# 请求头
DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Referer': 'https://data.eastmoney.com/',
}


class SectorFlowCrawler:
    """
    板块资金流向爬虫

    从东方财富获取板块级别的资金流向数据，包括：
    - 主力净流入（超大单 + 大单）
    - 散户净流入（中单 + 小单）
    - 板块成交额
    """

    def __init__(self, timeout: int = 15, max_retries: int = 3):
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.timeout = timeout
        self.max_retries = max_retries
        self._turnover_cache: Dict[str, Optional[float]] = {}

    def _request(self, url: str, params: dict) -> Optional[dict]:
        """
        带重试的 HTTP GET 请求

        Args:
            url: 请求地址
            params: 查询参数

        Returns:
            JSON 响应数据，失败返回 None
        """
        for attempt in range(self.max_retries):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.Timeout:
                wait = 2 ** attempt
                logger.warning(f"[SectorFlow] 超时 (尝试 {attempt + 1}/{self.max_retries})，等待 {wait}s")
                time.sleep(wait)
            except requests.exceptions.HTTPError as e:
                status = e.response.status_code if hasattr(e, 'response') and e.response is not None else 0
                if status == 429:
                    logger.warning("[SectorFlow] 限流(429)，等待 5s")
                    time.sleep(5)
                elif status in (502, 503, 504):
                    # 东方财富服务端临时错误，指数退避重试
                    wait = 3 * (2 ** attempt)
                    logger.warning(f"[SectorFlow] 服务端错误({status})，等待 {wait}s 后重试 ({attempt + 1}/{self.max_retries})")
                    time.sleep(wait)
                else:
                    logger.warning(f"[SectorFlow] HTTP 错误({status}): {e}")
                    time.sleep(2 ** attempt)
            except Exception as e:
                logger.warning(f"[SectorFlow] 请求异常: {e}")
                time.sleep(1)
        return None

    def fetch_sector_list(self, sector_type: str) -> List[Dict]:
        """
        抓取板块资金流向列表

        Args:
            sector_type: "industry"（行业板块）或 "concept"（概念板块）

        Returns:
            板块资金流向列表，每项包含原始API字段
        """
        fs = SECTOR_TYPE_MAP.get(sector_type)
        if not fs:
            logger.error(f"[SectorFlow] 未知板块类型: {sector_type}")
            return []

        params = {
            "pn": 1,
            "pz": 5000,
            "po": 1,
            "np": 1,
            "fltt": 2,
            "invt": 2,
            "fid": "f62",
            "fs": fs,
            "fields": SECTOR_FIELDS,
        }

        data = self._request(SECTOR_LIST_URL, params)
        if not data or "data" not in data or data["data"] is None:
            logger.warning(f"[SectorFlow] {sector_type} 列表返回为空")
            return []

        items = data["data"].get("diff", [])
        results = []

        for item in items:
            try:
                sector_code = str(item.get("f12", ""))
                sector_name = str(item.get("f14", ""))
                if not sector_code or not sector_name:
                    continue

                # 解析各单型资金流向（单位：元）
                super_large = self._safe_float(item.get("f66"))  # 超大单净流入
                large = self._safe_float(item.get("f72"))        # 大单净流入
                medium = self._safe_float(item.get("f78"))       # 中单净流入
                small = self._safe_float(item.get("f84"))        # 小单净流入
                main_net = self._safe_float(item.get("f62"))     # 主力净流入（API直接给出）

                # 如果 f62 缺失，用超大单+大单计算
                if main_net is None:
                    if super_large is not None and large is not None:
                        main_net = super_large + large

                # 散户净流入 = 中单 + 小单
                retail_net = None
                if medium is not None and small is not None:
                    retail_net = medium + small

                results.append({
                    "sector_code": sector_code,
                    "sector_name": sector_name,
                    "change_pct": self._safe_float(item.get("f3")),
                    "main_net_flow": main_net,
                    "super_large_flow": super_large,
                    "large_flow": large,
                    "medium_flow": medium,
                    "small_flow": small,
                    "retail_net_flow": retail_net,
                    "main_net_ratio": self._safe_float(item.get("f184")),
                    "data_category": sector_type,
                })
            except Exception as e:
                logger.warning(f"[SectorFlow] 解析板块数据异常: {e}")
                continue

        logger.info(f"[SectorFlow] {sector_type} 获取 {len(results)} 条")
        return results

    def fetch_turnover(self, sector_code: str) -> Optional[float]:
        """
        获取单个板块成交额（亿元）

        Args:
            sector_code: 板块代码

        Returns:
            成交额（亿元），失败返回 None
        """
        if sector_code in self._turnover_cache:
            return self._turnover_cache[sector_code]

        params = {
            "secid": f"90.{sector_code}",
            "fields": "f48",
            "fltt": 2,
            "invt": 2,
        }

        data = self._request(SECTOR_DETAIL_URL, params)
        turnover = None

        if data and "data" in data and data["data"] is not None:
            raw = data["data"].get("f48")
            if raw is not None:
                # f48 单位是元，转亿元
                turnover = float(raw) / 1e8

        self._turnover_cache[sector_code] = turnover
        time.sleep(0.1)  # 请求间隔，避免限流
        return turnover

    def fetch_all(self, limit: int = 30) -> List[Dict]:
        """
        完整抓取流程

        1. 抓取行业板块和概念板块列表
        2. 合并后按主力净流入降序排序
        3. 取前 limit 个板块
        4. 并发补充成交额
        5. 返回完整数据

        Args:
            limit: 取前 N 个板块补充成交额

        Returns:
            完整板块资金流向列表
        """
        # 第一步：抓取两种板块
        industry_list = self.fetch_sector_list("industry")
        time.sleep(0.5)  # 板块间延迟
        concept_list = self.fetch_sector_list("concept")

        # 第二步：合并排序
        all_sectors = industry_list + concept_list
        # 按主力净流入降序（None 排到最后）
        all_sectors.sort(key=lambda x: x.get("main_net_flow") or 0, reverse=True)

        # 第三步：取前 limit 个
        top_sectors = all_sectors[:limit]

        # 第四步：并发补充成交额
        if top_sectors:
            self._fill_turnovers(top_sectors)

        logger.info(f"[SectorFlow] 最终获取 {len(top_sectors)} 条板块数据")
        return top_sectors

    def _fill_turnovers(self, sectors: List[Dict]):
        """并发补充成交额"""
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_map = {
                executor.submit(self.fetch_turnover, s["sector_code"]): s
                for s in sectors
            }
            for future in as_completed(future_map):
                sector = future_map[future]
                try:
                    turnover = future.result()
                    if turnover is not None:
                        sector["turnover"] = turnover
                except Exception as e:
                    logger.warning(f"[SectorFlow] 成交额获取失败 {sector['sector_name']}: {e}")

    @staticmethod
    def _safe_float(value) -> Optional[float]:
        """安全转 float，None/空值返回 None"""
        if value is None or value == "-" or value == "":
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None


# 便捷函数
def fetch_sector_flow(limit: int = 30) -> List[Dict]:
    """便捷函数：一键抓取板块资金流向"""
    crawler = SectorFlowCrawler()
    return crawler.fetch_all(limit=limit)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    data = fetch_sector_flow(limit=5)
    for item in data:
        print(f"{item['sector_name']:10s} | main_net={item.get('main_net_flow')} | turnover={item.get('turnover')}")
