"""ReplayEngine：walk-forward 时点回放（S4 核心，评估对齐稿 §2.3）。

每个决策点 T：
1. （可选）增量研究：eval 命名空间 + 时间锁网关 + LeakageAuditHook（D7）；
2. 决策：DecisionLoop（eval manifest，created_at == T，叠加档案视图）；
3. 对照：LLM-only 裸投（无档案无工具）+ B&H（同窗口同成本）；
4. 对账：前瞻收益（PriceBook，评估器专用，agent 不可见）+ 强制成本模型。

硬门禁：本 run 的 leakage/attempt 事件 > 0 → verdict=contaminated，整批作废。
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, time
from pathlib import Path

from ..decision.loop import DecisionLoop
from ..decision.service import DecisionService
from ..eventstore.events import LEAKAGE_ATTEMPT, Event
from ..eventstore.store import EventStore
from ..gateway.gateway import DataGateway
from ..harness.manifest import RunManifest, RunMode
from ..knowledge.store import BitemporalStore
from ..knowledge.writer import ProfileWriter
from ..llm.base import LLM
from ..loop.hooks import LeakageAuditHook, LeakageDetected
from ..loop.kernel import AgentKernel
from ..research.loop import ResearchLoop
from .config import EvalConfig
from .prices import PriceBook
from .report import Aggregate, DecisionOutcome, EvalReport
from .stats import deflated_sharpe, max_drawdown, prob_sharpe, sharpe

EVAL_REPORT = "eval/report"
BASELINE_VOTE = "eval/baseline_vote"

GatewayFactory = Callable[[datetime, str], DataGateway]


class ReplayEngine:
    def __init__(
        self,
        *,
        kb: BitemporalStore,
        events: EventStore,
        decision_service: DecisionService,
        llm_agent: LLM,
        llm_baseline: LLM,
        price_book: PriceBook,
        artifacts_dir: str | Path,
        gateway_factory: GatewayFactory | None = None,
    ):
        self._kb = kb
        self._events = events
        self._svc = decision_service
        self._llm_agent = llm_agent
        self._llm_baseline = llm_baseline
        self._prices = price_book
        self._artifacts = Path(artifacts_dir)
        self._gateway_factory = gateway_factory

    def run(self, config: EvalConfig) -> EvalReport:
        eval_run_id = f"eval-{uuid.uuid4().hex[:8]}"
        namespace = f"eval:{eval_run_id}"
        outcomes: list[DecisionOutcome] = []

        for point in config.decision_points:
            # 决策时刻 = T 日收盘后（当日全天信息可知）
            t_dt = datetime.combine(point, time(23, 59, 59), tzinfo=UTC)
            manifest = RunManifest(
                run_id=eval_run_id,
                mode=RunMode.EVAL,
                eval_as_of=t_dt,
                backbone_model=config.backbone_model,
                backbone_cutoff=config.backbone_cutoff,
                allow_pit_b=config.allow_pit_b,
            )
            zone = manifest.cutoff_zone(t_dt).value
            for ticker in config.tickers:
                outcomes.append(
                    self._replay_one(config, manifest, namespace, ticker, point, t_dt, zone)
                )

        leakage_events = len(self._events.read(eval_run_id, types={LEAKAGE_ATTEMPT}))
        report = EvalReport(
            eval_run_id=eval_run_id,
            config_name=config.name,
            config_hash=config.config_hash(),
            verdict="contaminated" if leakage_events > 0 else "clean",
            leakage_events=leakage_events,
            outcomes=outcomes,
            aggregate=self._aggregate(config, outcomes),
        )
        self._persist(config, report)
        return report

    # ---------------- 单点回放 ----------------

    def _replay_one(
        self,
        config: EvalConfig,
        manifest: RunManifest,
        namespace: str,
        ticker: str,
        point: date,
        t_dt: datetime,
        zone: str,
    ) -> DecisionOutcome:
        if config.incremental_research:
            self._incremental_research(config, manifest, namespace, ticker, t_dt)

        # agent 决策
        card_id: str | None = None
        card = None
        try:
            loop = DecisionLoop(
                kb=self._kb,
                events=self._events,
                decision_service=self._svc,
                llm=self._llm_agent,
                manifest=manifest,
                namespace=namespace,
                hooks=[LeakageAuditHook(event_sink=self._events)],
            )
            card_id = loop.run("stock", ticker, now=t_dt)
        except LeakageDetected:
            card_id = None
        if card_id:
            card = self._svc.decisions.get(card_id)

        # LLM-only 对照（无档案无数据工具，裸模型先验）
        baseline_action = self._baseline_vote(manifest, ticker)

        # 对账（评估器视角，可见未来行情）
        fwd = self._prices.forward_return(ticker, point, horizon_months=config.horizon_months)
        if fwd is None:
            return DecisionOutcome(
                point=point, ticker=ticker, zone=zone, card_id=card_id,
                action=card.action.value if card else "none", incomplete=True,
            )
        (_ed, _ep), (_xd, _xp), gross = fwd

        sizing = card.position.sizing_pct if card and card.position else 0.0
        if card and card.action.value == "buy":
            net = config.cost.net_return(gross, sizing_pct=sizing)
            unit_net = gross - config.cost.round_trip_cost_rate()
        else:
            # sell/avoid/watch/hold/未出卡：P3 按零敞口记账（做空记账为 P4 增强）
            net, unit_net = 0.0, 0.0
        bh_net = config.cost.net_return(gross, sizing_pct=1.0)
        llm_unit = (
            gross - config.cost.round_trip_cost_rate() if baseline_action == "buy" else 0.0
        )
        return DecisionOutcome(
            point=point,
            ticker=ticker,
            zone=zone,
            card_id=card_id,
            action=card.action.value if card else "none",
            sizing_pct=sizing,
            gross_return=gross,
            net_return=net,
            unit_net_return=unit_net,
            baseline_bh_net_return=bh_net,
            baseline_llm_only_action=baseline_action,
            baseline_llm_only_unit_net=llm_unit,
        )

    def _incremental_research(
        self, config: EvalConfig, manifest: RunManifest, namespace: str, ticker: str, t_dt: datetime
    ) -> None:
        gateway = (
            self._gateway_factory(t_dt, manifest.run_id)
            if self._gateway_factory
            else DataGateway(
                mode="eval", eval_as_of=t_dt, events=self._events, run_id=manifest.run_id
            )
        )
        loop = ResearchLoop(
            store=self._kb,
            events=self._events,
            writer=ProfileWriter(store=self._kb, events=self._events),
            gateway=gateway,
            llm=self._llm_agent,
            manifest=manifest,
            max_rounds=2,
            namespace=namespace,
            gateway_sources=gateway.source_ids(),
            hooks=[LeakageAuditHook(event_sink=self._events)],
        )
        with contextlib.suppress(LeakageDetected):
            # 泄漏事件已落库，报告的硬门禁会判定
            loop.run("stock", ticker, objective=f"as_of {t_dt.date()} 增量研究", now=t_dt)

    def _baseline_vote(self, manifest: RunManifest, ticker: str) -> str:
        """LLM-only 对照：无档案、无数据工具，只有结构化投票出口。"""
        vote: dict[str, str] = {}

        def cast_vote(args: dict) -> dict:
            vote["action"] = str(args.get("action", "hold"))
            return {"content": "ok", "provenance": []}

        kernel = AgentKernel(
            store=self._events,
            llm=self._llm_baseline,
            manifest=manifest,
            tools={"cast_vote": cast_vote},
            max_steps=4,
        )
        kernel.run_turn(
            f"在完全没有任何档案与数据的情况下，凭你的先验对 {ticker} 投票：buy/hold/avoid。"
        )
        self._events.append(
            Event(
                run_id=manifest.run_id,
                type=BASELINE_VOTE,
                payload={"ticker": ticker, "action": vote.get("action", "none")},
            )
        )
        return vote.get("action", "none")

    # ---------------- 聚合与落盘 ----------------

    def _aggregate(self, config: EvalConfig, outcomes: list[DecisionOutcome]) -> Aggregate:
        done = [o for o in outcomes if not o.incomplete]
        point_nets = [o.net_return for o in done]
        point_units = [o.unit_net_return for o in done]
        n = len(done)

        buys = [o for o in done if o.action == "buy"]
        hit_rate = (
            sum(1 for o in buys if o.gross_return > 0) / len(buys) if buys else 0.0
        )

        # equity 曲线（逐点复利，仅用于 MDD）
        equity, curve = 1.0, []
        for r in point_nets:
            equity *= 1 + r
            curve.append(equity)

        periods_per_year = 12.0 / config.horizon_months
        point_sharpe = sharpe(point_nets, periods_per_year=periods_per_year)
        unit_sharpe = sharpe(point_units, periods_per_year=1.0)  # PSR/DSR 用每点（非年化）口径

        mean_net = sum(point_nets) / n if n else 0.0
        mean_unit = sum(point_units) / n if n else 0.0
        mean_bh = sum(o.baseline_bh_net_return for o in done) / n if n else 0.0
        llm_only_mean = sum(o.baseline_llm_only_unit_net for o in done) / n if n else 0.0

        # dev/holdout 时间序切分 → generalization gap
        split = max(1, int(len(done) * config.dev_fraction))
        dev, holdout = done[:split], done[split:]
        dev_mean = sum(o.net_return for o in dev) / len(dev) if dev else 0.0
        hold_mean = sum(o.net_return for o in holdout) / len(holdout) if holdout else 0.0

        return Aggregate(
            n_points=len(outcomes),
            n_complete=n,
            mean_net_return=mean_net,
            hit_rate=hit_rate,
            excess_vs_bh=mean_unit - mean_bh,
            llm_only_mean_net_return=llm_only_mean,
            kb_delta=mean_unit - llm_only_mean,
            sharpe=point_sharpe,
            max_drawdown=max_drawdown(curve),
            psr=prob_sharpe(unit_sharpe, benchmark_sr=0.0, returns=point_units),
            dsr=deflated_sharpe(unit_sharpe, returns=point_units, n_trials=config.n_trials),
            dev_mean_net=dev_mean,
            holdout_mean_net=hold_mean,
            generalization_gap=dev_mean - hold_mean,
        )

    def _persist(self, config: EvalConfig, report: EvalReport) -> None:
        out_dir = self._artifacts / report.eval_run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "config.json").write_text(config.model_dump_json(indent=2))
        (out_dir / "report.json").write_text(report.model_dump_json(indent=2))
        self._events.append(
            Event(
                run_id=report.eval_run_id,
                type=EVAL_REPORT,
                payload={
                    "verdict": report.verdict,
                    "leakage_events": report.leakage_events,
                    "config_hash": report.config_hash,
                    "mean_net_return": report.aggregate.mean_net_return,
                    "kb_delta": report.aggregate.kb_delta,
                    "report_path": str(out_dir / "report.json"),
                },
            )
        )

