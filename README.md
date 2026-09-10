# finance-agent

股票投资 agent 系统 — 四环节闭环：**Deep Research / Profile 档案 / 投资建议 / 评估**。

## 文档

- [DESIGN.md](DESIGN.md) — 总体设计文档（架构、四大 step、评估反穿越设计、UI、实施计划），**先读这份**
- [docs/knowledge-dossier-research-redesign.md](docs/knowledge-dossier-research-redesign.md) — Knowledge 可视化股票档案与 Research 深度升级方案（页面、数据契约、问题驱动研究、迁移与验收）
- [docs/knowledge-dossier-implementation-status.md](docs/knowledge-dossier-implementation-status.md) — 上述方案的**实施状态对照**（设计章节 → 代码落点，已完成/部分/未做如实标注）
- [docs/best-practices-evaluation.md](docs/best-practices-evaluation.md) — 业界最佳实践调研（LLM 回测污染证据、PIT 数据实践、评估协议、记忆架构），DESIGN.md 第 4.3/6 章的依据
- [docs/evaluation-design.md](docs/evaluation-design.md) — 评估体系对齐稿（过程评估/效果评估两部分隔离 + 插件化，含待确认决策点清单）
- [docs/research-capability-upgrade.md](docs/research-capability-upgrade.md) — 调研能力提升设计（/industry 行业漏斗、维度并行 loop、三 flash 模型分级、四新数据源、stalled 升级阶梯）；P1/P2/P3 已落地
- [model_agent.md](model_agent.md) — 通用 agent 设计框架（本设计的理论输入，含投资领域风险清单）

## 设计要点速览

- **双时态知识库**：每条事实带 `event_time`（何时为真）与 `knowledge_time`（何时可知），append-only，支持 `as_of(T)` 时间回溯投影
- **时间锁数据网关**：评估模式只放行 `available_at ≤ T` 的数据，无 PIT 保证的数据源 fail-closed
- **事件溯源**：append-only 事件日志是唯一真相源，prompt/UI/报告/档案都是投影
- **评估反穿越三层防线**：数据层（时间锁）+ 知识库层（as_of 隔离）+ 模型层（grounding 校验 + canary + LLM-only 基线）
- **权力分离**：研究 agent 无权改评估代码与 holdout 数据；评估结果是硬门禁，完整性不进加权总分

## 快速开始

```bash
uv sync --extra dev --extra data   # 安装依赖
uv run python -m finance_agent serve   # 一条命令：自动构建前端（首次）+ 起服务 + 打开浏览器
```

打开 http://127.0.0.1:8000 —— **command 制对话式交互**（参考 deepseek-harness）：

- **直接说事**：「帮我深度研究一下 BE」「600519 现在可以买吗」——主 agent（LLM）理解意图并自主调用 command；
- **显式 command**（确定性派发，不经模型理解）：
  - `/research <标的> [目标]` — S1 深度研究 + 过程评估
  - `/profile <标的>` — S1 研究 → S2 档案更新 + 过程评估
  - `/decide <标的>` — S1 → S2 → S3 决策卡 + 过程评估
  - `/industry <主题>` — 行业调研漏斗：F1 赛道地图 → F2 标的池（三 worker 并集、每票绑证据）→ F3 粗调研卡 + 人工闸口（可打回迭代）→ F4 深研 fan-out → F5 排序报告
  - `/evaluate <配置名>` — S4 独立效果评估（默认需审批；`--no-approval` 当次豁免）
- 每个 command = step agent 管道：各 step 跑在独立子 run（可钻取），父会话实时显示进度。

交互与编排层的完整设计：[docs/redesign-interaction-orchestration.md](docs/redesign-interaction-orchestration.md)。

**LLM 配置**：默认直接复用 pi 的 provider 配置（`~/.pi/agent/`，研究主力 pa/gpt-5.6-sol；fast 角色 kimi-k3；另有 GLM-5.3 / deepseek-v4-pro-0813 等别名可路由）。无 pi 配置时回落 `.env` 三件套（见 [.env.example](.env.example)）。LLM 读超时默认 180s，可用 `FINANCE_AGENT_LLM_TIMEOUT`（环境变量或 .env）覆盖。

**代理固化**：本机网络受限时，在 `.env` 写 `HTTPS_PROXY=http://127.0.0.1:7897`（可加 `HTTP_PROXY`）即可——`serve` 启动时自动注入进程环境；显式设置的环境变量优先，不会被 .env 覆盖。代码不探测系统代理（环境显式原则）。

**数据源 key**（`.env` 或环境变量；缺 key 的源 fail-closed 不注册，启动时打印提示）：web 语义搜索 `query_web_search` 默认走 **Novita 网关的 Exa passthrough**（`NOVITA_API_KEY`，Bearer 认证）；只配了 `EXA_API_KEY` 则回退直连 `api.exa.ai`（`x-api-key`）——两通道请求/响应同构，PIT 语义（`publishedDate` → `available_at`，无日期条目降级）完全一致。备份源 `TAVILY_API_KEY`（C 级，评估模式禁用）。

前端开发热更新模式才需要第二个进程：`cd frontend && npm run dev`（:5173，代理 /api 到 :8000）。

## 参考项目

- [deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) — 插件架构与 Web UI 交互范式参考
- auto-deep-research V1.0 — 调研流程与数据源实现的移植来源

## 开发

```bash
uv sync --extra dev   # 安装依赖（真实数据源 adapter 另需 --extra data）
uv run pytest         # 测试（含 PIT 穿越用例）
uv run ruff check src tests
```

`serve` 参数：`--no-open`（不开浏览器）/ `--no-build`（不自动构建前端）/ `--host/--port/--data-dir`。

### 工程约定（CI 强制，源自 2026-08-29 静默失败事故 [RCA](docs/incidents/2026-08-29-silent-failure-rca.md)）

1. **失败可见性三通道**：任何 fail-closed/错误路径的测试必须同时断言——事件落库 + 日志输出 + 用户可见状态（API 状态投影/UI）。缺一即测试失败。错误处理不允许「静默 return」。
2. **真实装配测试**：每条用户可达路径（CLI 命令、POST 端点）至少一个测试不注入假 runner；依赖注入只允许在系统边界（LLM provider、网络数据源）。

测试分层：unit（组件契约）→ integration（P0 全链路/冷启动 E2E）→ L1 失败可见性 → L2 CLI 真实子进程。

### 当前进度

**Research/Profile 能力加强第一轮已落地（分支 `feat/research-profile-capability-upgrade`，方案见 [tools-plugins 计划](docs/research-profile-tools-plugins-plan-2026-09-09.md)，对照见 [实施记录](docs/research-profile-tools-implementation-2026-09-10.md)）**

| 项 | 内容 |
| --- | --- |
| P0 可信度 | 冲突真裁决（获胜版本保存+晋升，不再只清标记）；claim 核验状态拆分（validated 仅=引用校验，`verification.evidence_support` 诚实标 unchecked）；一手来源与 PIT 分档统计；plan 模式 worker 不再被「逐字段写入」纪律压回填字段；rubric 评审收到论断原文+支持摘录 |
| 统一知识读取 | `research/context_tools.py`：get_research_context / query_observations·claims·calculations（过滤+游标，截断不静默）/ read_evidence（批量逐项成败）/ list_conflicts / adjudicate_conflict（typed 语义键裁决落事件）——S1/S2/合成/委员会共用同一契约 |
| S2 整合第一步 | profile_update 读冻结基线+本轮产出→裁决冲突→thesis 同时落 Fact（兼容）+带证据/limitations 的分析 Claim（dossier 总论新读侧） |
| Document Read v2 | run 级文档库（内容哈希去重，同财报只抓解一次）；PDF 保页码+惰性续解（**80 页后表格可达**）；fetch_document/read_document/search_document + 目录/完整性（full·partial·truncated·failed）透明；窗口 chunk 带 document/page locator；抓取失败≠未披露 |
| SEC 结构化披露 | `query_edgar_facts`（companyfacts XBRL：原始 tag/unit/期间/accn+原文链接，A 级 acceptance 时刻）；公开时刻 acceptance 优先、日精度保守取 UTC 日末；历史分段遍历；≤10 req/s 节流 |
| 哨兵题集脚手架 | `evals/sentinel_tasks.yaml`（6 题冻结）+ `scripts/run_sentinel.py`（隔离运行/信号采集）；真实对照运行待执行 |

验收：812 passed + ruff 通过（本轮新增 60+ 例：P0 整改/共享工具/文档分页/多页 PDF 惰性续解/XBRL 离线夹具/题集契约）。

**Knowledge 档案升级已落地（M0/M1/M2 + M3/M4 部分，已合入 `main`，截至 `d1b83ee`）**

| 层 | 内容 |
| --- | --- |
| typed 数据契约 | `knowledge/metrics.py` 判别联合观测（reported/calculated/guidance/consensus/model_estimate 各自强制来源义务）+ `normalization.py` 「保留原文+显式转换」可重算血缘 + `metric_store.py` 语义键版本链/时态化裁决 |
| 问题驱动研究 | `research/plan.py` 冻结 ResearchPlan（四模式预算，`/research BE --depth=deep --focus=…`）；终止由问题覆盖+字段覆盖+预算共同决定（满档案遇新问题仍研究）；`assessment.py` 硬门禁代码运行（引用可解析/数字可重算/覆盖达标才 sufficient） |
| 受控计算 | `research/calculations.py` 公式注册表（input_refs 可重算、Decimal、N/M 语义、Reverse DCF FCFF 反向求解+敏感性）；旧 calc 工具保留 |
| 报告产物 | `research/artifacts.py` ReportDocument 固定 block + `{{metric:id}}` 插值 + 验证后冻结；report.md/档案概览/Sessions 摘要同源渲染 |
| Dossier 读模型 | `dossier/` 投影器/服务/导出：冻结快照（data_hash 幂等发布）、十模块状态机（7 态降级不伪装 ready）、as_of 历史一致（不借未来裁决/资料）、JSON+MD 冻结导出 |
| `/api/v2` | entities/dossier/modules/evidence/series/compare/artifacts/changes/补研（幂等）/估值预览+情景保存/导出 jobs；越快照引用拒绝、上下文不匹配 409 |
| 前端 | hash 路由深链（实体/章节/时间/快照/证据）；KnowledgePage 研究档案列表；StockDossierPage 三层阅读（章节侧栏/时光机/来源抽屉/ECharts 按需十进制安全绘图+数据表回退）；ResearchReportPage；ComparePage；估值假设实验面板（debounce+assumption_hash，预览不写事实） |
| 迁移 | `scripts/migrate_dossier.py`：inventory dry-run / shadow 对账（27/27 生产实体）/ apply-typed 保守映射（确定不了单位期间就标 needs_normalization，不批量猜） |

验收：后端 454 passed（含 135 个新用例，覆盖 §13.1 九个测试组）+ 前端 37 passed；E2E：`/research --depth=targeted` → 计划→typed 产出→评估→validated 产物→快照发布→前后 diff 全链路集成测试。状态对照与已知边界：[实施状态文档](docs/knowledge-dossier-implementation-status.md)。

**加固与联调已完成（P4 之后）**

| 项 | 内容 |
| --- | --- |
| 真实数据联调 | EDGAR adapter 真实 API 冒烟通过（ticker→CIK 解析、filingDate PIT 过滤验证）；LLMRouter 支持 .env 加载 + 工具 schema 注入；CLI 真实模式注册数据源 adapter |
| research-rubric | LLM-as-judge 软反馈（D4：advisory，解析失败无害）：评分落 `research/rubric` 事件，gaps 进下一轮 brief |
| 反事实探针接入回放 | `counterfactual=True` 的评估对每个出卡决策点扰动 thesis 字段重放决策，PC/CI/IDS 进 EvalReport |
| SSE + 审批 | `GET /api/sessions/{id}/stream` 实时事件流；`ApprovalService` + `/api/approvals/*` + 前端审批横幅（D3 milestone 档落地）；Sessions 页可发起研究 |

**UI 骨架已完成（参考 deepseek-harness 范式）**

| 层 | 内容 |
| --- | --- |
| `api/app.py` | FastAPI 只读投影：Sessions（run 列表 + 事件时间线）/ Knowledge（实体 + as_of 时光机 + 证据回指）/ Decisions / Evaluations |
| `frontend/` | React+Vite+Tailwind 四页：Sessions 时间线（事件卡片可按类型高亮）· Knowledge 时光机（datetime 选择器切换历史投影）· Decisions 决策卡 · Evaluations 判定表 |
| `cli.py serve` | `python -m finance_agent serve` 启动 API（前端 `npm run dev` 代理 /api） |

**P4 硬化已完成**

| 模块 | 内容 |
| --- | --- |
| `evaluation/canary.py` | 合成诱饵：带唯一 token 的假「未来事实」，决策引用即整批 contaminated |
| `evaluation/counterfactual.py` | 反事实扰动探针 PC/CI/IDS（决策不随证据变化 = 背答案） |
| `evaluation/holdout.py` | holdout 预算账本（fail-closed）+ 报告脱敏为仅聚合（`EvalReport.redacted()`） |
| `harness/protected.py` | protected paths：评估代码/配置/holdout 数据对 agent 写禁止 |
| `evaluation/replay.py` | 断点恢复（checkpoint.json 原子写，同 eval_run_id 续跑） |

验收（DESIGN.md §9 P4）：canary 命中/豁免用例、预算耗尽拒绝、protected path 写拦截、中断续跑后 4 点报告完整——均可复现。

**P3 评估闭环已完成**

| 模块 | 内容 |
| --- | --- |
| `evaluation/replay.py` | ReplayEngine walk-forward 时点回放：增量研究（eval 命名空间）→ 决策 → 对照 → 对账 |
| `knowledge/store.py` | `as_of_overlay`：eval 有效档案 = 生产 as_of(T) + eval 增量（字段级竞合，反向隔离不变） |
| `evaluation/prices.py` | PriceBook 前瞻收益对账（保守成交口径；评估器专用，不进入 agent 工具面） |
| `evaluation/costs.py` | 强制成本模型（佣金 + 滑点双边，参数进 config 哈希） |
| `evaluation/stats.py` | Sharpe / MDD / PSR / **DSR**（多重检验校正，标准库实现） |
| `evaluation/report.py` | EvalReport：verdict 硬门禁（leakage>0 即 contaminated 整批作废）+ 截止日分区 + 聚合指标 |
| `evaluation/config.py` | 评估配置即声明式 mandate（冻结 + 内容哈希，示例见 `evals/mandates/example.json`） |

验收（DESIGN.md §9 P3）：4 决策点 walk-forward 全链路与手算一致；截止日污染区/诚实区正确分区；LLM-only 对照与 B&H 基线配对；注入未来记录 → verdict=contaminated；报告 + 配置哈希落盘。

**P2 决策闭环已完成**

| 模块 | 内容 |
| --- | --- |
| `decision/card.py` | DecisionCard（不可变）：action/conviction/horizon/rationale（仅证据 id）/失效条件/仓位/kb_snapshot_id |
| `decision/risk_review.py` | risk-review 硬门禁：仓位上限、证据链完整、thesis 字段须真实存在于档案、eval 模式 created_at==T 且证据不越界 |
| `decision/service.py` | 出卡唯一入口：review 违规即拒 + hook/verdict；**kb_snapshot_id 与当刻档案投影不符 → 拒** |
| `decision/store.py` | 决策卡 append-only 存储（namespace 隔离） |
| `decision/loop.py` | S3 单 turn：query_kb（as_of 决策时刻）→ propose_decision → 出卡/不出卡 |

验收（DESIGN.md §9 P2）：出卡被 hook 拦截的用例可复现（仓位超限/幽灵证据/快照不符/越界证据）；卡片证据链可逐条回指（rationale → evidence → 原文摘录）。

**P1 研究闭环（生产模式）已完成**

| 模块 | 内容 |
| --- | --- |
| `research/loop.py` | ResearchLoop：gap 分析 → LLM turn（工具）→ 过程评估 → IterationReport；收敛/停滞/预算三种终止 |
| `knowledge/schema.py + gaps.py` | 实体档案 schema（stock/industry 必填字段 + 新鲜度策略）+ GapAnalyzer（缺口驱动迭代） |
| `knowledge/guard.py` | numeric-guard：数值必须与证据原文逐字一致（writer 内硬门禁，含 knowledge_time 不变量） |
| `research/tools.py` | register_evidence / propose_fact / query_kb；**knowledge_time 由证据推导，不信任模型自报** |
| `llm/router.py` | LLMRouter 多 provider（env 三件套，角色路由）+ OpenAI 兼容客户端（transport 可注入） |
| `cli.py` | `python -m finance_agent research --ticker AAPL [--mock]` |

验收（DESIGN.md §9 P1）：脚本化 3 轮迭代完整度 0→0.4→0.8→1.0 单调提升收敛；全部事实绑证据；硬门禁拒绝不落库且循环继续（`test_research_loop.py`）。真实标的跑通：配置 provider 三件套后去掉 `--mock`。

**P0 地基**

| 模块 | 内容 |
| --- | --- |

| `eventstore/` | append-only 事件日志（SQLite）+ `derive_messages()` 投影（模型可见=已记录） |
| `knowledge/` | 双时态 facts（event_time/knowledge_time）+ 证据表 + as_of(T) 投影 + 命名空间隔离 + ProfileWriter 单写者（eval 模式拒写越界证据） |
| `gateway/` | DataGateway 时间锁（PIT 分级 fail-closed，评估模式双重过滤 + leakage/attempt 审计）+ EDGAR/行情 A 级源骨架 |
| `loop/` | 最小 agent kernel（turn/step 事件序）+ LeakageAuditHook（必达） |
| `harness/` | RunManifest（模式冻结 + backbone cutoff 分区） |

P0 验收：事件回放可重建任意 run 的模型上下文（不变量测试）；as_of(T) 正确性含 reporting-lag / restatement / 命名空间隔离 / 网关时间锁 / hook 拦截等穿越用例。
