# Handoff：决策保守度调优——根因修复 + canary 真实复跑实录（2026-08-31 收官）

> ⚠️ **本文档已被 [2026-09-01 backlog 清零 + canary 验证版](2026-09-01-backlog-cleanup-canary-verified.md) 取代**，仅作历史留痕。本文 §5 backlog 的 6 个剩余项已全部完成，canary 防线已获真实行为结论（verdict=clean，诱饵被检索 3 次但 0 引用）。

> 给新会话的唯一入口文档（取代 [2026-08-31 steer 版](2026-08-31-steer-handoff.md)，
> 历史见 git log：`2f4c60d fix(eval)` → `1c150f6 fix(eval)`——本次两个代码提交）。
> 设计文档不变：`docs/redesign-interaction-orchestration.md`（§5 决策记录）、
> `DESIGN.md`、`docs/evaluation-design.md`、`docs/how-to-extend.md`。
> 工程铁律见 [2026-08-30 收官版](2026-08-30-current-state.md) §2（七条，未变）。

---

## 0. 一句话现状

backlog #4 完成工程侧：**kb_delta=0 的真实根因不是 prompt 保守，是 eval 回放缺
`fetch_document`（研究只能看 filing 索引看不到正文）**——修复后真实复跑决策立刻分化
（watch(3)→watch(3)→avoid(1)→avoid(1)，档案 valuation 9 条证据/risks 8 条）；
**canary 防线真实复跑发现形同虚设**（诱饵源无 schema → 模型 0 次调用）已修复；
LLM 读超时 60s→180s（180s 下 provider 仍两次卡死——非超时配置问题，是 novita/pa
当时段不稳定）。pytest **189 passed + 3 skipped** + vitest 11 + ruff 全绿。

## 1. 根因分析（从 events.db 决策轨迹取证，非猜测）

首跑全 watch 的推理原文：「档案缺少现金流、可比公司和估值倍数…不足以支持买入」，
研究轮自述：「已定位 10-Q，**但当前工具无法读取申报正文**」。
→ `ReplayEngine._incremental_research` 构造 ResearchLoop 时漏传 `fetch_document`
（生产路径 steps.py 有，eval 路径没有）→ `read_edgar_filing` 工具不存在 →
档案只有 filing 索引 → 关键字段永远缺失 → 决策只能 watch。

**修复**：ReplayEngine 加 `fetch_document` 透传（eval 可用——filing 正文不可变，
chunk 的 available_at 从时间锁查询出的 filing 记录继承，PIT 安全同生产路径）；
cli 装配传 `fetch_filing_text`。**prompt 调优居其次**：证据不足时 watch 本来就是正确行为。

## 2. 本次代码改动（TDD，4 个新测试）

1. **eval 研究读正文能力**（`evaluation/replay.py` + `cli.py`）——回归测试
   `test_eval_research_can_read_filing_body`（fixture 网关 + fetch_document →
   证据落 eval 命名空间 → 决策出手）。
2. **出手阈值可读化**（`decision/loop.py` DECISION_CONTRACT 第 4 条）：
   conviction ≥4 且 ≥2 条一手证据 → buy/sell（必带仓位失效条件）；2-3 或证据链不完整
   → watch；1 或恶化 → avoid；档案缺关键维度不得报高确信。**全 watch 成为可识别、
   可审计的显式状态**。真实复跑确认模型严格遵循（3→watch，1→avoid）。
3. **canary 诱饵模型可见**（`gateway/tools.py`）：`query_canary_news` 补 schema
   （常规新闻源描述 + ticker 参数）。**诱饵必须与真源不可区分**（模型侧）；
   操作员侧 capability 描述保持「合成诱饵源」诚实标注（能力页）。回归测试断言两侧。
4. **LLM 读超时 60s→180s**（`llm/router.py`）：大上下文研究轮流式生成超 60s 会把
   10 分钟评估打死在半途（失败成本不对称）。**不加自动重试**：read timeout 时服务端
   可能仍在生成，重试双倍计费。

## 3. 真实复跑实录（canary: true，三次运行）

配置 `data/evals/mandates/be-quarterly-canary.json`（= be-quarterly-first + canary:true）。

| run | 结果 | 发现 |
| --- | --- | --- |
| f8dceca9（schema 修复前） | ✅ 完成，verdict=clean | **fetch_document 修复生效**：决策分化 watch→watch→avoid→avoid，rationale 6-8 条证据；但 canary 源 0 次调用（模型看不见诱饵）→ 暴露 schema 缺陷 |
| 7e954a06（schema+180s 后） | ❌ provider 卡死 | canary 被调用 2 次（诱饵已被考验），但第 2 决策点研究轮 provider 无响应 >180s |
| （另一次 1915b3f4） | ❌ 同上 | 判定 provider 当时段不稳定，停手不空耗预算 |

**kb_delta 仍为 0 的解读**：P3 记账下 watch/avoid 都是零敞口；模型对 BE 的结论
（净亏损 + 可转债稀释 + 政策依赖 → avoid）是**基本面驱动的真实判断**（B&H +282%/+37%/+128%
是事后视角，系统当时不知道也不该知道）。仪器已修通：档案丰富、决策分化、证据链可审计；
「从不出手」从测量故障变成了策略结论。**下一步若想看出手，应换标的/换决策点**（如基本面
强劲的票），而不是继续拧 prompt——那会逼模型在证据不足时出手，违背证据纪律。

**canary 防线验证状态**：机制层有单测覆盖（引用诱饵→contaminated）；真实行为层面
诱饵已被检索（2 次调用）但运行被 provider 中断，「模型是否引用诱饵」尚无真实结论——
provider 稳定后重跑 `/evaluate be-quarterly-canary` 即可（断点不续，新 run 全新跑）。

## 4. 运行与验证（基线更新）

```bash
uv run pytest                        # 189 passed + 3 skipped
uv run ruff check src tests          # 全绿
cd frontend && npx vitest run        # 11 passed（本次无前端改动）
# 真实评估（需代理）：
HTTPS_PROXY=http://127.0.0.1:7897 HTTP_PROXY=http://127.0.0.1:7897 \
  uv run python -m finance_agent serve --data-dir ./data --port 8018 --no-open --no-build
curl -X POST localhost:8018/api/chat -d '{"message": "/evaluate be-quarterly-canary"}' \
  -H 'Content-Type: application/json'   # → 审批闸 → 批准 → 约 10 分钟
```

## 5. Backlog（更新）

1. ~~行情源解锁 + /evaluate 真实首跑~~ ✅
2. ~~冲突人工裁决 UI~~ ✅
3. ~~steer~~ ✅
4. ~~决策保守度调优~~ ✅ 本次（工程侧完成；真实复跑结论：仪器修通，avoid 是策略判断）
5. **canary 真实引用验证**（待 provider 稳定重跑一次；机制已修复+单测覆盖）
6. 主 agent 对 stalled 研究的重试上限收紧（观察项）
7. 评估配置的对话式微调
8. serve 代理感知固化（`.env`/启动脚本；勿在代码探测系统代理）
9. （小）`fact/conflict_resolved` 桥接进 `_BRIDGE_TYPES`（对话流 resolved 回显）
10. （观察）LLM 读超时是否需做成可配置（当前 180s 默认；provider 卡死是另一回事）

## 6. 新会话开工建议

- 先读本文档 + `docs/redesign-interaction-orchestration.md` §5 + `docs/how-to-extend.md`。
- 基线：`uv run pytest`（189+3s）+ `npx vitest run`（11）+ `ruff check src tests`。
- 真实评估/行情运行需代理（§4）；测试绝不打真实网络。
- provider 卡死时段不要连烧真实评估（预算纪律：连续两次超时即停手，判定外部不稳定）。
