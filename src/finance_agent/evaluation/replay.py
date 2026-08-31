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
import json
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
from .canary import canary_record, detect_canary_in_evidence, make_canary_adapter
from .config import EvalConfig
from .counterfactual import (
    CounterfactualProbe,
    CounterfactualResult,
    DropField,
    ScaleField,
)
from .holdout import HoldoutLedger
from .prices import PriceBook
from .report import Aggregate, CounterfactualSummary, DecisionOutcome, EvalReport
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
        fetch_document: Callable[[str], str] | None = None,
        # fetch_document：申报正文抓取。eval 也可用——filing 正文不可变（amendment 是独立 filing），
        # 且 chunk 的 available_at 从「时间锁网关查询出的 filing 记录」继承（PIT 安全同生产路径）。
    ):
        self._kb = kb
        self._events = events
        self._svc = decision_service
        self._llm_agent = llm_agent
        self._llm_baseline = llm_baseline
        self._prices = price_book
        self._artifacts = Path(artifacts_dir)
        self._gateway_factory = gateway_factory
        self._fetch_document = fetch_document

    def run(self, config: EvalConfig, *, eval_run_id: str | None = None) -> EvalReport:
        eval_run_id = eval_run_id or f"eval-{uuid.uuid4().hex[:8]}"
        namespace = f"eval:{eval_run_id}"

        # holdout：预算制，fail-closed（评估对齐稿 §2.5）
        ledger: HoldoutLedger | None = None
        if config.is_holdout:
            ledger = HoldoutLedger(self._artifacts / "holdout_ledger.json")
            ledger.assert_allowed(config.name, budget=config.holdout_budget)

        # 断点恢复：加载已完成的 (point, ticker)
        checkpoint_path = self._artifacts / eval_run_id / "checkpoint.json"
        completed: set[str] = set()
        outcomes: list[DecisionOutcome] = []
        if checkpoint_path.exists():
            saved = json.loads(checkpoint_path.read_text())
            completed = set(saved.get("completed", []))
            outcomes = [DecisionOutcome(**o) for o in saved.get("outcomes", [])]

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
                key = f"{point.isoformat()}|{ticker}"
                if key in completed:
                    continue
                outcomes.append(
                    self._replay_one(config, manifest, namespace, ticker, point, t_dt, zone)
                )
                completed.add(key)
                self._save_checkpoint(checkpoint_path, completed, outcomes)

        outcomes.sort(key=lambda o: (o.point, o.ticker))

        # 反事实扰动（D9/P4）：对每个已出卡决策点重放扰动决策
        cf_results: list[CounterfactualResult] = []
        if config.counterfactual:
            for o in outcomes:
                if o.incomplete or not o.card_id:
                    continue
                r = self._run_counterfactual(config, eval_run_id, namespace, o)
                if r is not None:
                    cf_results.append(r)

        leakage_events = len(self._events.read(eval_run_id, types={LEAKAGE_ATTEMPT}))
        canary_triggered = self._canary_check(config, namespace)
        verdict = "contaminated" if (leakage_events > 0 or canary_triggered) else "clean"
        report = EvalReport(
            eval_run_id=eval_run_id,
            config_name=config.name,
            config_hash=config.config_hash(),
            verdict=verdict,
            leakage_events=leakage_events,
            canary_triggered=canary_triggered,
            counterfactual=_mean_cf(cf_results),
            outcomes=outcomes,
            aggregate=self._aggregate(config, outcomes),
        )
        self._persist(config, report)
        if ledger is not None:
            ledger.consume(config.name, budget=config.holdout_budget)
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
        if config.canary:
            adapter = make_canary_adapter()
            adapter.records = [canary_record(ticker, t_dt)]  # 按决策点生成诱饵
            gateway.register(adapter)

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
            fetch_document=self._fetch_document,
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

    @staticmethod
    def _save_checkpoint(path: Path, completed: set[str], outcomes: list[DecisionOutcome]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "completed": sorted(completed),
                    "outcomes": [o.model_dump(mode="json") for o in outcomes],
                },
                ensure_ascii=False,
            )
        )
        tmp.replace(path)  # 原子写

    def _canary_check(self, config: EvalConfig, namespace: str) -> bool:
        """扫描本 run 决策卡 rationale 引用的证据，命中诱饵 token 即污染。"""
        if not config.canary:
            return False
        cards = self._svc.decisions.list(namespace=namespace)
        evidence_ids = [eid for card in cards for eid in card.rationale]
        return detect_canary_in_evidence(self._kb, evidence_ids)

    def _run_counterfactual(
        self, config: EvalConfig, eval_run_id: str, namespace: str, outcome: DecisionOutcome
    ) -> CounterfactualResult | None:
        """对单个已出卡决策做反事实重放：扰动 thesis 引用字段，重跑决策环。

        manifest 按该点的 T 重建（created_at == T 是 risk-review 的硬校验），
        事件流与泄漏审计归属本 eval run——cf 重放中引用越界证据同样判污染。
        """
        from ..knowledge.models import Fact

        card = self._svc.decisions.get(outcome.card_id)
        t_dt = datetime.combine(outcome.point, time(23, 59, 59), tzinfo=UTC)
        manifest = RunManifest(
            run_id=eval_run_id,
            mode=RunMode.EVAL,
            eval_as_of=t_dt,
            backbone_model=config.backbone_model,
            backbone_cutoff=config.backbone_cutoff,
            allow_pit_b=config.allow_pit_b,
        )
        probe = CounterfactualProbe(
            self._kb,
            entity_kind=card.subject.kind,
            entity_id=card.subject.id,
            as_of=t_dt,
            namespace=namespace,  # 与决策同视图（生产 as_of(T) + eval 增量）
        )
        base_view = probe.base_view()
        perturbations = []
        for f in card.thesis_points:
            value = base_view.get(f, {}).get("value")
            if isinstance(value, (int, float)):
                perturbations.append(ScaleField(f, 0.1))
            else:
                perturbations.append(DropField(f))
        if not perturbations:
            return None

        def decider(view):
            """把（扰动后的）视图物化到一次性 cf 命名空间，重跑决策环。"""
            cf_ns = f"cf:{uuid.uuid4().hex[:8]}"
            for field, fv in view.items():
                if not fv.get("evidence_ids"):
                    continue
                self._kb.assert_fact(
                    Fact(
                        entity_kind=card.subject.kind,
                        entity_id=card.subject.id,
                        field=field,
                        value=fv["value"],
                        event_time=(
                            datetime.fromisoformat(fv["event_time"]) if fv.get("event_time") else None
                        ),
                        knowledge_time=datetime.fromisoformat(fv["knowledge_time"]),
                        evidence_ids=list(fv["evidence_ids"]),
                    ),
                    namespace=cf_ns,
                )
            loop = DecisionLoop(
                kb=self._kb,
                events=self._events,
                decision_service=self._svc,
                llm=self._llm_agent,
                manifest=manifest,
                namespace=cf_ns,
                hooks=[LeakageAuditHook(event_sink=self._events)],
            )
            with contextlib.suppress(LeakageDetected):
                card_id = loop.run(card.subject.kind, card.subject.id, now=t_dt)
                if card_id:
                    c = self._svc.decisions.get(card_id)
                    return {"action": c.action.value, "conviction": c.conviction}
            return {"action": "none", "conviction": 0}

        return probe.run(decider, perturbations)

    def _persist(self, config: EvalConfig, report: EvalReport) -> None:
        out_dir = self._artifacts / report.eval_run_id
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "config.json").write_text(config.model_dump_json(indent=2))
        # holdout 报告落盘即脱敏：只含聚合统计，改进者读不到逐 case 细节
        to_write = report.redacted() if config.is_holdout else report
        (out_dir / "report.json").write_text(to_write.model_dump_json(indent=2))
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



def _mean_cf(results: list[CounterfactualResult]) -> CounterfactualSummary | None:
    if not results:
        return None
    n = len(results)
    return CounterfactualSummary(
        trials=sum(r.trials for r in results),
        pc=sum(r.pc for r in results) / n,
        ci=sum(r.ci for r in results) / n,
        ids=sum(r.ids for r in results) / n,
    )
