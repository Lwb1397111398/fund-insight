"""
板块资金流向爬取脚本（GitHub Actions 专用）

使用东方财富直接 API，按申万一级行业聚合。
数据获取策略：按成交额取前80个 + 按主力净流入取前20个，去重后聚合。
"""

import sys
import os
from pathlib import Path

# 添加 scripts 目录到 Python 路径
sys.path.insert(0, str(Path(__file__).parent))

import requests
import json
from datetime import datetime
from collections import defaultdict
from sector_mapping import get_level1_sector

# === 配置 ===
API_URL = "https://push2.eastmoney.com/api/qt/clist/get"

# 东方财富 API 字段说明:
# f12=板块代码, f14=板块名称, f3=涨跌幅(%), f62=主力净流入, f184=主力净流入占比
# f66=超大单净流入, f69=超大单占比, f72=大单净流入, f75=大单占比
# f78=中单净流入, f81=中单占比, f84=小单净流入, f87=小单占比, f6=成交额


def fetch_sector_data(order_by: str, count: int) -> list:
    """
    从东方财富 API 获取板块资金流向数据

    Args:
        order_by: 排序字段（f6=成交额, f62=主力净流入）
        count: 获取数量

    Returns:
        板块数据列表
    """
    # 获取行业板块 (fs=m:90+t:2)
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
        "fields": "f12,f14,f2,f3,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f6,f124",
        "rt": "52975239",
        "_": int(datetime.now().timestamp() * 1000),
    }

    response = requests.get(API_URL, params=params, timeout=30)
    data = response.json()

    if not data or "data" not in data or not data["data"]:
        return []

    diff = data["data"].get("diff", [])
    if not diff:
        return []

    sectors = []
    for item in diff:
        try:
            # 解析数据
            code = item.get("f12", "")
            name = item.get("f14", "")
            change_pct = item.get("f3", 0) or 0
            main_net_inflow = item.get("f62", 0) or 0  # 主力净流入
            super_large_net = item.get("f66", 0) or 0   # 超大单净流入
            large_net = item.get("f72", 0) or 0         # 大单净流入
            medium_net = item.get("f78", 0) or 0        # 中单净流入
            small_net = item.get("f84", 0) or 0         # 小单净流入
            turnover = item.get("f6", 0) or 0           # 成交额

            # 转换为亿元
            main_net_inflow_yi = main_net_inflow / 1e8
            super_large_yi = super_large_net / 1e8
            large_yi = large_net / 1e8
            medium_yi = medium_net / 1e8
            small_yi = small_net / 1e8
            turnover_yi = turnover / 1e8

            # 自定义计算
            # 主力 = 超大单 + 大单
            custom_main = super_large_yi + large_yi
            # 散户 = 仅小单
            custom_retail = small_yi
            # 主力暗盘 = 主力 - 散户
            dark_pool = custom_main - custom_retail
            # 主力强度 = 暗盘 / 成交额 × 100
            intensity = (dark_pool / turnover_yi * 100) if turnover_yi > 0 else 0

            # 行为判定
            if intensity >= 3:
                behavior = "抢筹"
            elif intensity >= 1:
                behavior = "建仓"
            elif intensity >= -1:
                behavior = "洗盘"
            else:
                behavior = "出货"

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


def aggregate_by_level1(sectors: list) -> list:
    """
    按申万一级行业聚合板块数据

    聚合规则:
    - 涨跌幅: 按成交额加权平均
    - 资金流相关: 直接求和
    - 强度: 重新计算
    - 行为: 重新判定
    """
    # 按一级行业分组
    groups = defaultdict(lambda: {
        "_weighted_change": 0.0,
        "_total_weight": 0.0,
        "main_net_inflow": 0.0,
        "retail_net_flow": 0.0,
        "dark_pool": 0.0,
        "turnover": 0.0,
        "sub_sectors": [],
    })

    for sector in sectors:
        level1 = get_level1_sector(sector["name"])
        group = groups[level1]

        # 累加资金流
        group["main_net_inflow"] += sector["main_net_inflow"]
        group["retail_net_flow"] += sector["retail_net_flow"]
        group["dark_pool"] += sector["dark_pool"]
        group["turnover"] += sector["turnover"]

        # 加权涨跌幅
        if sector["turnover"] > 0:
            group["_weighted_change"] += sector["change_pct"] * sector["turnover"]
            group["_total_weight"] += sector["turnover"]

        group["sub_sectors"].append(sector["name"])

    # 生成聚合结果
    result = []
    for level1, data in groups.items():
        # 计算加权涨跌幅
        if data["_total_weight"] > 0:
            change_pct = data["_weighted_change"] / data["_total_weight"]
        else:
            change_pct = 0

        # 重新计算强度
        if data["turnover"] > 0:
            intensity = (data["dark_pool"] / data["turnover"]) * 100
        else:
            intensity = 0

        # 行为判定
        if intensity >= 3:
            behavior = "抢筹"
        elif intensity >= 1:
            behavior = "建仓"
        elif intensity >= -1:
            behavior = "洗盘"
        else:
            behavior = "出货"

        result.append({
            "name": level1,
            "code": "",
            "change_pct": round(change_pct, 2),
            "main_net_inflow": round(data["main_net_inflow"], 2),
            "retail_net_flow": round(data["retail_net_flow"], 2),
            "dark_pool": round(data["dark_pool"], 2),
            "intensity": round(intensity, 2),
            "behavior": behavior,
            "turnover": round(data["turnover"], 2),
            "sub_count": len(data["sub_sectors"]),
            "sub_sectors": data["sub_sectors"],
        })

    # 按主力净流入排序
    result.sort(key=lambda x: x["main_net_inflow"], reverse=True)
    return result


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

    # 4. 按申万一级行业聚合
    print("4. 按申万一级行业聚合...")
    aggregated = aggregate_by_level1(merged_sectors)
    print(f"   聚合后共 {len(aggregated)} 个板块")

    # 5. 打印统计
    print("\n=== 板块资金流向统计 ===")
    print(f"{'板块名称':12} {'涨跌幅':>8} {'主力净流入':>12} {'散户净流入':>12} {'主力暗盘':>12} {'主力强度':>8} {'行为':>6} {'成交额':>10}")
    print("-" * 100)

    for sector in aggregated[:15]:  # 只显示前15个
        print(f"{sector['name']:12} {sector['change_pct']:>7.2f}% {sector['main_net_inflow']:>11.2f}亿 {sector['retail_net_flow']:>11.2f}亿 {sector['dark_pool']:>11.2f}亿 {sector['intensity']:>7.2f}% {sector['behavior']:>6} {sector['turnover']:>9.2f}亿")

    if len(aggregated) > 15:
        print(f"... 还有 {len(aggregated) - 15} 个板块")

    # 6. 保存到数据库
    print("\n5. 保存到数据库...")
    try:
        save_to_database(aggregated)
        print(f"   成功保存 {len(aggregated)} 条记录")
    except Exception as e:
        print(f"   保存失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    print("\n=== 完成 ===")


def save_to_database(sectors: list):
    """
    保存板块资金流向数据到数据库

    Args:
        sectors: 聚合后的板块数据列表
    """
    import sys
    from pathlib import Path

    # 添加项目根目录到 Python 路径
    project_root = Path(__file__).parent.parent
    sys.path.insert(0, str(project_root))

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    # 导入项目配置
    from src.config import settings

    # 导入模型
    from src.models.database import SectorFundFlow, Base

    # 创建数据库连接
    engine = create_engine(settings.database_url, echo=False)

    # 创建表（如果不存在）
    Base.metadata.create_all(engine)

    # 创建会话
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        now = datetime.now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S")

        for sector in sectors:
            # 检查是否已存在（同一天同一个板块）
            existing = session.query(SectorFundFlow).filter(
                SectorFundFlow.sector_name == sector["name"],
                SectorFundFlow.date == date_str,
            ).first()

            if existing:
                # 更新现有记录
                existing.change_pct = sector["change_pct"]
                existing.main_net_inflow = sector["main_net_inflow"]
                existing.retail_net_flow = sector["retail_net_flow"]
                existing.dark_pool = sector["dark_pool"]
                existing.intensity = sector["intensity"]
                existing.behavior = sector["behavior"]
                existing.turnover = sector["turnover"]
                existing.update_time = now
            else:
                # 创建新记录
                record = SectorFundFlow(
                    sector_name=sector["name"],
                    sector_code=sector["code"],
                    change_pct=sector["change_pct"],
                    main_net_inflow=sector["main_net_inflow"],
                    retail_net_flow=sector["retail_net_flow"],
                    dark_pool=sector["dark_pool"],
                    intensity=sector["intensity"],
                    behavior=sector["behavior"],
                    turnover=sector["turnover"],
                    date=date_str,
                    time=time_str,
                    create_time=now,
                    update_time=now,
                )
                session.add(record)

        session.commit()
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()


if __name__ == "__main__":
    main()
