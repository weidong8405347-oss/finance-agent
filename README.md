# finance-agent

股票投资 agent 系统 — 四环节闭环：**Deep Research / Profile 档案 / 投资建议 / 评估**。

## 文档

- [DESIGN.md](DESIGN.md) — 总体设计文档（架构、四大 step、评估反穿越设计、UI、实施计划），**先读这份**
- [docs/best-practices-evaluation.md](docs/best-practices-evaluation.md) — 业界最佳实践调研（LLM 回测污染证据、PIT 数据实践、评估协议、记忆架构），DESIGN.md 第 4.3/6 章的依据
- [docs/evaluation-design.md](docs/evaluation-design.md) — 评估体系对齐稿（过程评估/效果评估两部分隔离 + 插件化，含待确认决策点清单）
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
# 配置 .env：OPENAI/ANTHROPIC/ZHIPUAI/DEEPSEEK 任选 provider 的三件套（API_KEY/BASE_URL/MODEL）
uv run python -m finance_agent serve   # 一条命令：自动构建前端（首次）+ 起服务 + 打开浏览器
```

打开 http://127.0.0.1:8000 —— Sessions 发起研究 / Knowledge 档案时光机 / Decisions 决策卡 / Evaluations 评估报告。

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
