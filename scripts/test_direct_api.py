#!/usr/bin/env python3
"""
测试直接调用东方财富 API 获取板块资金流向
"""
import sys
import requests
import json

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# 东方财富板块资金流向 API
# fs=m:90+t:2 表示行业板块
# f12=板块代码, f14=板块名称, f3=涨跌幅, f62=主力净流入
# f66=超大单净流入, f69=大单净流入, f72=中单净流入, f75=小单净流入
# f6=成交额
url = "https://push2.eastmoney.com/api/qt/clist/get"

params = {
    "pn": 1,
    "pz": 100,
    "po": 1,
    "np": 1,
    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    "fltt": 2,
    "invt": 2,
    "fid": "f62",
    "fs": "m:90+t:2",  # 行业板块
    "fields": "f12,f14,f3,f62,f66,f69,f72,f75,f6",
    "_": "1625292448803",
}

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}

print("=" * 60)
print("测试东方财富直接 API")
print("=" * 60)

try:
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    print(f"状态码: {resp.status_code}")

    data = resp.json()
    print(f"返回数据结构: {list(data.keys()) if isinstance(data, dict) else type(data)}")

    if "data" in data and data["data"]:
        items = data["data"].get("diff", [])
        print(f"板块数量: {len(items)}")

        if items:
            print("\n前3个板块:")
            for item in items[:3]:
                name = item.get("f14", "")
                change = item.get("f3", 0)
                main_flow = item.get("f62", 0)
                super_large = item.get("f66", 0)
                large = item.get("f69", 0)
                medium = item.get("f72", 0)
                small = item.get("f75", 0)
                turnover = item.get("f6", 0)

                print(f"\n  {name}:")
                print(f"    涨跌幅: {change}%")
                print(f"    成交额: {turnover/1e8:.2f} 亿")
                print(f"    主力净流入: {main_flow/1e8:.2f} 亿")
                print(f"    超大单: {super_large/1e8:.2f} 亿")
                print(f"    大单: {large/1e8:.2f} 亿")
                print(f"    中单: {medium/1e8:.2f} 亿")
                print(f"    小单: {small/1e8:.2f} 亿")

                # 验证零和
                total = super_large + large + medium + small
                print(f"    零和验证: {super_large}+{large}+{medium}+{small} = {total}")

            # 查找医药和证券
            print("\n" + "=" * 60)
            print("定位对标板块")
            print("=" * 60)
            for item in items:
                name = item.get("f14", "")
                if "医药" in name or "证券" in name:
                    print(f"\n{name}:")
                    print(f"  超大单: {item.get('f66', 0)/1e8:.2f} 亿")
                    print(f"  大单: {item.get('f69', 0)/1e8:.2f} 亿")
                    print(f"  中单: {item.get('f72', 0)/1e8:.2f} 亿")
                    print(f"  小单: {item.get('f75', 0)/1e8:.2f} 亿")
                    print(f"  成交额: {item.get('f6', 0)/1e8:.2f} 亿")
    else:
        print(f"无数据: {data}")

except Exception as e:
    print(f"请求失败: {e}")
