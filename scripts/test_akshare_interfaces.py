#!/usr/bin/env python3
"""
AkShare 接口可用性测试脚本（修正版）
使用已确认存在的接口名称。

注意：此脚本需要在国内 IP 环境运行（如 GitHub Actions）。
"""

import sys
import os
import time
import json
import akshare as ak
import pandas as pd
from datetime import datetime, timedelta

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


def test_sector_fund_flow():
    """测试板块资金流向接口"""
    log("=" * 60)
    log("测试 1: 板块资金流向 (stock_sector_fund_flow_rank)")
    log("=" * 60)

    try:
        df = ak.stock_sector_fund_flow_rank(indicator="今日", sector_type="行业资金流")
        log(f"[OK] 行业板块数据获取成功，共 {len(df)} 条")
        log(f"列名: {list(df.columns)}")
        if len(df) > 0:
            log("\n前3条数据:")
            print(df.head(3).to_string())
        return True, df
    except Exception as e:
        log(f"[FAIL] 获取失败: {e}")
        return False, None


def test_northbound_flow():
    """测试北向资金接口"""
    log("\n" + "=" * 60)
    log("测试 2: 北向资金行业净流入")
    log("=" * 60)

    # 尝试多个接口
    interfaces = [
        ("stock_hsgt_board_rank_em", lambda: ak.stock_hsgt_board_rank_em(symbol="北向资金", indicator="今日")),
        ("stock_hsgt_fund_flow_summary_em", lambda: ak.stock_hsgt_fund_flow_summary_em()),
        ("stock_hsgt_stock_statistics_em", lambda: ak.stock_hsgt_stock_statistics_em(symbol="北向资金", indicator="今日排行")),
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
        ("stock_lhb_jgmmtj_em (近一月)", lambda: ak.stock_lhb_jgmmtj_em(
            symbol="近一月",
            start_date=(datetime.now() - timedelta(days=30)).strftime("%Y%m%d"),
            end_date=datetime.now().strftime("%Y%m%d"),
        )),
        ("stock_lhb_detail_em (近一月)", lambda: ak.stock_lhb_detail_em(
            start_date=(datetime.now() - timedelta(days=30)).strftime("%Y%m%d"),
            end_date=datetime.now().strftime("%Y%m%d"),
        )),
        ("stock_lhb_stock_statistic_em", lambda: ak.stock_lhb_stock_statistic_em(symbol="近一月")),
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

    # 尝试可能的接口名
    possible_names = [
        "stock_dzjy_mrtj",
        "stock_dzjy_sctj",
        "stock_dzjy_mrmx",
        "stock_block_trade",
        "stock_bulk_trade",
    ]

    for name in possible_names:
        if hasattr(ak, name):
            log(f"找到接口: {name}")
            try:
                func = getattr(ak, name)
                df = func()
                log(f"[OK] {name} 成功，共 {len(df)} 条")
                log(f"列名: {list(df.columns)}")
                if len(df) > 0:
                    log("\n前5条数据:")
                    print(df.head(5).to_string())
                return True, df
            except Exception as e:
                log(f"[FAIL] {name} 失败: {e}")
        else:
            log(f"[SKIP] {name} 不存在")

    # 搜索 dzjy 相关
    dzjy_matches = [attr for attr in dir(ak) if "dzjy" in attr.lower()]
    if dzjy_matches:
        log(f"找到 dzjy 相关接口: {dzjy_matches}")
    else:
        log("未找到大宗交易相关接口")

    return False, None


def main():
    log("=" * 60)
    log("AkShare 接口可用性测试")
    log(f"测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"运行环境: {'GitHub Actions' if os.getenv('GITHUB_ACTIONS') else '本地'}")
    log("=" * 60)

    results = {}

    results["板块资金流向"], df_sector = test_sector_fund_flow()
    time.sleep(3)

    results["北向资金"], df_north = test_northbound_flow()
    time.sleep(3)

    results["龙虎榜"], df_tiger = test_tiger_list()
    time.sleep(3)

    results["大宗交易"], df_block = test_block_trade()

    # 汇总
    log("\n" + "=" * 60)
    log("测试结果汇总")
    log("=" * 60)
    for name, success in results.items():
        status = "[OK] 可用" if success else "[FAIL] 不可用"
        log(f"  {name}: {status}")

    available_count = sum(1 for v in results.values() if v)
    log(f"\n总计: {available_count}/{len(results)} 个接口可用")

    # 定位对标板块
    if df_sector is not None:
        log("\n" + "=" * 60)
        log("定位对标板块")
        log("=" * 60)
        for keyword in ["医药", "证券", "券商"]:
            col0 = df_sector.iloc[:, 0].astype(str)
            matches = df_sector[col0.str.contains(keyword, na=False)]
            if len(matches) > 0:
                log(f"\n'{keyword}' 相关板块:")
                print(matches.to_string())

    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
