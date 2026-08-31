# Handoff：当前状态全景（2026-08-30 收官版）

> 给新会话的唯一入口文档。历史过程见 git log（重设计批次：cf231d5 设计稿 v4 →
> ecc8202 R1 → e7fc344 R1.5 → b815fcf R2 → 09eb53e R3+R4 → 35b7a8d R5 → bedae58 R6）。
> 设计文档：`docs/redesign-interaction-orchestration.md`（交互/编排，§5 决策记录全）、
> `DESIGN.md`（总体设计）、`docs/evaluation-design.md`（评估）、`docs/how-to-extend.md`（扩展配方）。

---

## 0. 一句话现状

交互与编排层重设计**全部完成并验收**：command 制交互（主 agent + 四个 /command + step agent
子 run）、对话流 UI（流式 + 装配层 + 八类节点卡）、证据完整性防线（verified binding）、
档案 HTML 存档与图表、能力目录页、CIO 报告合成、评估内联卡。
**166 pytest + 5 vitest + ruff 全绿**；真实 BE 场景产出 deep-research 级证据绑定研报。

## 1. 系统形态（用户视角）

```text
对话（唯一主交互，底部 composer）
  ├─ 自然语言 → 主 agent（LLM 理解，宽松直调 command；decide 必问；evaluate 审批）
  └─ /research /profile /decide /evaluate → CommandRunner 确定性派发
       → step agent 管道（research → profile_update → synthesize → decide → process_eval）
       → 每个 step = 独立子 run（run/created.parent_run_id 关联，children 端点可钻取）
页面：对话（默认）/ Knowledge（档案列表+详情+图表+HTML 存档）/ Decisions / Evaluations / 能力
```

关键交互纪律：研究可自主启动；**决策卡永远先问用户**；**evaluate 默认审批**（`--no-approval`
或原话豁免，当次有效，落 `approval/waived`）；主 agent 工具异常弹性（错误回模型自我修正）。

## 2. 工程铁律（任何改动不得破坏）

1. **模型可见 = 已记录**：derive_messages 白名单投影；chunk/编排/审计事件不进白名单。
2. **证据 verified binding**：register_evidence 只接受 `chunk_id + 逐字摘录`
   （服务端子串校验，来源/available_at 从 chunk 元数据推导）；模型自编自引 = 拒绝。
3. **单写者** ProfileWriter；事实 append-only 版本链；**冲突 = 同 event_time 不同值**
   （演进不标冲突）；裁决走 resolve_conflict（落 fact/conflict_resolved）。
4. **失败三通道**：事件 + 日志 + 用户可见状态（test_l1_failure_visibility 是回归）。
5. **真实装配**：serve 与测试共用 `cli.build_orchestrator()`；测试不打真实网络/付费 API。
6. **评估权力分离**：eval 命名空间隔离、时间锁网关、holdout 预算（DESIGN.md §6）。
7. 测试绝不吃默认路径：`StepDeps.knowledge_dir` 必填（曾污染工作树）。

## 3. 运行与验证

```bash
uv run pytest                        # 166 passed
cd frontend && npx vitest run        # 5 passed（装配层）
uv run python -m finance_agent serve # 自动构建前端 + 开浏览器（:8000）
```

真实验收场景：「我想深度研究下BE这家公司，是否值得投资」→ 识别 BE → 自主 /research →
流式进度 → CIO 研报（证据锚点）→ 汇报并询问是否出卡（不自动出）。

LLM 配置：复用 pi 的 `~/.pi/agent/`（研究主力 pa/gpt-5.6-sol；fast = kimi-k3），无则 .env 三件套。

## 4. 架构地图（改动时的落点）

| 关注点 | 位置 |
| --- | --- |
| 主 agent 契约/工具 | `src/finance_agent/main_agent.py` |
| command 注册/解析 | `src/finance_agent/commands/registry.py` |
| step agent 编排 | `src/finance_agent/commands/runner.py` |
| step agents（S1/S2/合成/S3/评估） | `src/finance_agent/commands/steps.py`（**STEP_MANIFEST = 能力登记处**） |
| 证据完整性 | `src/finance_agent/research/evidence_desk.py` + `research/tools.py` |
| agent kernel（turn/step/流式） | `src/finance_agent/loop/kernel.py` |
| 会话 turn 认领 | `src/finance_agent/chat/service.py` |
| 事件词汇 | `src/finance_agent/eventstore/events.py` |
| 档案 schema/完整度 | `src/finance_agent/knowledge/schema.py` / `gaps.py` |
| HTML 存档 | `src/finance_agent/knowledge/render.py` |
| 装配（真实路径） | `src/finance_agent/cli.py build_orchestrator()` |
| 前端装配层 | `frontend/src/lib/assemble.ts`（+ vitest） |
| 前端节点渲染 | `frontend/src/components/nodes.tsx` |
| 扩展配方 | `docs/how-to-extend.md` |

## 5. Backlog（按价值排序，均已确认方向）

1. **行情源解锁**：本机网络受限（PyPI 不通、stooq 被反爬）。换网络环境即可
   （StooqPricesAdapter 已就绪）→ 解锁价格图 + **`/evaluate` 真实首跑**（ReplayEngine 装配已就绪）。
2. **冲突人工裁决 UI**：详情页「以此版本为准」按钮（后端 resolve_conflict 已就绪）。
3. **steer**（运行中改方向注入子 run；Q6 后置项）。
4. 主 agent 对 stalled 研究的重试上限收紧（观察项，曾自主重试 3 次）。
5. 评估配置的对话式微调（`/evaluate` 裸命令的配置选择卡已列名字，参数微调靠主 agent 口头）。

## 6. 新会话开工建议

- 先读本文档 + `docs/redesign-interaction-orchestration.md` §5 决策记录 + `docs/how-to-extend.md`。
- 改动前先跑 `uv run pytest` 建基线；TDD（先写失败测试）。
- 加能力照 `docs/how-to-extend.md` 配方；别动 §2 铁律。
- UI 改动先改 `assemble.ts` 装配层 + vitest 用例，再写渲染器。
