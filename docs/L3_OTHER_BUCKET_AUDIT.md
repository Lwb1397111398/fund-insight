# L3 只读核对：方向可验证性 / other 桶

- 日期：2026-07-29
- 性质：**只读**，不改代码
- 问题：模糊方向是否进命中率分母（`is_correct`）？

## 结论（一句话）

**系统没有独立的 `other` 桶。** 方向枚举实质是 **`up` / `down` / `flat`**；`flat`（震荡/中性）在验证入口被跳过，**默认不写 `is_correct`**，也不进博主物化加权分母。模糊文案若被 LLM **硬折成 up/down**，则会进分母——这是 L3 真正残留风险，不是 other 泄漏。

## 代码证据

| 位置 | 行为 |
| --- | --- |
| `llm_analyzer` prompt | `prediction_type` 仅为 **up/down/flat** |
| 解析兜底 | 正则：涨→up，跌→down，震荡/持平/neutral→**flat** |
| `prediction_verify_service` ~700 | `if prediction_type == 'flat': skip`，`skip_reason=neutral`，**不参与验证** |
| `blogger_stats.recalculate` | 分母过滤 `prediction_type != 'flat'` 且 `verify_count > 0` |
| 批量待验查询 | 同样排除 flat |
| 命中率 API `_hit_rate_map` | `is_correct IS NOT NULL ∧ ¬deleted`——**若**某条非 flat 被写入结论则计入；flat 正常不应有结论 |

## 与「other 桶」设计的差距

| 设想中的 L3 | 现状 |
| --- | --- |
| 模糊 → other → 不进分母 | 模糊 → 多被标 flat → 跳过验证（效果类似 other） |
| 显式 other 审计 | **无** `other` 类型；无法区分「真震荡」vs「模型不会标」 |
| 防 up/down 误标 | **无** 二次 triage；依赖 LLM 自律 |

## 建议（仍不实施）

1. **保持** flat 跳过验证（已是正确 fail-closed 方向）。  
2. 若要做 L3 强化：增加 `other`/`unknown` 类型或 `direction_confidence`，分析阶段低把握不写 up/down；验证拒绝非 {up,down} 写 `is_correct`。  
3. 可选只读 SQL（生产）：`prediction_type` 分布、`is_correct IS NOT NULL AND prediction_type='flat'` 脏行计数——若 >0 则需清理（P3 类）。

## 与 L1 关系

- L1 回测已排除 `flat` 行，与验证层一致。  
- L3 **不阻塞** L1 flag 决策；属上游数据质量增强，可并行排期。
