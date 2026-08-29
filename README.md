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

## 参考项目

- [deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) — 插件架构与 Web UI 交互范式参考
- auto-deep-research V1.0 — 调研流程与数据源实现的移植来源

## 开发

```bash
uv sync --extra dev   # 安装依赖（真实数据源 adapter 另需 --extra data）
uv run pytest         # 测试（含 PIT 穿越用例）
uv run ruff check src tests
```

### 当前进度

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
