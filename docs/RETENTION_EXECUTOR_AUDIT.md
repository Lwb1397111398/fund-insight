# 清理执行器只读审计

- 审计日：2026-07-29
- 对象：`RetentionCleanupService`（`retention-v2`）+ 生产一次真实执行痕迹
- 性质：只读，不改行为
- 动机：排除「误删 active / verified → 准确率静默变质」；顺带解释「汇总观点清不掉」

---

## 0. 一句话结论

| 问题 | 结论 |
| --- | --- |
| 会不会误删 **active / pending 未验证** 预测？ | **候选规则上不会**（pending 且未删直接进保护集） |
| 会不会误删 **仍有 is_correct 结论且未过窗** 的 verified？ | **仅当** `status∈{success,failed}` 且锚点日 `< today-30` 才进候选；生产当前 plan **predictions 候选=0** |
| 会不会动准确率？ | **会。** 2026-07-29 一次手动清理后 `blogger_accuracy_guard` 已记录多只博主 accuracy delta（例 −7.14）——备份能回数据，**回不了「中间被污染的观感/决策窗口」** |
| 汇总观点为何清不掉？ | **不是白名单保护 `is_summary`**，而是锚点 `max(viewpoint_date, valid_until)` + 30 天窗：34 条汇总 **全部** anchor ≥ cutoff，候选 **0** |
| 软删预测会不会硬删？ | **当前规则基本不会**：`status` 仍为 `pending` 的软删行在候选循环里被 `continue` 掉；生产 **926** 条 soft-delete 不在今日 plan 内 |

---

## 1. 执行器是什么

| 项 | 事实 |
| --- | --- |
| 类 | `src/services/retention_cleanup_service.py` → `RetentionCleanupService` |
| 策略版本 | `POLICY_VERSION = "retention-v2"` |
| 默认策略 | `retention_days=30`，`weekly_history_until_days=90`，`adopted_crawler_days=180`，`cleanup_audit_days=365`，`batch_size=200` |
| 入口 | `build_plan()` 预览；`execute(expected_fingerprint=...)` 真删 |
| API | `GET/POST /api/config/cleanup/*`（需 `ENABLE_DATA_CLEANUP` 默认 true + 执行头 `X-Danger-Confirm: cleanup-data`） |
| 调度 | 本地 `TaskScheduler._run_cleanup` 受同一开关；Render daily cron **不含** cleanup 步骤（只 fund_update + prediction_verify） |

---

## 2. 删除对象（类别与规则）

### 2.1 类别与执行顺序

```
predictions → viewpoints → posts → advice → crawler_records
→ analysis_logs → batch_tasks → sync_logs → fund_sync_retries
→ fund_history → funds → cleanup_tasks → cleanup_logs
```

### 2.2 各类候选条件（代码事实）

| 类别 | 进入候选的条件 | 明确不进 / 保护 |
| --- | --- | --- |
| **predictions** | `status ∈ {success, failed}` 且锚点 `verified_at.date() or target_date` `< today-30`；软删若仍在 `restore_before` 窗则跳过 | `status==pending && !is_deleted` → **pending 保护**；`status` 其它值（含仍为 pending 的软删）→ **直接 continue，不进候选**；混合 prediction_group 部分成员未到期 → 整组候选回滚 |
| **viewpoints** | `anchor=max(viewpoint_date, valid_until or viewpoint_date) < today-30`；软删且 `restore_before≥today` 保护 | anchor 未过期 → `active_viewpoints++`；**不区分 `is_summary`** |
| **posts** | 其下全部 prediction id ⊆ prediction 候选；或无预测且已分析且 `post_date<cutoff` 且无失败分析日志 | 仍挂未删预测的帖 |
| **advice** | `advice_date < cutoff` | 近 30 天建议 |
| **crawler_records** | 未采纳：`fetched/created < 30d`；已采纳：`< 180d`；若绑了 viewpoint 则要求该 viewpoint 也在候选中 | 仍引用未清理 viewpoint 的记录 |
| **analysis_logs / batch_tasks / sync / fund_sync_retry** | 终态且时间 `< 30d` | 非终态计入 `running_tasks` |
| **fund_history** | 不在「未决预测窗口保护 / 长期窗保护」内的过期点 | pending 预测相关净值窗、长期预测窗大量保护 |
| **funds** | 过期且无保护资金代码 | 映射/绑定/未决预测资金等 |
| **cleanup_logs** | `created_at < today-365` | 近一年审计 |
| **cleanup_item_logs** | **无独立类别**；仅随父 `cleanup_logs` 删除时级联 | 自身可无限涨 |

### 2.3 真删时副作用

| 动作 | 事实 |
| --- | --- |
| 删 prediction | 级联删 `VerificationTask`、`PredictionChangeLog`；可删整组 `PredictionGroup`；若 `verify_count>0` 且非 flat → **写入 Blogger.archived_*** 再 `recalculate_blogger_stats` |
| 删 viewpoint | 解绑 `CrawlerArticleRecord.viewpoint_id` 后硬删行 |
| 删 post | 清 AnalysisLog；viewpoint.post_id 置空 |
| 每条 | 写 `CleanupItemLog(action=delete, can_restore=False, reason=retention-v2...)` |
| Postgres 备份 | `_create_backup()` 非 sqlite **返回 None**（已发生：details.backup=null） |

---

## 3. 批量上限与幂等性

| 项 | 事实 |
| --- | --- |
| 单批 | `policy.batch_size` 默认 **200**（`_batches` 切片） |
| 单次 execute 总上限 | **无**全局 cap；候选全集按类扫完 |
| 指纹 | `sha256(version+today+policy+candidate_ids)`；execute 前重算 plan，不一致抛 `CleanupPlanChanged` |
| 幂等 | 真删后行不在；再次 build_plan 候选变少 → **近似幂等**。已删 id 不会重复删。`CleanupItemLog` 每次执行追加，**不幂等（只增）** |
| 并发 | 无分布式锁；两次 execute 交错未防护 |
| dry-run | **无一等公民 dry-run 模式**；preview=`build_plan` 只算 id，不写删。execute 即真删 |

---

## 4. 生产实测：当前 plan（2026-07-29，today 钉死）

| 候选类 | 数量 |
| --- | ---: |
| fund_history | 14 |
| 其它全部类别 | **0** |
| **total_candidates** | **14** |

| 保护计数 | 数量 |
| --- | ---: |
| pending_predictions | 991 |
| long_term_predictions | 195 |
| active_viewpoints | **583**（= 全表观点行，含软删与汇总） |
| fund_history 保护相关 | protected 窗口 2508；long_term_fund_history 1762 等 |
| protected_funds | 103 |
| running_tasks | 40 |

解读：执行器此刻几乎空转（只动 14 条净值历史）；**不是**「还在狂删 verified」。

---

## 5. 生产实测：已发生的一次真删（同日 07:39–07:50）

| 项 | 值 |
| --- | --- |
| cleanup_logs.id | 1 |
| trigger | manual |
| status | completed |
| total_items / success | 2601 / 2601 |
| failed | 0 |
| backup | **null** |
| 耗时 | ~11 分钟 |

| data_type | 删除条数 |
| --- | ---: |
| fund_history | 2310 |
| prediction | 148 |
| viewpoint | 106 |
| batch_task | 15 |
| advice | 14 |
| post | 8 |

`blogger_accuracy_guard`（节选事实）：

- 触及博主约 18 名
- 存在 **负向 delta**（例 blogger_id=33：57.14 → 50.0，delta **−7.14**；total_predictions 7→8 等归档重算现象）
- 亦有正向 delta（归档分计入后重算）

→ **准确率被清理改写已是既成事实**，不是假想风险。

---

## 6. 专题：汇总观点清不掉

### 6.1 数据

| 项 | 值 |
| --- | --- |
| `is_summary=true` | 34 行 |
| 全部 `is_deleted=false` | 34 |
| `source` | `daily_summary` |
| viewpoint_date 范围 | 2026-06-15 → 2026-07-28 |
| valid_until 范围 | 2026-07-01 → 2026-10-19 |

### 6.2 规则

```text
cutoff = today - 30 = 2026-06-29
anchor = max(viewpoint_date, valid_until or viewpoint_date)
仅当 anchor < cutoff 才进 viewpoints 候选
代码路径无 is_summary / daily_summary 特例
```

### 6.3 结果

| 判定 | 汇总 34 条 |
| --- | --- |
| `anchor < cutoff` | **0** |
| `viewpoint_date < cutoff`（若改用发文日） | 部分为 true（6 月中下旬） |
| `valid_until < today`（仅过期） | 部分为 true，但仍 ≥ cutoff 或与 max 规则叠加后不进候选 |

**根因（唯一主因）**：保留锚点取了 **`valid_until` 与 `viewpoint_date` 的较晚者`**。汇总常见 `valid_until = 观点日+7日` 乃至 +数月，导致「内容已过期/已无业务价值」仍被算作 active。

**不是**：FK 卡死、is_summary 白名单、删除函数漏实现（`_delete_viewpoints` 可硬删）。

### 6.4 连带：软删普通观点也硬删不动

| 项 | 值 |
| --- | --- |
| 非汇总软删 | 539 |
| 其中 `max(vp,vu) < cutoff` | **0** |
| 其中 `viewpoint_date < cutoff` | 156 |
| `valid_until < today` | 298 |
| restore 窗内 | 0 |

同一锚点规则 → 软删观点堆在表里，执行器今日 **viewpoints 候选=0**。

---

## 7. 专题：软删预测 ≠ 硬删候选（deleted 桶空洞）

| 项 | 值 |
| --- | --- |
| is_deleted=true | 926 |
| 均有 deleted_at | 926 |
| deleted_at < today-30 | **496** |
| 典型 status | 仍为 **pending**，is_correct null |

候选循环：

1. `pending && !is_deleted` → 保护（软删不走这支）
2. `status not in {success,failed}` → **continue**
3. 故 **pending+已软删** 永不硬删

与「deleted 桶 30 天硬删」产品意图 **不对齐**（当前实现缺口）。

---

## 8. 误删风险矩阵（针对准确率）

| 场景 | 当前行为 | 准确率影响 |
| --- | --- | --- |
| 未到期 pending | 保护 | 无 |
| 已验证 success/failed 且 target/verified 锚点 &lt;30 天前 | 可删；删前写入 archived_* 再重算 | **分母/分数会变**（guard 只记录，不阻断） |
| 已验证但 status 脏、或仅 is_correct 有值 | 可能进/不进候选取决于 status | 与 lifecycle 口径不一致 |
| active 方向信号 | 不因 cleanup 直接删 pending | 无 |
| 汇总观点 | 几乎不删 | 无准确率影响；占 viewpoints 体积 |
| fund_history 保护窗 | 大量保护 | 验证链优先 |

**准确率 guard**：`execute` 末尾对比 before/after，写入 log.details；**不回滚、不失败**。

---

## 9. 频率

| 来源 | 事实 |
| --- | --- |
| 生产 cleanup_logs | **1** 次（2026-07-29 manual） |
| cleanup_item_logs | 2601，全部来自该次 |
| Render daily | **无** cleanup |
| 本地调度 | 有入口，取决于进程是否常驻 + 开关 |
| 「每周 350 次」估算 | 来自 2601/7.5 周的外推；**实际是单次爆发写日志**，不是稳态周频 |

---

## 10. 与 Step0 数字的衔接

| 桶/表 | Step0 | 执行器现状 |
| --- | --- | --- |
| predictions deleted 926 | 可清理上限相关 | **不硬删**（规则空洞） |
| unverifiable 220 | 可策略 | **不单独识别**；仅当 status success/failed 且锚点过期 |
| cleanup_item_logs 2601 | 增长冠军之一 | **无 90 天自清理类别**（只跟 365 天 cleanup_logs） |
| 汇总 34 | 体积次要 | 锚点规则导致 **清不掉** |

---

## 11. 审计范围内不包含的事项

- 不改代码、不真删、不调策略参数
- 不评价 Supabase 平台备份是否开启
- 不设计滚动 180 天统计（已挂起）

---

## 12. 给后续实现的输入（仍非本文件建议正文，仅映射顾问三桶）

| 顾问桶 | 审计对照 |
| --- | --- |
| deleted 30 天硬删 | 现缺口：496 行已满足 deleted_at 年龄，执行器不收 |
| cleanup_item_logs 90 天 | 现缺口：无独立候选；2601 全 &lt;1 天龄，90 天策略当前删 0 |
| unverifiable 90 天 | 现缺口：无 lifecycle 集成；当前 unverifiable 最大年龄 55 天 → 策略先行删 0 |

汇总观点问题 **不在三桶清单内**，但是独立缺陷：锚点用 `valid_until` 导致业务上「该走」的汇总/软删观点永不进入候选。
