"""
板块资金流向数据抓取脚本
由 GitHub Actions 定时执行，数据直接写入 Supabase

使用 AkShare 获取东方财富板块资金流向数据（一次请求返回全部字段含成交额）。

使用方式:
1. GitHub Actions 自动执行（每个交易日 13:30 北京时间）
2. 手动触发: python scripts/fetch_sector_flow.py
"""
import os
import sys
import logging
from datetime import date
from typing import Dict, List, Optional

import akshare as ak
import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.models.database import SectorFundFlow

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# 数据库连接
DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    logger.error("DATABASE_URL 环境变量未设置")
    sys.exit(1)


def fetch_sector_fund_flow(sector_type: str) -> List[Dict]:
    """
    使用 AkShare 获取板块资金流向数据

    Args:
        sector_type: "行业资金流" 或 "概念资金流"

    Returns:
        板块数据列表
    """
    type_label = "行业" if "行业" in sector_type else "概念"
    logger.info(f"开始获取{type_label}板块资金流向...")

    try:
        df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type=sector_type)
    except Exception as e:
        logger.error(f"AkShare 获取{type_label}板块数据失败: {e}")
        return []

    if df is None or df.empty:
        logger.warning(f"{type_label}板块数据为空")
        return []

    # 打印列名以便调试
    logger.info(f"AkShare 返回列名: {df.columns.tolist()}")

    results = []
    for _, row in df.iterrows():
        try:
            sector_name = str(row.get("名称", ""))
            if not sector_name:
                continue

            # 涨跌幅
            change_pct = _safe_float(row.get("今日涨跌幅"))

            # 成交额（元）
            turnover_raw = _safe_float(row.get("成交额"))
            # 转为亿元
            turnover = turnover_raw / 1e8 if turnover_raw is not None else None

            # 各单型资金净流入（元）
            super_large = _safe_float(row.get("超大单净流入-净额"))  # 超大单
            large = _safe_float(row.get("大单净流入-净额"))          # 大单
            medium = _safe_float(row.get("中单净流入-净额"))         # 中单
            small = _safe_float(row.get("小单净流入-净额"))          # 小单

            # 主力净流入 = 超大单 + 大单
            main_net = None
            if super_large is not None and large is not None:
                main_net = super_large + large

            # 散户净流入 = 中单 + 小单
            retail_net = None
            if medium is not None and small is not None:
                retail_net = medium + small

            results.append({
                "sector_code": "",  # AkShare 不返回板块代码，后续通过映射补充
                "sector_name": sector_name,
                "change_pct": change_pct,
                "turnover": turnover,
                "main_net_flow": main_net,
                "super_large_flow": super_large,
                "large_flow": large,
                "medium_flow": medium,
                "small_flow": small,
                "retail_net_flow": retail_net,
                "data_category": "industry" if "行业" in sector_type else "concept",
            })
        except Exception as e:
            logger.warning(f"解析{type_label}板块数据异常: {e}")
            continue

    logger.info(f"{type_label}板块获取 {len(results)} 条")
    return results


def _safe_float(value) -> Optional[float]:
    """安全转 float"""
    if value is None or pd.isna(value):
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def calculate_dark_pool(main_net: Optional[float], retail_net: Optional[float]) -> Optional[float]:
    """
    主力暗盘 = 主力净流入 - 散户净流入

    主力 = 超大单 + 大单
    散户 = 中单 + 小单
    """
    if main_net is None or retail_net is None:
        return None
    return main_net - retail_net


def calculate_intensity(dark_pool: Optional[float], turnover: Optional[float]) -> Optional[float]:
    """
    主力强度 = (暗盘 / 成交额) × 100

    暗盘单位：元，成交额单位：亿元
    """
    if dark_pool is None or turnover is None or turnover == 0:
        return None
    return (dark_pool / (turnover * 1e8)) * 100


def judge_behavior(intensity: Optional[float]) -> Optional[str]:
    """
    行为判定：
    - ≥ 3  抢筹 (grab)
    - 1~3  建仓 (build)
    - -1~1 洗盘 (wash)
    - ≤ -1 出货 (sell)
    """
    if intensity is None:
        return None
    if intensity >= 3.0:
        return "grab"
    if intensity >= 1.0:
        return "build"
    if intensity > -1.0:
        return "wash"
    return "sell"


def save_to_database(records: List[Dict], flow_date: date, db_url: str) -> int:
    """保存数据到数据库（先删旧数据，再插入新数据）"""
    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    session = Session()

    saved = 0
    try:
        # 先删除当天旧数据（只保留最新一次抓取）
        deleted = session.query(SectorFundFlow).filter(
            SectorFundFlow.flow_date == flow_date
        ).delete(synchronize_session=False)
        if deleted > 0:
            logger.info(f"删除当天旧数据 {deleted} 条")

        for record in records:
            dark_pool = calculate_dark_pool(record.get("main_net_flow"), record.get("retail_net_flow"))
            intensity = calculate_intensity(dark_pool, record.get("turnover"))
            behavior = judge_behavior(intensity)

            new_record = SectorFundFlow(
                flow_date=flow_date,
                sector_code=record.get("sector_code", ""),
                sector_name=record.get("sector_name", ""),
                main_net_flow=record.get("main_net_flow"),
                retail_net_flow=record.get("retail_net_flow"),
                turnover=record.get("turnover"),
                sector_change_pct=record.get("change_pct"),
                dark_pool=dark_pool,
                main_intensity=intensity,
                behavior=behavior,
                data_category=record.get("data_category"),
                data_source="akshare_eastmoney",
            )
            session.add(new_record)
            saved += 1

        session.commit()
        logger.info(f"成功保存 {saved} 条记录")
    except Exception as e:
        session.rollback()
        logger.error(f"数据库操作失败: {e}")
    finally:
        session.close()

    return saved


def main():
    logger.info("=" * 50)
    logger.info("板块资金流向数据抓取开始（AkShare）")
    logger.info("=" * 50)

    flow_date = date.today()
    logger.info(f"目标日期: {flow_date}")

    # 获取行业板块
    industry_list = fetch_sector_fund_flow("行业资金流")

    # 获取概念板块
    concept_list = fetch_sector_fund_flow("概念资金流")

    # 合并
    all_sectors = industry_list + concept_list
    total_count = len(all_sectors)
    logger.info(f"合并后共 {total_count} 个板块")

    if not all_sectors:
        logger.warning("未获取到任何数据，保留旧数据不变。")
        sys.exit(0)

    # 按主力净流入降序排序
    all_sectors.sort(key=lambda x: x.get("main_net_flow") or 0, reverse=True)

    # 保存数据
    saved = save_to_database(all_sectors, flow_date, DATABASE_URL)
    logger.info(f"抓取完成: 保存 {saved}/{total_count} 条")

    if saved == 0:
        logger.warning("未保存任何数据，保留旧数据不变。")
        sys.exit(0)


if __name__ == "__main__":
    main()
