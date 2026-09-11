# F1 赛道地图 playbook（industry_map）

任务：把调研主题拆成可投资的子赛道地图，写入行业档案。

## 方法论
1. **先定义边界**：这个主题包含什么、不包含什么（防泛化稀释）。
2. **按价值链/技术路线拆子赛道**：每个子赛道写清——环节定义、代表公司类型、
   商业化阶段（概念/放量/成熟）。
3. **每个子赛道断言必须绑证据**：web 搜索到的公开来源（行业报告、公司官网、
   权威媒体），禁止凭训练记忆划分。
4. **市场空间数据**：market_size / growth_rate 优先引用带发布时间的来源
   （注意available_at 的 PIT 语义）；不同来源数字冲突时全部保留并标注分歧。

## 输出（写档案字段）
- `sub_sectors`（list）：[{name, definition, stage, rationale, evidence_ids}]
- INDUSTRY_SCHEMA 必填五字段：market_size / growth_rate / value_chain /
  competition / policy
- 找不到可靠来源就留白，不编造（留白比错判有价值）。

## 写数值的正确形态（F14 教训：形态误用会被门禁拒写）

propose_metric 的 `value_text` **只能是证据里逐字出现的数字原文**（含规模词），
不得拼接标签/期间文字：

- ✅ `value_text="28.9%"`（证据原文：「CAGR 2026-2032 28.9%」）+ `value_span` 给原句
- ❌ `value_text="CAGR 2026-2032 28.9%"`（拼接非逐字 → 拒写）

nature 纪律：
- 供应商一致预期（consensus）**必须有快照日期**（consensus.snapshot_at）——
  拿不出快照日期就退化为 nature=reported + 绑定来源证据，或干脆不写；
- 行业级指标写行业实体（market_size/growth_rate），公司财务写公司主体
  （subject 跨主体需在授权范围内）；
- 拒写信息里带修法：被拒后按提示修正重提，不要换写法蒙混。

批量提交首选 `submit_question_result`（观测+论断+答案一次过，逐项给拒绝原因）；
arguments 必须是合法 JSON（先写小批量验证形态，再放大）。
