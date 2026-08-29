# 交互与编排层重设计（归因 + 对齐稿）

> 来源：docs/handoff/2026-08-29-redesign-handoff.md 的归因作业与方案细化。
> 状态：**待用户对齐**（第 4 章决策点确认后才动工）。
> 约束：地基（EventStore / 双时态 KB / DataGateway / 决策卡 / 评估回放 / 可观测性）不动；
> 本稿只重构「用户 → 对话 → 主 agent → 能力插件」这一层。

---

## 1. 归因：为什么一直没对齐 dsh

### 1.1 一句话归因

**我们借了 dsh 的外形（输入框 + 消息列表），没有借 dsh 的机制（事件流 → 装配 → 投影）。**
三个用户-visible 问题（意图死板、UI 像 CRUD、交互模型差距大）是同一根因的三个投影：
我们把系统做成了「**命令分发器 + 任务日志查看器**」，而 dsh 是「**对话驱动的主 agent + 事件投影 UI**」。

### 1.2 逐层归因

| 层 | 我们做了什么 | dsh 做了什么 | 根因 |
| --- | --- | --- | --- |
| 意图理解 | `api/intent.py` 正则路由：消息 → intent → 直接起 research/decision 线程 | 没有这一层。理解是主 agent（LLM）的职责，system prompt 写纪律 | **用确定性 pattern 做 NLU 是模式错误**。正则 bug（CJK 词边界）只是表象；就算修好 BE，下一个「帮我看看那家做固态电池的上市公司」照样死。理解不可枚举，必须交给模型 |
| 交互单元 | 一次后端任务 = 一个 session（run_id 即 research run）；「对话」不存在，只有任务日志 | 对话 = session；任务是对话内部的 turn / 工具调用 / 子 agent | 交互单元错了：UI 组织围绕「任务」，所以长成 CRUD（列表 + 详情）；dsh 围绕「对话流」，任务是流里的节点 |
| 编排 | `/api/chat` 是 if-else 分发器，主 agent 不存在；ResearchLoop/DecisionLoop 是用户直达的入口 | 主 agent loop 常驻；一切能力（含 bash、subagent）是 loop 挂载的 tool/seam | 把「编排」理解成「把用户输入路由到固定管线」，而不是「主 agent 在对话中按需调用能力」。S1-S4 被做成了用户触发的入口，而不是 agent 可挂载的插件 |
| UI 渲染 | 按事件类型 if-else 平铺；工具调用直接 HIDDEN；无折叠无配对 | ui-conversation 装配层：Definition（match/update/fold）+ Turn/Step Location + 目标快照；tool 卡按 call_id 配对、递归嵌套；turn 结束后过程折叠 | 没有「投影」这一层：前端直接渲染原始事件流，等于把日志刷到屏幕上 |
| 审批 | 顶部 banner + 2s 轮询（带外通道） | `approval/asked`/`approval/decided` 审计事件 + answerer waterfall + **composer takeover**（审批是对话内联交互，attach 到已流式展示的 tool call） | 审批被当成「系统通知」而不是「对话中的一个待答问题」 |
| 流式 | LLM.complete 一次性返回；SSE 只是事件轮询推送 | `assistant/chunk*` 落日志（保 replay/UI 保真），UI 增量渲染 | 事件词汇缺 chunk 粒度；LLM 抽象无 stream |

### 1.3 深层教训（写进工程纪律）

1. **理解归模型，分发归代码的边界**：凡「理解自然语言」的环节一律进 agent prompt；代码只做确定性执行。新增任何「分类/识别」代码前问：这是不是在做 NLU？
2. **借范式要借机制**：UI 参考一个系统时，对齐的是它的事件模型与投影层，不是页面截图。
3. **对话是唯一主交互**：任何用户可达的动作都必须能在对话流中发起、观察、确认；带外通道（banner/独立表单）默认是错的。

---

## 2. 组件级对齐差距表（dsh → 本项目）

| # | dsh 机制（一手出处） | 本项目现状 | 重设计动作 |
| --- | --- | --- | --- |
| 1 | turn flow：`turn/start → agent/pre-step → step/start → agent/request → llm/stream → assistant/chunk* → assistant/message → tool/call* → tools/execute → tool/result* → step/end → turn/end`（docs/architecture.md） | `AgentKernel` 已有 turn/step/tool 事件序；缺 chunk、缺 inbox/claim 语义 | kernel 增加 streaming chunk 发射；chat 入口改为「消息落库 → turn 认领」 |
| 2 | 「模型可见 = 已记录」runtime invariant；deriveMessages 从日志投影 | 已有 `derive_messages` + 白名单 | 不变（地基）。chunk 类事件**不入**模型可见白名单 |
| 3 | ui-conversation 装配层：Definition（match/update/fold）+ Turn/Step Location + target 快照（docs/subsystems/conversation.md） | 前端按 type if-else 平铺 | 前端加事件→节点装配层（TS 版 Definition/Location 概念，大幅简化版） |
| 4 | tool-call 节点：call/result 按 callId 配对，递归嵌套 subCalls（ui-chat/conversation-nodes/tool.ts） | `tool/call` 被 HIDDEN；`tool/result` 是通用 debug 卡 | ToolCard 组件：配对折叠卡；插件调用嵌套子 agent 活动 |
| 5 | turn-process folding：turn/end 后过程行折叠为「N tool calls · M messages」控件，Compact 默认（ui-chat README） | 无 | TurnProcess 折叠控件（turn/end 触发，默认折叠） |
| 6 | subagent seam：model-facing `subagent` 工具，子 agent 独立 session，多 provider 共存（docs/subsystems/subagent.md） | 无 subagent 概念；ResearchLoop/DecisionLoop 是 API 直达命令 | **能力插件 = 子 agent**：`deep_research`/`draft_decision`/`run_evaluation` 作为主 agent 的工具，各自独立 child run（自己的 kernel + tools + hooks） |
| 7 | 子 agent 活动呈现：父会话 tool 卡 + header 血统目录 + sidebar「1 subagent running」（ui-subagent README、expected/sidebar-subagent-activity） | 无 | PluginActivityCard：父流 tool 卡内联展示子 run 实时进度（轮次/完整度/成本），可展开钻取子事件流 |
| 8 | 审批：`approval/asked`·`approval/decided` 审计事件、answerer waterfall、**composer takeover 内联**、fail-closed `unavailable`（docs/subsystems/approval.md、ui-approval） | ApprovalsBanner 顶部横幅 + 2s 轮询 | approval 事件入 EventStore；对话流内联审批卡（SSE 驱动，删轮询 banner） |
| 9 | composer：常驻底部、乐观发送 echo、busy 时 Stop/Queue 双态、排队（ui-conversation README） | textarea + 发送按钮；发送即阻塞 | composer 双态（Send ⇄ Stop）、运行中可排队（落库即入队） |
| 10 | session 侧栏：workspace 树、子 agent 会话**不进**普通侧栏 | 左 1/3 栏平铺 run_id 列表 | 侧栏收窄可折叠（240px）、会话标题（首条用户消息截断）、子 run 不进列表 |
| 11 | 意图理解：主 agent system prompt 纪律 | 正则 intent router | **删 `api/intent.py`**；识别/确认纪律写进主 agent 契约（§3.3） |
| 12 | turn token usage 行（完成 turn 展示精确用量） | 无 | LLMRouter 回传 usage → 随 turn/end 落库 → 折叠控件显示 |

---

## 3. 重设计方案

### 3.1 目标架构

```text
用户 → composer（底部常驻，Send/Stop 双态，可排队）
  ↓ POST /api/chat {session_id?, message}
SessionService：找/建 session（run_id）→ user/message 事件落库 → 认领一个 turn
  ↓
MainAgent（AgentKernel + 主 agent 契约 + 工具面 + hooks）
  │  轻量只读工具（经 DataGateway/KB，PIT 纪律不变）：
  │    query_kb / query_edgar / query_prices / …
  │  能力插件（重活 = 子 agent，各有各的 kernel+tools+hooks）：
  │    deep_research(target, objective)   → 现 ResearchLoop 改造
  │    draft_decision(target)             → 现 DecisionLoop + risk-review
  │    run_evaluation(config)             → 现 ReplayEngine（必过审批闸）
  │    ├─ 子 run：独立 run_id（parent=父 run, call_id 关联）
  │    ├─ 父流：tool/call（带 child_run_id）→ plugin/progress* → tool/result（摘要）
  │    └─ 子流：子 run 全部事件（UI 钻取）
  ↓
全部事件入 EventStore（真相源不变）→ SSE → UI 投影
```

关键形态决策：

- **主 agent 逻辑常驻、实现无状态**：上下文永远从 `derive_messages` 投影重建（「模型可见=已记录」铁律不变）。「常驻」体现在 inbox 语义——user/message 落库即入队，per-run 串行认领 turn；turn 进行中到达的消息排队为下一 turn 输入（steer 后续再做）。
- **插件 = 子 agent run**：复用现有 ResearchLoop/DecisionLoop（它们本就接受 run_id + events + manifest），包装为 `PluginTool`：创建 child manifest（`parent_run_id`、`correlation_id=call_id`）→ 跑子 loop → tool/result 返回结构化摘要。子 run 事件不进父会话模型上下文（只进摘要），防上下文爆炸。
- **插件进度桥接**：子 loop 的轮次级摘要（round_end / card_issued / 错误）桥接为父流 `plugin/progress` 事件（轻量聚合，关联 call_id），父流不打开子流也能渲染活动卡进度；细节钻取走子流。
- **eval 模式不变**：run_evaluation 插件创建 eval manifest（时间锁、命名空间隔离全在地基里），主 agent 本身永远 live 模式。

### 3.2 事件词汇扩展（入 EventStore；标注模型可见性）

| 事件 | 载荷要点 | 模型可见 |
| --- | --- | --- |
| `assistant/chunk` | `{text}` 流式增量 | 否（只服务 UI 保真/replay） |
| `plugin/progress` | `{call_id, child_run_id, kind, summary}`（如「第 2 轮研究完成 · 完整度 61%」） | 否 |
| `approval/asked` | `{approval_id, call_id?, op, detail, reason}` | 否 |
| `approval/decided` | `{approval_id, approved}` | 否 |
| `session/title` | `{title}`（首条用户消息截断生成） | 否 |

`tool/call` 载荷扩展：`{call_id, name, arguments, child_run_id?}`（插件调用时带子 run 指针）。

### 3.3 主 agent 契约（system prompt 骨架，替代 intent router）

```text
你是 finance-agent 的主 agent，一个投研对话伙伴。纪律：
1. 标的识别：用户提到公司（中文名/英文名/代码/别名/描述）时，先确定标的代码。
   能确定就直接说明（如「BE = Bloom Energy，NYSE」）；不能确定就追问，绝不猜。
2. 按需调用，绝不套餐：用户只想了解/研究 → 只调 deep_research；
   只有用户明确要投资建议时才调 draft_decision；研究完成 ≠ 自动出决策。
3. 先查档案：调 deep_research 前先 query_kb 看现有档案完整度，
   已有近期档案时告知用户并询问是增量研究还是重新研究。
4. draft_decision 前确认档案足够；不足则建议先研究。
5. run_evaluation 高成本：永远先说明成本并经审批。
6. 一切事实性断言引用证据 id；没有证据就说「我不知道」。
7. 长任务启动前，用一句话告知用户接下来会发生什么、大约多久。
```

### 3.4 API 变化

| 端点 | 变化 |
| --- | --- |
| `POST /api/chat` | 语义改为「投递消息到 session 主 agent inbox」。无 session_id → 创建 run + 落首消息；有 → 落消息。turn 认领由后台 worker 串行执行 |
| `POST /api/research` | **删除**（统一走 chat）；CLI `research` 子命令保留作 headless 调试 |
| `api/intent.py` | **删除** |
| `POST /api/approvals/{id}` | 保留；改为事件驱动（asked/decided 入 EventStore，SSE 推送） |
| `GET /api/approvals/pending` | 保留（刷新恢复用），但 UI 不再轮询 |
| `GET /api/sessions` | 投影加 `title`、`last_active`；排除子 run（`parent_run_id IS NULL`） |
| 新增 `GET /api/sessions/{run_id}/children` | 子 run 目录（钻取用） |

### 3.5 UI 重构（对话流为唯一主交互）

```text
┌────────────────────────────────────────────────────────────┐
│ header：finance-agent │ EVAL badge(条件) │ 导航(小)         │
├──────────┬─────────────────────────────────────────────────┤
│ 侧栏      │  对话流（max-w-3xl 居中，单列）                 │
│ 240px    │   user 气泡                                     │
│ 可折叠    │   assistant markdown 消息（streaming 增量）      │
│ 会话标题  │   ToolCard（call/result 配对折叠卡）             │
│ 状态+时间 │   PluginActivityCard（子 agent 实时进度，可钻取） │
│          │   ApprovalCard（内联：说明 + 允许/拒绝）          │
│          │   TurnProcess 折叠控件（turn 结束后默认折叠）     │
│          │   ErrorCard（失败三通道的用户可见通道）           │
├──────────┴─────────────────────────────────────────────────┤
│ composer（常驻底部：Send ⇄ Stop，运行中可排队）             │
└────────────────────────────────────────────────────────────┘
```

- 前端也加一层**装配**（简化版 dsh Definition）：事件流 → 节点列表（user/assistant/tool-pair/plugin/approval/turn-fold/error），渲染器只消费节点不消费原始事件。这是「UI 是投影」在前端的落地，也是后续加节点的扩展点。
- Knowledge/Decisions/Evaluations 保留为钻取页（从证据 chips、决策卡、评估事件跳转），不是并列的 CRUD 入口。
- `ApprovalsBanner` 删除（含 2s 轮询）。

### 3.6 不变量（重设计不得破坏）

1. 模型可见 = 已记录；derive_messages 白名单不变（chunk/progress/approval 不可见）。
2. 一切数据采集经 DataGateway；插件子 run 的 gateway 用子 manifest（eval 锁随 manifest 传递）。
3. 知识库单写者：deep_research 子 run 内的 ProfileWriter 纪律不变。
4. risk-review 必达：draft_decision 插件内部纪律不变。
5. 失败三通道 + 真实装配测试（两条永久规矩）：插件子 run 失败 → 子流 error 事件 + 父流 tool/result 带错误 + 日志。
6. 审批 fail-closed：超时/无应答 = rejected。

---

## 4. 待确认决策点（对齐后再动工）

| # | 决策点 | 建议 | 备选 |
| --- | --- | --- | --- |
| D1 | 主 agent 上下文模型 | 无状态重建：每 turn 从 EventStore 投影（铁律自然成立）；per-run 串行锁 + 消息排队 | 常驻对象持内存上下文（省投影成本，多一条上下文通道，违背铁律精神） |
| D2 | `assistant/chunk` 是否落库 | **落库**（dsh 同款：保 replay/UI 保真；SQLite 写入放大可接受；可定期清理老 run 的 chunk） | 只走 SSE 不落库（省存储，但 live 与 reload 后渲染不一致） |
| D3 | 插件进度呈现 | 父流 `plugin/progress` 轻量桥接 + 子流钻取 | 只靠子流（父流活动卡无实时进度） |
| D4 | 插件调用确认粒度 | deep_research：主 agent 口头确认即调（milestone 档精神）；draft_decision：口头确认；run_evaluation：强制审批卡 | 全部强制审批卡（打断感强） |
| D5 | 会话标题 | 首条用户消息截断 30 字 | LLM 生成（dsh 做法，多一次调用，后置） |
| D6 | 分期 | R1 编排层（后端+验收脚本）→ R2 对话流 UI → R3 streaming+成本仪表 | R1+R2 合并（风险：一次改动面太大） |

## 5. 分期与验收（TDD）

### R1 编排层重设计（后端）

- 删 `intent.py` + `/api/research`；`/api/chat` 改 inbox 语义；MainAgent 装配（契约 + 工具面 + 插件）；
  `PluginTool` 协议 + 三个插件包装；approval 事件化；事件词汇扩展；子 run 目录端点。
- 验收（真实装配 + MockLLM 脚本化，覆盖 handoff §4.5）：
  1. 「我想深度研究下BE这家公司，是否值得投资」→ 主 agent 识别 BE=Bloom Energy 并确认 → 用户「确认」→ deep_research 子 run 启动 → 完成后主 agent 摘要 + **询问是否需要投资建议，不自动出卡**。
  2. 闲聊（无标的）→ 直接答，不调任何插件。
  3. 「600519 现在可以买吗」且档案不足 → 建议先研究，不强行出卡。
  4. deep_research 子 run 失败 → 三通道可见（子流 error 事件 + 父流 tool/result 错误 + 日志）。
  5. run_evaluation → 必出 approval/asked；拒绝 → 不执行；超时 → rejected。
  6. 不变量回归：`uv run pytest` 全绿（地基测试不得改动语义）。

### R2 对话流 UI 重构

- 前端装配层（事件→节点）+ 六类节点渲染 + composer 双态/排队 + 侧栏收窄可折叠 + 审批内联卡 + 删 banner。
- 验收：R1 的六个场景在 UI 全程可见可操作（插件进度实时、审批内联、失败可见）；前端构建通过；L4 欠账记录（组件级测试补 R3 或专项）。

### R3 streaming + 成本仪表

- LLM 抽象加 stream；`assistant/chunk` 落库 + SSE；turn usage 落库 + 折叠控件显示。
- 验收：长回答逐字可见；reload 后渲染与 live 一致（chunk 投影 = message）；turn 折叠控件显示 token 用量。
