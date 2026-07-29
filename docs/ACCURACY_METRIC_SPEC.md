# 准确率口径规范（Step 1 收口）

**拍板（2026-07-29）**：产品「准确率」= **存活命中率**。

```
命中率 = count(is_correct = true ∧ is_deleted = false)
         / count(is_correct IS NOT NULL ∧ is_deleted = false)
```

| 名称 | 字段 | 含义 | 用途 |
| --- | --- | --- | --- |
| **准确率 / 命中率** | `hit_rate` / `avg_accuracy` / `accuracy_percent` | 上式 | 排行、首页卡片、提升正确率主指标 |
| **加权评分** | `accuracy_rate` / `weighted_score` | Σverify_score/(n×100)×100 | 次列保留，幅度信息；公式仍属 P1 backlog |
| **验证进度** | `progress_percent` | 已结论/存活有基金预测 | 不是准确率；分母排除软删 |

`verify_score` 公式未最终拍板前，**不得**用加权评分做「按战绩加权博主」的主杠杆。
