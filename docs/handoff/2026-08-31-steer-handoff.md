# Handoff：steer 落地（运行中改方向注入子 run，Q6 后置项）（2026-08-31 深夜）

> **已被 [2026-08-31 评估调优版](2026-08-31-eval-tuning-handoff.md)取代**（决策保守度
> 根因修复 + canary 真实复跑，基线更新为 189+3s / vitest 11）。

> 给新会话的唯一入口文档（取代 [2026-08-31 冲突裁决版](2026-08-31-conflict-resolve-ui.md)，
> 历史见 git log：`c759991 feat(steer)`——本次唯一代码提交）。
> 设计文档不变：`docs/redesign-interaction-orchestration.md`（§5 决策记录）、
> `DESIGN.md`、`docs/evaluation-design.md`、`docs/how-to-extend.md`。
> 工程铁律见 [2026-08-30 收官版](2026-08-30-current-state.md) §2（七条，未变）。

---

## 0. 一句话现状

backlog #3 完成：**steer（Q6 后置项）**——command 运行中用户改方向，
确定性注入当前 step 的子 run（`context/inject`，kernel 下一次模型调用重投影即见），
后续 step 启动时继承；`/steer` slash 入口 + `POST /api/sessions/{id}/steer` 端点 +
主 agent `steer_command` 工具（自然语言路径）+ UI 注入确认卡。
TDD 全程（9 pytest + 2 vitest 先红后绿）。pytest **185 passed + 3 skipped** +
vitest **11** + ruff 全绿 + build_orchestrator 真实装配冒烟通过。

## 1. steer 语义（与 stop 对偶；本次会话定稿的实现裁决）

| | stop（Q6 R1 已有） | steer（本次） |
| --- | --- | --- |
| 机制 | cancel 标记，轮次/步骤边界安全停 | `context/inject` 落当前子 run，下一次模型调用即见 |
| 事件 | 无独立类型（step 返回 cancelled） | `steer/requested`（会话流）+ `step_agent/progress`（🧭 改向行） |
| 生效时机 | 边界 | 立即（kernel 每次模型调用都从 store 重投影 derive_messages——同 turn 工具循环内也生效） |
| 继承 | 无 | 后续每个 step 启动时继承该 command 的**全部**改向（方向修正对剩余 pipeline 生效） |
| 目标 | 最近一个活跃（可指定 id） | **全部**活跃 command（Q9 并行研究都遵循；可 command_id 定向） |

**为什么用 `context/inject` 而非新事件类型**：它在 `MODEL_VISIBLE_TYPES` 白名单里
（白名单不加新类型，铁律 1 不动）；注入内容带 `[用户改方向]` 前缀。
**为什么默认全部而非最近一个**：stop 是「停掉这个」（单数、破坏性）；steer 是
「对当前工作的方向修正」，多 command 并行（Q9）时都应遵循。

## 2. 本次代码改动（TDD）

1. **`CommandRunner.steer(session_run_id, message, command_id=None)`**（`commands/runner.py`）：
   - 当前子 run 立即注入 + 登记到 `_steers[command_id]`（累计，append-only）；
   - `_execute` 每个 step 启动时（`STEP_AGENT_START` 后、跑 step 前）把累计改向注入新子 run；
     `_current_child[command_id]` 跟踪当前子 run（锁保护，step 结束清除）；
   - command 终态时 `_steers`/`_current_child` 清理；wake 摘要追加
     `（用户中途改向 ×N：最新「…」）`——主 agent 汇报能解释研究方向变化；
   - step 切换窗口到达的 steer → `delivered=false`，由下一 step 继承（不丢失）。
2. **API**（`api/app.py`）：
   - `POST /api/sessions/{run_id}/steer` `{message, command_id?}`（镜像 /stop；无 runner → `{"steered": []}`；空消息 422）；
   - `/api/chat` 的 `/steer <内容>` 分支：**会话级控制动作，非 pipeline command**
     （不进 `COMMANDS`，parse_command 之前拦截首词）；无活跃 → 对话式警告；
     裸 `/steer` → 用法提示；不派发 command、不起主 agent turn。
3. **主 agent**（`main_agent.py`）：`steer_command` 工具 + schema + 契约第 8 条更新
   （自然语言路径：command 运行中用户说「换个重点」→ 主 agent 转述注入）。
4. **前端**（照 how-to-extend 配方 5）：
   - `assemble.ts`：`steer/requested` → `steer` 节点（message/commandId/delivered）；
   - `nodes.tsx` SteerNode：天蓝确认卡「🧭 改向已注入」+ 生效时机说明；
   - Composer 补全菜单加本地 `/steer` 条目（非 command，不进 `/api/commands` 协议）。
5. UI 的 CommandCard 进度：改向行经 `step_agent/progress`（step="steer"）落在当前
   step 的进度列表（bridge 语义复用，`_BRIDGE_TYPES` 无需加 CONTEXT_INJECT——那会把
   system 契约也桥出去；steer 的父流进度由 runner 直接落）。

## 3. 运行与验证（基线更新）

```bash
uv run pytest                        # 185 passed + 3 skipped（网络冒烟默认跳过）
uv run ruff check src tests          # 全绿（docs/demo/gen_demo_archives.py 的 19 个预存 E501 不动）
cd frontend && npx vitest run        # 11 passed
uv run python -m finance_agent serve # 自动构建前端 + 开浏览器（:8000）
```

- 新增测试 11 个：runner 5（同 turn 注入即见 / 后续 step 继承且每子 run 恰一条 /
  定向注入 / 无活跃 / wake 摘要带改向）+ API 3（slash 注入 / 端点镜像 / 无活跃+用法）+
  主 agent 1（steer_command 工具）+ vitest 2（steer 节点）。
- 关键测试技法：`GatedLLM` 第一次模型调用挂起（返回带 tool_call 保证同 turn 有第二次
  调用），steer 在挂起期注入，断言第二次调用收到的 messages 含 `[用户改方向]`。
- 真实行情/评估运行的代理注意事项不变（08-31 行情源版 §1：`HTTPS_PROXY=http://127.0.0.1:7897`）。

## 4. 铁律自查（本次改动对照）

- 铁律 1：`steer/requested` 不进 `MODEL_VISIBLE_TYPES`；注入用既有白名单类型
  `context/inject`；主 agent 经 wake 摘要获知改向 ✓；
- 铁律 3/6：不涉知识库写入与 eval ✓（steer 只作用于 live 编排层）；
- 铁律 5：测试全离线（MockLLM/GatedLLM），serve 与测试共用装配 ✓；
- 铁律 7：新测试显式 tmp_path ✓。

## 5. Backlog（更新）

1. ~~行情源解锁 + /evaluate 真实首跑~~ ✅（08-31 行情源版）。
2. ~~冲突人工裁决 UI~~ ✅（08-31 冲突裁决版）。
3. ~~steer~~ ✅ 本次完成。
4. **决策保守度调优**：首跑 kb_delta=0（全 watch 零敞口）——研究 rubric/decision
   prompt 的出手阈值值得迭代；`canary: true` 打开复跑验证诱饵防线。（下一个建议任务：
   纯 prompt/rubric 层，无需网络；若要真实复跑评估则需代理）
5. 主 agent 对 stalled 研究的重试上限收紧（观察项）。
6. 评估配置的对话式微调（参数微调靠主 agent 口头）。
7. serve 的代理感知：`.env`/启动脚本固化 `HTTPS_PROXY`（勿在代码里探测系统代理）。
8. （小）`fact/conflict_resolved` 不进 `_BRIDGE_TYPES`——研究 agent 轮内自动裁决在
   对话流只有冲突 raised 的进度行，无 resolved 回显；如需可见可加桥接。
9. （新，观察项）steer 的真实 LLM 验证：本次注入机制由 GatedLLM 验证，值得在真实
   provider 下跑一次 /research + 中途 /steer，观察研究方向的响应质量。

## 6. 新会话开工建议

- 先读本文档 + `docs/redesign-interaction-orchestration.md` §5 + `docs/how-to-extend.md`。
- 改动前 `uv run pytest`（185+3s）+ `npx vitest run`（11）+ `ruff check src tests` 建基线。
- TDD 先写失败测试；UI 改动先 assemble.ts + vitest 再渲染器。
- gitignore 教训仍有效：加条目用 `/dir/` 锚定根目录或先 `git check-ignore -v` 验证。
