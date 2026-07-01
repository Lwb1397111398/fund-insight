"""
板块资金数据获取器

从 AkShare 获取各数据源，并组装成计算器所需的格式。

数据源：
1. 板块资金流向 (stock_sector_fund_flow_rank) - 超大单/大单/中单/小单
2. 北向资金行业净流入 (stock_hsgt_industry_flow)
3. 龙虎榜 (stock_tiger_list) - 按板块归集
4. 大宗交易 (stock_block_trade) - 按板块归集

注意：此模块需要在国内 IP 环境运行（如 GitHub Actions），
因为 AkShare 底层调用的东方财富 API 会阻断海外请求。
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Optional

import akshare as ak
import pandas as pd

logger = logging.getLogger(__name__)


def fetch_sector_fund_flow(sector_type: str = "行业资金流") -> Optional[pd.DataFrame]:
    """
    获取板块资金流向数据

    Args:
        sector_type: "行业资金流" 或 "概念资金流"

    Returns:
        DataFrame with columns: sector_name, change_pct, turnover(亿元),
        super_large_net(亿元), large_net(亿元), medium_net(亿元), small_net(亿元)
    """
    try:
        logger.info(f"获取{sector_type}数据...")
        df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type=sector_type)

        if df is None or df.empty:
            logger.warning(f"{sector_type}数据为空")
            return None

        logger.info(f"原始列名: {list(df.columns)}")

        # AkShare 返回的列名可能因版本不同而变化，需要动态识别
        # 常见列名模式：
        # "名称", "今日涨跌幅", "成交额", "主力净流入-净额", "超大单净流入-净额", etc.
        col_map = {}

        for col in df.columns:
            col_str = str(col)
            if "名称" in col_str:
                col_map[col] = "sector_name"
            elif "涨跌幅" in col_str and "今日" in col_str:
                col_map[col] = "change_pct"
            elif col_str == "成交额":
                col_map[col] = "turnover_raw"
            elif "超大单" in col_str and "净额" in col_str:
                col_map[col] = "super_large_net_raw"
            elif "大单" in col_str and "超大" not in col_str and "净额" in col_str:
                col_map[col] = "large_net_raw"
            elif "中单" in col_str and "净额" in col_str:
                col_map[col] = "medium_net_raw"
            elif "小单" in col_str and "净额" in col_str:
                col_map[col] = "small_net_raw"

        logger.info(f"列名映射: {col_map}")

        # 重命名列
        df = df.rename(columns=col_map)

        # 转换数值
        for col in ["turnover_raw", "super_large_net_raw", "large_net_raw",
                     "medium_net_raw", "small_net_raw"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        # 转换为亿元（东财接口返回的单位可能是元或万元，需要根据数值大小判断）
        for raw_col, target_col in [
            ("turnover_raw", "turnover"),
            ("super_large_net_raw", "super_large_net"),
            ("large_net_raw", "large_net"),
            ("medium_net_raw", "medium_net"),
            ("small_net_raw", "small_net"),
        ]:
            if raw_col in df.columns:
                # 如果最大值 > 1e6，说明是元，需要除以 1e8
                max_val = df[raw_col].abs().max()
                if max_val > 1e6:
                    df[target_col] = df[raw_col] / 1e8
                elif max_val > 1e2:
                    df[target_col] = df[raw_col] / 1e4
                else:
                    df[target_col] = df[raw_col]

        # 涨跌幅
        if "change_pct" in df.columns:
            df["change_pct"] = pd.to_numeric(df["change_pct"], errors="coerce").fillna(0)
        else:
            df["change_pct"] = 0.0

        logger.info(f"获取到 {len(df)} 个{sector_type}")
        return df[["sector_name", "change_pct", "turnover",
                   "super_large_net", "large_net", "medium_net", "small_net"]]

    except Exception as e:
        logger.error(f"获取{sector_type}失败: {e}")
        return None


def fetch_northbound_flow() -> Optional[dict]:
    """
    获取北向资金行业净流入

    Returns:
        dict: {行业名: 净流入(亿元)}
    """
    try:
        logger.info("获取北向资金行业数据...")

        interfaces = [
            ("stock_hsgt_industry_flow", lambda: ak.stock_hsgt_industry_flow(symbol="北向资金")),
            ("stock_hsgt_board_flow_em", lambda: ak.stock_hsgt_board_flow_em(symbol="北向资金")),
        ]

        df = None
        for name, func in interfaces:
            try:
                df = func()
                if df is not None and not df.empty:
                    logger.info(f"使用 {name} 获取北向资金数据")
                    break
            except Exception as e:
                logger.warning(f"{name} 失败: {e}")
                continue

        if df is None or df.empty:
            logger.warning("北向资金数据获取失败")
            return {}

        result = {}
        name_col = None
        flow_col = None

        for col in df.columns:
            col_str = str(col)
            if "行业" in col_str or "名称" in col_str or "板块" in col_str:
                name_col = col
            if "净流入" in col_str or "净额" in col_str:
                flow_col = col

        if name_col and flow_col:
            for _, row in df.iterrows():
                industry = str(row[name_col])
                flow = pd.to_numeric(row[flow_col], errors="coerce")
                if pd.notna(flow):
                    # 转换为亿元
                    if abs(flow) > 1e6:
                        flow = flow / 1e8
                    elif abs(flow) > 1e2:
                        flow = flow / 1e4
                    result[industry] = flow

        logger.info(f"获取到 {len(result)} 个行业的北向资金数据")
        return result

    except Exception as e:
        logger.error(f"获取北向资金失败: {e}")
        return {}


def fetch_tiger_list() -> Optional[dict]:
    """
    获取龙虎榜数据

    Returns:
        dict: {板块名: 机构净买入(亿元)} 或 {"_raw_data": DataFrame}
    """
    try:
        logger.info("获取龙虎榜数据...")

        df = None
        interfaces = [
            ("stock_tiger_list", lambda: ak.stock_tiger_list()),
            ("stock_lhb_detail_em", lambda: ak.stock_lhb_detail_em(
                start_date=(datetime.now() - timedelta(days=7)).strftime("%Y%m%d"),
                end_date=datetime.now().strftime("%Y%m%d"),
                symbol="近一月"
            )),
        ]

        for name, func in interfaces:
            try:
                df = func()
                if df is not None and not df.empty:
                    logger.info(f"使用 {name} 获取龙虎榜数据，共 {len(df)} 条")
                    break
            except Exception as e:
                logger.warning(f"{name} 失败: {e}")
                continue

        if df is None or df.empty:
            logger.warning("龙虎榜数据获取失败")
            return {}

        # 返回原始数据，后续需要和板块成分股关联
        return {"_raw_data": df}

    except Exception as e:
        logger.error(f"获取龙虎榜失败: {e}")
        return {}


def fetch_block_trade() -> Optional[dict]:
    """
    获取大宗交易数据

    Returns:
        dict: {板块名: 大宗交易净额(亿元)} 或 {"_raw_data": DataFrame}
    """
    try:
        logger.info("获取大宗交易数据...")

        df = None
        for i in range(5):
            date = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
            try:
                df = ak.stock_block_trade(date=date)
                if df is not None and not df.empty:
                    logger.info(f"获取 {date} 大宗交易数据，共 {len(df)} 条")
                    break
            except:
                continue

        if df is None or df.empty:
            logger.warning("大宗交易数据获取失败")
            return {}

        return {"_raw_data": df}

    except Exception as e:
        logger.error(f"获取大宗交易失败: {e}")
        return {}


def fetch_all(
    include_industry: bool = True,
    include_concept: bool = True,
    include_northbound: bool = True,
    include_tiger: bool = True,
    include_block_trade: bool = True,
) -> dict:
    """
    获取所有数据源

    Returns:
        dict with keys: industry_flow, concept_flow, northbound, tiger, block_trade
    """
    result = {}

    if include_industry:
        result["industry_flow"] = fetch_sector_fund_flow("行业资金流")
        time.sleep(2)

    if include_concept:
        result["concept_flow"] = fetch_sector_fund_flow("概念资金流")
        time.sleep(2)

    if include_northbound:
        result["northbound"] = fetch_northbound_flow()
        time.sleep(2)

    if include_tiger:
        result["tiger"] = fetch_tiger_list()
        time.sleep(2)

    if include_block_trade:
        result["block_trade"] = fetch_block_trade()

    return result


def prepare_calculator_input(flow_df: pd.DataFrame) -> list[dict]:
    """
    将 AkShare DataFrame 转换为计算器所需的输入格式
    """
    if flow_df is None or flow_df.empty:
        return []

    result = []
    for _, row in flow_df.iterrows():
        result.append({
            "sector_name": row["sector_name"],
            "turnover": row["turnover"],
            "change_pct": row["change_pct"],
            "super_large_net": row["super_large_net"],
            "large_net": row["large_net"],
            "medium_net": row["medium_net"],
            "small_net": row["small_net"],
        })

    return result
