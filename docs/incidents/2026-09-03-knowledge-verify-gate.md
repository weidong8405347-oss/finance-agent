# 2026-09-03 · Knowledge 质量准入整改 + merge-entity 自合并事故 RCA

## 背景

用户验收反馈：`industry:ai-for-science` 完整度显示 100%，点进去内容基本没有；`002837.SZ`、`ai-for-science-美股港股` 同病。另有 `2228.HK` 与 `02228.HK` 双档案并存。

## 根因（三层，全部实测确认）

1. **完整度口径只看字段有无，不看质量**：GapAnalyzer 把「字段存在」计为完整——垃圾值也算 100%。
2. **「点进去没内容」的直接原因**：28 个实体里 20 个**没有 HTML 存档**（archive 只在 command runner 路径生成，主 agent 对话路径写入的事实无存档）→ 详情页 iframe 404；且前端把字段值 `JSON.stringify` 后 `max-w-40 truncate`，长文本全被截断。
3. **无准入闸**：`propose_fact` 过了证据/时态硬门禁就直接进 prod；RubricJudge 仅 advisory。

## 整改（已落地）

- `knowledge/verify.py`：写侧硬门禁（空值/占位符/JSON 字符串/结构化类型 → 拒写 + `hook/verdict` 事件）+ 读侧质量投影（per-field status、quality_score、verified/draft）。
- `knowledge/normalize.py`：实体 ID 归一（三层纵深：parse_target / propose_fact / writer）。
- `render.py` 重做为 dashboard 式存档：状态 pill、KPI 网格、markdown-lite 排版、结构化表格、provenance footer；**读路径惰性物化**缺档即补渲染。
- API：entities 列表带质量投影；profile 带字段 issues。
- 前端 KnowledgePage：验收状态 pill + 质量分列 + 结构化值渲染（FactValue）。
- 测试：`tests/unit/test_knowledge_verify.py`（13 个），全套 309 绿。

## 事故：merge-entity 自合并删除 2228.HK 档案（38 条事实）

**经过**：CLI `merge-entity --from 02228.HK --to 2228.HK` 的实现里，from/to **都**做了归一化——`02228.HK` 归一后变成 `2228.HK`，等于把实体合并进自己：重放 38 条 → 同实体翻倍 → DELETE 时 76 行全部删除。`shutil.rmtree` 还删掉了 2228.HK 的存档目录。

**恢复**（事件溯源的价值得到兑现）：`fact/asserted` 事件 + `tool/call propose_fact` 事件（含完整 value）按 (knowledge_time, version) 顺序重放，38/38 条事实全部找回、16 个字段恢复。恢复脚本：`/tmp/recover_2228.py`（一次性）。

**防护**（已落地 + 测试覆盖）：
1. `merge_entity` 入口 fail-closed：`from_id == to_id` 直接 `ValueError`（`test_merge_entity_refuses_self_merge`）。
2. CLI 只对 `to_id` 归一化，`from_id` 保持原始形态（要清理的往往是不规范旧 id）。

## 遗留

- 合并后 2228.HK 浮现 6 个冲突字段（两条研究线数值分歧）——属真实冲突，待人工在详情页裁决（现有 ConflictResolver）。
- 弱字段（仅 C 级证据）未回流进研究 brief（gap 重做闭环）——有意为之（避免 loop 不稳定），后续若要接入走 rubric 软反馈通道。
