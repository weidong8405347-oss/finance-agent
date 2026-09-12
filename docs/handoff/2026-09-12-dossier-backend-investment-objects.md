# Dossier 后端数据模型与研究产出落地交接（2026-09-12，第二期）

> 依据：`docs/finance_agent_final_dossier_optimization_plan.md`（55 节）；承接 `2026-09-12-dossier-terminal-redesign.md`（前端展示层）。
> 范围：方案剩余的**后端数据模型与研究产出**工作（P0 ExecutiveSummary 补齐 / P1 What Changed Engine / P1 Investment Objects / P1 候选四象限 / P2 Profit Pool / P1 Expectations Layer / P0 股票档案验证）。
> 验证：后端 1119 通过（新增 67 个用例）、前端 100/100、`tsc -b` 干净、`build` + `build:export` 成功、离线导出 file:// 回归通过、Playwright 双视口截图核验。

## 方案条目 → 落地

| 方案条目 | 落地 |
| --- | --- |
| §47 ExecutiveSummary 四字段（P0） | `validate_structures` 新增行业 tear-sheet 硬校验（四字段全空且无 limitations 说明 → 拒绝，可修复重提；诚实出口不逼编造）；`submit_structures` 工具描述 + `_synthesize_brief` ⑥ 把 stage/why_now/value_capture/thesis_breakers 写成硬要求（为什么是现在 3-5 条、价值捕获一句话、证伪 ≤3 条、阶段一句话）。ai-for-science 定向补研已落库并上屏 |
| §32 What Changed Engine（P1） | 新增 `dossier/changes.py`（纯函数 `compute_change_log`）：发布时相对上一 live 快照计算结构化 diff（thesis/risk/metric/company_status/catalyst/module 六类，↑/!/+ 图标，总数 ≤6 按优先级截断），冻结进快照 `change_log` 字段；历史/as_of 投影不产生也不进基线序列（`latest_snapshot` 加 mode 过滤）。前端 `WhatChanged` 优先消费 `change_log`，无 diff 回退 `summary.key_changes`（回退路径也剥 raw ID） |
| §12-§15/§34 Investment Objects（P1） | `structures.py` 新增 `ThesisObject`（title/summary/importance/confidence/direction/support/counter/unresolved/supports/contradicts/monitor/related_companies/bear_case_status）与 `MoatAssessment`（§28 十维 score/confidence/trend/evidence_refs 骨架）。新模块 `dossier/investment_objects.py` 从冻结 claims+时间线+候选确定性推导：confidence 只来自「claim 状态 × 内容核验」映射（带 basis 说明，不从文本猜）；monitor 只做证据/公司交集的确定性链接；counter_refs 空 → bear_case_status=unmet（前端渲染「反证义务未履行」徽标，不假装无反证）；superseded 论点不进 theses；fact_summary 不进（§15 事实分层）。Moat 十维评分全部留空（无逐维度评级证据不编造），骨架承载四维证据组 |
| §20 候选四象限（P1） | 新增 `dossier/positioning.py`：technology_stage（early/preclinical/clinical/commercial/mature）与 commercial_stage（none/pilot/early_revenue/scaling/profitable）的规范序映射（按轴同义词表，跨轴语义不混用）；`build_quadrant` 产出 points + unpositioned（无法映射进「未定位」列，不塞进图里）。前端 `viz.tsx CandidateQuadrant`：5×5 离散网格散点（不造连续坐标）、气泡按 tier 着色、点击定位候选表行。坐标来源是离散分类映射，不是评分 |
| §22 Profit Pool（P2） | 新增 `dossier/profit_pool.py`：value_flow 边 + flow_known=true + 显式百分数才出 Stacked Bar；单一来源标 Estimated / Directional（§48.4，斜纹 + Est. 徽标）；份额合计偏离 100% 进 notes 不归一化（不改写原始数字）；合计 <100% 时右侧留空「未覆盖 N%」（不虚构「其他」段）。区间值（"55-65%"）/裸数字/金额不解析（不画视觉精确的图）。前端 `ProfitPoolBar` 进产业链章节；无定量证据维持定性文本（ai-for-science 现有 60%/15% 两条单源估计边已上图） |
| §25/§26 Expectations Layer（P1） | 新增 `gateway/adapters/consensus.py` `YFinanceConsensusAdapter`（source_id=consensus_yf，C 级 PIT：当前快照无历史回溯，评估模式 fail-closed 禁用；earnings_estimate/revenue_estimate/eps_trend/eps_revisions/growth_estimates 五族，单族失败降级可见）+ 网关注册（`query_consensus_yf` 工具自动装配）+ registry expectations 模块 metric_keys 扩展（consensus_eps/revenue/ebitda/fcf 等）。expectations 模块 payload 新增 `revision_series`（同一目标期间的 consensus 多快照按可知时刻排列）与 `price_series`（share_price 观测按交易日排列）；前端 `charts.tsx PriceVsRevisionChart` 双轴图（价格左腿 + 一致预期修订右腿，两腿都 ≥2 点才画，否则诚实不渲染） |
| 股票档案 Overview 验证（P0） | BE（Bloom Energy）完成三轮研究落库：Key Metrics（收入 2.02B USD/经营现金流 113.9M/资本开支 56.8M FY2025，陈旧标记如实）、Catalysts、Thesis Breakers、估值（Reverse DCF 面板正常）、预期差（consensus_eps/revenue 两期上图，按指标分面）、Why Now、Key Bottleneck 漏斗、Value Capture、Best Expressions。stage/why_now/value_capture/thesis_breakers 全部落库 |

## 过程中发现并修复的真实缺陷（全部有回归测试）

1. **`scheduling.build_schedule` 全局问题漏分发**：targeted 自定义问题（无 module）是全局问题，但档案完整度 1.0 时无任何 worker 槽位承载 → `DispatchError: 问题未完整分发`（fail-loud 假阳性，定向补研在第一轮就死）。修复：pending 且无问题组时建 misc 兜底组并承载全局问题。
2. **LLM 流式断流不重试**：novita→Bedrock 桥在长结构提交（submit_structures 大 JSON）中途断流，`started=True` 即弃疗。修复：assistant/chunk 不进模型上下文（只保 UI replay），传输层断流一律可重试；4xx 仍不重试——但**携带 Bedrock Runtime/ValidationException 标记的 400 可重试**（实测同一请求体重放即 200，是网关桥抖动不是参数错误）。
3. **流式错误体被吞**：`response.text` 在未 read 的流式响应上抛 ResponseNotRead，400 真实原因被掩盖成 `<unread streaming response>`。修复：raise_for_status 前先 `resp.read()`。
4. **data_hash 不含 structures 内容**：recover_structures 原地更新产物 structures 后，快照被幂等去重卡在旧 payload（回收内容永不上屏）。修复：structures 内容摘进 data_hash + 模块 data_ref 同样按内容（changed_modules 能发现原地更新）。
5. **富单元格被 `str(dict)` 吞成 repr**：对照矩阵 cells 提交为 dict（{value,note,observation_id}）时被归一层 repr 化（表格渲 Python repr、obs 引用困死文本）。修复：`_norm_comparison_matrix` 确定性拆包（value+note 为文本，observation_id 归位 observation_ids）。
6. **ThesisObject 标题用 targeted 问题原文**：targeted 问题的「文本」是操作指令（"这是结构补齐题…"），被当论点标题泄进投资者视图（§10）。修复：标题只用配方问题原文，targeted 回落论点首句。
7. **正文 raw ID 残留**：WhyNow/ValueCapture/ThesisBreakers/KeyDebate/核心依据/候选表 reason·evidence 列/对照表 cells/旧字段区（LegacyFacts）/WhatChanged 回退路径的内嵌 ev-/obs-/claim-/calc- 全部剥离子为 [n] 引用芯片（`splitTextRefs` 扩展：空括号/分隔符括号/孤立闭括号残留清理 + `stripRefsDeep` 递归剥离嵌套值）；投资者阅读区 0 raw ID（Playwright 自动扫描验证），raw ID 只保留在 research_sources 审计区与证据抽屉。

## 诚实性纪律（未放松）

- 无 typed 观测不进图表；gross_margin/net_debt/share_dilution 缺口如实标注（BE 未计算/未披露，不补零不估算）；
- confidence 只来自状态映射（basis 字段写明推导链：「引用校验通过 + 内容核验支持 → 0.8（状态映射，非统计置信度）」）；direction 恒 null（不从文本猜多空）；
- Moat 十维 score 全空（无逐维度评级证据不编造 1-5）；
- Profit Pool 单源份额标 Estimated / Directional，合计 75% 不归一化；
- 价格/财务数字十进制字符串进前端，只在绘图边界转 number；披露原文锚点（raw_text）优先于十进制反猜；
- consensus_yf 是 C 级 PIT（当前快照），评估模式 fail-closed 禁用（数据源登记表已声明）；
- Price vs Revision 图两腿不齐（BE 只有单时点 consensus 快照）→ 不渲染（诚实降级），expectations 模块只出 consensus 分面图 + 实际值表。

## 文件清单

**后端新增**：`dossier/changes.py`（What Changed 引擎）、`dossier/positioning.py`（阶段序映射）、`dossier/profit_pool.py`（利润池）、`dossier/investment_objects.py`（Thesis/Moat 推导）、`gateway/adapters/consensus.py`（一致预期源）
**后端改**：`dossier/models.py`（PROJECTOR_VERSION=4 + change_log/investment_objects 字段）、`dossier/projector.py`（investment_objects 投影 + structures 内容进 data_hash + 模块 data_ref 按内容）、`dossier/service.py`（发布时计算 change_log + candidate_pool 透 quadrant/moat + industry_chain 透 profit_pool + expectations 透 revision/price 序列 + investment_snapshot 透 theses）、`dossier/structures.py`（ThesisObject/MoatAssessment schema + tear-sheet 硬校验 + 富单元格拆包）、`dossier/registry.py`（expectations metric_keys）、`commands/steps.py`（tear-sheet 硬要求 + 逐 kind 小步提交节奏 + 跨结构 id 一致性要求 + flow_value 语义约定）、`research/scheduling.py`（全局问题兜底分发）、`llm/router.py`（断流重试 + 网关桥 400 重试 + 错误体捕获）、`knowledge/metric_store.py`（latest_snapshot mode 过滤）、`gateway/tools.py`（query_consensus_yf schema）、`plugins/builtin.py`（注册 consensus 插件）、`scripts/recover_structures.py`（--run-id 显式扫描，回收未定稿 run 的提交）
**前端改**：`types.ts`（ChangeLogEntry/ThesisObject/MoatAssessment/QuadrantPayload/ProfitPool/RevisionSeries/PricePoint + snapshot.change_log/investment_objects）、`overview.tsx`（WhatChanged 双路径 + WhyNow/ValueCapture/ThesisBreakers/KeyDebate/核心依据/瓶颈的 raw ID 剥离 + 阶段中文标签扩展 + stage 徽标截断）、`thesis.tsx`（ThesisObject 卡优先 + 置信度点 + 反证义务徽标 + 文本剥离）、`viz.tsx`（CandidateQuadrant + ProfitPoolBar）、`charts.tsx`（PriceVsRevisionChart 双轴）、`modules.tsx`（象限/利润池/预期分面接入 + 候选表与旧字段区剥离）、`StockDossierPage.tsx`（theses 透传 + stage 截断）
**测试新增**：`test_change_log.py`（18）、`test_positioning.py`（8）、`test_investment_objects.py`（11）、`test_profit_pool.py`（6）、`test_consensus_adapter.py`（9）、`test_router_roles.py` 新增重试边界 3 例、`test_structure_normalization.py` 新增富单元格 2 例、`test_research_scheduling.py` 新增兜底分发 1 例、`test_industry_modules_structures.py` 新增 tear-sheet 硬校验 1 例 + structures 内容哈希回归 2 例；前端 `viz.test.ts`/`charts.test.ts`/`overview.test.ts` 新增象限/利润池/双轴图/括号清理/targeted 标签用例

## 遗留与后续

- **BE 的 Investment View 文本是旧轮次结论**（提到「consensus_eps 未落成」——第三轮已登记成功，但 executive_summary 来自前轮合成；下一轮研究/合成会刷新。内容陈旧不是缺陷，快照语义如此）。
- **revions 单时点**：BE 的 consensus 只有当前快照（C 级源无历史），Price vs Revision 图不出（要 ≥2 时点）。积累多次补研后自然成线。yfinance 的 eps_trend 含 7/30/60/90 天前对比值——后续可让研究把 trend 的历史点登记为多个 consensus 快照（注意：那是 yfinance 当前视角的回填，PIT 语义需在 note 里写明）。
- **company_refs 不一致**（既有数据问题，prompt 已加约束）：industry_map 的 company_refs 用「公司名 (代码)」显示名，candidate_assessment 用 entity_id——BestExpressions 的环节列因此显示 —。下一轮行业研究重交 industry_map 即修复（submit_structures 描述已写明要求）。
- **Moat 评分通道**：MoatAssessment 骨架已投影（十维空 score + 证据组 + claim_refs）；评分需要 LLM 逐维度评级 + 证据绑定的新结构通道（建议作为 submit_structures 的第 6 个 kind，score 必须带 evidence_refs 才接受）。
- **novita 网关稳定性**：今日合成步骤 4 连败（400 抖动/断流/挂起），已由路由重试 + 逐 kind 小步提交缓解；若持续不稳，建议给 synthesize 角色配 fallback provider（路由层目前没有跨 provider 失败转移）。
- **gross_margin 派生**：BE 有 gross_profit + revenue 观测但未登记 gross_margin——研究应使用 calculate_metric 派生（margin 公式已存在），下一轮补研可带。
- **未提交改动**：本工作树沿用上期惯例未提交（前端改版 + 本期后端全部在工作树）。两期改动已交叉在同一批文件（service.py/steps.py/modules.tsx 等），提交前用 `git diff` 逐块自查。

## 验证命令（复核用）

```bash
# 后端
uv run pytest tests/ -q   # 1119 passed / 9 skipped
# 前端
cd frontend && npx tsc -b && npm test && npm run build && npm run build:export   # 100/100
# 档案
curl -s http://localhost:8000/api/v2/knowledge/industry/ai-for-science/dossier | jq '.summary | {stage, why_now, value_capture, thesis_breakers}'
curl -s http://localhost:8000/api/v2/knowledge/stock/BE/dossier | jq '.modules | map_values(.status)'
# 离线导出回归
curl -s -X POST http://localhost:8000/api/v2/dossiers/<snapshot_id>/exports -d '{"format":"html"}' -H "Content-Type: application/json"
```
