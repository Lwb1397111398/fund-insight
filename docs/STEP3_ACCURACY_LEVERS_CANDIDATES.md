# Step 3 候选：提升正确率（命中率）杠杆清单

主指标已钉死：**存活命中率**（见 `docs/ACCURACY_METRIC_SPEC.md`）。  
L1 已立项设计：`docs/L1_WEIGHTING_DESIGN.md`（**未实施**，flag 默认关，回测过关再开）。

| # | 杠杆 | 一行描述 | 作用点 | 风险/依赖 | 粗排序建议 |
| --- | --- | --- | --- | --- | --- |
| **L1** | **命中率 Beta 收缩加权** | 用存活 c/n + Beta(p₀,α) 替代加权分进证据权重，带 min_n 与 floor | `advice_evidence` 选博主/`reliability`/`weight` | 已实现 flag 默认关；回测 **shadow** 不开闸 | **P0 代码就绪 / 不开闸** |
| L3 | **方向可验证性门槛** | 模糊/不可验方向进 other，不进命中率分母、不进可加权证据 | 分析 triage / 验证入口 | 需核对 other 是否已排除 `is_correct` | **P1 便宜上游**（可与 L1 并行核对） |
| L6 | **验证质量** | 净值未就绪、未到 target 不写结论，减少脏 `is_correct` | `prediction_verify_service` | 边界多已做；保持回归 | **P1 保持** |
| L2 | **降权/排除低命中** | 对 p̂ 过低或长期垫底博主 cap/排除出证据 | 证据 caps + fail-closed | 证据变空→与 P1 联动；宜在 L1 稳定后 | **P2**（L1 开闸后） |
| L5 | **近期窗口命中率** | 90 天窗口 p̂ vs 生涯，证据可偏近期 | 排行/加权 | 样本更碎；与历史 180 天双口径相关 | **P2/P3** |
| L7 | **合并重复预测** | 同质预测合并，避免刷 n 与刷分母 | prediction groups | 改变 n 与命中分布 | **P2** 视重复率 |
| L4 | **板块/周期分层** | 分板块、分周期算命中再加权 | 展示与 sector 权重 | 样本碎裂严重 | **P3** 样本够再做 |
| L8 | **加权评分公式拍板** | `verify_score` 定稿后作幅度辅信号，**不**替代命中率 | 次列/辅权重 | 不阻塞命中率主线 | **P3 backlog** |

## 建议节奏

1. **L1**：设计+实现+回测 ✅ → 决策 **shadow**（flag 保持关；可后续 shadow 双跑/扩样本复评）  
2. **L3 只读** ✅：`docs/L3_OTHER_BUCKET_AUDIT.md`（无 other 桶；flat 已跳过验证）  
3. **L6**：不新开项目，验证路径回归保持  
4. 样本变厚或 shadow 复评后再考虑 **开 L1 闸** 或 **L2**  
5. L5/L7/L4/L8 按数据厚度插队  

## 相关文档

- `docs/ACCURACY_METRIC_SPEC.md` — 口径  
- `docs/STEP1_RANKING_FLIP.md` — 翻转证据  
- `docs/L1_WEIGHTING_DESIGN.md` — L1 四条件 + 预注册决策表  
- `docs/L1_WEIGHTING_BACKTEST_REPORT.md` — 回测数字与 shadow 决策  
- `docs/L3_OTHER_BUCKET_AUDIT.md` — other/flat 核对  
