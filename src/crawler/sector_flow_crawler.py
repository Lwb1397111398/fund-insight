"""
板块资金流向爬虫（东方财富直接 API 版）

核心口径（对齐直播间）：
- 主力 = 超大单 + 大单
- 散户 = 仅小单（中单被剥离，既不算主力也不算散户）
- 主力 + 散户 + 中单 = 0（零和）
- 主力 + 散户 ≠ 0（因为中单被排除）

字段映射（东方财富 push2 API）：
- f12: 板块代码
- f14: 板块名称
- f3: 涨跌幅
- f6: 成交额（元）
- f267: 主力净流入（超大单+大单）
- f269: 超大单净流入
- f271: 大单净流入
- f273: 中单净流入
- f275: 小单净流入
"""
import logging
import requests
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# 东方财富 API 配置
API_URL = "https://push2.eastmoney.com/api/qt/clist/get"
API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}

# 板块类型映射
SECTOR_TYPE_MAP = {
    "industry": "m:90+t:2",   # 行业板块
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
            "pz": 500,  # 最多获取500个板块
            "po": 1,
            "np": 1,
            "ut": "bd1d9ddb04089700cf9c27f6f7426281",
            "fltt": 2,
            "invt": 2,
            "fid": "f267",  # 按主力净流入排序
            "fs": fs,
            "fields": "f12,f14,f3,f6,f267,f269,f271,f273,f275",
            "_": "1625292448803",
        }

        for attempt in range(self.max_retries):
            try:
                resp = requests.get(
                    API_URL,
                    params=params,
                    headers=API_HEADERS,
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                data = resp.json()

                if "data" not in data or not data["data"]:
                    logger.warning(f"[SectorFlow] {sector_type} 返回为空")
                    return []

                items = data["data"].get("diff", [])
                break

            except Exception as e:
                logger.warning(f"[SectorFlow] API 请求失败 (尝试 {attempt + 1}/{self.max_retries}): {e}")
                if attempt == self.max_retries - 1:
                    logger.error(f"[SectorFlow] {sector_type} 获取失败，已重试 {self.max_retries} 次")
                    return []

        results = []
        for item in items:
            try:
                sector_name = item.get("f14", "")
                if not sector_name:
                    continue

                # 各单型资金净流入（元 → 亿元）
                super_large = self._safe_float(item.get("f269"))
                large = self._safe_float(item.get("f271"))
                medium = self._safe_float(item.get("f273"))
                small = self._safe_float(item.get("f275"))

                # 成交额（元 → 亿元）
                turnover_raw = self._safe_float(item.get("f6"))
                turnover = turnover_raw / 1e8 if turnover_raw is not None else None

                # 主力净流入 = 超大单 + 大单（也可以直接用 f267）
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
