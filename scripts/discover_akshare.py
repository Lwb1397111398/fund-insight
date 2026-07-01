#!/usr/bin/env python3
"""
发现 AkShare 可用的接口名称
"""
import sys
import akshare as ak

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

# 搜索关键词
keywords = ["sector_fund", "hsgt", "tiger", "lhb", "block_trade", "board_flow"]

print("搜索 AkShare 可用接口:")
print("=" * 60)

for kw in keywords:
    matches = [attr for attr in dir(ak) if kw.lower() in attr.lower()]
    print(f"\n关键词 '{kw}' 找到 {len(matches)} 个:")
    for m in matches[:10]:
        print(f"  - {m}")
    if len(matches) > 10:
        print(f"  ... 还有 {len(matches) - 10} 个")

# 也搜索中文关键词
print("\n" + "=" * 60)
print("搜索板块资金相关:")
sector_matches = [attr for attr in dir(ak) if "sector" in attr.lower() and "fund" in attr.lower()]
for m in sector_matches:
    print(f"  - {m}")

print("\n搜索北向资金:")
north_matches = [attr for attr in dir(ak) if "hsgt" in attr.lower() or "north" in attr.lower()]
for m in north_matches:
    print(f"  - {m}")

print("\n搜索龙虎榜:")
tiger_matches = [attr for attr in dir(ak) if "tiger" in attr.lower() or "lhb" in attr.lower()]
for m in tiger_matches:
    print(f"  - {m}")

print("\n搜索大宗交易:")
block_matches = [attr for attr in dir(ak) if "block" in attr.lower() and "trade" in attr.lower()]
for m in block_matches:
    print(f"  - {m}")
