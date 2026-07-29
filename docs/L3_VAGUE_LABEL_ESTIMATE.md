# L3 模糊硬标占比估计（只读）

- 日期：2026-07-29
- 数据源：`DATABASE_URL`
- 范围：未删除且 prediction_type 为 up/down/bullish/bearish
- **不改**抽取/验证代码；**不部署**本轮新功能

## 判定标准（规则，可复现）

| 标签 | 规则 |
| --- | --- |
| **clear** | 文本含与标签**同向**的明确涨/跌词，且无反向明确词 |
| **vague_hard** | 无明确方向词；或仅有观望/可能/震荡等模糊词；或多空词并存却硬标 up/down |
| **weak** | 有明确词但与标签**反向**（错标信号，另计） |
| **empty** | prediction_content 空 |

明确多：看多/看涨/加仓/突破/…；明确空：看空/看跌/减仓/破位/…
模糊：关注/观望/可能/震荡/分化/择时/中性/博弈/…

## 样本量与结果（全量 up/down）

| 项 | 值 |
| --- | ---: |
| 总样本 (up/down) | **870** |
| clear | 220 (25.3%) |
| **vague_hard** | **606 (69.7%)** |
| weak（反向明确） | 44 (5.1%) |
| empty | 0 |
| 已结论子集 | 254 |

## 决策 A（占比门槛：≥10% 才讨论改造）

- 模糊硬标占比估计：**69.7%**
- **动作（占比门）：立项 other 桶改造**
- 最终是否全套改分母，以 **附录 A 分组裁决 + 附录 B 预注册表** 为准。

## 样例（vague_hard 截断）

- id=2184 type=up correct=None: '短期趋势依然向上，耐心格局'
- id=2185 type=up correct=None: '短期调整到位后仍会震荡向上，耐心卧倒'
- id=2186 type=up correct=None: '耐心等风来'
- id=1363 type=down correct=True: '冲高回落，外加顶背离，短期调整可能增加，控制舱位是核心'
- id=72 type=up correct=None: '估值水平达到了超低估区间，哈基米分别是中证医疗4倍定投，创新药4倍定投。换句话说医药板块，哈基米是8倍定投。越跌越买'
- id=463 type=up correct=True: '石英股份作为原材料供应商，有起来的趋势，可以低吸'
- id=88 type=up correct=None: '创新药由3倍定投修改为4倍定投。'
- id=89 type=up correct=None: '中证医疗也由3倍定投修改为4倍定投。'
- id=91 type=up correct=None: '红利低波和港股红利是哈基米最省心的两个赛道，基本不用怎么多多关注，跌多了就多买点，涨了就少买点，舒服。'
- id=74 type=up correct=None: '跌了继续5倍定投吧'
- id=442 type=up correct=None: '保持每周四的固定定投频率不变'
- id=784 type=up correct=False: '指数完全不担心，我预感节后这波行情会直接顶到7月上旬。'

## 局限

- 词典启发式，非人工金标准；可能低估「向上/低吸」等未入库行话、高估「长文含关注」。
- 未读原帖全文，只看 `prediction_content`。
- 附录 A/B 为改造前裁决；本文件不实施抽取变更。

---

## 附录 A：已验证结论 vague / clear 分组裁决（只读）

- 分母：`is_correct IS NOT NULL` 且未删且 up/down（与上表同一启发式）
- 收益：有 start_nav/current_nav 时，up 用区间收益、down 用负区间收益

| 桶 | n | 正确数 | 命中率 | 平均方向收益 | 收益覆盖 |
| --- | ---: | ---: | ---: | ---: | ---: |
| clear | 70 | 38 | 54.3% | -1.1% | 100.0% |
| vague_hard | 167 | 101 | 60.5% | 0.4% | 100.0% |
| weak | 17 | 14 | 82.4% | 2.3% | 100.0% |
| empty | 0 | 0 | n/a | n/a | 0.0% |

- 已结论总体命中率：60.2%（n=254）
- 已结论中 vague 占比：65.7%；clear 占比：27.6%
- 若假设 vague 为纯噪声 50%、用总体反推 clear 理论命中（示意，非证明）：99.3%

### 附录 A 判读结果（严格执行附录 B，禁止改口）

- **verdict**：`similar_buckets`
- **detail**：clear_hit=54.3%, vague_hit=60.5%, abs_gap=6.2pp<10
- **action**：归档为标签卫生问题，不动命中率分母

---

## 附录 B：预注册判读表（先写规则再跑数）

| 查询结果 | 结论 | 动作 |
| --- | --- | --- |
| vague 命中率 ∈ **[45%, 55%]** 且 **clear − vague ≥ 10pp** | 噪声实锤 | **全套改造**：抽取加 other 类；验证 skip other；命中率**只算 clear**；新增**覆盖率**（clear/全部可标）；历史**不回填**仅前向；L1 shadow **清零重计**并标 `post_other` |
| **|clear - vague| < 10pp** | LLM 在模糊文本里读出了相近信号 | **归档标签卫生**，不动分母 |
| clear 高 >=10pp 但 vague 不在硬币带 | 灰色 | 可前向隔离 other，**不**自动等同噪声实锤，需人工确认 |
| 任一带 n<10 | 样本不足 | 不自动立项 |

本次跑数落入：`similar_buckets`。

### 与 L1 shadow 的交互（插旗）

- 改造会改变「可验证结论」定义；**禁止**把 pre-other 与 post-other 结论混进同一 +150 复评计数。
- 代码侧：`L1_SHADOW_DATA_ERA=pre_other|post_other`，other 上线日写 `L3_OTHER_CUTOVER_AT` 并重置 `L1_SHADOW_BASELINE_VERIFIED` / `L1_SHADOW_STARTED_AT`。
- 详见 `src/services/l1_shadow.reeval_status` 返回的 `data_era` / `era_note`。
