"""
板块资金流向爬虫（AkShare 版）
一次请求获取全部板块数据（含成交额），无需逐个板块单独请求。

核心口径（对齐直播间）：
- 主力 = 超大单 + 大单
- 散户 = 仅小单（中单被剥离，既不算主力也不算散户）
- 主力 + 散户 + 中单 = 0（零和）
- 主力 + 散户 ≠ 0（因为中单被排除）

数据源：AkShare → 东方财富
"""
import logging
from typing import Dict, List, Optional

import akshare as ak
import pandas as pd

logger = logging.getLogger(__name__)


class SectorFlowCrawler:
    """
    板块资金流向爬虫

    使用 AkShare 获取东方财富板块级别的资金流向数据，包括：
    - 主力净流入（超大单 + 大单）
    - 散户净流入（仅小单）
    - 中单净流入（被剥离，用于验证零和）
    - 板块成交额
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
        type_label = "行业资金流" if sector_type == "industry" else "概念资金流"

        for attempt in range(self.max_retries):
            try:
                df = ak.stock_sector_fund_flow_rank(
                    indicator="今日",
                    sector_type=type_label,
                )
                break
            except Exception as e:
                logger.warning(f"[SectorFlow] AkShare 请求失败 (尝试 {attempt + 1}/{self.max_retries}): {e}")
                if attempt == self.max_retries - 1:
                    logger.error(f"[SectorFlow] {type_label} 获取失败，已重试 {self.max_retries} 次")
                    return []

        if df is None or df.empty:
            logger.warning(f"[SectorFlow] {type_label} 返回为空")
            return []

        results = []
        for _, row in df.iterrows():
            try:
                sector_name = str(row.get("名称", ""))
                if not sector_name:
                    continue

                # 各单型资金净流入（元）
                super_large = self._safe_float(row.get("超大单净流入-净额"))
                large = self._safe_float(row.get("大单净流入-净额"))
                medium = self._safe_float(row.get("中单净流入-净额"))
                small = self._safe_float(row.get("小单净流入-净额"))

                # 成交额（元 → 亿元）
                turnover_raw = self._safe_float(row.get("成交额"))
                turnover = turnover_raw / 1e8 if turnover_raw is not None else None

                # 主力净流入 = 超大单 + 大单
                main_net = None
                if super_large is not None and large is not None:
                    main_net = super_large + large

                # 散户净流入 = 仅小单（核心口径！）
                retail_net = small

                results.append({
                    "sector_code": "",  # AkShare 不返回板块代码
                    "sector_name": sector_name,
                    "change_pct": self._safe_float(row.get("今日涨跌幅")),
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

        logger.info(f"[SectorFlow] {type_label} 获取 {len(results)} 条")
        return results

    def fetch_all(self, turnover_limit: int = 100) -> List[Dict]:
        """
        完整抓取流程

        1. 抓取行业板块和概念板块
        2. 合并后按主力净流入降序排序
        3. 返回全量数据（含成交额，无需额外请求）

        Args:
            turnover_limit: 保留参数（兼容旧接口）

        Returns:
            全量板块资金流向列表
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
        if value is None or pd.isna(value) or value == "-" or value == "":
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
        print(f"{item['sector_name']:10s} | main_net={item.get('main_net_flow')} | retail={item.get('retail_net_flow')} | turnover={item.get('turnover')}")
