# Handoff：交互与编排层重新设计（2026-08-29）

> 目的：新会话接手「交互与编排层重新设计」。本文档固化全部必要上下文——
> 读完本文档 + DESIGN.md §0/§1 + docs/evaluation-design.md §0 即可开工，无需翻聊天记录。

---

## 0. 一句话现状

地基（P0–P4）已建完且测试全绿，但**交互层与编排层的架构方向错了**，用户已明确提出重新设计。

## 1. 项目状态

- 仓库：`~/dev/work/finance-agent`，git 干净，HEAD=`981a211`
- 测试：`uv run pytest` → **135 passed, 1 skipped**（网络冒烟默认跳过）
- 运行：`uv run python -m finance_agent serve`（一条命令，自动构建前端+开浏览器）
- LLM 配置：复用 pi 的 `~/.pi/agent/{models.json,auth.json}`（`LLMRouter.from_pi()`），研究主力 `pa/gpt-5.6-sol`（novita），fast 角色 `kimi-k3`（dashscope）

### 已建成且不可推翻的地基（重设计的约束，不是推倒对象）

| 模块 | 位置 | 说明 |
| --- | --- | --- |
| EventStore | `src/finance_agent/eventstore/` | append-only 事件日志 + derive_messages 投影 + subscriber |
| 双时态知识库 | `knowledge/` | event_time/knowledge_time、as_of(T)、overlay、单写者+硬门禁 |
| DataGateway | `gateway/` | PIT 分级 + 评估模式时间锁 + leakage 审计 |
| 决策卡 | `decision/` | DecisionCard + risk-review + kb_snapshot 绑定 |
| 评估回放 | `evaluation/` | walk-forward + LLM-only 基线 + DSR + canary + 反事实 PC/CI/IDS + holdout 预算 |
| 可观测性 | `logging_setup.py` + 失败三通道 | 两条永久工程规矩（README「工程约定」节） |

### 文档地图

- `DESIGN.md` — 总体设计（四环节、架构、UI 章）
- `docs/evaluation-design.md` — 评估对齐稿（D3=milestone、D7=允许增量研究，已确认）
- `docs/best-practices-evaluation.md` — 业界调研证据（Profit Mirage/FinLeak-Bench 等）
- `docs/incidents/2026-08-29-silent-failure-rca.md` — 静默失败事故 RCA（两条永久规矩的来源）

---

## 2. 本次用户反馈（2026-08-29，三个问题）

用户输入「我想深度研究下BE这家公司，是否值得投资」，系统回复「想让我出投资建议的话，请带上标的代码」。

### 问题 1：意图识别死板（能力问题）

直接技术根因：意图路由用正则（`api/intent.py`），`\b[A-Z]{1,5}\b` 在 Python Unicode 模式下 CJK 字符算 word 字符，「下BE这」中 BE 两侧无词边界 → 不匹配。正则路由在第一个真实输入上就死了。

**深层根因（真正要归的因）**：用确定性 pattern 做自然语言理解是模式错误——「BE这家公司」需要 LLM 级别的实体理解（BE = Bloom Energy, NYSE）。修复方向不是补正则，是消灭这层：意图理解交给主 agent（LLM），不再需要 intent router。

### 问题 2：UI 体验差

- 会话列表挤在中间、内容无明显边框/视觉层级
- 本质：按「CRUD 页面」（列表+详情）思维做的，不是按「对话流」思维做的

### 问题 3：与 dsh 交互模型差距大（用户明确要求深度归因「为什么一直没对齐」）

已开始的 dsh 取证（本地克隆：`/tmp/pi-github-repos/deepseek-ai/deepseek-harness`）：
- `docs/architecture.md` 的 turn flow：`turn/start → agent/pre-step → step/start → agent/request → llm/stream → assistant/chunk* → assistant/message → tool/call* → tools/execute → tool/result* → step/end → turn/end`
- Web 端关键组件：`packages/client/ui-conversation/`（conversation 渲染）、`packages/client/ui-chat/src/client/conversation-nodes/`（节点类型）、composer 用 lexical editor（`.agents/notes/implemented/architecture/2026-08-20-web-composer-lexical-editor.md`）
- `apps/web/tests/expected/` 下有大量交互行为的 expected 快照（subagent-activity、streaming、approval 等），是理解交互细节的最佳入口
- **新会话要完成的归因**：dsh 的消息流/工具卡片/子 agent 嵌套/审批内联到底怎么组织；我们的差距清单要落到「组件级对齐表」

### 问题 4（最关键）：架构方向错了——用户原话要点

> 「现在整个设计应该是有非常大的问题，需要重新设计，按道理应该是完全 vibe coding 这种方式进行全部过程，agent loop 更多是提供自动闭环模式，但是并不是完全生搬硬套的实现，如果用户只想进行深度调研的话，也没有必要将 agent loop 完全执行一遍，只需要在其中一个step中根据plugin进行结果执行，这个plugin可能是一个完整agent加上tools/skills/mcp/hooks这些能力」

解读为设计约束：

1. **全对话式交互**：对话是唯一主交互（vibe coding 式），不是表单+按钮
2. **主 agent loop 是通用闭环**：observe→inspect→choose→act，streaming，用户可见过程
3. **领域流程不是固定管线，是可调用的能力（plugin）**：用户只想深度调研 → 只跑 research 子能力，绝不顺带跑决策；deep research 作为**子 agent plugin**（自己的 loop + tools + skills/hooks），由主 agent 在对话中按需调用
4. 现有 S1-S4 从「用户触发的入口」重构为「主 agent 可挂载的领域工具/子流程」

---

## 3. 重设计的建议骨架（待新会话细化对齐）

```text
用户对话（composer，底部固定）
   ↓
主 Agent Loop（通用、streaming、dsh 式 turn/step 渲染）
   │  工具面（全部经 DataGateway/KB，PIT 纪律不变）：
   │    query_kb / query_filings / query_prices / …（轻量只读工具）
   │  能力插件（重活，挂为子流程，各有各的 loop+hooks）：
   │    deep_research(target)      → 现有 ResearchLoop 改造为子 agent
   │    draft_decision(target)     → 现有 DecisionLoop + risk-review
   │    run_evaluation(config)     → 现有 ReplayEngine（高成本 → 审批闸）
   │  事件全部入 EventStore（真相源不变），UI 投影不变
```

- 主 agent 自己决定：闲聊就直接答；提到标的 → 查档案 → 问是否要深度研究；确认 → 调 deep_research 子 agent；子 agent 进度以「子 agent 活动卡」内联展示（参考 dsh `sidebar-subagent-activity`）
- 意图理解交给 LLM（主 agent 的系统 prompt 写明标的识别与确认纪律），正则路由删除
- 审批（milestone 档）内联在对话流中（dsh 的 access-confirmation 样式），不是顶部横幅

## 4. 新会话工作建议

1. 先读完本文档 + DESIGN.md §0/§1/§4 + evaluation-design.md §0
2. 归因作业：精读 dsh 的 `packages/client/ui-conversation` + `apps/web/tests/expected/` + `.agents/notes/implemented/`（交互细节一手资料），产出「组件级对齐差距表」
3. 出重设计方案（主 agent loop + 能力插件 + 对话式 UI），与用户**对齐后再开发**
4. 开发仍守两条永久规矩（失败三通道 / 真实装配测试）+ TDD
5. 交互验收标准（用户视角）：输入「我想深度研究下BE这家公司，是否值得投资」→ agent 识别 BE → 查档案 → 对话确认后启动深度研究子 agent → 过程流式可见 → 完成后给出研究结论摘要，并询问是否需要投资建议——**不自动跑决策**

## 5. 已知遗留小事

- `./data` 里有早期失败 run 的历史记录（live-e5f34fab 等），状态投影已能正确显示「失败+原因」，如需清零可删 `data/`
- ` ApprovalsBanner` 2s 轮询刷日志（噪音），重设计时改为事件驱动或拉长间隔
- 前端无组件级测试（L4 欠账）
