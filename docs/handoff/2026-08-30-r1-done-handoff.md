# Handoff：重设计 R1 完成（2026-08-30）

> 接上篇：docs/handoff/2026-08-29-redesign-handoff.md（归因与方向）+
> docs/redesign-interaction-orchestration.md（v4 定稿，§5 决策记录全）。
> 本文件固化 R1 完成状态与 R2 开工所需上下文。

---

## 0. 一句话现状

**R1（编排层）已完成并验收**：command 制交互（`/research` `/profile` `/decide` `/evaluate`）+
主 agent（自然语言自主调用 command）+ step agent 子 run 管道全部落地，测试 158 passed，
真实 BE 场景端到端跑通。**等用户确认后进 R2（对话流 UI 重构）**。

## 1. R1 交付物（commit ecc8202）

| 模块 | 位置 | 说明 |
| --- | --- | --- |
| Command 注册表 | `commands/registry.py` | 4 个 command 定义 + slash 解析 + catalog（composer 补全用 `/api/commands`） |
| Command 编排 | `commands/runner.py` | command/run·done 事件序、step 子 run 编排、进度桥接、审批闸（evaluate 默认强制，`--no-approval`/原话豁免落 `approval/waived`）、取消注册表 |
| Step agents | `commands/steps.py` | S1 研究 / S2 档案更新（thesis 修订）/ S3 决策（打回有界重试 ×2）/ 过程评估 / S4 评估；各跑独立 child run（`run/created` 带 parent_run_id） |
| 主 agent | `main_agent.py` | 契约（标的识别/宽松直调/decide 闸/多标的并行）+ 工具面（query_kb、run_command、stop_command、show_profile、query_edgar/prices） |
| ChatService | `chat/service.py` | inbox 语义：消息先落库 → per-run 串行 drain；command/done 经 context/inject 唤醒主 agent 汇报（Q1 异步唤醒） |
| kernel 增强 | `loop/kernel.py` | `run_turn(None)` 支持预落库输入；turn 编号 store 派生；**工具异常弹性**（错误回模型自我修正，不熔断 turn） |
| API | `api/app.py` | `/api/chat` 唯一写入口（slash 派发 / 主 agent）；sessions 投影带 title/last_active 且排除子 run；`/api/sessions/{id}/children`；**已删** `api/intent.py` 与 `/api/research` |
| 装配 | `cli.py build_orchestrator()` | serve 与测试共用同一真实装配路径 |

## 2. R1 验收结果

- `uv run pytest` → **158 passed, 1 skipped**；ruff 全绿
- 真实 BE 场景（handoff §4.5 场景 1）：「我想深度研究下BE这家公司，是否值得投资」→
  主 agent 正确识别 BE=Bloom Energy(NYSE) → 查档案 → 自主启动 /research →
  过程事件流完整（command/run→step_agent/*→command/done）→ 唤醒主 agent 汇报 →
  **未自动出决策卡**（decide 闸成立）
- 真实 run 抓出并修了两个真 bug：kernel 工具异常熔断 turn（已改弹性）；
  子 run id 跨 command 碰撞（已修：含 command_id）
- 失败路径：provider 缺失 → 对话式可操作报错；step 失败三通道齐全
  （test_l1_failure_visibility / test_e2e_coldstart 覆盖）

## 3. 已知遗留（非 R1 bug）

1. **EDGAR adapter 只返回 filing 索引（form+accession），不返回正文** → 研究 agent 拿不到
   verbatim_quote → 证据登记为空 → 完整度 0% stalled。grounding 纪律正确兜底
   （不编造、如实汇报「我不知道」），但**研究能力要可用需补 EDGAR 正文抓取**
   （document endpoint / full-text search）。这是数据能力增强，建议独立任务。
2. **yfinance 未装**（PyPI 网络当时不通）：`uv pip install yfinance` 后行情工具即可用；
   未装时错误已弹性可见。
3. 主 agent 在研究 stalled 时会自主重试（最多观察到 3 次后停下如实汇报）。
   可在契约里加「stalled 两次 → 停手汇报数据边界」降本，待观察。
4. 前端仍是旧版（SessionsPage 平铺渲染 + ApprovalsBanner 轮询）——**这正是 R2 要重做的**。
   旧前端对新事件流基本兼容（command/* 事件会显示为调试卡片）。

## 4. R2 范围（对话流 UI 重构，见设计稿 §3.7）

- 前端装配层（事件→节点，简化版 dsh Definition）：user/assistant/tool-pair/command/
  step-agent/approval/turn-fold/error/report-fold 八类节点
- CommandCard（进度条 + 嵌套 StepAgentCard）+ ResearchFoldCard（摘要折叠，点开展开
  artifact 全文）+ ProfileCard（show_profile 结果特化渲染）+ ApprovalCard 内联
  （SSE 驱动，删 2s 轮询 banner）+ TurnProcess 折叠 + composer（slash 补全 / Send⇄Stop / 排队）
- 参考实现：docs/demo/ui-redesign-demo.html（用户已确认形态）
- 验收：R1 七场景在 UI 全程可见可操作；前端构建通过

## 5. R3/R4 预告（混合模式：连续做，完成后一起验收）

- R3：ProfileRenderer（KB→自包含 HTML+内联 SVG，版本化 archive/）+ Knowledge 页重做
  （列表选中联动摘要 + 详情页 + 存档查看器；demo 已确认）
- R4：assistant/chunk streaming + turn usage 成本仪表
