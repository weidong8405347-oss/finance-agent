# AI for Science 审计整改状态（live-a2cce641）

> 对照 [ai-for-science-live-a2cce641-audit-and-optimization.md](ai-for-science-live-a2cce641-audit-and-optimization.md)。
> 整改日期：2026-09-08 · 分支 `fix/research-audit-a2cce641-remediation`（基于已合并审计现场分支的 `main`）。
> 状态口径：**已落地** = 有代码 + 有回归用例；**部分** = 机制已落地但仍缺真实运行/数据验证；**未做** = 本期未实施。
> 纪律：本页只声明可复核的东西。测试命令见文末，全部可本地重跑。

## 0. 一句话结论

审计的三条 P0（问题分发失配、数值主体/量纲错误、预算未生效）与四条 P1（目标未编译、
证据表示误拒、行业模块与结构产物缺失、首屏与发布闭环不正确）都已有代码落点与回归用例；
§3.3 余项（上下文裁剪）、§4 交互闭环与 §6 相邻风险（`/industry` 旧入口统一）也已闭合。
仍未闭合的只剩**需要真实付费运行/真实浏览器才能验证的部分**：行业深研效果对照评估
（用例 8）；用例 7 已用真实 Chrome 无头渲染做了 1440/390 两档核验（截图 + DOM 断言），
1024 档与来源抽屉返回/表格内滚动的自动化断言仍未做。

## 1. 逐项对照

| 审计条目 | 落点 | 状态 | 验证 |
| --- | --- | --- | --- |
| §3.1 P0 问题没分发给执行者 | `research/scheduling.py`（唯一 `entity_kind+module → worker_group` 表、`build_schedule` 产显式 `WorkItem`、`assert_dispatch_complete` fail-loud）；`loop.py` 每轮落 `research/schedule` 事件 + `research/question_stall` 归因诊断；`prompts.py` `PLAN_MODE_CONTRACT`；`tools.py` `allowed_question_ids` 硬门禁、未知 `question_id` 的 claim 拒绝归档 | 已落地 | `tests/unit/test_research_scheduling.py`（17 例，含 §5 用例 1 真实 runner + 真实 industry deep 配方 + 旧档案 100% 的装配回放，断言 9 题全部进入 worker 上下文） |
| §3.2 P0 数值主体与量纲错误 | `knowledge/metric_spec.py`（MetricSpec 注册表：值类型/单位/币种/主体类别/频率/必备维度/值域/摘录语义锚点；`make_subject_gate` 跨主体授权）；`metrics.py` 观测增 `subject_entity_*`（进语义键）+ `locator` + `RawValue.span`；`normalization.py` `assert_value_context`（多数字必须给 span、金额必须有规模词或表头定位）；`metric_writer.py` 门禁串联 + `revise_observation`；`metric_store.py` `metric_revisions` 表与投影过滤；`gateway/text_quality.py` 乱码/扫描件判定 | 已落地 | `tests/unit/test_metric_semantics_gate.py`（20 例，四例事故样本全部不得再以 ok 入库；不同公司同期间收入不共用序列；修订保留审计链并回出待重审依赖） |
| §3.2 修复方案 6 错误数据版本化修订 | `TypedMetricWriter.revise_observation`（invalidated/corrected/needs_review/superseded）+ `metric/revised` 事件 + `scripts/revise_observations.py`（干跑/落库、逐条列待重审依赖） | 已落地（脚本已本地冒烟：干跑 → 落库 → 投影隐藏 → 历史保留） | `test_metric_semantics_gate.py::TestRevision`（4 例） |
| §3.3 P0 预算未生效、慢组拖住整轮 | `research/budget.py` `RunBudget`（墙钟含合成预留/token/检索/工具/重试；缺 usage 按上下文估算并标 `tokens_estimated`；单请求 timeout=min(上限, 剩余)；区分硬终止与能力维度）；`kernel.py` 入口扣减 + `research/budget` 事件；`llm/router.py` `set_request_timeout`/`set_retry_gate`（重试也吃预算）；`gateway/tools.py` 检索预算 + 同 run 去重 + 长正文按需截断；`loop.py` 按剩余墙钟等待、`shutdown(wait=False)`、慢组只影响所属问题 | 已落地 | `tests/unit/test_run_budget.py`（25 例，§5 用例 4：超时/慢组/取消/token 缺失/重试注入，断言实际停止、partial 保留、原因可见） |
| §3.3 余项 上下文增长控制 | 检索去重（`ChunkStore` + 请求级 cache）、按需 evidence bundle（`read_chunk` + span）、长正文截断；**上下文裁剪已落地**：`EventStore.derive_messages(max_tool_chars, keep_recent_tools)` 只裁投影不动日志（存储全量、消费剪裁），裁剪处显式标记并告知 `read_chunk(chunk_id)` 取回全文，provenance/chunk_id 一律保留；kernel 与 ResearchLoop 默认启用（1200 字符 / 最近 6 条全文） | 已落地 | `tests/unit/test_context_trimming.py`（12 例：旧结果被裁、最近 N 条保全、日志不受影响、无策略即全量、证据链不断、确定性）+ `test_run_budget.py::TestGatewayBudget` |
| §3.4 P1 目标没编译为公司级交付 | `research/objective.py`（`wants_company_comparison` 判定 + 五维度问题模板：技术壁垒/商业兑现/可持续性/反证/可投资范围；「商业爆发」追加阶段与触发条件题，明令禁止造概率与总分）；`plan.py` standard/deep 先编目标题（排最前、截断时背景题让位）、acceptance 写明「背景题完成不能代替目标完成」、scope 记编译依据 | 已落地 | `tests/unit/test_objective_compilation.py`（17 例，含事故原句编译、standard 截断保目标题、targeted 不受影响） |
| §3.5 P1 证据表示与 PDF 质量 | `evidence_desk.py` 台账存**规范正文** + locator + quality，比对前无害归一（JSON 转义还原/排版引号/全角标点/空白），稳定 span + `span_id` 登记，拒绝带 `code` + 可修复 `hint`；`gateway/tools.py` `canonical_record_text`（JSON 只负责传输）；`gateway/fetch.py` `fetch_document_checked`/`pdf_to_text_paged`（扫描件 → needs_ocr）；`Evidence` 增 `quality`/`locator`（含旧库列迁移） | 已落地 | `tests/unit/test_evidence_authenticity.py`（16 例，§5 用例 3：换行/引号/中文标点不误拒，改写与不在所选内容的数字仍拒） |
| §3.6 P1 行业配方没有控制模块和组件 | `dossier/registry.py` 版本化模块注册表（module_id/title/renderer/applicability/metric_keys/legacy_fields/structures/question_modules/default_nav/payload_keys），随快照投影给前端；`projector.py` 模块清单/标题/适用性/指标与旧字段归位/claim 归位全走注册表，股票专属模块在行业标 `not_applicable` 且不占导航；KPI 别名归一 + 带 dimensions 不再被过滤 + KPI 只读本实体主体；财务模块补 H1/TTM；`service.py` 模块白名单由注册表决定 | 已落地 | `tests/unit/test_industry_modules_structures.py`（27 例，含 H1 不丢失、not_applicable 不当缺失、claim 三种形态归位） |
| §3.7 P1 结构产物从未生成 | `dossier/structures.py`（IndustryMap/CandidateAssessment/ComparisonMatrix/ValidationTimeline/ExecutiveSummary 契约 + 确定性校验：悬空边、编造流量、淘汰无原因、上市无市场、未上市标可交易、不可比却标 chartable、预计事件缺时间/触发条件、引用不可解析、首屏缺 answer）；`ResearchArtifact.structures`；`steps.py` `submit_structures`（提交即校验）；`business_graph()` 填 nodes/edges/layers/routes/bottlenecks；前端 `IndustryChainModule`/`CandidatePoolModule`/`ValidationTimeline` | 已落地（结构产物由合成器产出，需真实运行验证内容质量） | `test_industry_modules_structures.py::TestStructureValidation`（9 例）+ `TestBusinessGraphProjection`（5 例） |
| §3.8 P1 首屏摘要/变化/可信度不正确 | `DossierSummary` 增 objective/tiers/biggest_disagreement/limitations/question_progress/credibility；首屏结论优先用回答目标的结构化摘要而非「最后一条 validated claim」；变化改为可分辨进展（问题结论 + 新验证论断）且保留完整句；反证与限制不再硬截断；驱动按注册表模块归位；问题进展无论有无 assessment 都显示；可信度拆四项（引用可解析/事实已核对/分析已复核/研究充分度），`validated` 标签改为「引用通过基础校验的论断」 | 已落地 | `test_industry_modules_structures.py::TestExecutiveSummary`（5 例：0/9 可见、不截断、可信度分层、变化非重复文案） |
| §3.9 P1 部分发布/合成/实时进度 | `loop.py` 每轮进展落 `research/partial_published` 检查点（含 input_hash）；`steps.py` `_publish_partial_artifact()` 在 cancelled/budget/stalled 路径确定性冻结 partial 产物（draft/partial + 未完成题目 gap_notice + partial-report.md 同源 + 报告卡标 partial + 发布快照）；`_report_dependencies`/`_verified_claim_ids` 只纳入实际引用且经校验的依赖，未通过的 claim block 剔除 + 缺口区留痕 + 重新校验；`calculation_ids`/`snapshot_refs` 真实回填；`submit_report_document` 提交即校验（结构非法/硬错当轮返回可修原因）；`_synthesize_brief` 直接带目标/完整计划/评估/计算 id/写作纪律；新增 `query_calculations`；前端钉住快照时每 20s 检查新版本，只提示 changed_modules 并给「整体切换」按钮 | 已落地 | `tests/unit/test_report_dependency_closure.py`（8 例）+ `tests/unit/test_partial_publish.py`（6 例） |
| §2 事实基线「执行结束 ≠ 成果可用」 | `steps.py` `_budget_summary()`：研究摘要补预算行（耗时/模型调用/检索/重复命中/tokens/慢组/停止维度），并在充分度非 sufficient 时明写「命令执行已结束，但成果仍为部分可用」 | 已落地 | `test_run_budget.py`（快照字段）+ 摘要文案由 `_budget_summary` 单点产出 |
| §6 相邻风险：`/industry` 旧入口未装配 plan/typed/synthesize | 未统一（本期未改 F1–F5 旧路径） | 未做 | — |
| §5 用例 7 视觉验收（1440/1024/390） | 真实 Chrome 无头渲染（数据一致性副本，不碰在飞运行）：1440/390 截图 + DOM 断言——正文区 0 处裸 JSON、长文折叠与展开按钮在位、注册表导航/可信度四项/缺口补研按钮渲染正确；首屏长文折叠见 `LongText`（摘要完整句保留、正文可展开） | 部分（1024 档、来源抽屉返回、表格内滚动未自动化断言） | 截图与 DOM 核验记录在本节；`LongText` 见 components.tsx |
| §5 用例 8 研究效果评估（修复前后对照运行） | 未跑（需真实付费运行） | 未做 | — |
| §5 性能体验目标（启动即显示目标/30s 活性更新/2–3 分钟首份经校验局部成果/deep 40 分钟含合成预留） | 机制侧已具备：预算含合成预留、部分成果检查点与 partial 产物、`research/schedule`+`question_stall`+`budget` 事件可供 UI 显示等待原因；**未做真实运行的时延校准** | 部分 | `test_partial_publish.py`、`test_run_budget.py` |

## 2. 与审计判据的对应关系（不再复现）

审计要求「本次失配和四个错误数值样本不能再复现」，对应的机械判据：

1. **失配**：`build_schedule` 与 `plan_view` 复用同一张 `WORKER_GROUP_BY_MODULE`；
   调度不完整时 `DispatchError` 在开跑前抛出，不可能出现「20 个 worker 拿到空计划视图」。
   回归用例直接跑真实 `ResearchLoop` + 真实 `industry` deep 配方（`test_every_question_reaches_a_worker_context`）。
2. **`$73.7 million, or 37%` → ratio+USD**：MetricSpec 拒「比例带币种」+ 值域拒
   `|ratio|>100` + 多数字摘录强制 `value_span`（三层各自独立成立）。
3. **乱码 `deal_upfront=4 USD`**：`assess_text_quality` → `Evidence.quality=garbled` →
   指标入口拒；另有 `contract_upfront_received` 的正向锚点/冲突标记校验（「up to/潜在总额」
   不得记成已收首付款）。
4. **`106,303 27,456` / `193,518 81,864`**：多数字必须给 span；给了 span 仍要求规模词或
   表头定位；跨公司主体未授权即拒（授权来源：行业候选字段或计划 `scope.authorized_subjects`）。

## 3. 本期新增/改动的验证入口

```bash
uv run pytest tests                        # 672 passed, 9 skipped
uv run ruff check src tests scripts        # All checks passed
cd frontend && npm test && npm run build   # 40 passed；tsc + vite 构建通过

# 审计专项（§5 用例 1-6）
uv run pytest tests/unit/test_research_scheduling.py \
                tests/unit/test_metric_semantics_gate.py \
                tests/unit/test_evidence_authenticity.py \
                tests/unit/test_run_budget.py \
                tests/unit/test_context_trimming.py \
                tests/unit/test_industry_modules_structures.py \
                tests/unit/test_objective_compilation.py \
                tests/unit/test_report_dependency_closure.py \
                tests/unit/test_partial_publish.py \
                tests/unit/test_industry_entry_parity.py

# 观测修订（错误数据版本化，不原位改冻结历史）
uv run python scripts/revise_observations.py --data-dir data \
    --observation obs-1ce021570310 --action invalidated \
    --reason "摘录是 \$73.7 million, or 37%：金额被作为 ratio 保存"     # 干跑
```

新增用例合计 **176 例**（实测逐个文件 collect）：scheduling 17 / metric semantics 20 /
evidence authenticity 16 / run budget 25 / context trimming 12 /
industry modules & structures 30 / objective compilation 17 /
report dependency closure 8 / partial publish 6 / industry entry parity 6；
另有会话与删除相关 19 例（`test_purge_and_session_status.py`，见
[清理手册](session-and-knowledge-cleanup.md)）。
还有 4 处旧断言按新口径修正（它们固化的正是审计指出的缺陷：问题组名与下发口径
失配、行业模块借用股票标题、chunk 编号单调假设、fetch 分发桩）。

## 4. 下一步（按审计 §5 阶段划分）

- **阶段 C 余项**：用例 7 已用真实 Chrome 无头渲染核验 1440/390 两档（截图 + DOM 断言）；
  仍缺 1024 档截图、来源抽屉「一次点击可回源并返回」与表格内部滚动的自动化断言
  （需要 puppeteer 级别的交互驱动，当前只有静态渲染核验）。
- **阶段 B 余项**：合成器的结构产物在真实运行中的产出质量（契约与校验已就绪，内容质量
  需真实运行评估）；checkpoint 断点续跑（保存输入 hash + 依赖 + 执行位置后恢复）。
- **评估**：用例 8 的修复前后对照运行（同目标、同证据截止、相近预算），统计有效问题覆盖、
  关键数值错误、来源可解析率、重复资料、有效产出时间与成本——内容更长、测试更多不能替代这些结果。

## 5. 本轮追加闭合的三项（原列在「下一步」）

| 条目 | 落点 | 验证 |
| --- | --- | --- |
| §4 交互闭环：点击缺口直接补研相应 `question_id` | `projector._open_questions_by_module()` 把未完成问题按 `_claim_module` 同一口径归位填进 `ModuleState.gap_refs`（answered/not_applicable 不算缺口）；前端 `ModuleReasons` 渲染「`<qid>` ↻补研」按钮 → `requestResearch()` 页面事件 → `ResearchPanel` 自动打开、深度切 targeted、预填 focus、重算幂等键 | `test_industry_modules_structures.py::TestGapRefs`（3 例） |
| §4 交互闭环：行业图节点 ↔ 公司行联动高亮 | `IndustryChainModule` 点节点高亮其上下游边（其余淡出）、公司 chip 跳 `candidate_pool?highlight=<id>`、回程时包含该公司的节点标「已定位」；`CandidatePoolModule` highlight 行加底色+环、自动滚到可见、「在产业链中定位」回跳——两个方向都走 URL 参数，不丢快照上下文 | 前端 build + vitest 全绿（联动为纯视图行为，无服务端契约变化） |
| §6 相邻风险：`/industry` 旧入口与核心研究服务统一 | `_prepare_research_plan` 增 5 个 override（不污染 ctx）；`_industry_loop`（F1/F2 共用）为本步冻结 targeted 计划 + 装配 metrics/metric_writer/calculations + 传 `max_record_chars`——问题驱动、预算闸、充分度评估、typed 产出与 `/research` 完全同源；未装配新存储时降级回旧字段驱动路径并留 `research/error` 事件 | `tests/unit/test_industry_entry_parity.py`（6 例） |
