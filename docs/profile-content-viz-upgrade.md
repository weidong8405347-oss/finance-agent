# Profile 内容质量与可视化升级实施记录（2026-09-10）

> 对照 [finance_agent_profile_upgrade_plan.md](finance_agent_profile_upgrade_plan.md) 的 Phase 1–3（P0）。
> 状态口径与 [knowledge-dossier-implementation-status.md](knowledge-dossier-implementation-status.md) 一致：
> **已落地** = 有代码与测试；**未做** = 本期未实施（如实标注，不宣称 ready）。

## 0. 诊断：内容质量低的根因不是研究能力，是摄入契约

取证（`live-a2cce641--cmd-3975ce29-2-synthesize`，事件 seq 204136–204197）：

- 合成器 6 次提交 `submit_structures`，内容完备：**24 家候选公司**（tier/护城河/反证/
  下一次验证）、**4 层产业链图**（含瓶颈描述与价值流注记）、**6 项验证时间线**（带触发
  条件与窗口）、执行摘要、同口径对照矩阵；**67 个引用全部可解析**；
- 6 次全部被旧 fail-closed 契约整批拒绝，原因全是**形状漂移**而非语义问题：
  `bottleneck` 给描述字符串（schema 要 bool）、`relation` 给中文「支撑/价值捕获/采购」
  （要英文 Literal）、`status` 给 "pending"（要 expected）、`layers` 给显示名而
  `node.layer` 是 key（前端分组空列）、`cells` 给数组（要 dict）、
  `tiers/main_basis/credibility` 给字符串（要 list/dict）；
- 一个 kind 的一个字段非法 → 整批拒 → 12 步预算烧光 → 产物冻结 `structures=[]`；
- 行业页面因此退化为「尚无结构化产业链图/候选评估」占位符 + 长文本文字墙。

另三个独立缺陷：

1. **状态与 payload 口径不一致**：行业 `key_kpi` 模块状态用注册表路由判 ready，
   payload 却只查配方键（market_size/growth_rate/capacity_supply），实际观测键
   （rd_spend/临床成功率/定价离散度…）全被丢掉 → 永远空图；
2. **首屏指标条空屏**：配方 KPI 全缺时不回退，27 条 typed 观测在首屏不可见；
3. **ratio 显示事故**：存量 ratio 值是百分点（"85%"→85、"100x"→100），前端一律
   ×100 → 首屏显示 8500.0% / 13640.0% / 10000.0%。

## 1. Phase 1（Schema/摄入，P0）——已落地

| 方案要求 | 落点 | 状态 |
| --- | --- | --- |
| 结构化输出 → Schema 的确定性中间层（§24/§25） | `structures.normalize_structures()`：同义词表（relation/status/tier/listing/layer）、bottleneck 描述→true+搬入 note、cells 数组按列 zip、字符串→单元素列表、chartable 遇不可比行确定性降级；**逐条留痕 repairs**（提交响应+回收事件可回溯） | 已落地（43 例单测） |
| 提交即校验、可当轮修复 | `parse_structures_partial()` 按 kind 部分接受 + `submit_structures` 跨调用累计合并（重提只修被拒 kind）；步数预算 12→16；工具描述给出规范词表与形状示例 | 已落地 |
| 语义纪律不放松 | 引用可解析/淘汰给原因/不许编造流量/不可比不硬画图——归一之后原样执行；`parse_structures` 严格入口保留 | 已落地（纪律组单测） |
| Investment Objects（§6/§7/§15） | 既有 claim/候选/时间线已承载 thesis/catalyst/moat 语义；本期补 ExecutiveSummary tear-sheet 字段（stage/why_now/value_capture/thesis_breakers）+ IndustryMap.layer_labels/value_flow_note + relation 放宽为自由字符串（规范词表 CANONICAL_RELATIONS 含 value_flow；未知值前端如实显示不硬译） | 已落地（additive，旧产物降级为空块） |

**数据回收**（`scripts/recover_structures.py`）：只重放事件日志里模型真实提交过的
payload，经归一+原语义校验后按 kind 合并进最新 report 产物；默认 dry-run；落
`research/structures_recovered` 审计事件（来源 run/seq/修复清单）；产物限制区披露回收
来源；回收后重开快照。已对 `ai-for-science` 实跑：5 类结构全部回收，6 个行业模块从
占位符转 ready。**这是数据回收不是内容生成**——未发明任何引用或数值。

## 2. Phase 2（首屏 tear-sheet，P0）——已落地

- 首屏指标条回退填充：配方 KPI 全缺但有 typed 观测时补位到 6 张卡（只读本主体
  status=ok；行业实体跳过无 scope 标注的公司级财务键——公司收入不冒充行业 KPI；
  同值同单位双键登记去重）；
- tear-sheet 分块（§26）：阶段徽章 / WHY NOW / KEY BOTTLENECK / VALUE CAPTURE /
  NEXT VALIDATION（冻结时间线推导最近 expected 两项）/ THESIS BREAKERS——
  **有数据才渲染，无数据不显示空块**（旧产物自动降级）；
- KeyMetric/MetricPoint/series 点带 `raw_text` 披露原文锚点。

## 3. Phase 3（可视化语法，P0）——已落地

| 方案 §9 语法 | 落点 | 状态 |
| --- | --- | --- |
| Value Chain → Flow Graph（§27） | `viz.layoutChain`+`ValueChainGraph`：分层 DAG、贝塞尔边+箭头+关系标签、瓶颈红环、邻接高亮、公司芯片联动候选行；原分层卡片+边清单降为 `<details>` 数据表（键盘可达等价物） | 已落地 |
| Catalysts → Timeline（§30） | `viz.timelineScale`+`TimelineStrip`：部分日期解析（YYYY/YYYY-MM/YYYY-MM-DD）、年刻度、今天标记线、无日期事件进未排期区（不编造位置）；清单保留触发条件/证据 | 已落地 |
| Competition → 2×2/Matrix（§28） | 离散版：`StageLadder` 技术验证阶梯（early→…→mature 规范序+未知阶段排后）+ `TierStrip` 分层占比条（点击过滤）——**不造连续坐标**（阶段是离散分类） | 已落地 |
| Peer Comparison → Ranked Bar | `rankedBarColumns`+`RankedBars`：数值只来自服务端 `comparison_numerics`（冻结观测按 observation_ids 解析）；护栏 = chartable+行可比+≥2 值+同列同 unit+currency+列声明单位一致——混币种/引用基期观测的事故形态只给表 | 已落地 |
| KPI → Small Multiples | `MetricSmallMultiples` 按 (unit, currency) 分面（修 159.1B USD 与 136.4 ratio 同轴压平）；单期间 bar 限宽（修单类巨黑块） | 已落地 |
| Market Share → Ranked Bar | 同上护栏复用；当前数据混币种如实不出图 | 已落地（护栏生效） |
| Revenue Mix → Stacked Bar | **未做**：分部序列当前按单位分面已可读；堆叠视图待分部数据齐后加（不先建空壳） | 未做 |
| 股价图（TradingView） | **未做**：本实体无股价 typed 观测；方案 Phase 4 数据源接入后再议 | 未做 |

ratio 显示纪律：`formatRatioDisplay` 共用实现——披露原文锚点（"85%"/"100x"/
"80-90%"）逐字优先；无锚点 |v|≤1 按分数 ×100、否则按百分点原样。KPI 卡/tooltip/
数据表同源不分叉。

## 4. 验证

- 后端：`pytest` 738 passed / 9 skipped（新增 59 例：归一 43 + 回收 6 + 投影 10）；ruff 干净；
- 前端：`vitest` 63 passed（新增 viz 23 例：布局/日期解析/护栏/分组）；`tsc -b` 干净；生产构建通过；
- 真实数据：sqlite backup 副本 + 独立端口 + headless Chrome 1440 截图五模块
  （snapshot/industry_chain/candidate_pool/catalysts_risks/key_kpi）——流图、阶梯、
  时间线、小倍数、tear-sheet 均渲染正确，无裸 JSON、无空图冒充。

## 5. 未做（方案 Phase 4–7，如实标注）

| 方案章节 | 状态 | 理由 |
| --- | --- | --- |
| §18 OpenBB/EdgarTools 结构化数据源（Phase 4） | 未做 | 需外部数据源与 key；设计明示「不为设计文档安装依赖/购买数据源」 |
| §11/§5 Expectation Engine（consensus/revision，Phase 5） | 未做 | 依赖 consensus 数据采集；现有 expectations 模块按契约降级 |
| §29 Change Detection（Phase 7） | 未做 | 已有 changes API 与「刷新检查新快照」；自动 diff 推送待 SSE 闭环 |
| §23 shadcn/TanStack/React Flow 迁移 | 未做 | 现有零依赖 SVG 已满足语法表；迁移收益不抵破坏面 |
| §31 Investment Score | 未做 | 方案要求每项可回溯证据；当前无评分输入，不造黑箱分 |
| §12 KPI Engine 行业自定义指标注册 | 部分 | 回退填充已让真实观测上首屏；配方级行业 KPI 注册表待补研写入侧对齐 |
