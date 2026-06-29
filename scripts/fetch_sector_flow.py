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
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

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

            main_net = safe_float(item.get("f62"))
            super_large = safe_float(item.get("f66"))
            large = safe_float(item.get("f72"))
            medium = safe_float(item.get("f78"))
            small = safe_float(item.get("f84"))

            if main_net is None and super_large is not None and large is not None:
                main_net = super_large + large

            retail_net = None
            if medium is not None and small is not None:
                retail_net = medium + small

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
                "main_net_ratio": safe_float(item.get("f184")),
                "data_category": sector_type,
            })
        except Exception as e:
            logger.warning(f"解析板块数据异常: {e}")
            continue

    logger.info(f"{sector_type} 获取 {len(results)} 条")
    return results


def fetch_turnover(sector_code: str) -> Optional[float]:
    """获取单个板块成交额（亿元）"""
    params = {
        "secid": f"90.{sector_code}",
        "fields": "f48",
        "fltt": 2, "invt": 2,
    }

    data = fetch_with_retry(SECTOR_DETAIL_URL, params)
    if data and "data" in data and data["data"] is not None:
        raw = data["data"].get("f48")
        if raw is not None:
            return float(raw) / 1e8
    return None


def safe_float(value) -> Optional[float]:
    """安全转 float"""
    if value is None or value == "-" or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def calculate_dark_pool(main_net: Optional[float], retail_net: Optional[float]) -> Optional[float]:
    """主力暗盘 = 主力净流入 − 散户净流入"""
    if main_net is None or retail_net is None:
        return None
    return main_net - retail_net


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
    """保存数据到数据库（upsert）"""
    engine = create_engine(db_url)
    Session = sessionmaker(bind=engine)
    session = Session()

    saved = 0
    try:
        for record in records:
            dark_pool = calculate_dark_pool(record.get("main_net_flow"), record.get("retail_net_flow"))
            intensity = calculate_intensity(dark_pool, record.get("turnover"))
            behavior = judge_behavior(intensity)

            # Upsert
            sql = text("""
                INSERT INTO sector_fund_flow
                    (flow_date, sector_code, sector_name, main_net_flow, retail_net_flow,
                     turnover, sector_change_pct, dark_pool, main_intensity, behavior,
                     data_category, data_source, created_at)
                VALUES
                    (:flow_date, :sector_code, :sector_name, :main_net_flow, :retail_net_flow,
                     :turnover, :sector_change_pct, :dark_pool, :main_intensity, :behavior,
                     :data_category, 'eastmoney', NOW())
                ON CONFLICT (flow_date, sector_name) DO UPDATE SET
                    sector_code = EXCLUDED.sector_code,
                    main_net_flow = EXCLUDED.main_net_flow,
                    retail_net_flow = EXCLUDED.retail_net_flow,
                    turnover = EXCLUDED.turnover,
                    sector_change_pct = EXCLUDED.sector_change_pct,
                    dark_pool = EXCLUDED.dark_pool,
                    main_intensity = EXCLUDED.main_intensity,
                    behavior = EXCLUDED.behavior,
                    data_category = EXCLUDED.data_category,
                    data_source = 'eastmoney'
            """)

            # 先检查是否需要添加唯一约束
            try:
                session.execute(sql, {
                    "flow_date": flow_date,
                    "sector_code": record.get("sector_code", ""),
                    "sector_name": record.get("sector_name", ""),
                    "main_net_flow": record.get("main_net_flow"),
                    "retail_net_flow": record.get("retail_net_flow"),
                    "turnover": record.get("turnover"),
                    "sector_change_pct": record.get("change_pct"),
                    "dark_pool": dark_pool,
                    "main_intensity": intensity,
                    "behavior": behavior,
                    "data_category": record.get("data_category"),
                })
                saved += 1
            except Exception as e:
                logger.warning(f"保存 {record.get('sector_name')} 失败: {e}")
                session.rollback()
                continue

        session.commit()
        logger.info(f"成功保存 {saved} 条记录")
    except Exception as e:
        session.rollback()
        logger.error(f"数据库操作失败: {e}")
    finally:
        session.close()

    return saved


def ensure_constraint(db_url: str):
    """确保唯一约束存在"""
    engine = create_engine(db_url)
    with engine.connect() as conn:
        try:
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uix_sector_flow_date_name
                ON sector_fund_flow (flow_date, sector_name)
            """))
            conn.commit()
        except Exception:
            conn.rollback()


def main():
    logger.info("=" * 50)
    logger.info("板块资金流向数据抓取开始")
    logger.info("=" * 50)

    flow_date = date.today()
    logger.info(f"目标日期: {flow_date}")

    # 确保约束
    ensure_constraint(DATABASE_URL)

    # 抓取行业板块
    industry_list = fetch_sector_list("industry")
    time.sleep(1)

    # 抓取概念板块
    concept_list = fetch_sector_list("concept")

    # 合并排序
    all_sectors = industry_list + concept_list
    all_sectors.sort(key=lambda x: x.get("main_net_flow") or 0, reverse=True)

    # 取前 50 个补充成交额
    top_sectors = all_sectors[:50]
    for sector in top_sectors:
        turnover = fetch_turnover(sector["sector_code"])
        if turnover is not None:
            sector["turnover"] = turnover
        time.sleep(0.1)

    if not top_sectors:
        logger.error("未获取到任何板块数据")
        sys.exit(1)

    # 保存
    saved = save_to_database(top_sectors, flow_date, DATABASE_URL)
    logger.info(f"抓取完成: 保存 {saved}/{len(top_sectors)} 条")

    if saved == 0:
        logger.error("未保存任何数据")
        sys.exit(1)


if __name__ == "__main__":
    main()
