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
