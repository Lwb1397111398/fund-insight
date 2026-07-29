# Step 1 准确率只读审计（2026-07-29）

性质：**只读**，不改业务代码。  
范围：后端计算位置、分母口径、生产实数、前端展示位、差异列表。

---

## 0. 地面真值（生产 SQL，`is_deleted=false` 存活行）

| 指标 | 值 |
| --- | ---: |
| alive 总预测 | 1211 |
| `is_correct` 非空（已验证结论） | **220** |
| `is_correct=true` | **134** |
| `is_correct=false` | **86** |
| `is_correct` 空 | 991 |
| status∈{success,failed,verified} | 220（与 is_correct 非空对齐，回填后） |
| `is_expired=true` 列 | 220 |
| `verify_count>0` 且非 flat | 220 |

**命中率地面真值（存活）**：134 / 220 = **60.909…% → 60.91%**

含软删台账的全库结论：correct 167 + incorrect 138 = **305**；命中 167/305 = **54.75%**

---

## 1. 计算位置与分母口径一览

| # | 位置 | 函数/字段 | 分母 | 分子 | 公式类型 | 是否含软删 | 产出字段 | 单位 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | `stats_service.get_overall_stats` | pred 聚合 | `is_correct IS NOT NULL` 且未删 | `is_correct=true` | **命中率** | 否 | `avg_accuracy` | 0–100 |
| B | `stats_service.get_prediction_stats` | 同上 | 同上（`expired` 兼容名） | 同上 | **命中率** | 否 | `accuracy` | 0–100 |
| C | `stats_service.get_blogger_stats` | `avg(Blogger.accuracy_rate)` | —（物化列均值） | — | **加权分均值** | 间接 | `avg_accuracy` | 0–100 |
| D | `utils/blogger_stats.recalculate_*` | 物化 `Blogger.accuracy_rate` | `verify_count>0` 且非 flat 未删 **+ archived_*** | `sum(verify_score)` | **分数加权** `score/(n*100)*100` | 归档含已删 | `accuracy_rate` | 0–100 |
| E | `prediction_service.get_stats` | 全库/按博主 | `is_correct IS NOT NULL`（**未滤 is_deleted！** base 仅 `is_deleted==False` 实际有） | correct | **命中率** | 否（base_filter 含未删） | `accuracy` | **0–1 比例** |
| F | `prediction_service.get_verify_progress` | 有 fund_code 的预测 | `is_correct IS NOT NULL` | correct | **命中率** | **是（无 is_deleted 过滤）** | `accuracy_percent` | 0–100 |
| G | API `GET /api/bloggers` | 直出 ORM | 读物化 D | — | 加权分 | 间接 | `accuracy_rate` | 0–100 |
| H | API `GET /api/stats` | A+B+C 打包 | 见上 | — | 混合 | — | `data.overall/predictions/bloggers` | — |

---

## 2. 生产实数对账

| 源 | 字段 | 实数 | 与地面真值 |
| --- | --- | ---: | --- |
| A overall | avg_accuracy | **60.91** | = 134/220 ✅ |
| A overall | correct/incorrect/expired | 134 / 86 / 220 | ✅ |
| B predictions | accuracy | **60.91** | ✅ |
| C bloggers | avg_accuracy | **49.84** | ≠ 60.91（**不同指标**：博主物化加权分的简单平均） |
| E get_stats | accuracy | **0.6091** | 数值同命中率但 **单位是比例不是百分数** ⚠️ |
| F verify_progress | accuracy_percent | **54.8** | = 167/305 含软删台账 ⚠️ |
| F verify_progress | total | 1694 | 含软删；≠ alive 1211 ⚠️ |
| D 物化重算 | score 公式 vs stored | 24 博主 **delta=0** | 物化与重算一致 ✅ |
| D 命中率 vs stored 加权 | 15/24 博主 \|Δ\|>0.5 | 最大约 **+8.5pp** | **同叫「准确率」、两套定义** ⚠️ |

### 博主「加权准确率」vs「命中率」示例（生产）

| blogger_id | stored 加权% | 命中率% | Δ |
| --- | ---: | ---: | ---: |
| 32 | 62.06 | 70.59 | +8.53 |
| 36 | 66.58 | 75.00 | +8.42 |
| 34 | 63.80 | 70.00 | +6.20 |
| 47 | 75.47 | 80.00 | +4.53 |
| 67 | 50.55 | 54.84 | +4.29 |

加权分用 `verify_score`（0–100 连续分），命中率用 `is_correct` 布尔。UI 列名是「**加权准确率**」，与 D 一致；但若用户口语「准确率」不区分，会与 A/B 的 60.91 对不上。

---

## 3. 前端展示位（index / dashboard / market）

| 界面 | 元素 | 数据源 | 展示的「准确率」？ |
| --- | --- | --- | --- |
| **首页仪表盘卡片** | 博主/帖子/预测/观点/基金计数 | `GET /api/stats` → `overall.*` | **不直接显示 avg_accuracy**；只显示 total/pending |
| **博主榜（首页表）** | 「加权准确率」列 | `GET /api/bloggers` → `accuracy_rate` | **是：D 加权分** |
| **博主管理页表** | 同左 | 同上 | **是：D 加权分** |
| **预测列表** | flat 提示「不计准确率」 | 文案 | 无数字 |
| **投资建议卡** | 市场情绪 greedy/fearful | advice API | **不是预测准确率** |
| **清理结果** | blogger_accuracy_guard | cleanup result | 加权分 before/after delta |
| **market 记分卡** | 未发现独立「市场准确率」组件 | — | 基金涨跌均值在 `stats.funds.avg_*_growth`，**不是预测准确率** |

结论：顾问说的「三处」若映射到本产品，更接近：

1. **Dashboard 总览**（`/api/stats` overall，命中率在 payload 里但首页卡片未渲染）  
2. **博主排行/列表**（加权准确率，主展示）  
3. **预测/验证进度类**（`get_verify_progress` 若被某 UI 调用；当前 index 主路径未直接绑 `accuracy_percent`）

---

## 4. 差异清单（问题登记，不修）

| ID | 差异 | 影响 |
| --- | --- | --- |
| D1 | **两套「准确率」**：全局命中率 60.91% vs 博主加权分（均值 49.84，个体可差 8pp+） | 用户口头对账必炸 |
| D2 | `get_verify_progress` **含软删** → 54.8% 与存活 60.91% 分叉 | 若展示会低估 |
| D3 | `get_stats.accuracy` 为 **0–1**，其它为 **0–100** | 前端若直接拼 `%` 会显示 0.6% |
| D4 | 博主分母是 `verify_count>0`，全局是 `is_correct IS NOT NULL` | 今日巧合相等（220=220），规则不等价 |
| D5 | 博主分子是 **verify_score 连续分**，不是 correct 计数 | 「提升正确率」要先定义优化哪个 |
| D6 | overall `avg_accuracy` 在 API 有、首页卡片 **未展示** | 总览「准确率」实际不可见 |
| D7 | flat 预测：博主重算排除，全局 is_correct 路径未单独排除 flat | 若 future flat 带 is_correct 会进全局分母 |

---

## 5. 分母统一（cd21a32）验证结论

| 声称 | 实况 |
| --- | --- |
| stats / prediction_service 统计分母用 is_correct | **A/B/E 已落地**，与地面 220/134 对齐 |
| 全产品「准确率」都读同一分母 | **否**：博主线仍是 verify_score 加权；verify_progress 含删 |
| 数字互相对得上 | **A≡B≡E(×100)**；**C/D/F 故意或意外不同** |

---

## 6. Step 1 后续输入（仅登记，本文件不建议方案）

要「提升正确率」必须先钉死产品指标，候选：

1. **全局命中率** = correct / (is_correct 非空 ∩ 未删) → 今日 60.91%  
2. **博主加权分** = Σverify_score / (n×100) → 排行榜正在用  
3. **幅度敏感分**（已有 verify_score 连续值）vs 纯方向对错  

未决：滚动窗口、是否含归档、是否含软删台账、flat 排除是否全局化。

---

## 7. 审计方法

- 代码静态检索 `accuracy` / `is_correct` / `blogger_stats`  
- 生产只读 SQL + 调用 `StatsService` / `PredictionService` / 物化重算对比  
- 前端 `web/index.html` 绑定点检索  
- **无写库、无改展示**
