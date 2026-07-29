# 投资建议 P3 观测字段盘点（只读，不动工）

对照 P0 EvidencePack + P1 阶段标记 + P2 缓存键，三阶段观测残余如下。

## 已有

| 字段 | 来源 |
|------|------|
| as_of_date / evidence_hash | EvidencePack / get_data_for_advice |
| exclusions / conflicts / meta 计数与截断 | EvidencePack |
| weight / prediction_id / viewpoint_id | EvidencePack |
| _stage_statuses / _stage_status / _stage_reason | three_stage |
| cache_key = evidence\|prompt\|model | API + create data_hash |
| 拒绝原因 code/details + warning 日志 | API 拒绝路径 |
| 写路径枚举规范化 | validate_advice_output |
| 读路径不硬校验 | get_latest_advice |

## 仍缺（差得少，可挂 P2 尾巴或登记）

1. **prompt 全文/哈希未落库** — 仅有 ADVICE_PROMPT_VERSION 常量  
2. **各阶段 token/耗时/费用** — LLM 层有 call_stats，未挂到单次 advice  
3. **拒绝记录持久化** — 现仅日志 + API 响应；无 reject 表  
4. **模型温度/max_tokens 快照** — 未写入 advice 行  
5. **阶段中间全文长期存储** — 仅当次响应带回 viewpoint_summary / prediction_analysis  

## 建议

- 差得少：若运维需要「拒绝可查」，加轻量 `advice_generation_log`（success/reject + code + hash）可单开小项。  
- 差得多的「全文落库 / token 明细」登记挂起，不阻塞建议线收口。  

**建议线收口判据**：P1 fail-closed + P2 缓存键 + 回归全绿 → 本清单即可挂起。
