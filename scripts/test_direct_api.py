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
url = "https://push2.eastmoney.com/api/qt/clist/get"

# 请求所有可能的资金流向字段
params = {
    "pn": 1,
    "pz": 5,  # 只取5条用于调试
    "po": 1,
    "np": 1,
    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    "fltt": 2,
    "invt": 2,
    "fid": "f62",
    "fs": "m:90+t:2",  # 行业板块
    # 请求所有字段
    "fields": "f12,f14,f3,f62,f66,f69,f72,f75,f6,f184,f66,f69,f72,f75,f84,f85,f116,f117,f128,f140,f141,f136,f137,f138,f139,f142,f143,f144,f145,f146,f147,f148,f149,f150,f151,f152,f153,f154,f155,f156,f157,f158,f159,f160,f161,f162,f163,f164,f165,f166,f167,f168,f169,f170,f171,f172,f173,f174,f175,f176,f177,f178,f179,f180,f181,f182,f183,f184,f185,f186,f187,f188,f189,f190,f191,f192,f193,f194,f195,f196,f197,f198,f199,f200,f201,f202,f203,f204,f205,f206,f207,f208,f209,f210,f211,f212,f213,f214,f215,f216,f217,f218,f219,f220,f221,f222,f223,f224,f225,f226,f227,f228,f229,f230,f231,f232,f233,f234,f235,f236,f237,f238,f239,f240,f241,f242,f243,f244,f245,f246,f247,f248,f249,f250,f251,f252,f253,f254,f255,f256,f257,f258,f259,f260,f261,f262,f263,f264,f265,f266,f267,f268,f269,f270,f271,f272,f273,f274,f275,f276,f277,f278,f279,f280,f281,f282,f283,f284,f285,f286,f287,f288,f289,f290,f291,f292,f293,f294,f295,f296,f297,f298,f299,f300",
    "_": "1625292448803",
}

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}

print("=" * 60)
print("测试东方财富直接 API - 字段探测")
print("=" * 60)

try:
    resp = requests.get(url, params=params, headers=headers, timeout=10)
    print(f"状态码: {resp.status_code}")

    data = resp.json()

    if "data" in data and data["data"]:
        items = data["data"].get("diff", [])
        print(f"板块数量: {len(items)}")

        if items:
            # 打印第一个板块的所有字段
            print("\n第一个板块的所有字段:")
            first = items[0]
            for key, value in sorted(first.items()):
                print(f"  {key}: {value}")

            # 打印证券板块的数据
            print("\n" + "=" * 60)
            print("证券板块数据:")
            for item in items:
                name = item.get("f14", "")
                if "证券" in name:
                    print(f"\n{name}:")
                    for key, value in sorted(item.items()):
                        print(f"  {key}: {value}")
                    break

except Exception as e:
    print(f"请求失败: {e}")
