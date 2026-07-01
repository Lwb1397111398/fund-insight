#!/usr/bin/env python3
"""
测试直接调用东方财富 API 获取板块资金流向（修正版字段映射）
"""
import sys
import requests

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

url = "https://push2.eastmoney.com/api/qt/clist/get"

params = {
    "pn": 1,
    "pz": 100,
    "po": 1,
    "np": 1,
    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    "fltt": 2,
    "invt": 2,
    "fid": "f267",  # 按主力净流入排序
    "fs": "m:90+t:2",  # 行业板块
    "fields": "f12,f14,f3,f6,f267,f269,f271,f273,f275",
    "_": "1625292448803",
}

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}

print("=" * 60)
print("测试东方财富直接 API（修正版字段映射）")
print("=" * 60)

try:
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    print(f"状态码: {resp.status_code}")

    data = resp.json()

    if "data" in data and data["data"]:
        items = data["data"].get("diff", [])
        print(f"板块数量: {len(items)}")

        if items:
            # 验证字段映射
            print("\n字段映射验证:")
            print("  f267 = 主力净流入 (超大单+大单)")
            print("  f269 = 超大单净流入")
            print("  f271 = 大单净流入")
            print("  f273 = 中单净流入")
            print("  f275 = 小单净流入")

            # 打印前3个板块
            print("\n前3个板块:")
            for item in items[:3]:
                name = item.get("f14", "")
                change = item.get("f3", 0)
                turnover = item.get("f6", 0) / 1e8
                main_flow = item.get("f267", 0) / 1e8
                super_large = item.get("f269", 0) / 1e8
                large = item.get("f271", 0) / 1e8
                medium = item.get("f273", 0) / 1e8
                small = item.get("f275", 0) / 1e8

                print(f"\n  {name}:")
                print(f"    涨跌幅: {change}%")
                print(f"    成交额: {turnover:.2f} 亿")
                print(f"    主力净流入: {main_flow:.2f} 亿")
                print(f"    超大单: {super_large:.2f} 亿")
                print(f"    大单: {large:.2f} 亿")
                print(f"    中单: {medium:.2f} 亿")
                print(f"    小单: {small:.2f} 亿")

                # 验证零和
                total = super_large + large + medium + small
                print(f"    零和验证: {super_large:.2f}+{large:.2f}+{medium:.2f}+{small:.2f} = {total:.2f}")

                # 验证主力 = 超大单+大单
                main_check = super_large + large
                print(f"    主力验证: {super_large:.2f}+{large:.2f} = {main_check:.2f} (应等于 {main_flow:.2f})")

            # 定位对标板块
            print("\n" + "=" * 60)
            print("定位对标板块")
            print("=" * 60)
            for item in items:
                name = item.get("f14", "")
                if "医药" in name or "证券" in name:
                    turnover = item.get("f6", 0) / 1e8
                    main_flow = item.get("f267", 0) / 1e8
                    super_large = item.get("f269", 0) / 1e8
                    large = item.get("f271", 0) / 1e8
                    medium = item.get("f273", 0) / 1e8
                    small = item.get("f275", 0) / 1e8

                    print(f"\n{name}:")
                    print(f"  涨跌幅: {item.get('f3', 0)}%")
                    print(f"  成交额: {turnover:.2f} 亿")
                    print(f"  主力净流入: {main_flow:.2f} 亿")
                    print(f"  超大单: {super_large:.2f} 亿")
                    print(f"  大单: {large:.2f} 亿")
                    print(f"  中单: {medium:.2f} 亿")
                    print(f"  小单: {small:.2f} 亿")
                    print(f"  散户资金（仅小单）: {small:.2f} 亿")
                    print(f"  主力暗盘 = 主力 - 散户 = {main_flow:.2f} - {small:.2f} = {main_flow - small:.2f}")
                    if turnover > 0:
                        intensity = (main_flow - small) / turnover * 100
                        print(f"  主力强度 = {main_flow - small:.2f} / {turnover:.2f} * 100 = {intensity:.2f}")

    else:
        print(f"无数据: {data}")

except Exception as e:
    print(f"请求失败: {e}")
