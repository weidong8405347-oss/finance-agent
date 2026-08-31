# Handoff：行情源解锁 + /evaluate 真实首跑（2026-08-31）

> 给新会话的唯一入口文档（取代 2026-08-30 版，历史见 git log：
> `7652a27 feat(prices): 行情源可用性容错`——本次唯一代码提交）。
> 设计文档不变：`docs/redesign-interaction-orchestration.md`（§5 决策记录）、
> `DESIGN.md`、`docs/evaluation-design.md`、`docs/how-to-extend.md`。

---

## 0. 一句话现状

backlog #1 完成：**行情能力在本网络真实解锁（yfinance 经系统代理）**，
**/evaluate 走完整产品路径（含真实审批闸）完成首次真实回放**——
BE 4 个季度决策点、40 次 LLM 调用、verdict=clean、零泄漏、eval 命名空间隔离验证通过。
pytest **173 passed + 3 skipped**（网络冒烟门控）+ vitest 5 + ruff 全绿。

## 1. 本机网络实况（与 08-30 handoff 认知的关键差异）

| 依赖 | 状态 | 说明 |
| --- | --- | --- |
| PyPI | ✅ 慢但通 | `uv sync --extra data --extra dev` 可用（需耐心） |
| EDGAR | ✅ 直连 | |
| LLM（novita/dashscope） | ✅ 直连 | 复用 `~/.pi/agent/`（research=pa/gpt-5.6-sol，fast=kimi-k3） |
| **stooq** | ❌ **全局反爬** | CSV 端点已启用 JS PoW 挑战（直连 200 挑战页、经代理 404）。**换网络也不通，不做绕过**——降级为回退链首跳（空结果优雅降级） |
| **yfinance/Yahoo** | ⚠️ **需走代理** | 本机有系统代理 `127.0.0.1:7897`（macOS 系统级），但 Python/httpx/yfinance **不自动用系统代理**——shell 需显式 `HTTPS_PROXY=http://127.0.0.1:7897`（httpx trust_env）。直连则 Yahoo 403 |

**结论：任何要行情的真实运行（serve/research/evaluate）都要带代理环境变量启动。**

## 2. 本次代码改动（TDD，7 个新单测全绿）

1. **`YFinancePricesAdapter` 兼容 yfinance ≥1.7**：`yf.download` 返回 `(Price, Ticker)`
   双层列 → `_flatten_columns` 按 Ticker 层降层；NaN 行跳过；空下载 → `[]`
   （骨架首次真跑即暴露的解析崩溃）。
2. **`StooqPricesAdapter` 优雅降级**：HTTP 层故障（404/限流/网络）→ `[]` 不抛错
   （免费源可用性漂移是常态；硬失败门禁上移到对账层）。
3. **`DataGateway.query_any(sources, request)`**：按序回退原语（空/异常/未注册 → 下一源）。
4. **`PriceBook.from_gateway(require=True)`**：eval 对账行情按回退次序装配；任一标的
   全源无数据 → `PriceDataUnavailableError` 显式失败（铁律 4——空 PriceBook 会把
   整轮 LLM 预算烧成全 incomplete）。
5. **`PRICE_SOURCE_ORDER = ("prices_stooq", "prices")`**（`gateway/adapters/prices.py`）：
   档案图表（`runner._maybe_archive_profile`）与 eval 对账（`cli.eval_runner`）共用。
6. 网络冒烟（`FINANCE_AGENT_NETWORK=1` 门控，默认跳过）：EDGAR 直连 + yfinance（需
   `HTTPS_PROXY`）+ stooq 降级姿态。

铁律未动（§2 七条全部保持；本次只加了源选择容错，PIT/单写者/白名单/装配路径零改动）。

## 3. /evaluate 真实首跑实录（可复现）

```bash
mkdir -p data/evals/mandates && cat > data/evals/mandates/be-quarterly-first.json <<'EOF'
{ "name": "be-quarterly-first", "tickers": ["BE"],
  "decision_points": ["2025-06-30","2025-09-30","2025-12-31","2026-03-31"],
  "horizon_months": 3, "backbone_model": "pa/gpt-5.6-sol",
  "backbone_cutoff": "2025-06-30",
  "cost": {"commission_bps": 5.0, "slippage_bps": 10.0},
  "incremental_research": true, "n_trials": 5, "dev_fraction": 0.7,
  "allow_pit_b": false, "canary": false, "counterfactual": false, "is_holdout": false }
EOF
HTTPS_PROXY=http://127.0.0.1:7897 HTTP_PROXY=http://127.0.0.1:7897 \
  uv run python -m finance_agent serve --data-dir ./data --port 8017 --no-open --no-build
# 另一终端：
curl -X POST localhost:8017/api/chat -H 'Content-Type: application/json' \
     -d '{"message": "/evaluate be-quarterly-first"}'
curl localhost:8017/api/approvals/pending   # 拿 approval_id（审批闸！）
curl -X POST localhost:8017/api/approvals/<id> -H 'Content-Type: application/json' -d '{"approved": true}'
```

**结果**（`data/evals/live-17ce459e--cmd-cea3e431-1-evaluate/report.json`）：

- 4/4 决策点全部完成对账（PriceBook 由 yfinance 回退供数——直连 stooq 空结果现场验证了回退链）
- verdict=**clean**，leakage_events=0，canary 未触发；污染区/诚实区分区正确（cutoff 2025-06-30）
- 系统 4 点全 WATCH（零敞口 net=0）；LLM-only 基线也全弃权；B&H +282%/−3%/+37%/+128%
- 首跑即诚实信号：**决策策略极保守**（BE 高波动 + 弱基本面下 watch 是合理姿态，但
  kb_delta=0 说明档案没有转化为出手——这是下一轮调优的明确方向）
- 40 次 LLM 调用 / 1.7M tokens / 约 10 分钟
- 隔离验证：prod decisions=0、prod facts 10 条原封未动；eval 命名空间 4 卡 + 4 事实 ✓

## 4. 运行与验证

```bash
uv run pytest                        # 173 passed + 3 skipped（网络冒烟默认跳过）
FINANCE_AGENT_NETWORK=1 HTTPS_PROXY=http://127.0.0.1:7897 \
  uv run pytest tests/integration/test_real_data_smoke.py   # 3 passed（真实数据源）
cd frontend && npx vitest run        # 5 passed
uv run python -m finance_agent serve # 自动构建前端 + 开浏览器（:8000）
```

## 5. Backlog（更新）

1. ~~行情源解锁 + /evaluate 真实首跑~~ ✅ 本次完成。
2. **冲突人工裁决 UI**：详情页「以此版本为准」按钮（后端 resolve_conflict 已就绪）——下一个建议任务。
3. **steer**（运行中改方向注入子 run；Q6 后置项）。
4. 主 agent 对 stalled 研究的重试上限收紧（观察项）。
5. 评估配置的对话式微调（参数微调靠主 agent 口头）。
6. （新）**决策保守度调优**：首跑暴露 kb_delta=0（全 watch 零敞口）——研究 rubric/decision
   prompt 的出手阈值值得一轮迭代；以及把 `canary: true` 打开复跑一次验证诱饵防线。
7. （新）serve 的代理感知：当前需手工带 `HTTPS_PROXY` 启动；可考虑 `.env`/启动脚本固化
   （勿在代码里探测系统代理——保持环境显式）。

## 6. 新会话开工建议

- 先读本文档 + `docs/redesign-interaction-orchestration.md` §5 + `docs/how-to-extend.md`。
- 改动前 `uv run pytest` 建基线（173+3s）；TDD 先写失败测试。
- 真实行情/评估运行记得代理环境变量（§1）；测试绝不打真实网络。
- stooq 反爬若未来解除，`PRICE_SOURCE_ORDER` 无需改动（回退链自动恢复首选）。
