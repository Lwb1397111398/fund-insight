# L1 设计：按命中率 Beta 收缩加权博主（证据层）

- 状态：**已实现；正式 flag 关；shadow 双跑默认开；回测决策 shadow**
- 日期：2026-07-29
- 依赖：Step 1 已钉死「准确率 = 存活命中率」
- 作用面：仅 `AdviceEvidenceBuilder` 证据权重（选博主 / `reliability_score` / 预测 `weight`）
- **服务路径**：legacy（`ADVICE_L1_HIT_WEIGHTING=0`）
- **shadow**：`ADVICE_L1_SHADOW=1` 双跑 l1 只写 `meta.l1_shadow` + `data/l1_shadow.jsonl`，不进 LLM
- **复评触发**：新增存活结论 ≥150 **或** 自 `L1_SHADOW_STARTED_AT` 起满 6 周（`l1_shadow.reeval_status`）
- 代码：`l1_weighting.py`、`l1_shadow.py`、`advice_evidence.py`、config/render env
- 报告：`L1_WEIGHTING_BACKTEST_REPORT.md`；L3：`L3_OTHER_BUCKET_AUDIT.md`、`L3_VAGUE_LABEL_ESTIMATE.md`

---

## 0. 现状问题（为何必须改）

当前 `src/services/advice_evidence.py`：

| 点 | 现状 | 问题 |
| --- | --- | --- |
| 筛选 | `Blogger.accuracy_rate >= 50` + `total_predictions >= 3` | 用的是**加权评分**，与命中率主指标错位（排名翻转 16/17） |
| 排序 | `order_by(accuracy_rate.desc())` | 同上 |
| 收缩 | `reliability = 50 + (acc-50)*min(1, n/10)` | 仍锚在加权分；n=1 的 100% 会冲高 |
| 权重 | `weight = (rel/100)*(conf/100)` | 无探索下限，易富者愈富 |
| meta | `weight_strategy_version = p0.global_accuracy.v1` | 版本位可替换 |

Step1 翻转证据：加权第 3（75 分）命中仅 57% → 命中榜第 11。裸加权进建议 = 把抛硬币选手抬进证据。

---

## 1. 硬约束（顾问四条件，缺一不开闸）

### (a) Beta 收缩 —— 禁止裸命中率

样本约 220 结论 / 17 博主，人均 ~9；n=9 时 95% CI 约 ±32pp。裸比率 = 噪声当信号。

**后验均值（Beta–Binomial）**：

```text
p̂_i = (c_i + α · p₀) / (n_i + α)
```

| 符号 | 含义 | 默认 |
| --- | --- | --- |
| `c_i` | 存活命中：`is_correct=true ∧ is_deleted=false` 条数 | 实时统计 |
| `n_i` | 存活已结论：`is_correct IS NOT NULL ∧ is_deleted=false` | 实时统计 |
| `p₀` | 全局先验命中率 | **0.609**（可配置；上线前用生产全量再算一次校准值） |
| `α` | 先验强度（等价伪样本数） | **15**（可配置，建议扫描 10/15/20） |

效果直觉：

- n=0 → p̂ = p₀（完全先验）
- 5/8 ≈ 0.625 原始 → 收缩后约 **0.615**（几乎不动）
- 30/40 = 0.75 → 收缩后约 **0.711**（显著上浮但仍有阻尼）
- 1/1 = 1.0 → 收缩后约 **0.633**（小样本 100% 不再冲顶）

实现输出两套字段（审计用）：

- `hit_rate_raw`：c/n（n=0 为 null）
- `hit_rate_shrunk`：p̂（0–1）
- `reliability_score`：p̂ × 100（与现字段同量纲 0–100，便于 LLM/旧代码）

**禁止**：`reliability ∝ hit_rate_raw` 且无收缩。

### (b) min_n 门槛 + 新博主中性

| 规则 | 默认 | 行为 |
| --- | --- | --- |
| `min_n` | **10** | `n_i < min_n` → **不**用 raw 排挤；p̂ 仍用 Beta（等价强先验），并打标 `evidence_tier=prior` |
| 新博主 / 无结论 | n=0 | p̂ = p₀，`evidence_tier=neutral`，**不惩罚不优待** |
| TOP 展示门槛 | 已有 ≥5（API） | **与 L1 证据 min_n 分离**；展示可严可松，证据层默认 10 |

筛选逻辑相对现状变更：

1. **不再**用 `accuracy_rate` 做硬过滤主条件。
2. 活跃且有可行动预测的博主一律可进 map（补全 missing 逻辑保留）。
3. 排序 / top_bloggers 池：按 `hit_rate_shrunk`（或 p̂）降序，但 `n < min_n` 的人排在「有足够样本」之后、同 tier 内再比 p̂（避免 n=1 伪高占满 top15）。
4. 可选软门槛：`n >= min_n` 的优先填满 top_bloggers，不足再用 prior/neutral 补位（记 exclusion reason）。

### (c) 探索下限（防富者愈富）

预测合成权重（相对现状升级）：

```text
w_raw = p̂_i * (confidence_j / 100)     # p̂ 已是 0–1
w     = max(w_floor, w_raw)             # 默认 w_floor = 0.4 * median(w_raw) 或绝对下限
```

**默认参数**：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `weight_floor_mode` | `relative` | `relative`：`floor = floor_ratio × mean(p̂_active)`；`absolute`：固定下限 |
| `floor_ratio` | **0.4** | 相对模式：权重不低于「活跃博主平均 p̂」的 40% |
| `absolute_floor` | 0.15 | absolute 模式备用（p̂×conf 量纲下） |

另：**每博主 cap**（已有 `max_predictions_per_blogger=3`）保留，防止单人刷票；下限保证低 p̂ 博主仍有流量进入验证闭环。

审计：每条预测带 `weight`、`weight_floored: bool`、`blogger_p_hat`。

### (d) Walk-forward 回测（硬门槛）

**目的**：证明「收缩加权」相对「等权」在历史切片上不伤（最好提升）聚合命中率；有净值时同时看收益。

#### 数据与防泄漏

- 仅用 `as_of = t` **之前**已落地的结论算权重：  
  `Prediction.is_correct IS NOT NULL AND is_deleted=false AND COALESCE(verified_at::date, target_date) < t`
- 评估对象：`prediction_date < t` 且 `target_date` 落在测试窗、之后已验证的预测（或按 target 到期日滚动）。
- **禁止**用全样本命中率回写 t 时刻权重。

#### 切片方案（数据仅 ~7.5 周时的务实版）

| 方案 | 做法 | 何时用 |
| --- | --- | --- |
| **A. 时序对半** | 按 `verified_at`/`target_date` 中位数切 train/test | 样本极薄时的方向性信号（最低交付） |
| **B. 滚动周** | 每周五 as_of：用过去全部历史算权，评估未来 7 天到期结论 | 有 ≥6 个可评周时 |
| **C. 扩展** | 样本变厚后改为标准 walk-forward（训 4 周 / 测 1 周滚动） | 后续 |

默认交付 **A + 若周数够则 B**。

#### 对比臂

| 臂 | 权重 |
| --- | --- |
| `equal` | 所有入选预测 w=1（或仅 conf） |
| `legacy` | 现网 `accuracy_rate` + 简单 shrink（`p0.global_accuracy.v1`） |
| `l1_beta` | 本文 Beta + min_n + floor（`l1.hit_beta.v1`） |

#### 指标（双轨）

1. **命中率（主，先行）**  
   - 无权重：等权命中率  
   - 有权重：Σ w·1_correct / Σ w（仅对已结论样本）  
   - 报告：绝对差、相对差、各臂 n
2. **收益（终局，有净值才算）**  
   - 对每条可匹配 `fund_code` 的预测：方向 × (end_nav/start_nav - 1)  
   - 多空简化：bullish 用区间收益，bearish 用负区间收益；无净值跳过并计 `return_coverage`  
   - 聚合：等权与加权平均收益；**不**承诺组合回测引擎完整度  
3. **稳定性**  
   - 权重分布熵 / top20% 权重占比（富者愈富诊断）  
   - 被 floor 抬起的比例

#### 预注册决策规则（写进设计后再跑回测；禁止数字出来后改口）

| 回测结果 | 动作 |
| --- | --- |
| `l1_beta` 命中率 **> equal 且 > legacy**（默认边际 >1pp），且收益不劣化（`return_coverage≥30%` 时加权收益 ≥ equal） | **开闸**（允许将 flag 置 on，仍需人工点确认） |
| `l1_beta` **输给任一臂**（低于该臂 >1pp） | **不开闸**，查因（α/floor/泄漏/样本） |
| 样本太薄或命中率落在 **±1pp 平局带**、无法判胜负 | **不开闸**；转 **shadow**（双跑只记录不生效），积累 N 周实盘后再复评 |

说明：

- 「分不出胜负」是短窗数据的**预期结局**，不得硬解读为开闸。  
- shadow 实现可后置；决策上先记 `action=shadow`。  
- 收益覆盖 <30% 时收益项记 `n/a`，**不单独因收益缺测而开闸**。

#### 交付物

- 脚本：`scripts/backtest_l1_weighting.py`（只读 DB）  
- 报告：`docs/L1_WEIGHTING_BACKTEST_REPORT.md`（数字 + **按上表**动作）  
- **无报告不得开 flag**；有报告也必须落在「开闸」行才允许改 env

---

## 1.5 Legacy 臂 = 线上真实行为（确认）

| 项 | 结论 |
| --- | --- |
| `Blogger.accuracy_rate` 写入 | **仍是加权评分** `Σverify_score/(n×100)×100`（`src/utils/blogger_stats.py`） |
| Step1 变更 | API **另算** `hit_rate`（存活 is_correct）；**不**回写 `accuracy_rate` |
| EvidenceBuilder flag 关 | 筛选/排序/reliability **吃 `accuracy_rate`** + `50+(acc-50)*min(1,n/10)` |
| 与展示口径 | 展示已按命中率；建议证据 flag 关时仍按加权分 → 正是 L1 要修的内外不一致 |
| legacy 臂 | **= 上述 flag 关路径**；对比基准真实，不是“旧 hit_rate” |

差异记录：物化分母还排除 `prediction_type==flat` 且要求 `verify_count>0`；命中率分母是 `is_correct IS NOT NULL ∧ ¬deleted`（可含非 flat 的已结论）。回测 legacy 用表上 `accuracy_rate` 快照近似（无历史逐日重算）。

---

## 2. 特征开关与版本

| 项 | 值 |
| --- | --- |
| Env | `ADVICE_L1_HIT_WEIGHTING=0`（默认关） |
| 策略版本 meta | 开：`l1.hit_beta.v1`；关：保持 `p0.global_accuracy.v1` |
| 配置项（可后续进 system_config） | `l1_p0`, `l1_alpha`, `l1_min_n`, `l1_floor_ratio` |
| 回滚 | 关 flag 即回 legacy，无需迁数据 |

实现时只改证据构建与 meta；**不改**博主表物化 `accuracy_rate` 含义（加权评分仍次列）。

---

## 3. 代码落点（实施时，本阶段不写代码）

| 模块 | 变更 |
| --- | --- |
| `src/core/config.py` | 读 flag 与 α/p0/min_n/floor |
| `src/services/advice_evidence.py` | `_build_blogger_reliability` 改为存活 is_correct 聚合 + Beta；筛选/排序；weight floor；meta 版本 |
| `src/services/blogger_hit_stats.py`（新建可选） | 共用「存活 c/n」查询，供 API 与 L1 一致 |
| `scripts/backtest_l1_weighting.py` | walk-forward |
| `tests/unit/test_l1_beta_weighting.py` | 公式单测：n=0→p0；1/1 收缩；min_n 标签；floor；flag 开关 |
| 文档 | 本设计 + 回测报告 |

**明确不做（本 L1）**：改首页展示公式、改 verify_score、硬删除低命中博主（那是 L2）。

---

## 4. 参数一览（可调，默认如下）

```text
ADVICE_L1_HIT_WEIGHTING = false
L1_P0                   = 0.609
L1_ALPHA                = 15
L1_MIN_N                = 10
L1_FLOOR_RATIO          = 0.4
L1_TOP_BLOGGERS         = 15          # 与现 top_bloggers 对齐
L1_MAX_PRED_PER_BLOGGER = 3           # 保持
```

α 敏感度：回测脚本应对 α∈{10,15,20} 出三列，默认仍 15。

---

## 5. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 小样本 p0 估偏 | 开闸前用生产重算 p0；α 加大更稳 |
| 数据太短回测无统计功效 | 对半切只给方向；写明 conf 低；宁可不开 |
| floor 过高 → 近似等权 | 报告 floor 触发率；>50% 触发则降 floor 或查 p̂ 塌缩 |
| 与 LLM 提示词耦合 | 字段名兼容 `reliability_score`；prompt 不写死加权分 |
| 和 P1 fail-closed | 证据空仍走 insufficient_evidence；L1 不主动抽空 |

---

## 6. 实施顺序（flag 后）

1. 纯函数 `beta_hit_rate(c, n, p0, alpha)` + 单测  
2. 存活 c/n 查询（与 bloggers 路由 `_hit_rate_map` 对齐口径）  
3. flag 分支接入 EvidenceBuilder  
4. 回测脚本 + 报告  
5. 人工看报告 → 决定是否 `ADVICE_L1_HIT_WEIGHTING=1`  
6. 灰度后看 exclusion / weight 分布一周

**本回合交付仅第 0–5 章设计；不写实现、不开 flag。**

---

## 7. 验收清单（实施阶段用）

- [ ] 单测：收缩公式边界  
- [ ] flag=0 时 evidence meta 与权重与现网一致（回归）  
- [ ] flag=1 时无 `accuracy_rate` 作为 reliability 主输入  
- [ ] 回测报告含 equal / legacy / l1 三臂 + 双指标  
- [ ] 文档写明开闸或不开闸理由  

---

## 附录：与排名翻转的关系

L1 不直接改 `/api/bloggers` 排序（Step1 已按 hit_rate）。L1 改的是**建议证据里谁更重**，避免「展示已按命中率、建议仍按加权分」的内外不一致。
