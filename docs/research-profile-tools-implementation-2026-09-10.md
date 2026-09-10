# Research / Profile 能力加强：实施记录（2026-09-10，两轮）

> 对应设计方案：[research-profile-tools-plugins-plan-2026-09-09.md](research-profile-tools-plugins-plan-2026-09-09.md)
> 第一轮范围：方案 §11.2「最值得先做的范围」= **P0 + 统一知识读取 + Document Read v2 + SEC 财务工具 + 哨兵题集脚手架**。
> 第二轮范围：哨兵基线 A/A' 真实运行 + 基线发现整改（R1–R8、F1–F13）+ **P1-C 薄插件层 + P2-A 证据核验与研究路径 + P2-B profile.consolidator**。
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
uv run pytest                                        # 939 passed, 9 skipped（第二轮后）
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
| SearchBroker（Exa 主 + Tavily 备、转载族去重）、Exa 新参数 Novita 透传验证 | 未做（文档层内容哈希去重已落） |
| Docling/OCR 结构解析试点、extract_table（表头/单元格定位） | 未做——基线实证 2228 题 4/8 中文 PDF garbled 是当前最大原文可得性缺口，B 组首选 |
| engine.deep_research 外部研究引擎试点、MCP bridge | 未做（方案第三批） |
| 重要结论第二次独立提取/抽样人工核对（§5.4 检查 4） | 未做（属 P3 评估/人工流程） |
| 24 题扩展、消融、盲评（P3） | 未做（6 题哨兵 + A/A' 对照已跑通，扩展依失败分布决策） |
| 定时监控/事件驱动刷新（§9.3 后续产品能力） | 未做（第一期用户触发 refresh，按计划） |

## 6. 下一步建议（按方案 §11.2 失败分布决策，第二轮后更新）

1. 跑 B 组对照：在当前代码（F10–F13 + P1-C + P2-A/B 全部落地）上重跑哨兵 6 题，
   与基线 A/A' 对照（重点：2228 提交率与量表、BE typed 观测稳定性、ai4s F1–F5 全链、
   verify_claim 核验覆盖率、duplicate_chunks 下降）。
2. Docling 试点 + extract_table：基线实证中文 PDF garbled（2228 题 4/8 文档）是
   原文可得性最大缺口；保页码基础上补表头/单元格定位。
3. source.earnings 验收卡：发行人 IR + FMP 小样本对照（方案 §7.2），解锁 guidance
   兑现时间线（NVDA 哨兵题的完整形态）。
4. P3：24 题扩展与盲评（C vs A 胜率）、插件消融（关 verifier/关知识复用/关第二搜索源）。
5. 投影层消费 claim_invalidations 的 UI 展示（随下轮 dossier 页面升级）。
