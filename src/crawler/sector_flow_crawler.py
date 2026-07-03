"""
板块资金流向爬虫（东方财富直接 API 版）

核心口径（对齐直播间）：
- 主力 = 超大单 + 大单
- 散户 = 仅小单（中单被剥离，既不算主力也不算散户）
- 主力 + 散户 + 中单 = 0（零和）
- 主力 + 散户 ≠ 0（因为中单被排除）

字段映射（东方财富 push2 API，今日口径）：
- f12: 板块代码
- f14: 板块名称
- f3: 涨跌幅
- f6: 成交额（元）
- f62: 今日主力净流入
- f66: 今日超大单净流入
- f72: 今日大单净流入
- f78: 今日中单净流入
- f84: 今日小单净流入
"""
import logging
import time
import requests
import urllib3
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 东方财富 API 配置
API_URLS = [
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://push2delay.eastmoney.com/api/qt/clist/get",
    "https://push2his.eastmoney.com/api/qt/clist/get",
    "https://push2test.eastmoney.com/api/qt/clist/get",
]
API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://data.eastmoney.com/bkzj/hy.html",
}

# 板块类型映射
SECTOR_TYPE_MAP = {
    "industry": "m:90+s:4",   # 行业板块（东方财富页面当前口径）
    "concept": "m:90+t:3",    # 概念板块
}


class SectorFlowCrawler:
    """
    板块资金流向爬虫

    直接调用东方财富 push2 API，获取板块级别的资金流向数据。
    """

    def __init__(self, timeout: int = 30, max_retries: int = 3):
        self.timeout = timeout
        self.max_retries = max_retries

    def fetch_sector_list(self, sector_type: str) -> List[Dict]:
        """
        抓取板块资金流向列表

        Args:
            sector_type: "industry"（行业板块）或 "concept"（概念板块）

        Returns:
            板块资金流向列表
        """
        fs = SECTOR_TYPE_MAP.get(sector_type, SECTOR_TYPE_MAP["industry"])

        params = {
            "pn": 1,
            "pz": 100,  # 东方财富页面默认分页，过大时接口容易断开
            "po": 1,
            "np": 1,
            "ut": "8dec03ba335b81bf4ebdf7b29ec27d15",
            "fltt": 2,
            "invt": 2,
            "fid": "f62",  # 按今日主力净流入排序
            "fs": fs,
            "fields": "f12,f14,f3,f6,f62,f66,f72,f78,f84",
            "_": int(time.time() * 1000),
        }

        items = []
        page = 1
        total = None
        while True:
            params["pn"] = page
            params["_"] = int(time.time() * 1000)
            page_items = None

            for attempt in range(self.max_retries):
                api_url = API_URLS[attempt % len(API_URLS)]
                try:
                    resp = requests.get(
                        api_url,
                        params=params,
                        headers=API_HEADERS,
                        timeout=self.timeout,
                        verify=False,
                    )
                    resp.raise_for_status()
                    data = resp.json()

                    if "data" not in data or not data["data"]:
                        logger.warning(f"[SectorFlow] {sector_type} 第 {page} 页返回为空")
                        return items

                    total = data["data"].get("total", total)
                    page_items = data["data"].get("diff", [])
                    break

                except Exception as e:
                    logger.warning(f"[SectorFlow] API 请求失败 (页 {page}, 源 {api_url}, 尝试 {attempt + 1}/{self.max_retries}): {e}")
                    if attempt == self.max_retries - 1:
                        logger.error(f"[SectorFlow] {sector_type} 第 {page} 页获取失败，已重试 {self.max_retries} 次")
                        return items

            if not page_items:
                break

            items.extend(page_items)
            if total is not None and len(items) >= int(total):
                break
            if len(page_items) < params["pz"]:
                break
            page += 1

        results = []
        for item in items:
            try:
                sector_name = item.get("f14", "")
                if not sector_name:
                    continue

                # 各单型资金净流入（元 → 亿元）
                super_large_raw = self._safe_float(item.get("f66"))
                large_raw = self._safe_float(item.get("f72"))
                medium_raw = self._safe_float(item.get("f78"))
                small_raw = self._safe_float(item.get("f84"))

                super_large = super_large_raw / 1e8 if super_large_raw is not None else None
                large = large_raw / 1e8 if large_raw is not None else None
                medium = medium_raw / 1e8 if medium_raw is not None else None
                small = small_raw / 1e8 if small_raw is not None else None

                # 成交额（元 → 亿元）
                turnover_raw = self._safe_float(item.get("f6"))
                turnover = turnover_raw / 1e8 if turnover_raw is not None else None

                # 主力净流入 = 超大单 + 大单
                main_net = None
                if super_large is not None and large is not None:
                    main_net = super_large + large

                # 散户净流入 = 仅小单（核心口径！）
                retail_net = small

                results.append({
                    "sector_code": item.get("f12", ""),
                    "sector_name": sector_name,
                    "change_pct": self._safe_float(item.get("f3")),
                    "turnover": turnover,
                    "main_net_flow": main_net,
                    "super_large_flow": super_large,
                    "large_flow": large,
                    "medium_flow": medium,
                    "small_flow": small,
                    "retail_net_flow": retail_net,
                    "data_category": sector_type,
                })
            except Exception as e:
                logger.warning(f"[SectorFlow] 解析板块数据异常: {e}")
                continue

        logger.info(f"[SectorFlow] {sector_type} 获取 {len(results)} 条")
        return results

    def fetch_all(self, turnover_limit: int = 100) -> List[Dict]:
        """
        完整抓取流程

        1. 抓取行业板块和概念板块
        2. 合并后按主力净流入降序排序
        3. 返回全量数据（含成交额，无需额外请求）
        """
        # 抓取两种板块
        industry_list = self.fetch_sector_list("industry")
        concept_list = self.fetch_sector_list("concept")

        # 合并排序
        all_sectors = industry_list + concept_list
        all_sectors.sort(key=lambda x: x.get("main_net_flow") or 0, reverse=True)

        logger.info(f"[SectorFlow] 最终获取 {len(all_sectors)} 条板块数据")
        return all_sectors

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
    return crawler.fetch_all()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    data = fetch_sector_flow(limit=5)
    for item in data[:5]:
        print(f"{item['sector_name']:10s} | main={item.get('main_net_flow', 0):.2f} | retail={item.get('retail_net_flow', 0):.2f} | turnover={item.get('turnover', 0):.2f}")
