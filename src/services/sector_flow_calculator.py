"""
板块主力资金量化计算器

核心口径（对齐直播间）：
- 主力 = 超大单 + 大单
- 散户 = 仅小单（中单被剥离，既不算主力也不算散户）
- 主力 + 散户 + 中单 = 0（零和）
- 主力 + 散户 ≠ 0（因为中单被排除）

计算公式：
1. 主力资金 = (超大单 + 大单) + 北向 + 龙虎榜 + 大宗
2. 散户资金 = 小单净流入（直接取值，无需计算）
3. 主力暗盘 = 主力资金 - 散户资金
4. 主力强度 = 主力暗盘 / 成交额 * 100
5. 主力行为 = 根据主力强度判定
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SectorFlowResult:
    """板块资金流向计算结果"""
    sector_name: str  # 板块名称
    sector_type: str  # 板块类型: industry/concept

    # 基础行情
    change_pct: float = 0.0  # 涨跌幅 (%)
    turnover: float = 0.0  # 成交额 (亿元)

    # 东财原始数据
    super_large_net: float = 0.0  # 超大单净流入 (亿元)
    large_net: float = 0.0  # 大单净流入 (亿元)
    medium_net: float = 0.0  # 中单净流入 (亿元)
    small_net: float = 0.0  # 小单净流入 (亿元)

    # 增量机构数据
    northbound_flow: float = 0.0  # 北向资金行业净流入 (亿元)
    tiger_net_buy: float = 0.0  # 龙虎榜机构净买入 (亿元)
    block_trade_net: float = 0.0  # 大宗交易净额 (亿元)

    # 计算结果
    main_capital: float = 0.0  # 主力资金 (亿元) = 超大单+大单 + 增量
    retail_capital: float = 0.0  # 散户资金 (亿元) = 小单净流入
    dark_pool: float = 0.0  # 主力暗盘 (亿元) = 主力资金 - 散户资金
    main_intensity: float = 0.0  # 主力强度 = 主力暗盘 / 成交额 * 100
    behavior: str = "洗盘"  # 主力行为

    # 元数据
    data_date: str = ""  # 数据日期
    update_time: str = ""  # 更新时间


def calculate_sector_flow(
    sector_name: str,
    turnover: float,  # 成交额（亿元）
    change_pct: float,  # 涨跌幅 (%)
    super_large_net: float,  # 超大单净流入（亿元）
    large_net: float,  # 大单净流入（亿元）
    medium_net: float,  # 中单净流入（亿元）
    small_net: float,  # 小单净流入（亿元）
    northbound_flow: float = 0.0,  # 北向资金净流入（亿元）
    tiger_net_buy: float = 0.0,  # 龙虎榜机构净买入（亿元）
    block_trade_net: float = 0.0,  # 大宗交易净额（亿元）
) -> SectorFlowResult:
    """
    计算板块主力资金流向指标

    核心口径：
    - 主力 = 超大单 + 大单
    - 散户 = 仅小单（中单被剥离）
    - 主力暗盘 = 主力资金 - 散户资金
    - 主力强度 = 主力暗盘 / 成交额 * 100
    """
    # Step 1: 东财基础主力 = 超大单 + 大单
    eastmoney_main = super_large_net + large_net

    # Step 2: 主力资金 = 东财主力 + 增量机构资金
    main_capital = eastmoney_main + northbound_flow + tiger_net_buy + block_trade_net

    # Step 3: 散户资金 = 小单净流入（直接取值）
    retail_capital = small_net

    # Step 4: 主力暗盘 = 主力资金 - 散户资金
    dark_pool = main_capital - retail_capital

    # Step 5: 主力强度 = 主力暗盘 / 成交额 * 100
    main_intensity = (dark_pool / turnover * 100) if turnover > 0 else 0.0

    # Step 6: 主力行为判定
    behavior = judge_behavior(main_intensity)

    return SectorFlowResult(
        sector_name=sector_name,
        sector_type="industry",
        change_pct=change_pct,
        turnover=turnover,
        super_large_net=super_large_net,
        large_net=large_net,
        medium_net=medium_net,
        small_net=small_net,
        northbound_flow=northbound_flow,
        tiger_net_buy=tiger_net_buy,
        block_trade_net=block_trade_net,
        main_capital=main_capital,
        retail_capital=retail_capital,
        dark_pool=dark_pool,
        main_intensity=main_intensity,
        behavior=behavior,
        data_date=datetime.now().strftime("%Y-%m-%d"),
        update_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def judge_behavior(intensity: float) -> str:
    """
    判定主力行为

    - 抢筹：主力强度 >= 3
    - 建仓：1 <= 主力强度 < 3
    - 洗盘：-1 < 主力强度 < 1
    - 出货：主力强度 <= -1
    """
    if intensity >= 3:
        return "抢筹"
    elif intensity >= 1:
        return "建仓"
    elif intensity > -1:
        return "洗盘"
    else:
        return "出货"


def calculate_batch(
    sectors_data: list[dict],
    northbound_data: Optional[dict] = None,
    tiger_data: Optional[dict] = None,
    block_trade_data: Optional[dict] = None,
) -> list[SectorFlowResult]:
    """
    批量计算板块资金流向

    Args:
        sectors_data: 板块基础数据列表，每项包含:
            - sector_name, turnover, change_pct
            - super_large_net, large_net, medium_net, small_net
        northbound_data: {板块名: 净流入(亿元)}
        tiger_data: {板块名: 机构净买入(亿元)}
        block_trade_data: {板块名: 大宗交易净额(亿元)}

    Returns:
        计算结果列表
    """
    results = []

    for sector in sectors_data:
        sector_name = sector["sector_name"]

        northbound = northbound_data.get(sector_name, 0.0) if northbound_data else 0.0
        tiger = tiger_data.get(sector_name, 0.0) if tiger_data else 0.0
        block = block_trade_data.get(sector_name, 0.0) if block_trade_data else 0.0

        result = calculate_sector_flow(
            sector_name=sector_name,
            turnover=sector["turnover"],
            change_pct=sector["change_pct"],
            super_large_net=sector["super_large_net"],
            large_net=sector["large_net"],
            medium_net=sector["medium_net"],
            small_net=sector["small_net"],
            northbound_flow=northbound,
            tiger_net_buy=tiger,
            block_trade_net=block,
        )
        results.append(result)

    return results
