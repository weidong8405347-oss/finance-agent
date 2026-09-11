# Research / Profile 能力加强：实施记录（2026-09-10，两轮 + 第三/四/五/六轮）

> 对应设计方案：[research-profile-tools-plugins-plan-2026-09-09.md](research-profile-tools-plugins-plan-2026-09-09.md)
> 第一轮范围：方案 §11.2「最值得先做的范围」= **P0 + 统一知识读取 + Document Read v2 + SEC 财务工具 + 哨兵题集脚手架**。
> 第二轮范围：哨兵基线 A/A' 真实运行 + 基线发现整改（R1–R8、F1–F13）+ **P1-C 薄插件层 + P2-A 证据核验与研究路径 + P2-B profile.consolidator**。
> 第三轮范围：交付复核 code review 12 条正确性意见修复（发布门禁、时态修订、失效语义贯通、整合提交原子化、依赖闭包、证据血缘展开、文档版本/完整性、来源主机名识别，见 §6）。
> 第四轮范围：review 建议的下一步——**P1-C 执行闭环收口** + SearchBroker（见 §7）。
> 第五轮范围：review 剩余项中无外部依赖的两项——**P2-A 来源独立性判断** 与
> **P1-A 表格/单元格定位轻量路径**（见 §7.7）。
> 第六轮范围：review 剩余项继续——**P2-A 二次独立核验**（不同模型复核重大/数值型结论）
> 与 **P2-B 完整变化解释及重算**（更正观测进失效闭包、依赖计算引用重映射重跑）（见 §7.8）。
> 第七轮范围：**B 组对照新发现整改**——F15 核验覆盖率（合成定稿前服务端批量核验 +
> 未核验软问题可见）与 F16 信号口径拆分（去重命中 vs 唯一 chunk 分列）（见 §7.9）。
> 分支 `feat/research-profile-capability-upgrade`；基线 `c718a66`（main）。
> 状态口径：**已落地** = 有代码与测试；**未做** = 本轮未实施（如实标注）。
> 哨兵基线 A（6 题）与 A' 验证重跑（3 题 + ai4s 漏斗）为**真实付费运行**；未购买数据服务、未跑 ChatGPT Deep Research 同题对照。基线详情见 [sentinel-baseline-A-2026-09-10.md](sentinel-baseline-A-2026-09-10.md)。

## 1. 交付对照（方案章节 → 代码落点 → 测试）

| 方案条目 | 交付 | 代码落点 | 测试 |
| --- | --- | --- | --- |
| §2/§11 P0「旧事实冲突工具」 | **已落地**：裁决真正选择并保存获胜版本——`keep_fact_id`/`keep_evidence_id` 定位获胜方 → 非最新版同值晋升落当前投影 → 清竞争标记；定位失败 fail-loud 不清标记；事件带 winner/promoted id | `knowledge/writer.py::adjudicate_conflict`；`research/tools.py::resolve_conflict`；人工裁决 API 行为不变 | `test_p0_trust_remediation.py::TestConflictAdjudication`（6 例） |
| §2/§5.4 P0「论断核验」 | **已落地（第一步）**：核验状态拆分为独立字段 `verification{references_valid, evidence_support, numeric_checks, analysis_review, counter_evidence_search}`；`validated` 仅表示引用校验过；旧数据缺省 `unchecked` 不冒充内容已核验；assessment 披露未核验数量并进 notes。内容级 verifier（verify_claim/EvidencePack）属 P2-A，**未做** | `research/artifacts.py::ClaimVerification`；`research/tools.py::propose_claim`；`research/assessment.py` | `TestClaimVerificationSplit`（4 例） |
| §2 P0「来源质量」 | **已落地**：`first_party_observations` 不再统计 PIT A——按来源角色分档（issuer_filing/media_secondary/vendor_snapshot/market_data），PIT 单独计数 `pit_a_observations`；guidance=发行人一手、consensus=vendor、calculated=derived、证据源解析不了=unknown（不默认一手） | `research/assessment.py::SOURCE_ROLES/classify_observation_source`；`loop._finalize_assessment` 传 evidence→source 映射 | `TestSourceQualitySeparation`（2 例） |
| §2 P0「研究策略」 | **已落地**：分配到问题的 worker 用问题驱动纪律（搜索发现→精读→证据→观测/论断→立即交题），legacy 补字段模式才保留逐字段纪律；playbook 双模式改写；精读提示只引用实际装配的工具 | `research/loop.py::_worker_discipline`；`playbooks/dimension_researcher.md` | `TestPlanDisciplineAndRubricInput`；`test_research_scheduling.py` brief 断言 |
| §2 P0「研究评审」 | **已落地**：rubric digest 附本轮论断原文 + 支持证据摘录 + 问题结论/未解决项（有界 12k 字符，超限显式截断），评审不再只看 ID 和计数 | `research/loop.py::_build_judge_digest` | `TestPlanDisciplineAndRubricInput`（3 例） |
| §5.3 统一知识与档案工具（PR#2） | **已落地**：共享模块 `get_research_context / query_observations / query_claims / query_calculations / read_evidence / list_conflicts / adjudicate_conflict`——过滤 + 游标分页（截断不静默：total/next_cursor）、批量证据逐项成功/失败、typed 语义键裁决（版本链归属 + 证据关联校验，落 `metric/conflict_resolved` 事件，投影按时态取获胜版本）；as_of/namespace/实体由运行上下文固定，模型参数只能缩小范围 | `research/context_tools.py`（新）；S1 `make_research_tools` 注入（`resolve_conflict` 保兼容别名）；合成 step 内联查询替换为共享实现（限额沿用旧上限，只读不装配裁决）；committee/rank_report 的 read_evidence 统一契约 | `test_context_tools.py`（19 例） |
| §9.1 S2 整合（第一步） | **已落地**：`profile_update` 从「query_kb→propose_thesis」扩展为：读冻结基线与本轮产出（含计划问题结论）→ 检查/裁决开放冲突 → thesis 同时落 Fact（兼容投影）+ 带证据/limitations 的分析 Claim（`legacy_field=thesis`，dossier 投影据此取总论）；6→10 步。完整 consolidator（依赖失效/语义 diff/prepare+commit）属 P2-B，**未做** | `commands/steps.py::step_profile_update`、`_PROFILE_CONTRACT`、`PROFILE_TOOL_SCHEMAS` | `TestSharedWiring::test_s2_*`（2 例，真实装配） |
| §5.1 Document Read v2（PR#3） | **已落地**：run 级 DocumentStore（内容哈希去重、同版本复用不重抓、同内容不同来源身份分开 PIT 不互相污染）；PDF 逐页解析保页码（首次上限 400 页，之外惰性续解——**80 页后表格可达**）；失败页显式记录；HTML 标题目录；`fetch_document / read_document / search_document` 三工具 + `read_edgar_filing` 兼容别名；completeness（full/partial/truncated/failed + total/parsed/failed/unparsed）随响应返回；窗口 chunk 带 document/page locator 下沉进 Evidence；抓取失败显式区分「≠未披露」；直接 URL 抓取 C 级诚实降级 | `gateway/documents.py`（新）；`gateway/fetch.py::pdf_pages/fetch_document_paged/extract_headings`；`research/tools.py` 文档工具组；loop/steps/cli 装配（`StepDeps.fetch_document_paged` 缺省安全，eval 回放退回旧路径） | `test_document_read_v2.py`（20 例，手工构造多页 PDF） |
| §5.2/§7.1 SEC 结构化披露（PR#5） | **已落地**：`query_edgar_facts`（companyfacts XBRL：原始 tag/unit/期间/fy/fp/form/accn + value_text 逐字数字 + 原文链接；tags/forms/units/period/limit 过滤；按 CIK 缓存；形状异常 fail-loud）；公开时刻 **acceptanceDateTime 优先**，只有日精度 filingDate 时保守取 UTC 日末（不再当作零点公开）；`query_edgar` 支持历史分段遍历（include_history，单段失败降级可见）；SEC ≤10 req/s 进程内节流 | `gateway/adapters/edgar_facts.py`（新）；`gateway/adapters/edgar.py::acceptance_or_eod/_throttled_get`；`gateway/tools.py` schema；cli 注册（评估回放网关保持最小源集） | `test_edgar_facts.py`（16 例，离线夹具） |
| §10.1 哨兵题集（脚手架） | **已落地（脚手架）**：6 题冻结定义（BE 订单口径 / 2228.HK 中文财报 / AI4S 技术兑现 / 财报更正 / 80 页后表格 / 指引兑现）+ 运行器（隔离数据目录、事件信号采集、超时/跳过显式）。**真实运行未执行**；3 题 ticker 待冻结 | `evals/sentinel_tasks.yaml`；`scripts/run_sentinel.py` | `test_sentinel_tasks.py`（3 例，离线） |

本轮新增/更新测试 60+ 例；全量 `uv run pytest` **812 passed, 9 skipped**；`ruff check` 通过。

## 2. 行为契约变化（下游需要知道的）

1. **`validated` 语义收窄**：论断 `status=validated` 仅表示「引用校验过」；内容级支持性看
   `verification.evidence_support`（当前全部 `unchecked`，P2-A verifier 才产出非 unchecked）。
   assessment `evidence_quality` 新增 `validated_claims_content_unchecked` 与来源分档键；
   `first_party_observations` 的含义变了（真一手，不再是 PIT A）。
2. **EDGAR `available_at` 语义**：acceptance（分钟精度）优先；缺失时日精度保守取 UTC 日末。
   评估回放同日边界更保守（安全方向）；payload 透明保留 `filingDate/acceptanceDateTime`。
3. **合成 step 的 typed 查询返回形状**：从裸数组变为 `{total, returned, cursor, next_cursor, items}`
   （截断不静默）。模型侧契约（_SYNTHESIZE_CONTRACT_V2）已同步。
4. **S2 产出**：thesis 现在同时落 Fact + Claim（`legacy_field=thesis`）；dossier 投影的总论
   读取路径不变（validated analysis claim 优先），旧 decision 入口读 Fact 不受影响。
5. **研究工具面扩大**：S1 worker 新增 get_research_context/query_observations/query_claims/
   query_calculations/read_evidence/list_conflicts/adjudicate_conflict/fetch_document/
   read_document/search_document；`resolve_conflict`、`read_edgar_filing` 保留为兼容别名。
   新增数据源 `query_edgar_facts`（serve 装配默认注册，无需 key）。
6. **预算快照**新增 `duplicate_documents/documents_stored`（重复资料率可见性）。

## 3. 未做（第一轮末状态；第二轮已推进项见 §5，最新清单见 §5.6）

| 方案条目 | 状态 |
| --- | --- |
| P1-C 薄插件层（registry/contracts/executor/manifest freezing/能力页编译） | 未做——本轮共享模块与 adapter 已按「先包装现有能力」的方向收敛装配点，为 registry 化做准备 |
| P2-A EvidencePack + 内容级 verify_claim + 反证闭环 + rubric 新输入完整体 | 未做（本轮只落了核验状态字段拆分与 rubric digest 内容化） |
| P2-B profile.consolidator（prepare/commit_profile_update、依赖失效、语义 diff）、source.earnings/consensus/HK 授权接入 | 未做（S2 只落了整合第一步） |
| SearchBroker（Exa 主 + Tavily 备、转载族去重）、Exa 新参数验证 | 未做（文档库层面的内容哈希去重已落） |
| Docling/OCR 结构解析试点、表格抽取 extract_table | 未做（保页码 + 惰性续解已落，结构解析是下一个增量） |
| §8.3 语义压缩（保留问题状态与关键证据的压缩事件） | 未做（沿用现有投影裁剪） |
| P3 24 题对照、消融、盲评 | 未做（6 题哨兵脚手架已就绪，真实运行待执行） |
| MCP bridge / engine.deep_research 试点 | 未做（方案第三批） |

## 4. 验收入口

```bash
uv run pytest                                        # 1017 passed, 9 skipped（第七轮后）
uv run ruff check src tests scripts

# 第一轮专项回归
uv run pytest tests/unit/test_p0_trust_remediation.py    # P0 可信度与策略
uv run pytest tests/unit/test_context_tools.py           # 统一知识读取 + S2 整合装配
uv run pytest tests/unit/test_document_read_v2.py        # 文档服务/分页/完整性/PIT
uv run pytest tests/unit/test_edgar_facts.py             # XBRL/acceptance/历史分段

# 第二轮专项回归
uv run pytest tests/unit/test_baseline_findings_round3.py # F10 量表闸/F11 修复回环/F12 裁决纪律
uv run pytest tests/unit/test_plugin_registry.py          # P1-C 插件层（含迁移 parity）
uv run pytest tests/unit/test_claim_verifier.py           # P2-A 核验/批量提交/子问题/状态卡
uv run pytest tests/unit/test_profile_consolidator.py     # P2-B 整合/幂等/时态失效

# 第三轮专项回归（交付复核 code review 12 条）
uv run pytest tests/unit/test_review_fixes_round2.py      # 发布门禁/时态修订/失效贯通/原子提交/
                                                          # 依赖闭包/血缘展开/文档版本/来源主机名

# 第四轮专项回归（P1-C 执行闭环 + SearchBroker）
uv run pytest tests/unit/test_plugin_registry.py          # 注册表/冻结/executor/运行期绑定
uv run pytest tests/unit/test_search_broker.py            # 双源主备/去重/转载族/预算/trace

# 第五轮专项回归（review 剩余项）
uv run pytest tests/unit/test_source_independence.py      # 来源独立性归组/核验记录/评估披露
uv run pytest tests/unit/test_tables.py                   # 表格候选抽取/定位/完整性纪律

# 第六/七轮专项回归
uv run pytest tests/unit/test_claim_verifier.py           # 含二次独立核验（TestSecondIndependentReview）
uv run pytest tests/unit/test_profile_consolidator.py     # 含变化解释与重算（TestRecomputeCalculations）
uv run pytest tests/unit/test_report_dependency_closure.py # 含合成批量核验（TestBatchVerificationAtSynthesize）

# 哨兵题集（脚手架离线自检 → 真实付费运行需 LLM key + 网络）
uv run python scripts/run_sentinel.py --list
uv run python scripts/run_sentinel.py --task be-orders-revenue --dry-run
uv run python scripts/run_sentinel.py --all --auto-approve-gates   # 真实运行（隔离数据目录）
```

## 5. 第二轮：基线验证与 P1-C/P2 交付（2026-09-10 晚）

### 5.1 哨兵基线 A / A'（真实运行，详见基线报告）

- 基线 A（6 题，commit c7f5af5→33fd781）：5/6 题走通全链（3 题 sufficient），
  411 页 PDF 附注直达、中文乱码诚实拦截、重述链条逐字准确、指引-实际正确配对；
  运行中即时整改 R1–R8，基线发现 F1–F9 已全部整改（a19fa90，27 例回归）。
- A' 验证重跑（NVDA/BE/2228.HK + ai4s 漏斗）：三题全改善（typed refs 0→4/8、
  focus 题编译命中、提交率 3/12→12/13、单位违例归零）；ai4s F1→F4.5 全链验证、
  新发现 F10–F13。
- ai4s F5 补跑（旧运行器 120min 超时杀死 daemon 线程丢失 rank_report）：已用
  宽限窗口版运行器（45min grace + 180min 题时限）在冻结 worktree 重跑，
  结果落 `data/sentinel/ai4s-final/`。

### 5.2 基线发现整改（F10–F13，commit 9ec8283 + P2-A）

| # | 整改 | 落点 |
| --- | --- | --- |
| F10 量表归一 | 规模词识别扩展（RMB'000/'000s → thousand）；写侧硬闸：原文/表头声明规模词而换算链无步骤 → 拒写给修法；离群扫描（同语义组 ~10^3/10^6 倍 + 同值异键）进 assessment 披露与 propose_metric 响应预警 | normalization/metric_writer 2c/assessment/numeric_consistency_scan |
| F11 stalled 提前终止 | 提交类门禁拒绝（propose_metric/claim/fact、answer、calc）与高价值精读（文档工具/read_chunk ≥3）= 可验证探索；plan 模式下 exploration-only 轮给 ≤2 轮修复回环（不重置预算），超限 stalled 且诊断卡指向拒绝原因；组级基础设施失败（死 provider）不享受回环 | loop/_Tracker/IterationReport.exploration_only |
| F12 冲突未裁决 | PLAN_MODE_CONTRACT：数值结论交题前 list_conflicts，有则先 adjudicate 或在 unresolved 注明 | prompts |
| F13 重复窗口注册 | 状态卡列出已存档文档与证据 ID 回流下轮 brief（复用不重抓）+ EvidencePack 按问题组织证据（根治方向） | loop 状态卡/evidence_pack |

### 5.3 P1-C 薄插件层（commit 83bd3ec，方案 §6.2）

- `plugins/` 包：contracts（PluginManifest/ToolDefinition/HOST_CAPABILITIES）、
  registry（重名拒绝/api_version/依赖迭代/配置校验/场景编译/能力页 payload）、
  executor（参数校验/超时/有界重试扣预算/错误码封装/trace 事件）、
  freezing（plugins/manifest_frozen，密钥不进哈希）、builtin（14 个内建插件
  包装现有能力）。
- cli 装配改 registry 驱动：**迁移不改变行为**（编译出的 adapter 集合与旧手工
  装配逐一相等，parity 测试锁定）；缺凭证 missing_config 可见；akshare 缺失
  degraded 可见；capabilities_info 输出插件编译视图；step_research/profile_update
  落 manifest 冻结事件（无 registry 的旧装配跳过，行为不变）。
- 内建强约束仍在宿主（时间准入/证据绑定/MetricSpec/单写者/快照隔离），
  manifest 无任何字段可关闭它们。

### 5.4 P2-A 证据核验与研究路径（commit 45b2024，方案 §5.4/§8.1/§8.3）

- EvidencePack（research/evidence_pack.py）：从既有 typed 数据组装（观测背后证据
  原文一并进包），input_hash 可复现；不建新事实库。
- verify_claim（research/verifier.py）：硬检查（引用/数字逐字/主体期间/冲突，
  确定性代码）+ 内容审查（原子论断逐条 verdict，LLM 可审计意见非真值）；
  聚合是硬规则：数字硬失败封顶 partially_supported（软评分不得抵消硬失败）；
  contradicted/insufficient 的 validated 降级 draft；发布规则：ArtifactValidator
  对 contradicted 引用硬失败、insufficient 软问题；LLM 不可用/解析失败 →
  content_review_available=false 诚实降级（不冒充已核验）。
- 反证闭环：counter_search{queries,sources,found} 落 verification.notes，
  找不到反证也保存检索范围；无记录 → next_actions 要求补检索。
- submit_question_result：一题的观测/论断/答案一次提交，逐项门禁不放松，
  新接受引用自动并入 support_refs。
- track_sub_question：内部子问题版本化追加（触发证据/退出条件），硬约束不扩
  预算与范围，同文本幂等，越位拒绝；回流 plan brief。
- 语义压缩状态卡：轮末确定性构建（目标/问题状态/证据 ID+locator/已存档文档/
  冲突/论断与核验状态/下一步），落 research/context_compressed 事件
  （source_events 区间 + state_hash，原日志不删可重建）并回流下一轮 brief。
- assessment 新增 claims_by_evidence_support 分档；rubric digest 带核验状态。

### 5.5 P2-B profile.consolidator（commit 3e18d16 + c8c06b1，方案 §9.1/§9.3）

- dependency_graph：document→observation→calculation→claim→module 依赖边
 （复用现有引用关系）。
- prepare_profile_update：确定性只读预览（待合并/重复与量表可疑/冲突/失效依赖/
  预期 diff/change_set_id + expected_base_hash）。
- commit_profile_update：幂等（change_set_id 重放返回首次结果）；基线哈希不符
  拒绝（并发新观测/修订才触发；S2 自身的 thesis/裁决不触发）；invalidate_claims
  逐条验归属后**追加** claim_invalidations（时态记录，旧快照与冻结 payload 不变）；
  profile/update_committed + profile/claim_invalidated 事件。
- S2 全链：prepare → 裁决 → thesis（Fact+Claim）→ verify_claim → commit；
  拒绝回到模型上下文可修正重试。

### 5.6 第二轮后仍未做（如实清单）

| 方案条目 | 状态 |
| --- | --- |
| source.earnings（电话会/指引）、source.consensus（一致预期） | 未接——依赖供应商验收与套餐权限（方案 §7.2 验收卡流程），guidance 观测契约已就绪 |
| source.disclosures_hk/cn 授权接入（HKEX IIS/巨潮） | 未做——现有 hkex_news 公开通道保留，正式授权接口待商务 |
| SearchBroker（Exa 主 + Tavily 备、转载族去重） | **已落地**（第四轮，含预算/trace/台账纪律）；Exa 新参数 Novita 透传验证仍未做 |
| Docling/OCR 结构解析试点、extract_table（表头/单元格定位） | 未做——基线实证 2228 题 4/8 中文 PDF garbled 是当前最大原文可得性缺口，B 组首选 |
| engine.deep_research 外部研究引擎试点、MCP bridge | 未做（方案第三批） |
| 重要结论第二次独立提取/抽样人工核对（§5.4 检查 4） | 未做（属 P3 评估/人工流程） |
| 24 题扩展、消融、盲评（P3） | 未做（6 题哨兵 + A/A' 对照已跑通，扩展依失败分布决策） |
| 定时监控/事件驱动刷新（§9.3 后续产品能力） | 未做（第一期用户触发 refresh，按计划） |

## 6. 第三轮：交付复核 code review 修复（12 条，tests/unit/test_review_fixes_round2.py 30 例）

| review | 问题 | 修复 | 代码落点 |
| --- | --- | --- | --- |
| R1 [P1] | 数值硬检查失败（原文 300M/论断 950M）论断仍 validated、报告校验零问题 | verifier 对 numeric/analysis 失败、引用硬失败一律降级 validated→draft；ArtifactValidator 直接消费四项核验状态：`claim_numeric_check_failed`/`claim_analysis_review_failed` 硬失败、`claim_evidence_partial` 软可见 | `research/verifier.py`、`research/artifacts.py` |
| R2 [P1] | read_evidence 按 ID 读 obs/calc/claim 不验主体/namespace/截止时间（2020 年 AAPL 评估上下文读到生产库 BE 后续资料） | typed 引用逐条过 get_ref_meta 校验 namespace+实体+登记时间；ev 按可知时间、fact 按 knowledge_time 守上下文截止；逐项返回拒绝原因 | `research/context_tools.py::_typed_meta_guard/_temporally_blocked` |
| R3 [P1] | 核验直接覆盖原 claim，历史投影被改写 | 新增 `research_claim_versions` 版本链（save_claim 追加 recorded_at 版本行，旧库按 created_at 补基线迁移）；claims_as_of 按 recorded_at≤T 重建历史状态 | `knowledge/metric_store.py` |
| R4 [P1] | claim_invalidations 落库但无人消费 | claims_as_of 默认排除失效论断（invalidated_at≤T，历史时点仍可见；include_invalidated=True 带标记审计）；发布门禁 `invalidated_claim_ref` 硬失败；read_evidence 回读带失效标记 | `knowledge/metric_store.py`、`research/artifacts.py`、`research/context_tools.py` |
| R5 [P2] | prepare 默认取最新快照、commit 默认按无快照算哈希 → 误报基线已变化 | commit 缺省基线与 prepare 对齐（取最新快照），选定快照绑定进提交 payload；S2 schema/hint 引导回传 base_snapshot | `dossier/consolidator.py`、`commands/steps.py` |
| R6 [P2] | 逐条校验逐条提交，第二条非法时第一条已落库 | 先全量校验再 `record_profile_update` 原子落库（失效记录+台账同事务，并发竞争整体回滚按重放），事件在落库后追加 | `dossier/consolidator.py`、`knowledge/metric_store.py` |
| R7 [P2] | 失效预览只查观测的直接引用，漏 observation→calculation→claim 下游 | `refs_to` 泛化（任意 ref + 实体/时间过滤 + 派生观测桶），prepare 沿依赖闭包 BFS（200 条防御上限显式截断） | `knowledge/metric_store.py`、`dossier/consolidator.py` |
| R8 [P2] | EvidencePack 只展开观测背后的原文，claim 引 claim 时核验看不到原始依据 | 血缘递归展开（claim→support_refs、calc→input_refs、obs→evidence_refs+calculation_ref），via/indirect 标记，深度 4/总量 24/循环截断；间接死端进 missing_evidence 不拖垮直接引用 | `research/evidence_pack.py` |
| R9 [P2] | verdict 接受任意字符串，NOT_SUPPORTED 被聚合归为 supported | verdict 枚举约束（大小写/连字符规范化后严格匹配）；非法条目丢弃并计数留痕，全非法按核验不可用诚实降级 | `research/verifier.py` |
| R10 [P2] | PDF 去重哈希只覆盖已解析文本，同前缀不同后续的 PDF 复用错原件 | 版本身份 = 原件 bytes 哈希（解析文本哈希降为信息性指纹）；同原件再登记并入新解析页，不同原件必为新文档 | `gateway/documents.py` |
| R11 [P2] | 完整性按最大已解析页码，中间缺页被掩盖（1、3 页解析即报 full） | `missing_pages` 按实际页集合计算；有失败页 partial、有缺页 truncated；read_document 的 unread_pages 同步修正并附缺页清单 | `gateway/documents.py`、`research/tools.py` |
| R12 [P2] | URL 子串包含 sec.gov 即归发行人披露 | urlparse 取真实 hostname，精确/合法子域匹配；userinfo/查询参数/仿冒域（sec.gov.evil.com）不再误判；媒体源不因此升档 | `research/assessment.py` |

行为契约变化（第三轮，下游须知）：

1. **validated 论断的核验硬失败会真降级**：numeric_checks/analysis_review 失败或引用失效
   时 verify_claim 直接落 draft（此前仅 contradicted/insufficient 降级）；发布门禁同步硬拦。
2. **claims_as_of 默认不含已失效论断**（时态合并：失效前的历史时点仍可见）；需要审计
   视图时传 include_invalidated=True。档案投影/报告校验/导出随之自动生效。
3. **read_evidence 上下文隔离**：跨实体/跨命名空间/晚于上下文截止的引用逐项拒绝
   （此前只拦跨实体 fact）；同实体截止前引用不受影响。
4. **DocumentStore 版本身份改为原件哈希**：同 URL 同解析前缀但原件不同的 PDF 不再
   被误判同版本；completeness_payload 新增 missing_pages，read_document 新增
   unread_page_list。

## 7. 第四轮：P1-C 执行闭环收口 + SearchBroker 输入能力

review 指出「ToolExecutor 仅被测试调用；生产仍单独装配 handler，尚未统一执行、
预算与冻结实际能力集合」。本轮修复：

| 项 | 修复 | 代码落点 |
| --- | --- | --- |
| 编译结果决定执行 | 新增 `RuntimeBinder`：执行面 = 编译声明 ∩ run 装配 handler；bound_undeclared（装配缺陷信号）/ declared_unbound（声明未装配）双清单显式可见不静默 | `plugins/runtime.py`（新） |
| 统一执行纪律 | S1（串行+维度并行组）、S2、S3、行业研究、deep dive 的模型可见工具全部经 ToolExecutor：参数校验（required/顶层类型）、超时（慢工具 verify_claim/文档族放宽上限）、可重试错误有界重试（吃 RunBudget）、错误码封装（不包装成空结果）、`plugins/tool_trace`（plugin_id 归属声明插件，新增 `CompiledSet.tool_owners`） | `commands/steps.py::_bind_runtime_tools`；`research/loop.py`（plugin_set 入参）；`plugins/registry.py` |
| 冻结实际能力集合 | `plugins/runtime_bound` 事件按 run 幂等落库（bound/undeclared/unbound 三清单 + config_hash 锚点），与 `plugins/manifest_frozen`（声明面）配对 | `plugins/runtime.py` |
| 声明完整性补漏 | `propose_candidates`/`submit_card` 此前有 handler/schema 但无插件声明 → 新增 `research.industry` 插件（stages=["industry"]）补齐 | `plugins/builtin.py` |
| 线程池治理 | ToolExecutor 支持注入共享池；生产经 binder 用进程级有界共享池（32 worker），serve 模式不随 run 数泄漏线程 | `plugins/executor.py`、`plugins/runtime.py` |

行为契约变化（第四轮，下游须知）：

1. **工具错误形态**：handler 抛异常/超时时，模型收到的内容从 kernel 的
   `error: 工具 X 执行失败…` 文本变为结构化 envelope（`error_code/message/
   retryable/tool/plugin_id`）；handler 自行返回的 rejected/error 文本不变。
2. **未知工具参数在进 handler 前被拒**（invalid_arguments），handler 零调用。
3. **无 registry 的旧装配/回放路径行为完全不变**（binder 不介入）。
4. 事件新增 `plugins/runtime_bound`（实际执行面）与 `plugins/compile_failed`
   （编译失败可见）；`plugins/tool_trace` 现在有生产流量。

未接线（如实标注）：main_agent / decision loop / evaluation replay 的 kernel
面向不同能力域，不经研究插件编译集；行业漏斗 F1/F3a kernel 的绑定待下轮
（`research.industry` 声明已就位，stage="industry" 编译即可接入）。

### 7.6 SearchBroker（方案 §5.1，P1-A 输入能力；tests/unit/test_search_broker.py 14 例）

- `gateway/search_broker.py`：Exa 主 / Tavily 备双源代理——默认只打主源；
  主源报错/零召回/低召回（<3）回退合并备源（回退原因进 trace）；mode=dual 强制双源；
  canonical URL 去重（www/跟踪参数/尾斜杠/大小写归一）+ 转载族归并（同文不同 URL
  只算一个原始来源，family_size/family_urls 可见）；主源命中保原序，备源新增附加。
- 纪律：每个实际调用的引擎真实扣一次检索预算（双源合并 = 双份成本不账外运行）；
  逐条记录保留各自 source_id/available_at/pit_grade（时间准入仍在 DataGateway）；
  结果逐条落 ChunkStore（chunk_id 可引证据），`_broker` 元数据只在传输层可见、
  不进台账正文（canonical_record_text 排除 `_` 前缀键）；缓存命中不重复扣预算；
  参数/回退/去重统计落 `gateway/search_broker` 事件。
- 装配：S1（串行+并行组）、行业 F1/F2/F3a 步进工具面；`research.web` 插件声明
  `search_sources`（能力 `web.search_broker`）；`maybe_make_search_broker`
  fail-closed（无搜索源注册 → 不装配）。
- 未做（方案后续）：双源合并的召回质量评估（题集驱动）、reranker/向量召回、
  Exa 新参数的 Novita 透传契约验证。

### 7.7 第五轮追加（review 剩余项：来源独立性 + 表格定位轻量路径）

| 项 | 交付 | 代码落点 | 测试 |
| --- | --- | --- | --- |
| P2-A 可靠来源独立性判断 | EvidencePack 按 文档→canonical URL→正文哈希 归组支持证据（`source_independence`：independent_sources/single_source/groups，compact 透出进核验 prompt）；verify_claim 记录 independent_sources（ClaimVerification/VerificationResult/事件三处），supported 但单族 → 显式标注「不构成独立佐证」（可见性纪律，不硬拦——单 filing 是合法事实源）；assessment 披露 `single_source_supported_claims` | `research/evidence_pack.py::compute_source_independence`；`research/verifier.py`；`research/artifacts.py::ClaimVerification`；`research/assessment.py` | `test_source_independence.py`（7 例） |
| P1-A 表格/单元格定位（轻量路径） | `extract_table` 工具：启发式候选表抽取（表头/行/单元格 + 币种/期间候选 + ragged_rows 等校验问题 + page/table/row/col 定位）；只扫已解析页，缺页显式列出；数字不直接成事实（回读原文→证据绑定→typed 准入不变）；Docling 试点落地后退为轻量回退路径 | `gateway/tables.py`（新）；`research/tools.py`；`documents.reader` 插件声明 | `test_tables.py`（7 例） |

review 剩余项中仍需外部条件的（如实未做）：OCR 修复与 Docling/Unstructured 试点对照
（重依赖决策点）、电话会/一致预期/授权披露供应商验收（商务/账号）、P3 效果验收
（24 题/留出集/消融/盲评）、独立复核的人工抽样流程（§5.4 检查 4 的人工部分）。

### 7.9 第七轮追加（B 组对照新发现 F15/F16 整改）

| 发现 | 整改 | 代码落点 | 测试 |
| --- | --- | --- | --- |
| F15 核验覆盖率 3/106（工具在但模型不主动用） | 合成定稿前**服务端批量核验**：报告实际引用的 validated 未核验论断按 kind 优先 + 创建序取 top-8 直接核验（judge 一审 + research 二审）；结果落时态版本链；新发现 contradicted/数值失败被发布门禁硬拦（产物降 draft）；核验不可用诚实记 unavailable 不冒充；剩余未核验在产物校验备注逐条可见（`claim_content_unchecked` 软问题）；批量统计进 `research/artifact_created` 事件 | `commands/steps.py::_batch_verify_referenced_claims`；`research/artifacts.py` | `test_report_dependency_closure.py::TestBatchVerificationAtSynthesize`（3 例） |
| F16 dup_chunks 口径混合（同页重读的去重命中与重复资料输入混为一谈） | 预算快照新增 `unique_chunks`（唯一 chunk 数）与去重命中分列；runner/compare 脚本透出；判读基准 = 唯一内容占比 | `research/loop.py`；`scripts/run_sentinel.py`、`scripts/compare_sentinel.py` | 随既有脚本测试 |

F14（ai4s F1 行业 typed 回归 15→0）需消融对照运行（关闭状态卡/新工具面），
属真实运行实验，不在本轮代码范围。

### 7.10 第八轮追加（P3 基建 + 重依赖/商务前置项）

| 项 | 交付 | 落点 |
| --- | --- | --- |
| P3 题集与留出保护 | 24 题冻结（12 单股/8 行业比较/4 增量刷新链式题；16 iter + 8 holdout；关键任务 repeat=3）；运行器 --tasks-file/--group/链式执行/holdout 预算门（--allow-holdout + HoldoutLedger 扣减） | `evals/sentinel_tasks_24.yaml`；`scripts/run_sentinel.py`；`test_sentinel_tasks.py` +4 例 |
| 消融开关（方案 §10.2 + F14） | 五组件环境变量开关（state_card/verifier/broker/knowledge_context/second_search），仅评估运行用，生产默认空集；接线 S1/S2/合成/网关注册，关闭留痕可归因 | `harness/ablation.py`；loop/steps/cli 接线；`test_ablation.py` 6 例 |
| F14 消融驱动 | ai4s F1 单步双跑（状态卡开/关）对照脚本（结果见 data/sentinel/ablate-f14/） | `scripts/ablate_f14.py` |
| 盲评打包器 | A/C 产物按题匿名 X/Y（种子可复现）+ rubric 卡 + 评审后开封 key.json（600）；两人同 rubric 双评 | `scripts/package_blind_review.py` |
| 供应商验收卡（电话会/一致预期） | FMP/Financial Datasets 验收卡线束（覆盖/字段/PIT/失败码如实进卡）；本机无 key → blocked 卡已生成（data/vendor-cards/），商务补齐后重跑同一线束即可 | `scripts/vendor_acceptance.py`；`test_vendor_acceptance.py` 6 例（离线假 transport） |
| Docling 试点（重依赖决策） | **决定性证据**（乱码中文 PDF garbled→ok；411 页年报分部附注还原 4 表结构；对照组无退化）——决策建议：Docling 按页路由走 OCR/表结构，pypdf 保留轻量路径 | `scripts/pilot_docling.py`；[试点记录](docling-pilot-spike-2026-09-11.md) |
| F14 消融 | 双跑 n=2/变体：r1 on=0/off=5、r2 on=0/off=0——**状态卡挤压假设不成立**（差异不复现）；真实机制=写路径纪律（囤证据少写 + 形态拒写），对策=配方补「写数值的正确形态」 | `scripts/ablate_f14.py`；playbooks/industry_map.md、dimension_researcher.md；结果 data/sentinel/ablate-f14*/ |

未做（如实）：24×3 全矩阵真实运行（按 repeat 需约 40+ 次付费运行，属排期任务而非一轮工作）；
Unstructured 对照半边（Docling 证据对目标失败类已决定性，双装留待新问题类出现时）；
盲评的人工评审本身（打包器就绪，待 C 组完成后打包送评）。

**C 组运行**：已启动（冻结 HEAD `460ecf8`，同 6 题同运行器，`data/sentinel/baseline-C/`）——
完成后与 A/A′/B 对照（compare_sentinel.py），盲评打包走 package_blind_review.py。

### 7.8 第六轮追加（P2-A 二次独立核验 + P2-B 变化解释及重算）

| 项 | 交付 | 代码落点 | 测试 |
| --- | --- | --- | --- |
| P2-A 独立复核（自动化部分，§5.4 检查 4） | verify_claim 二次独立核验：触发=数值型结论一审 supported 或 double_check=true；**必须是不同模型**（同模型/缺省 → 跳过并留痕，不制造复核表象）；两审不一致 → 审慎封顶 partially_supported；second_review 进结果/事件/verified_by | `research/verifier.py`；S1/S2 装配第二核验者（judge 核验、research 复核） | `test_claim_verifier.py::TestSecondIndependentReview`（5 例） |
| P2-B 完整变化解释及重算 | 失效闭包根补上**已更正**观测（corrected_observation_ids，时态一致）；commit 可选 recompute_calculations：依赖计算引用值剥离 + 失效引用重映射替代版本 + 同批 calc→calc 链式重映射后重跑（input_hash 幂等不重复建行）；无替代版本显式拒绝；重算明细 + 残留依赖进 commit 响应与事件（完整变化解释）；闭包遍历收敛为单一 `_dependency_closure`（prepare/重算/变化解释同源） | `dossier/consolidator.py`；`knowledge/metric_store.py`；S2 工具 schema/装配 | `test_profile_consolidator.py::TestRecomputeCalculations`（4 例） |

## 8. 下一步建议（按方案 §11.2 失败分布决策；2026-09-11 B 组完成后更新）

> **B 组对照已跑完（6/6 completed，代码冻结 a54906e，不含第三/四轮整改）**：
> typed 入库覆盖 2/6→5/6、NVDA 一手观测 5/5、2228 提交率 3/12→8/13 且
> verify_claim 真实拦截 2 次数字未定位、ai4s 一次全通（F5 核查型报告）；
> 新发现 F14（ai4s F1 行业 typed 回归 0）/F15（核验覆盖率 3/106）/
> F16（dup_chunks 两极）/F17（runner 信号采集，已修）——详见
> [B 组对照报告](sentinel-baseline-B-comparison-2026-09-11.md)。

1. ~~跑 B 组对照~~ **已完成**；F15/F16 已整改（§7.9），F14 消融排除状态卡嫌疑、
   配方纪律修复已落（§7.10）；
2. ~~Docling 试点~~ **已完成**（决定性证据，决策建议见 spike 记录）；生产化接入
   （parser_mode 路由 + parser extra + 解析器版本进文档元数据）是下一实现轮；
3. source.earnings 验收卡线束已就绪（本机无 key，blocked 卡已生成）；
   商务补齐后重跑 `scripts/vendor_acceptance.py` 即出探针证据；
4. P3：24 题题集/留出预算门/消融开关/盲评打包器已就绪；C 组 6 题运行中；
   全矩阵 24×3 与盲评送审属排期任务；
5. claim_invalidations 的 UI 展示（读侧贯通与发布拦截第三轮已落，仅剩 dossier 页面
   「已失效」区块展示，随下轮页面升级）。
