#!/usr/bin/env python3
"""
测试不同的板块类型参数，找到申万一级行业
"""
import sys
import requests

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

url = "https://push2.eastmoney.com/api/qt/clist/get"

# 不同的板块类型参数
SECTOR_TYPES = {
    "m:90+t:2": "当前使用（行业板块）",
    "m:90+t:3": "概念板块",
    "m:90+t:1": "可能的申万一级",
    "m:90+t:4": "可能的申万一级",
    "m:90+t:5": "可能的申万一级",
    "m:90+t:6": "可能的申万一级",
    "m:90+t:7": "可能的申万一级",
    "m:90+t:8": "可能的申万一级",
    "m:90+t:9": "可能的申万一级",
    "m:90+t:10": "可能的申万一级",
    "m:90+t:11": "可能的申万一级",
    "m:90+t:12": "可能的申万一级",
    "m:90+t:13": "可能的申万一级",
    "m:90+t:14": "可能的申万一级",
    "m:90+t:15": "可能的申万一级",
}

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}

print("=" * 60)
print("测试不同的板块类型参数")
print("=" * 60)

for fs, desc in SECTOR_TYPES.items():
    params = {
        "pn": 1,
        "pz": 5,  # 只取5条
        "po": 1,
        "np": 1,
        "ut": "bd1d9ddb04089700cf9c27f6f7426281",
        "fltt": 2,
        "invt": 2,
        "fid": "f267",
        "fs": fs,
        "fields": "f12,f14",
        "_": "1625292448803",
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        data = resp.json()

        if "data" in data and data["data"]:
            items = data["data"].get("diff", [])
            count = data["data"].get("total", 0)
            names = [item.get("f14", "") for item in items[:5]]
            print(f"\n{fs} ({desc}):")
            print(f"  总数: {count}")
            print(f"  示例: {', '.join(names)}")
        else:
            print(f"\n{fs} ({desc}): 无数据")
    except Exception as e:
        print(f"\n{fs} ({desc}): 错误 - {e}")
