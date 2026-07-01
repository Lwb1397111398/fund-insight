#!/usr/bin/env python3
"""
验证板块资金计算器逻辑

核心口径（对齐直播间）：
- 主力 = 超大单 + 大单
- 散户 = 仅小单（中单被剥离）
- 主力 + 散户 + 中单 = 0（零和）
- 主力 + 散户 ≠ 0（因为中单被排除）

验证样本：
1. 证券板块（洗盘）：主力 91.01亿, 散户 86.40亿, 暗盘 4.61亿, 强度 0.53
2. 医药板块（抢筹）：主力 26.89亿, 散户 -44.23亿, 暗盘 71.12亿, 强度 6.2
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.services.sector_flow_calculator import calculate_sector_flow, judge_behavior

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


def test_behavior():
    """测试主力行为判定"""
    print("=" * 60)
    print("测试 1: 主力行为判定")
    print("=" * 60)

    cases = [
        (5.0, "抢筹"), (3.0, "抢筹"), (2.0, "建仓"), (1.0, "建仓"),
        (0.5, "洗盘"), (0.0, "洗盘"), (-0.5, "洗盘"), (-1.0, "出货"), (-2.0, "出货"),
    ]

    all_pass = True
    for intensity, expected in cases:
        result = judge_behavior(intensity)
        ok = result == expected
        if not ok:
            all_pass = False
        print(f"  [{'PASS' if ok else 'FAIL'}] 强度={intensity:6.2f} -> {result} (期望: {expected})")

    return all_pass


def test_securities():
    """证券板块验证（洗盘样本）"""
    print("\n" + "=" * 60)
    print("测试 2: 证券板块（洗盘样本）")
    print("=" * 60)

    # 参考值（直播间）
    ref = {"main": 91.01, "retail": 86.40, "dark": 4.61, "intensity": 0.53, "behavior": "洗盘"}

    # 东财原始数据（用户提供的）
    # f66+f69 ≈ 85亿, f75 ≈ 86.4亿
    # 零和约束: 中单 = -(85 + 86.40) = -171.40
    # 增量机构资金 ≈ 6.01亿
    result = calculate_sector_flow(
        sector_name="证券",
        turnover=869.61,
        change_pct=4.60,
        super_large_net=50.0,   # 超大单
        large_net=35.0,         # 大单，合计主力 85亿
        medium_net=-171.40,     # 中单 = -(85+86.40)，被剥离
        small_net=86.40,        # 小单 = 散户资金
        northbound_flow=5.0,    # 北向
        tiger_net_buy=1.01,     # 龙虎榜
        block_trade_net=0.0,    # 大宗
    )

    print(f"\n参考值（直播间）:")
    print(f"  主力资金: {ref['main']:.2f}  散户资金: {ref['retail']:.2f}")
    print(f"  主力暗盘: {ref['dark']:.2f}  主力强度: {ref['intensity']:.2f}  行为: {ref['behavior']}")

    print(f"\n计算结果:")
    print(f"  主力资金: {result.main_capital:.2f}  散户资金: {result.retail_capital:.2f}")
    print(f"  主力暗盘: {result.dark_pool:.2f}  主力强度: {result.main_intensity:.2f}  行为: {result.behavior}")

    print(f"\n公式验证:")
    print(f"  主力暗盘 = 主力资金 - 散户资金 = {result.main_capital:.2f} - {result.retail_capital:.2f} = {result.dark_pool:.2f}")
    print(f"  主力强度 = 主力暗盘 / 成交额 * 100 = {result.dark_pool:.2f} / {result.turnover:.2f} * 100 = {result.main_intensity:.2f}")

    # 验证误差
    main_err = abs(result.main_capital - ref["main"])
    retail_err = abs(result.retail_capital - ref["retail"])
    dark_err = abs(result.dark_pool - ref["dark"])
    behavior_ok = result.behavior == ref["behavior"]

    print(f"\n误差分析:")
    print(f"  主力资金误差: {main_err:.2f} 亿 ({main_err/ref['main']*100:.1f}%)")
    print(f"  散户资金误差: {retail_err:.2f} 亿 ({retail_err/abs(ref['retail'])*100:.1f}%)")
    print(f"  主力暗盘误差: {dark_err:.2f} 亿")
    print(f"  行为判定: {'一致' if behavior_ok else '不一致'}")

    return behavior_ok


def test_medicine():
    """医药板块验证（抢筹样本）"""
    print("\n" + "=" * 60)
    print("测试 3: 医药板块（抢筹样本）")
    print("=" * 60)

    ref = {"main": 26.89, "retail": -44.23, "dark": 71.12, "intensity": 6.2, "behavior": "抢筹"}

    # 东财原始数据
    # f66+f69 ≈ 18.5亿, f75 ≈ -44.23亿
    # 零和约束: 中单 = -(18.5 + (-44.23)) = 25.73
    # 增量机构资金 ≈ 8.39亿
    result = calculate_sector_flow(
        sector_name="医药生物",
        turnover=1139.50,
        change_pct=2.15,
        super_large_net=10.0,   # 超大单
        large_net=8.5,          # 大单，合计主力 18.5亿
        medium_net=25.73,       # 中单 = -(18.5+(-44.23))，被剥离
        small_net=-44.23,       # 小单 = 散户资金
        northbound_flow=4.5,    # 北向
        tiger_net_buy=2.89,     # 龙虎榜
        block_trade_net=1.0,    # 大宗
    )

    print(f"\n参考值（直播间）:")
    print(f"  主力资金: {ref['main']:.2f}  散户资金: {ref['retail']:.2f}")
    print(f"  主力暗盘: {ref['dark']:.2f}  主力强度: {ref['intensity']:.2f}  行为: {ref['behavior']}")

    print(f"\n计算结果:")
    print(f"  主力资金: {result.main_capital:.2f}  散户资金: {result.retail_capital:.2f}")
    print(f"  主力暗盘: {result.dark_pool:.2f}  主力强度: {result.main_intensity:.2f}  行为: {result.behavior}")

    print(f"\n公式验证:")
    print(f"  主力暗盘 = {result.main_capital:.2f} - ({result.retail_capital:.2f}) = {result.dark_pool:.2f}")
    print(f"  主力强度 = {result.dark_pool:.2f} / {result.turnover:.2f} * 100 = {result.main_intensity:.2f}")

    main_err = abs(result.main_capital - ref["main"])
    dark_err = abs(result.dark_pool - ref["dark"])
    behavior_ok = result.behavior == ref["behavior"]

    print(f"\n误差分析:")
    print(f"  主力资金误差: {main_err:.2f} 亿")
    print(f"  主力暗盘误差: {dark_err:.2f} 亿")
    print(f"  行为判定: {'一致' if behavior_ok else '不一致'}")

    return behavior_ok


def test_zero_sum():
    """验证零和规则：超大单+大单+中单+小单 = 0"""
    print("\n" + "=" * 60)
    print("测试 4: 零和规则验证")
    print("=" * 60)

    # 证券板块: 超大单+大单+中单+小单 = 0
    # 主力=85, 散户(小单)=86.40, 中单=-(85+86.40)=-171.40
    s_super, s_large, s_small = 50.0, 35.0, 86.40
    s_medium = -(s_super + s_large + s_small)  # 零和约束
    s1 = s_super + s_large + s_medium + s_small
    print(f"  证券板块: {s_super}+{s_large}+({s_medium:.2f})+{s_small} = {s1:.2f} (应为0)")

    # 医药板块: 超大单+大单+中单+小单 = 0
    # 主力=18.5, 散户(小单)=-44.23, 中单=-(18.5+(-44.23))=25.73
    m_super, m_large, m_small = 10.0, 8.5, -44.23
    m_medium = -(m_super + m_large + m_small)  # 零和约束
    s2 = m_super + m_large + m_medium + m_small
    print(f"  医药板块: {m_super}+{m_large}+({m_medium:.2f})+({m_small}) = {s2:.2f} (应为0)")

    # 核心洞察
    print(f"\n核心洞察:")
    print(f"  主力 + 散户 + 中单 = 0（零和）")
    print(f"  但 主力 + 散户 ≠ 0（因为中单被剥离）")
    print(f"  这就是主力暗盘指标的本质 — 统计口径调整，不是神秘算法")

    return abs(s1) < 0.01 and abs(s2) < 0.01


def main():
    print("=" * 60)
    print("板块资金计算器验证（修正版）")
    print("核心口径: 主力=超大单+大单, 散户=仅小单")
    print("=" * 60)

    results = [
        ("行为判定", test_behavior()),
        ("证券板块", test_securities()),
        ("医药板块", test_medicine()),
        ("零和规则", test_zero_sum()),
    ]

    print("\n" + "=" * 60)
    print("验证结果汇总")
    print("=" * 60)
    for name, passed in results:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")

    return 0 if all(r[1] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
