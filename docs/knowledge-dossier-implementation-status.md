# Knowledge 档案升级实施状态对照

> 对照 [knowledge-dossier-research-redesign.md](knowledge-dossier-research-redesign.md) v1.0 的落地清单。
> 更新：2026-09-08 · 分支 `fix/research-audit-a2cce641-remediation`（上一分支已合并进 `main`）。
> 状态口径：**已落地** = 有代码与测试；**部分** = 契约已落地但依赖数据/后续阶段；**未做** = 本期未实施（如实标注，不宣称 ready）。
> 本轮增量：[ai-for-science-live-a2cce641 审计](ai-for-science-live-a2cce641-audit-and-optimization.md) 的 P0/P1 整改，逐项对照见 [整改状态](ai-for-science-audit-remediation.md)。

## 里程碑完成度

| 阶段 | 设计要求 | 状态 |
| --- | --- | --- |
| M0 契约和样本 | Dossier/Observation/Claim/Plan schema、对照样本、迁移 dry-run | **已落地**（样本见 `docs/samples/dossier/`，dry-run 见 `scripts/migrate_dossier.py inventory`，27 生产实体盘点通过） |
| M1 可阅读档案闭环 | 兼容 projector、typed 读取/换算血缘、裁决时态投影、snapshot API、列表/详情、来源抽屉、基础财务图 | **已落地**（真实 BE 数据冒烟通过；旧 KB → 新页面可读、无 archive 也可用） |
| M2 深研闭环 | ResearchPlan、问题终止、typed 写入、CalculationRun、分章 ReportDocument、验证、partial 发布 | **已落地**（E2E 集成测试：`/research --depth=targeted` → 计划 → 观测/论断 → 评估 sufficient → 产物 validated → report.md 同源 → 快照发布 + 前后 diff） |
| M3 预期与行业增强 | 指引 vs 实际、Reverse DCF、敏感性、同业、首批行业模板、管理层兑现 | **部分**：Reverse DCF（FCFF 反向求解 + 二维敏感性 + 假设实验面板/情景保存）与首批配方（通用/工业设备/生物科技/行业）已落地；**guidance/consensus 数据采集与管理层兑现时间线依赖数据源接入**，模块按契约正确降级（missing + 原因），未以空图宣称完成 |
| M4 迁移及稳定 | 历史裁决批量回填、影子迁移、HTML 导出、性能/可访问性/恢复/回滚 | **部分**：影子迁移与对账 manifest（27/27 成功）、JSON/Markdown 导出、幂等发布/断点恢复已落地；**历史裁决批量回填与 HTML 导出未做**（HTML 待组件契约稳定后加入，设计 §11.4 即为第一期之后） |

## 设计章节 → 代码落点

| 设计章节 | 落点 | 状态 |
| --- | --- | --- |
| §6.1 数据对象（8 张逻辑表） | `knowledge/metric_store.py`（source_documents / metric_observations / calculation_runs / research_plans / research_claims / research_artifacts / dossier_snapshots / conflict_resolutions，独立 `data/metrics.db`） | 已落地 |
| §6.2 核心数值类型（判别联合） | `knowledge/metrics.py`（reported/calculated/guidance/consensus/model_estimate 各自强制来源义务；TTM 仅流量指标；value 十进制字符串，缺失≠0） | 已落地 |
| §6.2 标准化「保留原文+显式转换」 | `knowledge/normalization.py`（版本化公式注册表、`recompute_lineage` 逐步重算、逐叶数值校验堵嵌套/字符串漏检、亿=10^8 等中英规模词、年份歧义消解） | 已落地 |
| §6.4 期间/单位/口径规则 | 语义键版本化、重述竞争版本、裁决时态化（`resolved_at`，历史不借用未来裁决）、`ttm_from_observations` 连续性/口径门禁 | 已落地 |
| §6.5 事件投影 | `metric/asserted`（完整 payload）等新事件（`eventstore/events.py`）；写入 = 索引 + 事件；崩溃恢复依赖幂等（semantic_hash / input_hash / data_hash） | 已落地（「先事件后投影 + offset 记录」的完整事件溯源重建未做，当前为双写幂等） |
| §6.6 快照一致性 | `DossierContext` 冻结、live 首开固定服务端时刻、live 身份哈希不含 as_of（数据不变复用冻结快照）、三时间区分、`kb_snapshot_id` 兼容保留 | 已落地 |
| §7.1-7.3 研究模式/流程/问题结构 | `research/plan.py`（四模式预算表、问题状态机、targeted 确定性 id）；`commands/registry.py` 解析 `--depth/--focus/--refresh` 进 manifest | 已落地 |
| §7.4-7.5 深度配方/行业模板 | `playbooks/research/recipes/{general,industrial_equipment,biotech,industry}.yaml`；配方识别给来源、不确定回落通用 | 已落地 |
| §7.6 完成与质量门禁 | `research/assessment.py`（integrity 硬门禁代码运行、覆盖率 unavailable 不计已回答、冲突未裁决不出无条件结论、verdict 三态）；rubric 仅 advisory 不进门禁 | 已落地 |
| §7.7 预算并发恢复 | 计划预算接管轮次（`loop._effective_budget`）；typed 产出计入进展（防 stalled 误判）；问题级 checkpoint（问题状态存计划 payload） | 已落地（wall-clock 上限与 per-provider 限速未单独实施，检索预算经 max_rounds×max_steps 间接约束） |
| §7.8 报告产物 | `research/artifacts.py`（固定 block 类型、`{{metric:id}}` 插值、`[ev-xxx]` 未知 id 硬失败、report.md/概览/摘要同源渲染） | 已落地 |
| §8.1 计算服务 | `research/calculations.py`（input_refs 解析与漂移拒绝、公式注册表 12 个、Decimal、N/M 语义、幂等 input_hash） | 已落地（ROIC 等待口径明确后加，符合设计） |
| §8.2 预期与管理层兑现 | `guidance_delta` 公式（区间落点 + 中点标注）与 Expectations 模块契约 | 部分（兑现时间线/股价反应窗口依赖数据源，未做） |
| §8.3 Reverse DCF | FCFF 模型、有界二分、求解区间/残差/收敛状态、WACC≤终值增速禁用、fcf_margin 简化标注、二维敏感性 | 已落地 |
| §8.4-8.5 /decide 边界与滑块 | 预览不写事实、情景保存=模型 artifact（assumption_hash 409 防慢响应覆盖）、页面明示「假设实验/归 /decide」 | 已落地（前端本地预览模式未做，全部走后端确定性计算） |
| §9 数据源策略 | EDGAR/HKEX 等沿用现有 adapter；本次**未新增数据源**（设计明示不为设计文档安装依赖/购买数据源） | 未做（按计划） |
| §10.1 API v2 | `api/dossier.py` 全表（entities/dossier/snapshot/modules/evidence/documents/series/compare/artifacts/plans/changes/research requests 幂等/valuations preview+scenarios/exports+jobs；404/409/422/200+reasons 语义） | 已落地（`GET /dossiers/{sid}/documents/{did}` 依赖文档目录数据，契约已就绪；202 快照构建 job 未做——当前投影为同步毫秒级） |
| §10.2 前端结构 | `app/route.ts`、`features/dossier/{types,api,charts,components,modules,legacy}`、`pages/{Knowledge,StockDossier,ResearchReport,Compare}Page` | 已落地 |
| §10.3 SSE 补研闭环 | 问题/评估/产物/快照事件桥接进 Sessions 流（`runner._BRIDGE_TYPES` + 摘要）；补研幂等 key | 已落地（dossier 页的实时 published 推送未做，用「刷新（检查新快照）」按钮替代；SSE cursor 去重复用现有机制） |
| §11.1 迁移 | `scripts/migrate_dossier.py`（inventory dry-run / shadow 对账 / apply-typed 保守映射：显式币种+日期才转、规模词不猜量级、幂等 checkpoint、迁移值标 PIT-B） | 已落地（旧报告 [ev-id] 批量重链与 thesis→claim 批量迁移未做，读侧已兼容 legacy_analysis 投影） |
| §11.3 发布与回滚 | 新链路装配缺省安全：`StepDeps.metrics=None` 时旧管线行为不变（等价 `typed_metrics_enabled=false`）；`research_plan_enabled` 开关已有；additive schema、回滚不删新表 | 部分（`dossier_ui_enabled` 前端灰度开关未做——新页面默认可用，旧「数据与审计」与 HTML 存档入口保留即回滚路径） |
| §11.4 导出 | JSON + Markdown（绑定 data_hash/as_of/情景差异注明；历史导出默认不附加新情景） | 已落地（HTML/PDF 按计划后置） |
| §13.1 正确性测试 | 数值语义/财务期间/来源/历史与评估/模型/研究/发布恢复/前端/兼容迁移 9 组 | 已落地（135 个新用例；浏览器 E2E 与截图验收未做，见下） |
| §13.2 对照样本评估 | 冻结样本 3 个已备（附录 A.1） | 部分（新旧管线盲评对照需真实研究运行，属实施后评估任务） |

## Code review 修复记录（2026-09-08，31 条意见全部处置）

针对外部 review 的 9 条 P1 + 22 条 P2，逐条修复并补回归测试（`tests/unit/test_review_fixes.py` 等 35+ 用例）：

| # | 问题 | 修复 |
| --- | --- | --- |
| P1-1 | 数值量级未绑定证据（million 写成 billion 能过） | `normalization.assert_magnitude_bound`：数字+规模词必须在摘录中逐字可定位（等价类 bn≡billion；表头模式共现）；接入 TypedMetricWriter 硬门禁 |
| P1-2 | 模块请求重查当前库，补录历史观测改变冻结快照 | `SnapshotInputs` 冻结输入版本集（fact/observation/claim/artifact/计算/计划 id）；模块/序列/导出按 id 读冻结版本；漂移只提示刷新 |
| P1-3 | 冲突集合/版本链未按 as_of 过滤，历史页泄露未来竞争值 | `conflicted_semantic_hashes(as_of, exclude_resolved)` + `observation_history(as_of)`；投影/模块全部改用 |
| P1-4 | 计算引用不验命名空间/实体/可知时间 | `get_ref_meta` 上下文门禁（同 ns+同实体+as_of 前可知）；preview 支持 base_snapshot 绑定冻结输入 |
| P1-5 | 论断只验支持引用存在性 | support+counter 全部带上下文验证；ArtifactValidator 重查 claim 两侧引用（unresolved_claim_ref 硬失败） |
| P1-6 | metric_table 裸值/comparison 无验证分支 | 表内无 observation_id 的非豁免数值 = unsourced_metric_cell 硬失败；comparison 同规则 |
| P1-7 | 计划问题未分配给并行维度组 | `_QUESTION_MODULE_TO_GROUP` 映射（financial_quality→financial 等）；targeted/无模块问题全组可见；answered 不下发 |
| P1-8 | answer_question 读改写竞态丢更新 | `MetricStore.update_plan_question` 锁内原子合并；并发双线程回归测试 |
| P1-9 | 历史/eval 视图可调生产裁决入口 | LegacyFacts 传 readOnly（mode≠live 或 ns≠prod）；版本链标注「未按历史 as_of 过滤」 |
| P2-10 | 历史快照用最新计划 | `plans_for(as_of)`；计划在 as_of 后被更新 → 问题状态置 historical_unknown（不冒充当时进展） |
| P2-11 | 评估查询无命名空间/时间过滤（LIMIT 1 后才过滤） | SQL 内 namespace+created_at≤T 过滤；assessment 事件带 namespace |
| P2-12 | 快照哈希缺计划/计算依赖 | data_hash 纳入 plan 问题状态指纹 + calculation ids |
| P2-13 | 计算幂等键缺实体/引用身份 | input_hash 纳入 namespace:entity + 完整 InputRef（kind/ref_id/value） |
| P2-14 | 通用计算入口无口径校验 | 跨币种输入拒绝（需显式 fx 链）；ttm_sum 通用入口强制四季度观测引用+连续性/同口径/不同版本 |
| P2-15 | 序列仅按 metric_key 分组混口径 | series_set 按完整语义键（频率/维度/basis/币种/nature）拆分；KPI/财务主图只消费合并披露/计算值 |
| P2-16 | 比较接口 FY 对 Q4 返回 comparable | 共同 period_end+frequency+basis+币种全一致才 comparable；新增 frequency/period_end 限定参数 |
| P2-17 | 图表时间轴逐序列扩展导致错位 | 先固定完整时间轴（按 period_end 排序）再排列各序列；前端对齐回归测试 |
| P2-18 | useMemo 依赖不含值 | 指纹序列化完整 points 值/nature/conflict |
| P2-19 | 快照与模块请求共用取消序号 | snapSeq/modSeq 分离；新快照到达作废在飞模块响应 |
| P2-20 | 刷新按钮只重拉钉住的旧快照 | 刷新 = 去掉 snapshot 参数重新打开（重新投影，数据有变则切新快照） |
| P2-21 | 指标点击用全局首条证据背书 | KeyMetric 携带自己的 evidence_refs，点击直达对应摘录 |
| P2-22 | 快照来源目录漏 claim 证据 | evidence_refs 纳入 claim support/counter 的 ev-* 引用 |
| P2-23 | 补研丢 entity_kind（行业变股票） | industry 实体构造 `industry:<slug>` 标的；非法 kind 422 |
| P2-24 | 行业配方模块无内容 | business_graph 读 value_chain/competition/sub_sectors；行业模块标题覆写（产业链与瓶颈/候选池与竞争） |
| P2-25 | 情景保存不验实体/ns/模型版本/输入归属 | 四重校验（计算实体=快照实体、同 ns、model_version=公式@版本、输入引用⊆基线冻结集） |
| P2-26 | 用户情景污染默认结论投影 | ResearchArtifact.purpose=scenario；首屏结论/覆盖/模块状态全部排除；来源模块分区展示 |
| P2-27 | 情景保存无 markdown 正文 | 保存前 render_markdown（产物页可读） |
| P2-28 | 假设变化后旧结果仍可保存 | invalidate()：任何输入变化立即作废结果+在飞请求；必填清空同步清除 |
| P2-29 | 模块加载失败包成空 payload 致页面崩溃 | 独立错误态+重试；组件防御性默认值 |
| P2-30 | 会计括号负数记成正数 | parse_raw_number 保留 (1,234) 符号（含括号包规模词） |
| P2-31 | HK$ 被当成 USD | 币种提示顺序：HK$→US$→裸$→中文币种词 |

## 已知边界与后续（如实清单）

> 2026-09-08 审计整改后的状态修订（audit §6 建议）：**基础契约与部分单股链路已完成，
> 行业深研与生产质量验收进行中**。逐项整改对照见
> [ai-for-science-audit-remediation.md](ai-for-science-audit-remediation.md)。

1. **数据源未扩展**：guidance/consensus/电话会/A 股结构化披露依赖 §9.1 的 adapter 验证任务；相关模块当前按契约降级（missing + 原因），这是设计要求的行为，不是缺陷掩盖。
2. **事件溯源重建**：`metric/asserted` 事件带完整 payload 可重建观测索引，但「先事件后投影 + offset」的严格顺序与全量重放工具未实施；当前一致性靠幂等键（semantic_hash/input_hash/data_hash）。
3. **历史裁决批量回填**（M4）与 HTML 导出：未做。旧 fact 冲突的时态裁决投影仅新观测通道具备；旧通道保持原 `resolve_conflict` 行为（新页面历史视图中旧字段裁决入口已设只读）。
4. **浏览器 E2E**：前端逻辑经 vitest 覆盖（路由/数值边界），三档视口的截图验收（§13.3 / audit §5 用例 7）未跑真实浏览器。
5. **性能目标**（p95 <300/500ms、数万观测）：本地 27 实体规模下即时响应；设计要求的规模压测未做。
6. ~~**wall-clock/检索调用硬预算**：以轮次×步数近似~~ → **已整改**（audit §3.3）：`research/budget.py` 的 RunBudget 在 LLM 与网关入口真实扣减墙钟/token/检索/工具/重试，慢组超时不再拖住整轮。
7. ~~**上下文压缩未做**（audit §3.3 余项）~~ → **已整改**：除检索去重、按需 evidence bundle（`read_chunk`）与长正文截断外，`EventStore.derive_messages(max_tool_chars, keep_recent_tools)` 对较早的工具结果做投影级裁剪（只裁消费不动日志，裁剪处显式标记并告知如何取回全文，provenance/chunk_id 保留），研究循环默认 1200 字符 / 最近 6 条全文。仍未做的是**语义摘要**（用小模型把旧轮次压成要点），当前是确定性截断。
8. **真实行业深研效果评估未跑**（audit §5 用例 8）：修复前后同目标/同证据截止/相近预算的对照运行（有效问题覆盖、关键数值错误率、来源可解析率、重复资料、有效产出时间与成本）需要一次真实付费运行，尚未执行。

## 验证入口

```bash
uv run pytest tests                        # 672 passed, 9 skipped（含审计整改新增 176 例 + 会话/删除 19 例）
cd frontend && npm test && npm run build   # 40 passed；tsc + vite 构建通过
uv run python scripts/migrate_dossier.py --data-dir data inventory   # 迁移盘点（只读）
uv run python scripts/revise_observations.py --data-dir data \
    --observation obs-xxxx --action needs_review --reason "…"        # 观测修订干跑
uv run python -m finance_agent serve       # → #/knowledge 列表 → 实体档案 → 来源抽屉 → 补研
```

审计整改的专项验收入口（对应 audit §5 用例 1-6）：

```bash
uv run pytest tests/unit/test_research_scheduling.py        # 用例1 问题分发装配回放
uv run pytest tests/unit/test_metric_semantics_gate.py      # 用例2 错误数据四例
uv run pytest tests/unit/test_evidence_authenticity.py      # 用例3 来源真实性
uv run pytest tests/unit/test_run_budget.py                 # 用例4 预算/慢组故障注入
uv run pytest tests/unit/test_industry_modules_structures.py # 用例5 真实产物回放
uv run pytest tests/unit/test_partial_publish.py            # 部分发布与终止路径
uv run pytest tests/unit/test_objective_compilation.py \
           tests/unit/test_report_dependency_closure.py     # 目标编译与报告依赖闭包
```
