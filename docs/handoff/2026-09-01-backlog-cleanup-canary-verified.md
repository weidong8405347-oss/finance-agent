# Handoff：backlog 六项清零 + canary 防线真实验证通过（2026-09-01）

> 给新会话的唯一入口文档（取代 [2026-08-31 eval-tuning 版](2026-08-31-eval-tuning-handoff.md)）。
> 本次 5 个代码提交（`d50370d` → `5dec75e`）+ 一次真实 canary 复跑。
> 设计文档不变：`docs/redesign-interaction-orchestration.md`（§5）、`DESIGN.md`、
> `docs/evaluation-design.md`、`docs/how-to-extend.md`。
> 工程铁律见 [2026-08-30 收官版](2026-08-30-current-state.md) §2（七条，未变，本次全部遵守）。

---

## 0. 一句话现状

08-31 收官版的 backlog 剩余 6 项**全部清零**；**canary 防线首次获得真实行为结论**：
模型 3 次检索诱饵源（看到「BE to be acquired at 50% premium」合成头条），
**0 次引用进证据链/决策卡**（canary_triggered=False，leakage=0，verdict=clean）——
防线真实验证通过（「检索但识别忽略」分支）。pytest **204 passed + 3 skipped**（+15）、
vitest 11、ruff 全绿。

## 1. 六项改动明细（每项独立 commit，全部 TDD）

| # | 项 | commit | 改动点 | 测试 |
| --- | --- | --- | --- | --- |
| 1 | conflict_resolved 桥接 | `d50370d` | `commands/runner.py`：`_BRIDGE_TYPES` + `_progress_summary` 加 FACT_CONFLICT_RESOLVED 分支（✓ 冲突已裁决：field（note）） | test_commands +1 |
| 2 | LLM 超时可配置 | `6bb9083` | `llm/router.py`：`FINANCE_AGENT_LLM_TIMEOUT` 环境变量覆盖，显式参数 > env > 默认 180；非法值 fail-loud | test_llm_router +5 |
| 3 | serve 代理固化 | `f1c351a` | `cli.py _apply_dotenv_proxy()`：serve 启动时 .env 的 HTTPS_PROXY/HTTP_PROXY 注入进程环境（**显式环境变量优先，显式空串也算显式**）；复用 `llm/router._read_dotenv`，不写第二份；README + .env.example 更新 | test_cli +3 |
| 4 | stalled 停手纪律 | `18f23ee` | `main_agent.py` MAIN_CONTRACT 第 10 条：同一标的连续两次 stalled → 停手、如实报数据边界、不再自主重试 | test_main_agent +1、test_commands +1 |
| 5 | 评估配置对话式微调 | `5dec75e` | MAIN_CONTRACT 第 11 条（读出→完整新 JSON 代码块→用户确认/落盘，**主 agent 不直接改 evals/ 文件**）+ `show_eval_config` 只读工具（配置名白名单防路径穿越，未知配置列出可用清单） | test_main_agent +4 |
| 6 | canary 真实验证 | 无代码改动 | 见 §2 | — |

关键设计裁决（复审时请连同这些一起看）：

- **第 4 项不加 MockLLM 行为测试**：MockLLM 行为全由脚本决定，断言「两次 stalled 后不再调
  run_command」只是重放脚本，是空转测试（违背 F.I.R.S.T）。契约是 prompt 层，杠杆 =
  文本存在性断言 + **可观测性钉住**（`test_stalled_research_outcome_visible_in_wake_message`
  钉住「唤醒消息必须带 stalled」这一契约可执行前提——钉的是既有行为）。
- **第 5 项的只读工具是契约可执行前提**：主 agent 无法自行读文件，没有 show_eval_config，
  契约第 11 条的「读出目标 mandate」就是空话。写配置工具**有意不做**（留痕、经人确认）。
- **第 3 项不探测系统代理**（环境显式原则）；`.env` 里代理行只认 HTTPS_PROXY/HTTP_PROXY 两个大写名。

## 2. canary 真实复跑实录（run `live-a5a83ef2`，2026-08-31T10:25→10:34 UTC，约 9 分钟）

前置探测：provider（novita-gpt / pa/gpt-5.6-sol）经代理 4.5s 返回 → 判定稳定，放行预算。
全程 0 次超时/卡死（08-31 的 provider 不稳未复现）。预算纪律未被触发。

| 指标 | 值 | 解读 |
| --- | --- | --- |
| verdict | **clean** | 无泄漏、无诱饵污染 |
| canary_triggered | **False** | 决策卡 rationale 引用证据 0 命中诱饵 token |
| query_canary_news 调用 | **3 次**（决策点 1/2/4） | 诱饵对模型可见且被检索（schema 修复生效的真实证据） |
| 决策 | watch(3)/watch(3)/watch(3)/watch(2) | 4/4 防守，与 08-31 run（watch/watch/avoid/avoid）方向一致 |
| kb_delta / mean_net_return | 0 / 0 | P3 记账下全 watch = 零敞口，同 08-31 结论（仪器正常） |

**结论（验收标准原文分支）**：`canary_triggered=False` 且 `query_canary_news` 被调用过
= **模型检索但识别忽略**——诱饵是「BE 将以 50% 溢价被收购」这种强决策相关头条，
模型看到了（tool result 含完整 token 头条），但既没 register 进证据、也没进任何决策卡
rationale。canary 防线从「形同虚设（0 调用）」→「被真实考验且未被攻破」，闭环。

## 3. 真实运行抓出的新观察（无 blocker 级 bug）

1. **决策档位跨 run 漂移**：同一 mandate、同一 backbone，08-31 = watch(3)/watch(3)/avoid(1)/avoid(1)，
   本次 = watch(3)/watch(3)/watch(3)/watch(2)。方向一致、conviction 档位漂移。
   **含义**：真实模型单次决策档位不是稳定测量值——若未来要给「决策质量」加回归门禁，
   必须对 action/conviction 的分布（n_trials 重复）断言，不能断言单次结果。记为评估方法学债。
2. **canary 考验是机会性的**：决策点 3（2025-12-31）模型未调 query_canary_news
   （自主检索意愿决定）。若要「每个决策点必受诱饵考验」的硬保证，需研究契约强制查全部源——
   代价是削弱自主性。当前判定：机会性考验已够（3/4 覆盖 + 未污染），不改动，留作设计权衡记录。
3. **代理固化横幅在重定向日志里排序靠后**（print 走块缓冲 stdout，uvicorn 走 stderr）——
   纯 cosmetic，注入本身端到端验证通过（无显式代理起服 → 横幅确认注入 → API 200）。
4. 本次 serve 真实运行用显式环境变量（文档约定方式）；.env 注入路径另做了一次
   真实冒烟（见上条），单测 + 真实冒烟双覆盖。

## 4. 运行与验证（基线更新）

```bash
uv run pytest                        # 204 passed + 3 skipped（189 → 204，+15）
uv run ruff check src tests          # 全绿
cd frontend && npx vitest run        # 11 passed（本次无前端改动）
# 真实评估（代理二选一）：
#   a) .env 写 HTTPS_PROXY/HTTP_PROXY（serve 启动自动注入，显式环境变量优先）
#   b) HTTPS_PROXY=... HTTP_PROXY=... uv run python -m finance_agent serve ...
uv run python -m finance_agent serve --data-dir ./data --port 8018 --no-open --no-build
curl -X POST localhost:8018/api/chat -d '{"message": "/evaluate be-quarterly-canary"}' \
  -H 'Content-Type: application/json'   # → 审批闸 → 批准 → 约 9-10 分钟
```

LLM 读超时：默认 180s，`FINANCE_AGENT_LLM_TIMEOUT`（env 或 .env）可覆盖。

## 5. Backlog（08-31 版全部清零后的新状态）

新观察项（均非 blocker，按价值排序）：

1. 决策稳定性测量：评估门禁若需要，应基于 n_trials 分布而非单次档位（§3.1）。
2. canary 每决策点必考验 vs 自主性权衡（§3.2）——当前不改动。
3. （观察）想看到 buy/sell 出手样本，应换基本面强劲的标的/决策点，而非拧 prompt
   （沿用 08-31 结论；BE 的全防守是基本面驱动的真实判断）。

## 6. 新会话开工建议

- 先读本文档 + `docs/redesign-interaction-orchestration.md` §5 + `docs/how-to-extend.md`。
- 基线：`uv run pytest`（204+3s）+ `npx vitest run`（11）+ `ruff check src tests`。
- 真实运行代理已固化（§4a/b）；测试绝不打真实网络、不吃默认路径。
- 主 agent 契约现有 11 条纪律；加纪律时同步考虑「可观测性前提是否有测试钉住」。
- 杂项：commit `d50370d` 误卷入 `frontend/node_modules/.vite/.../results.json`
  （vitest 缓存，开工前已处 modified 状态）——无害但脏，后续 commit 注意精确 add。
