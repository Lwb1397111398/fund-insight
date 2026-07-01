"""
板块资金流向数据抓取脚本
由 GitHub Actions 定时执行，数据直接写入 Supabase

核心口径（对齐直播间）：
- 主力 = 超大单 + 大单
- 散户 = 仅小单（中单被剥离）
- 主力 + 散户 + 中单 = 0（零和）
- 主力 + 散户 ≠ 0（因为中单被排除）

使用方式:
1. GitHub Actions 自动执行（每个交易日 13:30 北京时间）
2. 手动触发: python scripts/fetch_sector_flow.py
"""
import os
import sys
import logging
from datetime import date
from typing import Dict, List, Optional
from collections import defaultdict

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from src.models.database import SectorFundFlow
from src.crawler.sector_mapping import get_level1_sector

# 设置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# 数据库连接
DATABASE_URL = os.getenv("DATABASE_URL", "")
if not DATABASE_URL:
    logger.error("DATABASE_URL 环境变量未设置")
    sys.exit(1)

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


def fetch_sector_fund_flow(sector_type: str) -> List[Dict]:
    """
    使用东方财富直接 API 获取板块资金流向数据

    Args:
        sector_type: "industry"（行业板块）或 "concept"（概念板块）

    Returns:
        板块数据列表
    """
    fs = SECTOR_TYPE_MAP.get(sector_type, SECTOR_TYPE_MAP["industry"])
    type_label = "行业" if sector_type == "industry" else "概念"

    logger.info(f"开始获取{type_label}板块资金流向...")

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

    try:
        resp = requests.get(API_URL, params=params, headers=API_HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if "data" not in data or not data["data"]:
            logger.warning(f"{type_label}板块数据为空")
            return []

        items = data["data"].get("diff", [])

    except Exception as e:
        logger.error(f"API 获取{type_label}板块数据失败: {e}")
        return []

    results = []
    for item in items:
        try:
            sector_name = item.get("f14", "")
            if not sector_name:
                continue

            # 涨跌幅
            change_pct = _safe_float(item.get("f3"))

            # 成交额（元 → 亿元）
            turnover_raw = _safe_float(item.get("f6"))
            turnover = turnover_raw / 1e8 if turnover_raw is not None else None

            # 各单型资金净流入（元 → 亿元）
            super_large = _safe_float(item.get("f269"))  # 超大单
            large = _safe_float(item.get("f271"))          # 大单
            medium = _safe_float(item.get("f273"))         # 中单
            small = _safe_float(item.get("f275"))          # 小单

            # 转换为亿元
            if super_large is not None:
                super_large = super_large / 1e8
            if large is not None:
                large = large / 1e8
            if medium is not None:
                medium = medium / 1e8
            if small is not None:
                small = small / 1e8

            # 主力净流入 = 超大单 + 大单
            main_net = None
            if super_large is not None and large is not None:
                main_net = super_large + large

            # 散户净流入 = 仅小单（核心口径！）
            retail_net = small

            results.append({
                "sector_code": item.get("f12", ""),
                "sector_name": sector_name,
                "change_pct": change_pct,
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
            logger.warning(f"解析{type_label}板块数据异常: {e}")
            continue

    logger.info(f"{type_label}板块获取 {len(results)} 条")
    return results


def _safe_float(value) -> Optional[float]:
    """安全转 float"""
    if value is None or value == "-" or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def calculate_dark_pool(main_net: Optional[float], retail_net: Optional[float]) -> Optional[float]:
    """
    主力暗盘 = 主力净流入 - 散户净流入

    主力 = 超大单 + 大单
    散户 = 仅小单
    """
    if main_net is None or retail_net is None:
        return None
    return main_net - retail_net


def calculate_intensity(dark_pool: Optional[float], turnover: Optional[float]) -> Optional[float]:
    """
    主力强度 = (暗盘 / 成交额) × 100

    暗盘单位：亿元，成交额单位：亿元
    """
    if dark_pool is None or turnover is None or turnover == 0:
        return None
    return (dark_pool / turnover) * 100


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
                data_source="eastmoney_push2",
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


def aggregate_by_level1(sectors: List[Dict]) -> List[Dict]:
    """
    将细分板块聚合到申万一级行业

    Args:
        sectors: 细分板块数据列表

    Returns:
        按申万一级行业聚合后的数据列表
    """
    aggregated = defaultdict(lambda: {
        "sector_code": "",
        "sector_name": "",
        "change_pct": None,
        "turnover": 0.0,
        "main_net_flow": 0.0,
        "super_large_flow": 0.0,
        "large_flow": 0.0,
        "medium_flow": 0.0,
        "small_flow": 0.0,
        "retail_net_flow": 0.0,
        "data_category": "industry",
        "sub_sectors": [],
    })

    for sector in sectors:
        # 映射到申万一级行业
        level1_name = get_level1_sector(sector["sector_name"])

        # 聚合数据
        agg = aggregated[level1_name]
        agg["sector_name"] = level1_name
        agg["sub_sectors"].append(sector["sector_name"])

        # 累加数值字段
        if sector.get("turnover") is not None:
            agg["turnover"] += sector["turnover"]
        if sector.get("main_net_flow") is not None:
            agg["main_net_flow"] += sector["main_net_flow"]
        if sector.get("super_large_flow") is not None:
            agg["super_large_flow"] += sector["super_large_flow"]
        if sector.get("large_flow") is not None:
            agg["large_flow"] += sector["large_flow"]
        if sector.get("medium_flow") is not None:
            agg["medium_flow"] += sector["medium_flow"]
        if sector.get("small_flow") is not None:
            agg["small_flow"] += sector["small_flow"]
        if sector.get("retail_net_flow") is not None:
            agg["retail_net_flow"] += sector["retail_net_flow"]

        # 涨跌幅用成交额加权平均
        if sector.get("change_pct") is not None and sector.get("turnover") is not None:
            if agg["_weighted_change"] is None:
                agg["_weighted_change"] = 0.0
                agg["_total_weight"] = 0.0
            agg["_weighted_change"] += sector["change_pct"] * sector["turnover"]
            agg["_total_weight"] += sector["turnover"]

    # 计算加权平均涨跌幅
    result = []
    for level1_name, agg in aggregated.items():
        if agg.get("_total_weight") and agg["_total_weight"] > 0:
            agg["change_pct"] = agg["_weighted_change"] / agg["_total_weight"]
        # 清理临时字段
        agg.pop("_weighted_change", None)
        agg.pop("_total_weight", None)
        result.append(agg)

    return result


def main():
    logger.info("=" * 50)
    logger.info("板块资金流向数据抓取开始（东方财富直接 API）")
    logger.info("=" * 50)

    flow_date = date.today()
    logger.info(f"目标日期: {flow_date}")

    # 获取行业板块
    industry_list = fetch_sector_fund_flow("industry")

    # 获取概念板块（可选，如果失败不影响主流程）
    concept_list = []
    try:
        concept_list = fetch_sector_fund_flow("concept")
    except Exception as e:
        logger.warning(f"概念板块获取失败，跳过: {e}")

    # 合并
    all_sectors = industry_list + concept_list
    total_count = len(all_sectors)
    logger.info(f"合并后共 {total_count} 个细分板块")

    if not all_sectors:
        logger.warning("未获取到任何数据，保留旧数据不变。")
        sys.exit(0)

    # 按申万一级行业聚合
    aggregated = aggregate_by_level1(all_sectors)
    logger.info(f"聚合后共 {len(aggregated)} 个申万一级行业")

    # 按主力净流入降序排序
    aggregated.sort(key=lambda x: x.get("main_net_flow") or 0, reverse=True)

    # 保存数据
    saved = save_to_database(aggregated, flow_date, DATABASE_URL)
    logger.info(f"抓取完成: 保存 {saved}/{len(aggregated)} 条")

    if saved == 0:
        logger.warning("未保存任何数据，保留旧数据不变。")
        sys.exit(0)


if __name__ == "__main__":
    main()
