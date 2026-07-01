#!/usr/bin/env python3
"""
AkShare 接口可用性测试脚本
测试 Phase 1 所需的 4 个核心接口：
1. 板块资金流向 (stock_sector_fund_flow_rank)
2. 北向资金行业净流入 (stock_hsgt_industry_flow)
3. 龙虎榜 (stock_tiger_list)
4. 大宗交易 (stock_block_trade)

注意：此脚本需要在国内 IP 环境运行（如 GitHub Actions），
因为 AkShare 底层调用的东方财富 API 会阻断海外请求。
"""

import sys
import os
import time
import json
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta

# 确保输出编码正确
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


def log(msg):
    """统一日志输出"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def test_sector_fund_flow():
    """测试板块资金流向接口"""
    log("=" * 60)
    log("测试 1: 板块资金流向 (stock_sector_fund_flow_rank)")
    log("=" * 60)

    try:
        # 获取行业板块资金流向
        df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
        log(f"[OK] 行业板块数据获取成功，共 {len(df)} 条")
        log(f"列名: {list(df.columns)}")
        if len(df) > 0:
            log("\n前3条数据:")
            print(df.head(3).to_string())

        time.sleep(2)

        # 获取概念板块资金流向
        df_concept = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="概念资金流")
        log(f"\n[OK] 概念板块数据获取成功，共 {len(df_concept)} 条")
        log(f"列名: {list(df_concept.columns)}")

        return True, df
    except Exception as e:
        log(f"[FAIL] 获取失败: {e}")
        return False, None


def test_northbound_flow():
    """测试北向资金行业净流入接口"""
    log("\n" + "=" * 60)
    log("测试 2: 北向资金行业净流入")
    log("=" * 60)

    # 尝试多个可能的接口
    interfaces = [
        ("stock_hsgt_industry_flow", lambda: ak.stock_hsgt_industry_flow(symbol="北向资金")),
        ("stock_hsgt_board_flow_em", lambda: ak.stock_hsgt_board_flow_em(symbol="北向资金")),
    ]

    for name, func in interfaces:
        try:
            df = func()
            log(f"[OK] {name} 成功，共 {len(df)} 条")
            log(f"列名: {list(df.columns)}")
            if len(df) > 0:
                log("\n前5条数据:")
                print(df.head(5).to_string())
            return True, df
        except Exception as e:
            log(f"[FAIL] {name} 失败: {e}")

    return False, None


def test_tiger_list():
    """测试龙虎榜接口"""
    log("\n" + "=" * 60)
    log("测试 3: 龙虎榜")
    log("=" * 60)

    interfaces = [
        ("stock_tiger_list", lambda: ak.stock_tiger_list()),
        ("stock_lhb_detail_em (近一月)", lambda: ak.stock_lhb_detail_em(
            start_date=(datetime.now() - timedelta(days=30)).strftime("%Y%m%d"),
            end_date=datetime.now().strftime("%Y%m%d"),
            symbol="近一月"
        )),
    ]

    for name, func in interfaces:
        try:
            df = func()
            log(f"[OK] {name} 成功，共 {len(df)} 条")
            log(f"列名: {list(df.columns)}")
            if len(df) > 0:
                log("\n前5条数据:")
                print(df.head(5).to_string())
            return True, df
        except Exception as e:
            log(f"[FAIL] {name} 失败: {e}")

    return False, None


def test_block_trade():
    """测试大宗交易接口"""
    log("\n" + "=" * 60)
    log("测试 4: 大宗交易")
    log("=" * 60)

    # 尝试今天和前几天的数据
    for i in range(5):
        try:
            date = (datetime.now() - timedelta(days=i)).strftime("%Y%m%d")
            df = ak.stock_block_trade(date=date)
            log(f"[OK] {date} 大宗交易数据获取成功，共 {len(df)} 条")
            log(f"列名: {list(df.columns)}")
            if len(df) > 0:
                log("\n前5条数据:")
                print(df.head(5).to_string())
            return True, df
        except Exception as e:
            log(f"[FAIL] {date}: {e}")
            time.sleep(1)

    return False, None


def locate_target_sectors(df_sector):
    """定位对标板块（医药、证券）"""
    log("\n" + "=" * 60)
    log("定位对标板块（医药、证券）")
    log("=" * 60)

    if df_sector is None:
        log("无板块数据")
        return

    for keyword in ["医药", "证券", "券商"]:
        # 搜索第一列（板块名称）
        col0 = df_sector.iloc[:, 0].astype(str)
        matches = df_sector[col0.str.contains(keyword, na=False)]
        if len(matches) > 0:
            log(f"\n'{keyword}' 相关板块:")
            print(matches.to_string())
        else:
            log(f"\n未找到 '{keyword}' 相关板块")


def main():
    log("=" * 60)
    log("AkShare 接口可用性测试")
    log(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"运行环境: {'GitHub Actions' if os.getenv('GITHUB_ACTIONS') else '本地'}")
    log("=" * 60)

    results = {}

    # 测试各接口
    results["板块资金流向"], df_sector = test_sector_fund_flow()
    time.sleep(3)

    results["北向资金"], df_north = test_northbound_flow()
    time.sleep(3)

    results["龙虎榜"], df_tiger = test_tiger_list()
    time.sleep(3)

    results["大宗交易"], df_block = test_block_trade()

    # 汇总结果
    log("\n" + "=" * 60)
    log("测试结果汇总")
    log("=" * 60)
    for name, success in results.items():
        status = "[OK] 可用" if success else "[FAIL] 不可用"
        log(f"  {name}: {status}")

    available_count = sum(1 for v in results.values() if v)
    log(f"\n总计: {available_count}/{len(results)} 个接口可用")

    # 定位对标板块
    if results["板块资金流向"]:
        locate_target_sectors(df_sector)

    # 输出 JSON 结果供后续使用
    result_file = "akshare_test_results.json"
    with open(result_file, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "results": results,
            "available_count": available_count,
            "total": len(results)
        }, f, ensure_ascii=False, indent=2)
    log(f"\n结果已保存到 {result_file}")

    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
