"""
板块资金流向数据抓取脚本
由 GitHub Actions 定时执行，数据直接写入 Supabase

使用方式:
1. GitHub Actions 自动执行（每个交易日 15:30 北京时间）
2. 手动触发: python scripts/fetch_sector_flow.py
"""
import os
import sys
import time
import logging
from datetime import date, datetime
from typing import Dict, List, Optional

import requests
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

# 东方财富 API
SECTOR_LIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
SECTOR_DETAIL_URL = "https://push2.eastmoney.com/api/qt/stock/get"
SECTOR_FIELDS = "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f85,f124"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Referer': 'https://data.eastmoney.com/',
}


def fetch_with_retry(url: str, params: dict, max_retries: int = 3) -> Optional[dict]:
    """带重试的 HTTP 请求"""
    session = requests.Session()
    session.headers.update(HEADERS)

    for attempt in range(max_retries):
        try:
            resp = session.get(url, params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            wait = 2 ** attempt
            logger.warning(f"超时 (尝试 {attempt + 1}/{max_retries})，等待 {wait}s")
            time.sleep(wait)
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if hasattr(e, 'response') and e.response is not None else 0
            if status in (502, 503, 504):
                wait = 3 * (2 ** attempt)
                logger.warning(f"服务端错误({status})，等待 {wait}s 后重试")
                time.sleep(wait)
            elif status == 429:
                time.sleep(5)
            else:
                logger.error(f"HTTP 错误({status}): {e}")
                break
        except Exception as e:
            logger.error(f"请求异常: {e}")
            time.sleep(1)
    return None


def fetch_sector_list(sector_type: str) -> List[Dict]:
    """获取板块资金流向列表"""
    fs_map = {"industry": "m:90+t:2", "concept": "m:90+t:3"}
    fs = fs_map.get(sector_type)
    if not fs:
        return []

    params = {
        "pn": 1, "pz": 5000, "po": 1, "np": 1,
        "fltt": 2, "invt": 2, "fid": "f62",
        "fs": fs, "fields": SECTOR_FIELDS,
    }

    data = fetch_with_retry(SECTOR_LIST_URL, params)
    if not data or "data" not in data or data["data"] is None:
        logger.warning(f"{sector_type} 列表返回为空")
        return []

    items = data["data"].get("diff", [])
    results = []

    for item in items:
        try:
            sector_code = str(item.get("f12", ""))
            sector_name = str(item.get("f14", ""))
            if not sector_code or not sector_name:
                continue

            # 东方财富API字段：
            # f66=超大单净流入, f72=大单净流入
            # f78=中单净流入, f84=小单净流入
            super_large = safe_float(item.get("f66"))
            large = safe_float(item.get("f72"))
            medium = safe_float(item.get("f78"))
            small = safe_float(item.get("f84"))

            # 主力净流入 = 超大单 + 大单 + 中单
            main_net = None
            if super_large is not None and large is not None and medium is not None:
                main_net = super_large + large + medium

            # 散户净流入 = 小单
            retail_net = small

            # 主力暗盘 = 主力 - 散户（按用户策略公式计算）
            dark_pool = None
            if main_net is not None and retail_net is not None:
                dark_pool = main_net - retail_net

            results.append({
                "sector_code": sector_code,
                "sector_name": sector_name,
                "change_pct": safe_float(item.get("f3")),
                "main_net_flow": main_net,
                "super_large_flow": super_large,
                "large_flow": large,
                "medium_flow": medium,
                "small_flow": small,
                "retail_net_flow": retail_net,
                "dark_pool": dark_pool,
                "main_net_ratio": safe_float(item.get("f184")),
                "data_category": sector_type,
            })
        except Exception as e:
            logger.warning(f"解析板块数据异常: {e}")
            continue

    logger.info(f"{sector_type} 获取 {len(results)} 条")
    return results


def fetch_turnover(sector_code: str) -> Optional[float]:
    """获取单个板块成交额（亿元），超时返回 None"""
    params = {
        "secid": f"90.{sector_code}",
        "fields": "f48",
        "fltt": 2, "invt": 2,
    }

    # 使用较短超时，避免个别板块拖慢整体
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        resp = session.get(SECTOR_DETAIL_URL, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data and "data" in data and data["data"] is not None:
            raw = data["data"].get("f48")
            if raw is not None:
                return float(raw) / 1e8
    except Exception:
        pass
    return None


def safe_float(value) -> Optional[float]:
    """安全转 float"""
    if value is None or value == "-" or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def calculate_intensity(dark_pool: Optional[float], turnover: Optional[float]) -> Optional[float]:
    """主力强度 = (暗盘 / 成交额) × 100"""
    if dark_pool is None or turnover is None or turnover == 0:
        return None
    return (dark_pool / (turnover * 1e8)) * 100


def judge_behavior(intensity: Optional[float]) -> Optional[str]:
    """行为判定"""
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
            # 暗盘 = 主力 - 散户（已在fetch_sector_list中计算）
            dark_pool = record.get("dark_pool")
            intensity = calculate_intensity(dark_pool, record.get("turnover"))
            behavior = judge_behavior(intensity)

            # 直接插入（已先删旧数据，无需 upsert）
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
                data_source="eastmoney",
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
    logger.info("板块资金流向数据抓取开始")
    logger.info("=" * 50)

    flow_date = date.today()
    logger.info(f"目标日期: {flow_date}")

    # 抓取行业板块
    industry_list = fetch_sector_list("industry")
    time.sleep(1)

    # 抓取概念板块
    concept_list = fetch_sector_list("concept")

    # 合并排序
    all_sectors = industry_list + concept_list
    all_sectors.sort(key=lambda x: x.get("main_net_flow") or 0, reverse=True)

    total_count = len(all_sectors)
    logger.info(f"合并后共 {total_count} 个板块")

    # 取前 100 个主力资金最活跃的板块补充成交额
    top_n = min(100, len(all_sectors))
    top_sectors = all_sectors[:top_n]
    logger.info(f"开始补充前 {top_n} 个板块的成交额...")

    # 总时间预算：5 分钟（300s），避免个别超时拖垮整体
    budget_seconds = 300
    start_time = time.time()
    success_count = 0
    skip_count = 0

    for i, sector in enumerate(top_sectors):
        # 检查时间预算
        elapsed = time.time() - start_time
        if elapsed > budget_seconds:
            logger.warning(f"时间预算用尽 ({elapsed:.0f}s)，停止补充成交额")
            break

        turnover = fetch_turnover(sector["sector_code"])
        if turnover is not None:
            sector["turnover"] = turnover
            success_count += 1
        else:
            skip_count += 1
        time.sleep(0.05)  # 限流：50ms 间隔
        if (i + 1) % 50 == 0:
            logger.info(f"  成交额进度: {i + 1}/{top_n} (成功 {success_count}, 跳过 {skip_count})")

    logger.info(f"成交额补充完成: 成功 {success_count}, 跳过 {skip_count}, 用时 {time.time() - start_time:.0f}s")

    # 保存全量数据（包括无成交额的，至少有资金流向数据）
    if not all_sectors:
        logger.error("未获取到任何板块数据")
        sys.exit(1)

    saved = save_to_database(all_sectors, flow_date, DATABASE_URL)
    logger.info(f"抓取完成: 保存 {saved}/{total_count} 条（含 {top_n} 条有成交额，前100名）")

    if saved == 0:
        logger.error("未保存任何数据")
        sys.exit(1)


if __name__ == "__main__":
    main()
