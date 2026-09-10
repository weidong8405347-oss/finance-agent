# Research / Profile 能力加强：第一轮实施记录（2026-09-10）

> 对应设计方案：[research-profile-tools-plugins-plan-2026-09-09.md](research-profile-tools-plugins-plan-2026-09-09.md)
> 实施范围：方案 §11.2「最值得先做的范围」= **P0 + 统一知识读取 + Document Read v2 + SEC 财务工具 + 哨兵题集脚手架**。
> 分支 `feat/research-profile-capability-upgrade`；基线 `c718a66`（main）。
> 状态口径：**已落地** = 有代码与测试；**未做** = 本轮未实施（如实标注）。
> 本轮没有执行真实付费研究运行、没有购买数据服务、没有跑 ChatGPT Deep Research 同题对照。

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

## 3. 未做（如实清单，按方案阶段）

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
uv run pytest                                        # 812 passed, 9 skipped
uv run ruff check src tests scripts

# 本轮专项回归
uv run pytest tests/unit/test_p0_trust_remediation.py    # P0 可信度与策略
uv run pytest tests/unit/test_context_tools.py           # 统一知识读取 + S2 整合装配
uv run pytest tests/unit/test_document_read_v2.py        # 文档服务/分页/完整性/PIT
uv run pytest tests/unit/test_edgar_facts.py             # XBRL/acceptance/历史分段

# 哨兵题集（脚手架离线自检 → 真实付费运行需 LLM key + 网络）
uv run python scripts/run_sentinel.py --list
uv run python scripts/run_sentinel.py --task be-orders-revenue --dry-run
uv run python scripts/run_sentinel.py --task be-orders-revenue   # 真实运行（隔离数据目录）
```

## 5. 下一步建议（按方案 §11.2 的失败分布决策）

1. 跑 A 组基线：`scripts/run_sentinel.py --all`（先冻结 3 个待选 ticker），
   把 assessment/预算/重复资料信号存成基线 JSON。
2. P2-A 内容级 verifier（EvidencePack + verify_claim）：把 `evidence_support` 从
   unchecked 变成可审计核验意见——这是「论断接受内容级核验」的最后一公里。
3. Docling 试点 + extract_table：在保页码基础上补表头/单元格定位，
   用哨兵题 deep-pdf-late-table 对照 pypdf 路径。
4. P1-C 薄插件层：把本轮收敛的装配点（context_tools/documents/adapters）迁到
   类型化 manifest + registry，能力页从编译结果生成。
