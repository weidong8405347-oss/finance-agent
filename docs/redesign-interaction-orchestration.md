# 交互与编排层重设计（归因 + 对齐稿）

> 来源：docs/handoff/2026-08-29-redesign-handoff.md 的归因作业与方案细化。
> 状态：**v3 待最终确认**（§5 剩余决策点）。
> v2（2026-08-29 用户输入）：交互主体改为 **command 制**——四个 step +
> 独立评估系统做成 `/command`；无 command 走主 agent 对话；主 agent 也可自主调用 command；
> command 内每个 step = 一个 step agent（可挂 plugin 提升能力）。
> v3（2026-08-29 用户确认 + 新增）：command 命名定案；`/evaluate` 默认强制审批、
> 仅当 prompt 明确「不用审批」才豁免；新增两块——**研究结论折叠卡**（长文只显摘要，
> 点击展开）与**股票档案完整 UI**（全部股票档案浏览 + HTML 存档 + 图表）。
> 约束：地基（EventStore / 双时态 KB / DataGateway / 决策卡 / 评估回放 / 可观测性）不动；
> 本稿只重构「用户 → 对话 → 主 agent / command → step agent」这一层。

---

## 1. 归因：为什么一直没对齐 dsh

### 1.1 一句话归因

**我们借了 dsh 的外形（输入框 + 消息列表），没有借 dsh 的机制（事件流 → 装配 → 投影）。**
三个用户-visible 问题（意图死板、UI 像 CRUD、交互模型差距大）是同一根因的三个投影：
我们把系统做成了「**命令分发器 + 任务日志查看器**」，而 dsh 是「**对话驱动的主 agent + 事件投影 UI**」。

### 1.2 逐层归因

| 层 | 我们做了什么 | dsh 做了什么 | 根因 |
| --- | --- | --- | --- |
| 意图理解 | `api/intent.py` 正则路由：消息 → intent → 直接起 research/decision 线程 | 没有这一层。理解是主 agent（LLM）的职责，system prompt 写纪律 | **用确定性 pattern 做 NLU 是模式错误**。正则 bug（CJK 词边界）只是表象；理解不可枚举，必须交给模型 |
| 交互单元 | 一次后端任务 = 一个 session（run_id 即 research run）；「对话」不存在，只有任务日志 | 对话 = session；任务是对话内部的 turn / 工具调用 / 子 agent / command | 交互单元错了：UI 组织围绕「任务」，所以长成 CRUD；dsh 围绕「对话流」，任务是流里的节点 |
| 编排 | `/api/chat` 是 if-else 分发器，主 agent 不存在；ResearchLoop/DecisionLoop 是用户直达的入口 | 主 agent loop 常驻；一切能力（command/skill/subagent/tool）挂在一个注册体系上 | 把「编排」理解成「路由到固定管线」，而不是「主 agent + 可调用能力注册表」。S1-S4 被做成了用户触发的入口，而不是可组合的能力 |
| UI 渲染 | 按事件类型 if-else 平铺；工具调用直接 HIDDEN；无折叠无配对 | ui-conversation 装配层：Definition（match/update/fold）+ Turn/Step Location + 目标快照；tool 卡按 call_id 配对、递归嵌套；turn 结束后过程折叠 | 没有「投影」这一层：前端直接渲染原始事件流，等于把日志刷到屏幕上 |
| 审批 | 顶部 banner + 2s 轮询（带外通道） | `approval/asked`/`approval/decided` 审计事件 + answerer waterfall + **composer takeover**（审批是对话内联交互，attach 到已流式展示的 tool call） | 审批被当成「系统通知」而不是「对话中的一个待答问题」 |
| 流式 | LLM.complete 一次性返回；SSE 只是事件轮询推送 | `assistant/chunk*` 落日志（保 replay/UI 保真），UI 增量渲染 | 事件词汇缺 chunk 粒度；LLM 抽象无 stream |

### 1.3 深层教训（写进工程纪律）

1. **理解归模型，分发归代码**：凡「理解自然语言」的环节一律进 agent prompt；代码只做确定性执行。新增任何「分类/识别」代码前问：这是不是在做 NLU？
2. **借范式要借机制**：UI 参考一个系统时，对齐的是它的事件模型与投影层，不是页面截图。
3. **对话是唯一主交互**：任何用户可达的动作都必须能在对话流中发起、观察、确认；带外通道（banner/独立表单）默认是错的。

---

## 2. 组件级对齐差距表（dsh → 本项目）

| # | dsh 机制（一手出处） | 本项目现状 | 重设计动作 |
| --- | --- | --- | --- |
| 1 | turn flow：`turn/start → agent/pre-step → step/start → agent/request → llm/stream → assistant/chunk* → assistant/message → tool/call* → tools/execute → tool/result* → step/end → turn/end`（docs/architecture.md） | `AgentKernel` 已有 turn/step/tool 事件序；缺 chunk、缺 inbox/claim 语义 | kernel 增加 streaming chunk 发射；chat 入口改为「消息落库 → turn 认领」 |
| 2 | 「模型可见 = 已记录」runtime invariant；deriveMessages 从日志投影 | 已有 `derive_messages` + 白名单 | 不变（地基）。chunk/progress/approval/command 事件**不入**模型可见白名单 |
| 3 | ui-conversation 装配层：Definition（match/update/fold）+ Turn/Step Location + target 快照（docs/subsystems/conversation.md） | 前端按 type if-else 平铺 | 前端加事件→节点装配层（TS 版 Definition/Location 的简化版） |
| 4 | tool-call 节点：call/result 按 callId 配对，递归嵌套 subCalls（ui-chat/conversation-nodes/tool.ts） | `tool/call` 被 HIDDEN；`tool/result` 是通用 debug 卡 | ToolCard 组件：配对折叠卡 |
| 5 | turn-process folding：turn/end 后过程行折叠为「N tool calls · M messages」控件，Compact 默认（ui-chat README） | 无 | TurnProcess 折叠控件 |
| 6 | subagent seam：子 agent 独立 session，父会话 tool 卡嵌套 + header 血统目录（docs/subsystems/subagent.md、ui-subagent） | 无 subagent 概念 | step agent = 子 agent：command run 下的 child run（`parent_run_id` 关联），父会话活动卡嵌套进度 |
| 7 | **human command**：插件注册 `CommandDefinition`，composer slash 发现并直接派发（不建模型消息），`command/run`/`command/done` 落日志，chat 渲染 CommandNode（docs/subsystems/commands.md、conversation-nodes/command.ts） | 无 command 概念 | **CommandRegistry**：四个领域 command 注册；composer slash 补全；`command/run`/`command/done` 事件；CommandCard 节点 |
| 8 | **skill 模型侧调用**：catalog 进 prompt + model-facing `skill` tool；skill-invocation-policy 约束何时可用（docs/subsystems/skills.md） | 无 | command catalog 进主 agent prompt + `run_command` 工具：主 agent 自主判断调用 command，与 slash 入口同一派发 |
| 9 | 审批：approval 审计事件 + answerer waterfall + **composer takeover 内联**、fail-closed `unavailable`（docs/subsystems/approval.md、ui-approval） | ApprovalsBanner 顶部横幅 + 2s 轮询 | approval 事件入 EventStore；对话流内联审批卡（SSE 驱动，删轮询 banner） |
| 10 | composer：常驻底部、乐观发送 echo、busy 时 Stop/Queue 双态、slash command 认领后保持样式文本（ui-conversation README） | textarea + 发送按钮；发送即阻塞 | composer：slash 补全 + Send ⇄ Stop 双态 + 运行中排队 |
| 11 | session 侧栏：workspace 树、子 agent 会话**不进**普通侧栏 | 左 1/3 栏平铺 run_id 列表 | 侧栏收窄可折叠（240px）、会话标题（首条用户消息截断）、子 run 不进列表 |
| 12 | 意图理解：主 agent system prompt 纪律 | 正则 intent router | **删 `api/intent.py`**；识别/确认纪律写进主 agent 契约（§3.4） |
| 13 | turn token usage 行 | 无 | LLMRouter 回传 usage → 随 turn/end 落库 → 折叠控件显示 |

---

## 3. 重设计方案（command 制交互）

### 3.1 三层交互模型

```text
 composer（底部常驻：slash 补全 / Send⇄Stop / 排队）
    │
    ├─ 无 command  →  主 agent 对话 turn（直接 LLM 应答；
    │                  主 agent 可经 run_command 工具自主调用 command）
    │
    └─ /command  →  CommandRegistry 派发（不经过主 agent 的理解，
                     显式、确定、可审计）
                     │
                     ▼
              CommandRun（父 run：command/run … command/done）
                     │  按 command 定义编排 step agent
        ┌────────────┼────────────┬──────────────┐
        ▼            ▼            ▼              ▼
   step agent   step agent   step agent    过程评估 step
   (S1 研究)    (S2 档案)    (S3 决策)     （软反馈+硬门禁）
   每个 = 子 agent：自己的 kernel loop + 契约 + 工具 + 挂载 plugin/hook
   独立 child run（parent_run_id 关联），上下文互相隔离
```

三条原则：

1. **显式优先**：用户敲 `/command` = 确定性派发，不让模型猜；用户说自然语言 = 主 agent 理解，
   主 agent 可判断后调用 command（同一派发管线，`run_command` 工具）。
2. **command = step agent 的编排**：四个 step 与独立评估系统各是一个 step agent 定义；
   command 定义 = 有序 step 列表 + 数据接力规则。
3. **step agent 可挂 plugin**：plugin 挂在 step agent 上（dsh 的 per-agent scoped 能力思路），
   提升该 step 的能力与效果——新增优化 = 写 plugin，不动主干（原则 2 不变）。

### 3.2 Command 目录（名字即设计：复用四个 step + 独立评估）

| command | step pipeline | 说明 |
| --- | --- | --- |
| `/research <标的> [目标]` | S1 研究 → 过程评估 | 只调研不出档案重组；过程评估给质量反馈 |
| `/profile <标的>` | S1 研究 → S2 档案更新 → 过程评估 | 调研并落库/修订档案（thesis/facts） |
| `/decide <标的>` | S1 研究 → S2 档案 → S3 决策卡 → 过程评估 | 全链路到出卡（risk-review 硬门禁不变） |
| `/evaluate <配置>` | S4 独立效果评估 | eval 模式隔离环境；**默认强制审批**，仅当 prompt 中明确「不用审批」才豁免（豁免落 `approval/waived` 事件，可审计） |

- **幂等性由 gap 分析保证**：`/decide` 每次都含 S1，但档案新鲜完整时 S1 零轮收敛
  （gap 分析输出「无需研究」），不会重复花钱。
- **数据接力不走对话上下文**：step agent 之间的输入输出经 KB / DecisionStore /
  结构化 handoff payload（事件载荷），不经共享消息列表——各 step 上下文隔离
  （DESIGN.md 独立视角隔离原则的自然延伸）。
- **过程评估 = evalresult step**：evaluation-design.md §1 的插件组
  （evidence-binding / numeric-guard / coverage-check / risk-review…），
  硬门禁失败即打回（事件可见），软反馈进 gap 分析。
- command 定义是**配置**（step 列表 + 策略），新增 command = 加配置 + 组合既有 step agent。

### 3.3 Step agent 与 plugin 挂载

```python
StepAgentDef = {
    "name": "research",                # S1
    "contract": RESEARCH_CONTRACT,     # 系统 prompt（grounding 纪律等）
    "tools": ["query_kb", "query_edgar", "query_prices", "register_evidence", "propose_fact"],
    "plugins": [GapAnalysis, CounterEvidence, RubricJudge],   # 可插拔能力
    "hooks": ["evidence-binding", "numeric-guard", "step-report"],  # 必达
    "loop": "rounds",                  # rounds（S1 轮次制）| single-turn（S3）
    "budget": {"max_rounds": 3, "max_steps_per_round": 16},
}
```

- step agent 复用 `AgentKernel`（稳定 kernel 不变）；`loop` 字段选驱动器
  （S1 用现有 ResearchLoop 的轮次驱动，S3 用单 turn 驱动）。
- **plugin 挂载点**：before_round / after_round / on_brief 等（每类驱动器声明自己的挂载面）；
  hook 仍是必达门禁，plugin 是可插拔增强——二者分工不变（原则 4）。
- 每个 step agent 跑在**独立 child run**（独立 manifest：`parent_run_id`、`correlation_id`），
  eval 命令的子 run 继承 eval 模式与时间锁（manifest 传递，地基不变）。

### 3.4 主 agent 契约（system prompt 骨架，替代 intent router）

```text
你是 finance-agent 的主 agent，一个投研对话伙伴。纪律：
1. 标的识别：用户提到公司（中文名/英文名/代码/别名/描述）时，先确定标的代码。
   能确定就直接说明（如「BE = Bloom Energy，NYSE」）；不能确定就追问，绝不猜。
2. 按需调用，绝不套餐：用户只想了解/研究 → 最多调 /research；
   只有用户明确要投资建议时才考虑 /decide；研究完成 ≠ 自动出决策。
3. 先查档案再建议：调用 command 前先 query_kb 看现有档案完整度与新鲜度，
   告知用户现状并确认要做什么（milestone 档：高成本操作先请示）。
4. /evaluate 高成本：永远先说明成本并经审批卡确认。
5. 一切事实性断言引用证据 id；没有证据就说「我不知道」。
6. 你能调用的 command 目录（以 run_command 工具调用）：
   /research /profile /decide /evaluate —— 各自语义见 catalog。
7. 长任务启动前，用一句话告知用户接下来会发生什么、大约多久。
```

### 3.5 事件词汇扩展（入 EventStore；标注模型可见性）

| 事件 | 载荷要点 | 模型可见 |
| --- | --- | --- |
| `command/run` | `{command_id, name, args, raw_input}`（dsh 同款） | 否 |
| `command/done` | `{command_id, outcome, summary}` | 否 |
| `step_agent/start` | `{call_id?, child_run_id, step, command_id}` | 否 |
| `step_agent/progress` | `{child_run_id, summary}`（轮次级轻量摘要桥接到父流） | 否 |
| `step_agent/end` | `{child_run_id, status, summary}` | 否 |
| `assistant/chunk` | `{text}` 流式增量 | 否（只服务 UI 保真/replay） |
| `approval/asked` · `approval/decided` | `{approval_id, op, detail}` / `{approved}` | 否 |
| `approval/waived` | `{op, basis}`（用户原话依据；豁免必须可审计） | 否 |
| `report/published` | `{child_run_id, kind, title, summary, artifact_path, quality_flags}` —— 折叠卡数据源；全文为磁盘 artifact，事件只带摘要+指针 | 否 |
| `session/title` | `{title}`（首条用户消息截断） | 否 |

子 run 内部仍用现有 turn/step/tool 事件全集；父流只承载轻量桥接事件，
UI 钻取细节时打开子流。`derive_messages` 白名单不变（§2 行 2）。

### 3.6 API 变化

| 端点 | 变化 |
| --- | --- |
| `POST /api/chat` | 统一入口：`{session_id?, message}`。message 以 `/` 开头 → command 派发；否则主 agent turn。两种路径都先把 user/message（或 command/run）落库 |
| `POST /api/research` | **删除**（统一走 chat/command）；CLI `research` 子命令保留作 headless 调试 |
| `api/intent.py` | **删除** |
| `GET /api/commands` | command catalog（composer 补全 + 帮助） |
| `POST /api/approvals/{id}` | 保留；改为事件驱动（asked/decided 入 EventStore，SSE 推送） |
| `GET /api/approvals/pending` | 保留（刷新恢复用），UI 不再轮询 |
| `GET /api/sessions` | 投影加 `title`、`last_active`；排除 child run |
| `GET /api/sessions/{run_id}/children` | 子 run 目录（step agent / command 钻取） |

### 3.7 UI（对话流为唯一主交互）

```text
┌────────────────────────────────────────────────────────────┐
│ header：finance-agent │ EVAL badge(条件) │ 导航(小)         │
├──────────┬─────────────────────────────────────────────────┤
│ 侧栏      │  对话流（max-w-3xl 居中，单列）                 │
│ 240px    │   user 气泡 / assistant markdown（streaming）    │
│ 可折叠    │   ToolCard（call/result 配对折叠卡）             │
│ 会话标题  │   CommandCard（/decide BE · 运行中 ▓▓░░ 3/4 步） │
│ 状态+时间 │     └ StepAgentCard × N（嵌套：S1 研究 第2轮 ·   │
│          │       完整度 61% · 可展开钻取子流）              │
│          │   ApprovalCard（内联：说明 + 允许/拒绝）          │
│          │   ResearchFoldCard（研究结论：默认只显标题+摘要+ │
│          │     质量旗标，点击展开全文；长文不刷屏）          │
│          │   TurnProcess 折叠控件（turn 结束默认折叠）       │
│          │   ErrorCard（失败三通道的用户可见通道）           │
├──────────┴─────────────────────────────────────────────────┤
│ composer（slash 补全 + Send ⇄ Stop + 运行中排队）           │
└────────────────────────────────────────────────────────────┘
```

- 前端加**装配层**（简化版 dsh Definition）：事件流 → 节点列表
  （user/assistant/tool-pair/command/step-agent/approval/turn-fold/error），
  渲染器只消费节点。这是「UI 是投影」在前端的落地，也是加节点的扩展点。
- CommandCard 是 command run 的投影：step 进度条 + 嵌套 StepAgentCard +
  完成后的 outcome 摘要；StepAgentCard 可展开看子流（轮次/工具/证据 chips）。
- Knowledge/Decisions/Evaluations 保留为钻取页（从证据 chips、决策卡、评估事件跳转）。
- `ApprovalsBanner` 删除（含 2s 轮询）。

### 3.8 研究结论折叠卡与档案 HTML 存档

**ResearchFoldCard（对话流内）**：S1 产出的研究报告是长文，不进对话流刷屏。
step agent 完成时发 `report/published`（摘要 + artifact 指针）；全文渲染为
Markdown artifact 存 `data/reports/<child_run_id>/round-N.md`；UI 默认只显
标题 + 摘要 + quality_flags，点击展开经 API 拉全文。事件体积可控，
「UI 可回指 event id」不破（artifact 路径在事件载荷里）。

**档案 HTML 存档（磁盘投影，版本化）**：

- 档案本体仍是 EventStore/KB 投影；**command run 的 S2 step 完成且档案有变化时**
  由 ProfileRenderer 生成自包含单文件 HTML：
  `knowledge/stocks/<TICKER>/archive/<kb_snapshot_id>.html` + `latest.html` 软链。
  版本化存档 = as_of 时光机的磁盘对应物，历史版本永不删除。
- **自包含 + 服务端渲染 SVG 图表**：价格走势、财务指标趋势等图表以**内联 SVG**
  渲染进 HTML（Jinja 模板，无 JS/无外部依赖）——可离线打开、可 diff、可审计，
  与「投影」定位一致（图表数据经 DataGateway 生产模式查询，写入 HTML 时保留
  证据指针）。不用 ECharts 等运行时库（外部依赖 + 不可审计）。
- HTML 内每个事实数字保留证据锚点（hover 出原文摘录 + available_at），
  与 DESIGN.md §7.3「一切数字可溯源」一致。

**Knowledge 页升级为完整档案 UI**：

- **全部股票档案列表**：所有实体（含历史研究过、含退市标记）一屏可查，
  显示完整度/新鲜度/冲突数/最近研究时间；行业档案同列。
- **档案详情页**：facts 分维度表 + thesis + timeline + 冲突标记；
  as_of 时光机（日期选择整页切换投影，diff 标出后来被修订的内容）；
  **HTML 存档查看器**：版本列表 + iframe 内嵌渲染（或新页打开原始 HTML）。
- 入口双向：Knowledge 页可进；对话流里证据 chips / 决策卡 / ResearchFoldCard
  中的标的也可点击跳入对应档案页。

### 3.9 不变量（重设计不得破坏）

1. 模型可见 = 已记录；derive_messages 白名单不变。
2. 一切数据采集经 DataGateway；子 run 的 gateway 用子 manifest（eval 锁随 manifest 传递）。
3. 知识库单写者：S2 step agent 内的 ProfileWriter 纪律不变。
4. risk-review 必达：S3 step agent 内部纪律不变；过程评估硬门禁不变。
5. 失败三通道 + 真实装配测试：step agent 失败 → 子流 error 事件 + 父流 step_agent/end(status=error) + 日志。
6. 审批 fail-closed：超时/无应答 = rejected；`/evaluate` 默认强制审批，豁免必须落 `approval/waived`（带用户原话依据）。
7. 评估权力分离：`/evaluate` 走 eval 命名空间，永不回流生产（地基，不动）。

---

## 4. 分期与验收（TDD）

### R1 编排层（后端 + 验收脚本）

删 `intent.py` + `/api/research`；CommandRegistry + 四个 command 定义；
step agent 抽象（S1/S2/S3 从现有 Loop 改造）+ plugin 挂载面；
`/api/chat` 统一入口（command 派发 / 主 agent turn / `run_command` 工具）；
approval 事件化；事件词汇扩展；children 端点。

验收（真实装配 + MockLLM 脚本化）：

1. 「我想深度研究下BE这家公司，是否值得投资」→ 主 agent 识别 BE=Bloom Energy
   并确认 → 用户「确认」→ 主 agent 调 `/research` → step agent 子 run 运行 →
   完成后主 agent 摘要 + **询问是否需要投资建议，不自动出卡**。
2. `/decide BE`（显式 slash）→ 确定性派发，不经主 agent 理解；
   S1→S2→S3→过程评估依次运行，父流可见 step 进度。
3. 闲聊（无标的）→ 直接答，不调任何 command。
4. 档案新鲜时 `/decide BE` → S1 gap 分析零轮收敛（不重复花钱）。
5. step agent 失败 → 三通道可见（子流 error + 父流 step_agent/end(error) + 日志）。
6. `/evaluate` → 默认必出 approval/asked；拒绝 → 不执行；超时 → rejected；
   prompt 明确「不用审批」→ 落 approval/waived（含原话依据）后直接执行。
7. 不变量回归：`uv run pytest` 全绿（地基测试语义不动）。

### R2 对话流 UI

前端装配层 + 节点渲染（含 CommandCard/StepAgentCard 嵌套、**ResearchFoldCard**
摘要折叠卡）+ composer slash 补全 / 双态 / 排队 + 侧栏收窄可折叠 + 审批内联卡 + 删 banner。

验收：R1 的七个场景在 UI 全程可见可操作；研究长文默认折叠、点击展开全文；
前端构建通过（L4 组件测试欠账记录在案）。

### R3 档案 UI + HTML 存档

ProfileRenderer（KB → 自包含 HTML + 内联 SVG 图表 + 版本化 archive/）；
Knowledge 页重做（全部股票/行业档案列表、详情页、as_of 时光机、HTML 存档查看器）；
对话流与档案页双向跳转。

验收：跑一次 `/profile AAPL` 后生成 `archive/<snapshot>.html` 且 latest 更新；
HTML 离线打开图表可见、数字 hover 出证据；Knowledge 页列出全部历史股票档案；
as_of 切换后整页投影正确（地基 as_of 测试语义不动）。

### R4 streaming + 成本仪表

LLM 抽象加 stream；`assistant/chunk` 落库 + SSE；turn usage 落库 + 折叠控件显示。

验收：长回答逐字可见；reload 后渲染与 live 一致；折叠控件显示 token 用量。

---

## 5. 待确认决策点（对齐后再动工）

| # | 决策点 | 建议 | 备选 |
| --- | --- | --- | --- |
| D1 | 主 agent 上下文模型 | 无状态重建：每 turn 从 EventStore 投影；per-run 串行锁 + 消息排队 | 常驻内存上下文（多一条上下文通道，违背铁律精神） |
| D2 | command 命名 | **已定**：`/research` `/profile` `/decide` `/evaluate`（2026-08-29 用户确认） | — |
| D3 | command 审批策略 | **已定**：research/profile/decide 主 agent 口头确认即调；`/evaluate` **默认强制审批，仅 prompt 明确「不用审批」才豁免**，豁免落 `approval/waived` 事件可审计（2026-08-29 用户确认） | — |
| D8 | 档案 HTML 存档方案 | 自包含单文件 HTML + 服务端渲染内联 SVG 图表（无 JS/无外部依赖，可审计可 diff） | ECharts 等 JS 运行时库（交互强但依赖外部、不可审计） |
| D4 | `assistant/chunk` 是否落库 | **落库**（dsh 同款：保 replay/UI 保真；可定期清理老 run 的 chunk） | 只走 SSE 不落库（省存储，live 与 reload 渲染不一致） |
| D5 | step agent 进度呈现 | 父流 step_agent/progress 轻量桥接 + 子流钻取 | 只靠子流（父流活动卡无实时进度） |
| D6 | 会话标题 | 首条用户消息截断 30 字 | LLM 生成（dsh 做法，多一次调用，后置） |
| D7 | 分期 | R1 → R2 → R3 → R4 分四期（档案 UI+HTML 存档独立成 R3） | 合并分期（改动面大，回归风险高） |
