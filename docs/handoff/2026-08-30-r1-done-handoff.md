# Handoff：重设计 R1+R1.5+R2 完成（2026-08-30）

> 接上篇：docs/handoff/2026-08-29-redesign-handoff.md（归因与方向）+
> docs/redesign-interaction-orchestration.md（v4 定稿，§5 决策记录全）。

---

## 0. 一句话现状

**重设计四期全部完成**（R1 编排层 / R1.5 证据完整性 / R2 对话流 UI + streaming / R3 档案 UI + HTML 存档 / R4 用量仪表），
163 passed + 前端构建通过 + 真实 /research BE 全链路验证（3 轮收敛 90%、存档/报告/用量/冲突旗标在线）。
commits：ecc8202(R1) → e7fc344(R1.5) → b815fcf(R2) → 09eb53e(R3+R4)。

## R3+R4 要点（commit 09eb53e）

- ProfileRenderer：自包含 HTML（内联 SVG/无 JS）+ 证据锚点 hover + 版本化 archive/
  （**内容哈希**幂等，与 kb_snapshot_id 的 as_of 语义区分）
- API：entities 健康度投影 / archives 版本列表+读取 / reports 全文在线阅读
- KnowledgePage 重做（列表选中联动摘要 + 详情页 + 时光机 + 存档查看器）
- AssistantReply.usage → kernel 按 turn 聚合 → TurnFold 显示 token
- **整改**：StepDeps.knowledge_dir 必填（测试曾污染仓库工作树，真实 run 抓出）；
  usage 容忍 provider 嵌套明细（ValidationError 真实抓出）；knowledge/ 入 .gitignore

## 下一步候选（backlog，按价值排序）

1. **yfinance 安装**（PyPI 网络当时不通）→ 行情工具可用后估值维度解锁
2. **steer**（运行中改方向，Q6 后置项）
3. **主 agent 对 stalled 研究的重试上限**收紧（观察到自主重试 3 次才停）
4. **前端组件级测试**（L4 欠账，vitest）
5. **评估配置的中文补全体验**（/evaluate 配置选择卡的对话式微调）
6. **INDUSTRY 档案与多实体对比视图**

## R1.5（验收事故整改，commit e7fc344）

用户在 R1 验收中发现「研究质量差几个数量级」——根因是**证据完整性漏洞**：
旧 register_evidence 信任模型自报的 quote/来源/available_at，参数记忆可自编自引。
修复（verified binding）：

- `research/evidence_desk.py`：ChunkStore（run 级检索台账，chunk_id 递增 chk-0001…）+
  verify_and_build（quote 必须是 chunk 逐珠子串；元数据全部从 chunk 推导，模型自报不信）
- `read_edgar_filing` 工具：filing 记录 chunk → 抓正文（edgar.fetch_filing_text）→
  关键词窗口切块；PIT 元数据从 filing 记录继承
- 网关工具返回项带 chunk_id：「模型可见的记录才可引为证据」闭环
- 进度桥接加密：子 run 每次 tool/call 都桥到父流 step_agent/progress
- **回归**：test_fabricated_quote_is_rejected（编造摘录被拒）
- 验证：真实 BE run 读 10-K 正文，FY2025 营收 20.24 亿(+37.3%) 等事实带证据落库

## R2（对话流 UI，commit b815fcf）

- 前端装配层 `lib/assemble.ts`（事件→节点）+ 八类节点渲染器（`components/nodes.tsx`）
- Composer：slash 补全 + Send⇄Stop + 运行中排队；侧栏 240px 可折叠；删 ApprovalsBanner
- streaming：`assistant/chunk` 落库（白名单不变）；OpenAICompatLLM.stream_complete
  （SSE 解析含无 choices 帧容错——真实 provider 抓出的 bug）；MockLLM 流式替身
- `POST /api/sessions/{id}/stop` 停 command
- 已知欠账：前端无组件级测试（L4）；ResearchFoldCard 的全文暂时只指向磁盘 artifact
  （R3 档案 UI 时补在线阅读）

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
