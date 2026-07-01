"""
板块资金流向爬取脚本（GitHub Actions 专用）

使用东方财富直接 API，按成交额取前80个 + 按主力净流入取前20个，去重后保存。
不做申万一级行业聚合（API 返回的已经是一级行业）。
"""

import sys
import os
from pathlib import Path

# 添加 scripts 目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent))

import requests
import json
from datetime import datetime
from sector_mapping import get_level1_sector

# === 配置 ===
API_URL = "https://push2.eastmoney.com/api/qt/clist/get"

# 东方财富 API 字段说明:
# f12=板块代码, f14=板块名称, f3=涨跌幅(%), f62=主力净流入, f184=主力净流入占比
# f269=超大单净流入, f271=大单净流入, f273=中单净流入, f275=小单净流入, f6=成交额


def fetch_sector_data(order_by: str, count: int, max_retries: int = 3) -> list:
    """
    从东方财富 API 获取板块资金流向数据

    Args:
        order_by: 排序字段（f6=成交额, f62=主力净流入）
        count: 获取数量
        max_retries: 最大重试次数

    Returns:
        板块数据列表
    """
    params = {
        "pn": 1,
        "pz": count,
        "po": 1,  # 降序
        "np": 1,
        "ut": "b2884a393a59ad64002292a3e90d46a5",
        "fltt": 2,
        "invt": 2,
        "fid0": order_by,
        "fs": "m:90+t:2",  # 行业板块
        "stat": 1,
        "fields": "f12,f14,f3,f6,f267,f269,f271,f273,f275",
        "rt": "52975239",
        "_": int(datetime.now().timestamp() * 1000),
    }

    for attempt in range(max_retries):
        try:
            response = requests.get(API_URL, params=params, timeout=60)
            response.raise_for_status()
            data = response.json()
            break
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"   请求失败，重试 {attempt + 1}/{max_retries}: {e}")
                import time
                time.sleep(5)
            else:
                raise

    if not data or "data" not in data or not data["data"]:
        return []

    diff = data["data"].get("diff", [])
    if not diff:
        return []

    sectors = []
    for item in diff:
        try:
            code = item.get("f12", "")
            name = item.get("f14", "")
            change_pct = item.get("f3", 0) or 0
            turnover = item.get("f6", 0) or 0
            super_large_net = item.get("f269", 0) or 0
            large_net = item.get("f271", 0) or 0
            medium_net = item.get("f273", 0) or 0
            small_net = item.get("f275", 0) or 0

            # 转换为亿元
            super_large_yi = super_large_net / 1e8
            large_yi = large_net / 1e8
            medium_yi = medium_net / 1e8
            small_yi = small_net / 1e8
            turnover_yi = turnover / 1e8

            # 主力 = 超大单 + 大单
            custom_main = super_large_yi + large_yi
            # 散户 = 仅小单
            custom_retail = small_yi
            # 主力暗盘 = 主力 - 散户
            dark_pool = custom_main - custom_retail
            # 主力强度 = 暗盘 / 成交额 × 100
            intensity = (dark_pool / turnover_yi * 100) if turnover_yi > 0 else 0

            # 行为判定（英文标签，与数据库一致）
            if intensity >= 3:
                behavior = "grab"
            elif intensity >= 1:
                behavior = "build"
            elif intensity >= -1:
                behavior = "wash"
            else:
                behavior = "sell"

            sectors.append({
                "name": name,
                "code": code,
                "change_pct": change_pct,
                "main_net_inflow": round(custom_main, 2),
                "retail_net_flow": round(custom_retail, 2),
                "dark_pool": round(dark_pool, 2),
                "intensity": round(intensity, 2),
                "behavior": behavior,
                "turnover": round(turnover_yi, 2),
            })
        except Exception as e:
            print(f"Error parsing item: {e}")
            continue

    return sectors


def main():
    """主函数"""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 开始获取板块资金流向数据")

    # 1. 按成交额获取前80个
    print("1. 按成交额获取前80个板块...")
    sectors_by_turnover = fetch_sector_data("f6", 80)
    print(f"   获取到 {len(sectors_by_turnover)} 个板块")

    # 2. 按主力净流入获取前20个
    print("2. 按主力净流入获取前20个板块...")
    sectors_by_inflow = fetch_sector_data("f62", 20)
    print(f"   获取到 {len(sectors_by_inflow)} 个板块")

    # 3. 合并去重
    print("3. 合并去重...")
    seen = set()
    merged_sectors = []
    for sector in sectors_by_turnover + sectors_by_inflow:
        if sector["name"] not in seen:
            seen.add(sector["name"])
            merged_sectors.append(sector)
    print(f"   去重后共 {len(merged_sectors)} 个板块")

    # 4. 打印统计
    BEHAVIOR_CN = {"grab": "抢筹", "build": "建仓", "wash": "洗盘", "sell": "出货"}

    print("\n=== 板块资金流向统计 ===")
    print(f"{'板块名称':12} {'涨跌幅':>8} {'主力净流入':>12} {'散户净流入':>12} {'主力暗盘':>12} {'主力强度':>8} {'行为':>6} {'成交额':>10}")
    print("-" * 100)

    for sector in merged_sectors[:15]:
        behavior_cn = BEHAVIOR_CN.get(sector['behavior'], sector['behavior'])
        print(f"{sector['name']:12} {sector['change_pct']:>7.2f}% {sector['main_net_inflow']:>11.2f}亿 {sector['retail_net_flow']:>11.2f}亿 {sector['dark_pool']:>11.2f}亿 {sector['intensity']:>7.2f}% {behavior_cn:>6} {sector['turnover']:>9.2f}亿")

    if len(merged_sectors) > 15:
        print(f"... 还有 {len(merged_sectors) - 15} 个板块")

    # 5. 保存到数据库
    print("\n5. 保存到数据库...")
    try:
        # 先删除今天旧数据
        delete_old_data()
        # 插入新数据
        save_to_database(merged_sectors)
        print(f"   成功保存 {len(merged_sectors)} 条记录")
    except Exception as e:
        print(f"   保存失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print("\n=== 完成 ===")


def delete_old_data():
    """删除今天旧数据"""
    from sqlalchemy import create_engine, text

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL 环境变量未设置")

    engine = create_engine(database_url, echo=False)

    with engine.connect() as conn:
        date_str = datetime.now().strftime("%Y-%m-%d")
        result = conn.execute(
            text("DELETE FROM sector_fund_flow WHERE flow_date = :date"),
            {"date": date_str}
        )
        conn.commit()
        print(f"   已删除 {result.rowcount} 条旧数据")


def save_to_database(sectors: list):
    """
    保存板块资金流向数据到数据库

    Args:
        sectors: 板块数据列表
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL 环境变量未设置")

    engine = create_engine(database_url, echo=False)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        for sector in sectors:
            session.execute(
                text("""
                    INSERT INTO sector_fund_flow
                    (flow_date, flow_time, sector_name, sector_code,
                     main_net_flow, retail_net_flow, dark_pool, main_intensity, behavior,
                     sector_change_pct, turnover, data_source)
                    VALUES
                    (:flow_date, :flow_time, :sector_name, :sector_code,
                     :main_net_flow, :retail_net_flow, :dark_pool, :main_intensity, :behavior,
                     :change_pct, :turnover, :data_source)
                """),
                {
                    "flow_date": date_str,
                    "flow_time": now,
                    "sector_name": sector["name"],
                    "sector_code": sector["code"],
                    "main_net_flow": sector["main_net_inflow"],
                    "retail_net_flow": sector["retail_net_flow"],
                    "dark_pool": sector["dark_pool"],
                    "main_intensity": sector["intensity"],
                    "behavior": sector["behavior"],
                    "change_pct": sector["change_pct"],
                    "turnover": sector["turnover"],
                    "data_source": "github_actions",
                }
            )

        session.commit()
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()


if __name__ == "__main__":
    main()
