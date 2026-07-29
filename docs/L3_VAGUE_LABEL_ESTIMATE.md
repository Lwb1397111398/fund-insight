# L3 模糊硬标占比估计（只读）

- 日期：2026-07-29
- 数据源：`DATABASE_URL`
- 范围：未删除且 prediction_type 为 up/down/bullish/bearish
- **不改**抽取/验证代码

## 判定标准（规则，可复现）

| 标签 | 规则 |
| --- | --- |
| **clear** | 文本含与标签**同向**的明确涨/跌词，且无反向明确词 |
| **vague_hard** | 无明确方向词；或仅有观望/可能/震荡等模糊词；或多空词并存却硬标 up/down |
| **weak** | 有明确词但与标签**反向**（错标信号，另计） |
| **empty** | prediction_content 空 |

明确多：看多/看涨/加仓/突破/…；明确空：看空/看跌/减仓/破位/…
模糊：关注/观望/可能/震荡/分化/择时/中性/博弈/…

## 样本量与结果

| 项 | 值 |
| --- | ---: |
| 总样本 (up/down) | **870** |
| clear | 220 (25.3%) |
| **vague_hard** | **606 (69.7%)** |
| weak（反向明确） | 44 (5.1%) |
| empty | 0 |
| 已结论子集 | 220 |
| 已结论中 vague_hard | 146 (66.4%) |

## 决策（预注册：≥10% 立项）

- 模糊硬标占比估计：**69.7%**
- **动作：立项 other 桶改造**

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

- 词典启发式，非人工金标准；可能低估「行话暗示方向」、高估「长文含关注二字」。
- 未读原帖全文，只看 `prediction_content`。
- 若立项：加 other/unknown、验证 skip、分母剔除；本报告不实施。
