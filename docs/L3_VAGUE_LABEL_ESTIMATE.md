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

## 附录 C：clear 桶标签审计 + bucket×tier 交叉表（只读）

- 日期：2026-07-30
- 数据源：`DATABASE_URL`
- **不改生产、不 push**
- 复读方法：对 clear 桶随机 50 条（seed=20260729）做**否定/条件句敏感**规则复读（非金标准人工，可复现）
- 博主 tier：存活已结论 n≥10 → empirical；1–9 → prior；0 → neutral

### C1 预注册判读表

| 结果 | 动作 |
| --- | --- |
| clear 抽样**错配率 ≥15%** | **方向标签 bug 立项**修抽取（否定/条件句），P0 |
| 错配率 **&lt;15%** 且交叉表显示 clear 差主要由 prior 拖累 | 成分效应，记录观察 |
| 错配率 **&lt;15%** 且成分未解释 clear 异常 | **信号备忘录**移交基金线（清晰喊单降权/反指候选） |

### C2 clear 抽样审计

| 项 | 值 |
| --- | ---: |
| clear 全量 | 220 |
| 抽样 | 50 |
| **错配数** | **4** |
| **错配率** | **8.0%** |
| 原因计数 | net_polarity_bull_vs_down:2, only_negated_bear_words:2, net_polarity_bear_vs_up:2, only_negated_bull_words:2 |

#### 错配样例（截断）

- id=1051 type=down correct=None reasons=['net_polarity_bull_vs_down', 'only_negated_bear_words']: '等到大部分人都上车了，主力就要撤退了，最后肯定是一地鸡毛，留下广大散户站岗，然后随着不断下跌，各种利空也会慢慢释放出来。'
- id=2337 type=down correct=None reasons=['net_polarity_bull_vs_down', 'only_negated_bear_words']: '科技再次启动大行情的前提是大部分普通散户先坚持不住卖出，重仓科技的要么尽快找机会跑，要么坚持到底忍受波动调整'
- id=1997 type=up correct=False reasons=['net_polarity_bear_vs_up', 'only_negated_bull_words']: '新建主动基，小买一点儿看看能不能走一波反弹'
- id=1388 type=up correct=None reasons=['net_polarity_bear_vs_up', 'only_negated_bull_words']: '不影响长期上涨逻辑'

#### 非错配样例

- id=752 type=up: '创业板指接近前高，按牛市不言顶的节奏，突破是迟早的'
- id=1977 type=up: '在算力回撤前，纳斯达克属于值得重仓的方向，当时更看好纳斯达克，现在回撤真正发生，可以两者较均衡地加仓，但优先加均衡的全球科技'
- id=2072 type=down: '科技这波调整，短期趋势走弱，不拿，等企稳再接回来'

### C3 bucket × tier 交叉表（仅已结论）

| bucket | tier | n | 命中率 | 方向收益 | 收益n |
| --- | --- | ---: | ---: | ---: | ---: |
| clear | empirical | 63 | 50.8% | -2.2% | 63 |
| clear | prior | 13 | 61.5% | -1.3% | 13 |
| vague_hard | empirical | 152 | 56.6% | -0.4% | 152 |
| vague_hard | prior | 33 | 60.6% | 0.7% | 33 |
| weak | empirical | 15 | 80.0% | 1.9% | 15 |
| weak | prior | 3 | 100.0% | 24.9% | 3 |

- 成分读数：clear×empirical hit=50.8% (n=63) vs clear×prior 61.5% (n=13), gap=-10.7pp

### C4 裁决（按 C1，禁止改口）

- **verdict**：`signal_candidate`
- **错配率**：8.0%
- **action**：错配率<15% 且交叉表未显示「仅 prior 拖累」→ 写信号备忘录移交基金线（清晰喊单类或需降权/反指候选）；shadow 期继续观察 weak 桶

### C5 信号备忘录草稿（仅当 verdict=signal_candidate 时启用）

- 正式备忘录：**`docs/SIGNAL_LOUD_CALLS_MEMO.md`**（含 loud=late、双 tier 收益为负、8/26 移交、观察期禁动权重）。
- 观察：附录 A 中 clear 命中 54.3%/收益为负，vague 60.5%/收益略正；**两 tier 的 clear 皆命中≥50% 且收益为负**。
- 基金线候选（仅评审，不进生产）：对 clear/激动措辞**降权**观察；weak 谨慎措辞 n 仍小。
- 复现：`scripts/audit_l3_clear_labels.py` seed=20260729，启发式 **loud-calls-heuristics.v1**。

---

## 附录 D：附录间 n 对账（一行式）

| 快照 | 已结论 n | clear | vague_hard | weak | 说明 |
| --- | ---: | ---: | ---: | ---: | --- |
| 附录 A（estimate 脚本跑数日） | **254** | 70 | 167 | **17** | 70+167+17=254 |
| 附录 C（audit 脚本稍后跑） | **279** | 76 | 185 | **18** | 63+13+152+33+15+3=279 |
| 差值 | **+25** | +6 | +18 | **+1** | |

**原因（一行）**：两次只读跑数之间生产库**新写入存活已结论**（验证任务持续跑），非口径变更、非 soft-delete 回流；weak **未缩减**（17→18）。若口头出现「276 / weak 15」，276 为中间态未落文档，**15 实为 C 表 weak×empirical 子行**（非 weak 全桶）。
