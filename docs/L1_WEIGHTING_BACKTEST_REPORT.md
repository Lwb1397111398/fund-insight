# L1 加权 Walk-forward 回测报告

- 生成日：2026-07-29
- 数据源：`DATABASE_URL`
- 切片：时序对半（方案 A）；cut=`2026-07-09`
- 样本：全量已结论 220；train 110；test 110
- p0=0.609；α 默认 15；min_n=10（权重用全 train c/n+Beta，不因 min_n 丢弃）
- flag：**未开**（本报告不部署）

## Legacy 臂语义确认

Step1 **未改** `Blogger.accuracy_rate` 写入语义：仍为 `total_verify_score/(total_predictions*100)*100`（加权评分，分母=verify_count>0 且非 flat 且未删）。API 新增的 `hit_rate` 是只读聚合，**不**回写该列。因此 legacy 臂 = 现网 EvidenceBuilder flag 关路径（`accuracy_rate` + `50+(acc-50)*min(1,n/10)`），对比基准真实。 全量实证命中率=60.91%（设计 p0=0.609）。

## 预注册决策规则

| 结果 | 动作 |
| --- | --- |
| l1_beta 命中率 > equal **且** > legacy，收益不劣化 | **开闸** |
| l1_beta 输给任一臂（>1pp） | **不开闸**，查因 |
| 分不出胜负（±1pp） | **shadow** 双跑 N 周复评 |

## 主结果（floor_ratio=0.4）

| 臂 | n | 加权命中率 | 同集等权命中 | 加权收益 | 收益覆盖 | top20%权重 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| equal | 110 | 55.45% | 55.45% | 0.25% | 100.0% | 20.0% |
| legacy | 110 | 57.16% | 55.45% | 0.45% | 100.0% | 23.8% |
| l1_beta | 110 | 56.25% | 55.45% | 0.38% | 100.0% | 22.9% |

- l1 floor 绝对值：0.2489
- floor 触发率：0.0%

## floor 敏感性（仅 l1_beta）

| floor_ratio | 加权命中率 | 加权收益 | floor触发率 |
| ---: | ---: | ---: | ---: |
| 0.2 | 56.25% | 0.38% | 0.0% |
| 0.4 | 56.25% | 0.38% | 0.0% |
| 0.6 | 56.23% | 0.37% | 8.2% |

## 决策（按预注册表，禁止现场改口）

- **动作**：`shadow`
- **理由**：命中率分不出胜负（±1pp 内）→ shadow 双跑积累后再评
- 收益附注：return_ok

## 局限

- 数据窗短（约数周），对半切统计功效弱；平局/shadow 是预期内结局。
- legacy 臂的 accuracy_rate 取自**当前物化列**在 train 行上的快照近似，非历史逐日重算 verify_score。
- 收益用 start_nav/current_nav，非完整组合回测。

## 下一步

- 若 `gate_on`：人工复核后才允许 `ADVICE_L1_HIT_WEIGHTING=1`（本交付默认仍关）。
- 若 `shadow` / `no_gate`：保持 flag 关；可加 shadow 日志或扩样本后再跑。
