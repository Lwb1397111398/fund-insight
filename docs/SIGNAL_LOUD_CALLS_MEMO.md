# 信号备忘录：清晰喊单（loud calls）— clear 桶

- 状态：**移交基金线 8/26 评审队列**；**观察期内禁止动选股权重**
- 写入日：2026-07-30
- 来源：L3 附录 A/C（`docs/L3_VAGUE_LABEL_ESTIMATE.md`）
- 生产影响：**无**（零权重变更、零抽取变更）

---

## 1. 现象（钉死数字）

启发式桶 `clear` = 预测文案含与 up/down **同向**明确涨跌词、且无反向明确词（词典见复现节）。

### 1.1 桶级（附录 A，已结论）

| 桶 | n | 命中率 | 方向收益 |
| --- | ---: | ---: | ---: |
| clear | 70 | 54.3% | **−1.1%** |
| vague_hard | 167 | 60.5% | +0.4% |
| weak | 17 | 82.4% | +2.3% |

### 1.2 双 tier（附录 C，已结论；tier=存活结论数 n≥10 empirical / 1–9 prior）

| bucket × tier | n | 命中率 | 方向收益 |
| --- | ---: | ---: | ---: |
| clear × **empirical** | 63 | **50.8%** | **−2.2%** |
| clear × prior | 13 | 61.5% | **−1.3%** |
| vague × empirical | 152 | 56.6% | −0.4% |
| vague × prior | 33 | 60.6% | +0.7% |
| weak × empirical | 15 | 80.0% | +1.9% |

**关键不对称**：clear 在 **两个 tier** 都是「命中 ≥50% 但方向收益为负」。  
不是「只在小样本博主上亏」，empirical 层亏得**更狠**（−2.2%）。

### 1.3 已排除的解释（预注册表已跑）

| 假设 | 结果 |
| --- | --- |
| 模糊硬标污染分母（vague≈抛硬币） | 否：vague 60.5% 非硬币；other 全套改造**归档** |
| clear 标签 bug（否定句误判） | 否：抽样 50 错配率 **8% &lt; 15%** |
| 成分效应（prior 拖累 clear） | 否：empirical 比 prior **更差**（命中 gap −10.7pp） |

→ 预注册裁决：`signal_candidate`（信号备忘录，不修抽取）。

---

## 2. 判读：loud = late

点估计故事：

> **喊得越清晰，位置往往越晚。**  
> 明确「看涨/加仓/突破」类话术，更容易出现在**已经拉升、剩余空间小、回撤不对称**的段落；方向标签仍可「蒙对」一小段或末段同向，但 **Σ 方向收益为负**（踏错一脚亏得多）。

这比「clear 是噪声」更 actionable：

- 不是随机差，而是**时点/位置**偏差；
- 与「命中率尚可、收益为负」同向；
- 双 tier 同构，降低「某几个博主」偶然性。

**注意**：仍是观察期假说，**不是**已批准的生产规则。8/26 前不得写入选股权重。

---

## 3. 复现方式（钉死版本，随时重跑）

| 项 | 值 |
| --- | --- |
| 脚本 | `scripts/audit_l3_clear_labels.py` |
| 占比/分桶脚本 | `scripts/estimate_l3_vague_labels.py` |
| 随机种子 | `SEED = 20260729` |
| 抽样 n | 50（clear 桶） |
| 启发式版本 | **loud-calls-heuristics.v1**（与两脚本内 `CLEAR_BULL` / `CLEAR_BEAR` / `VAGUE` 正则一致；否定复读见 `audit_clear_label`） |
| tier 定义 | 存活 `is_correct` 非空计数：≥10 empirical，1–9 prior，0 neutral |
| 收益 | `start_nav`→`current_nav`；up 用区间收益，down 取负 |
| 文档快照 | `docs/L3_VAGUE_LABEL_ESTIMATE.md` 附录 A/C |

```bash
# 只读重推导（需 DATABASE_URL 或本地库）
python scripts/estimate_l3_vague_labels.py
python scripts/audit_l3_clear_labels.py
```

改词典或 seed 必须**升启发式版本号**并新开一节，禁止静默改 v1 数字。

---

## 4. 移交与禁区

### 移交

- **接收方**：基金线 / 选股观察评审（目标窗 **~2026-08-26**）
- **输入材料**：本备忘录 + L3 附录 A/C
- **建议评审题**（不预支答案）：
  1. 观察期末 clear 桶收益不对称是否仍在？
  2. 若在：降权 clear / 激动措辞 是否进入候选规则？
  3. weak（谨慎措辞）n 仍小，是否仅观察不加权？

### 禁区（观察期）

- **禁止**改 AdviceEvidence / 选股 / 基金推荐权重以「消化」本信号  
- **禁止**把 clear 降权写进生产 flag  
- **允许**只读重跑脚本、人工抽检校准 8% 错配率、把结果贴回附录  

捕捉本信号**不需要动生产**：脚本 + seed 已足够未来重推导。

---

## 5. 与 L1 shadow 的关系

- L1 正式加权仍关；shadow 双跑 log-only。  
- **tier 分离度**（empirical vs prior 全面比较）并入 L1 复评清单，**不单开调查**——见 `l1_shadow.tier_separation_report` / `reeval_status["tier_separation"]`。  
- 若复评时 empirical 系统不优于 prior：动摇的是分层/加权根基，与 loud=late **同一份数据回答两个问题**。  
- other 改造已归档；`L1_SHADOW_DATA_ERA=pre_other` 休眠保留。

---

## 6. 一句话给 8/26

> clear（loud）双 tier 皆「命中尚可、收益为负」；标签 bug 与成分效应已排除；假说 loud=late；观察期禁动权重；用脚本 v1+seed 复现。
