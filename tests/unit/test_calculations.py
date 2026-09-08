"""计算服务验收（设计 §8.1/§8.3/§13.1「模型」组）。

关键场景：零/负分母 N/M、缺净债务不默认零、WACC≤终值增速禁用、无根报错、
input_refs 解析与漂移拒绝、幂等、TTM 连续性门禁、Decimal 精确性。
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from finance_agent.eventstore.store import EventStore
from finance_agent.knowledge.metric_store import MetricStore
from finance_agent.knowledge.metrics import MetricPeriod, RawValue, ReportedObservation
from finance_agent.knowledge.models import Evidence, PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.research.calculations import (
    CalculationError,
    CalculationService,
    InputRef,
)

NOW = datetime(2025, 6, 1, tzinfo=UTC)


def assume(label, value, unit=""):
    return InputRef(kind="assumption", label=label, value=value, unit=unit)


@pytest.fixture()
def svc(tmp_path):
    store = MetricStore(tmp_path / "m.db")
    events = EventStore(tmp_path / "e.db")
    return CalculationService(store, events=events), store, events


class TestGuards:
    def test_zero_denominator_is_not_meaningful(self, svc):
        s, _, _ = svc
        r = s.calculate(entity_kind="stock", entity_id="X", formula_id="margin",
                        inputs=[assume("revenue", "0"), assume("net_income", "5")],
                        assumptions={"margin_kind": "net"})
        assert r.status == "not_meaningful" and r.result is None
        assert "分母为零" in (r.error or "")

    def test_negative_base_growth_not_auto_shown(self, svc):
        s, _, _ = svc
        r = s.calculate(entity_kind="stock", entity_id="X", formula_id="yoy_growth",
                        inputs=[assume("current", "5"), assume("prior", "-10")])
        assert r.status == "not_meaningful" and "N/M" in (r.error or "")

    def test_net_debt_requires_both_inputs(self, svc):
        s, _, _ = svc
        with pytest.raises(CalculationError, match="缺输入"):
            s.calculate(entity_kind="stock", entity_id="X", formula_id="net_debt",
                        inputs=[assume("total_debt", "100")])  # cash 缺失不默认为零

    def test_unknown_formula_fails_loud(self, svc):
        s, _, _ = svc
        with pytest.raises(CalculationError, match="未知公式"):
            s.calculate(entity_kind="stock", entity_id="X", formula_id="magic_valuation",
                        inputs=[])

    def test_failed_result_never_looks_valid(self, svc):
        s, _, _ = svc
        r = s.calculate(entity_kind="stock", entity_id="X", formula_id="cagr",
                        inputs=[assume("begin", "-5"), assume("end", "10"), assume("years", "3")])
        assert r.status == "not_meaningful" and r.result is None

    def test_decimal_precision_no_float_pollution(self, svc):
        s, _, _ = svc
        r = s.calculate(entity_kind="stock", entity_id="X", formula_id="fcf_from_cfo",
                        inputs=[assume("cfo", "0.3"), assume("capex", "0.1")],
                        assumptions={"unit": "USD"})
        assert Decimal(r.result) == Decimal("0.2")  # float 会得 0.19999999999999998


class TestReverseDcf:
    BASE = {
        "wacc": "0.10", "terminal_g": "0.025", "tax_rate": "0.21", "years": "10",
        "ebit_margin": "0.12", "da_ratio": "0.03", "capex_ratio": "0.04", "nwc_ratio": "0.05",
        "unit": "USD", "currency": "USD",
    }

    def test_wacc_le_terminal_g_disabled(self, svc):
        s, _, _ = svc
        r = s.calculate(entity_kind="stock", entity_id="BE", formula_id="reverse_dcf",
                        inputs=[assume("revenue_0", "1000")],
                        assumptions={**self.BASE, "wacc": "0.02", "target_ev": "3000"})
        assert r.status == "failed" and "发散" in (r.error or "")

    def test_no_root_reports_bounds(self, svc):
        s, _, _ = svc
        r = s.calculate(entity_kind="stock", entity_id="BE", formula_id="reverse_dcf",
                        inputs=[assume("revenue_0", "1000")],
                        assumptions={**self.BASE, "target_ev": "999999999"})
        assert r.status == "failed" and "不在可解区间" in (r.error or "")
        assert r.extra.get("ev_at_g_min") and r.extra.get("ev_at_g_max")

    def test_solve_implied_growth_with_residual(self, svc):
        s, _, _ = svc
        r = s.calculate(entity_kind="stock", entity_id="BE", formula_id="reverse_dcf",
                        inputs=[assume("revenue_0", "1000"), assume("net_debt", "200")],
                        assumptions={**self.BASE, "target_ev": "3000"})
        assert r.status == "ok"
        g = Decimal(r.result)
        assert Decimal("-0.2") <= g <= Decimal("0.6")
        # 求解区间/误差/收敛状态可审计（§8.3）
        assert abs(Decimal(r.extra["residual"])) <= Decimal("3000") * Decimal("0.0001")
        assert r.extra["solve_interval"] == ["-0.2", "0.6"]
        assert r.extra["equity_model"] is not None

    def test_fcf_margin_mode_labeled(self, svc):
        s, _, _ = svc
        r = s.calculate(entity_kind="stock", entity_id="BE", formula_id="reverse_dcf",
                        inputs=[assume("revenue_0", "1000")],
                        assumptions={"wacc": "0.10", "terminal_g": "0.02", "tax_rate": "0.2",
                                     "years": "8", "target_ev": "2000", "mode": "fcf_margin",
                                     "fcf_margin": "0.08"})
        assert r.status == "ok"
        assert any("收入占比假设" in w for w in r.warnings)

    def test_missing_net_debt_no_equity_output(self, svc):
        s, _, _ = svc
        r = s.calculate(entity_kind="stock", entity_id="BE", formula_id="reverse_dcf",
                        inputs=[assume("revenue_0", "1000")],
                        assumptions={**self.BASE, "target_ev": "3000"})
        assert r.status == "ok" and r.extra["equity_model"] is None
        assert any("净债务未知不默认为零" in w for w in r.warnings)

    def test_sensitivity_grid_cells_inherit_inputs(self, svc):
        s, _, _ = svc
        r = s.calculate(entity_kind="stock", entity_id="BE", formula_id="sensitivity_grid",
                        inputs=[assume("revenue_0", "1000")],
                        assumptions={**self.BASE, "g": "0.10",
                                     "axis_x": "0.05,0.15", "axis_y": "0.08,0.12",
                                     "vary_x": "g", "vary_y": "wacc"})
        assert r.status == "ok"
        cells = r.extra["cells"]
        assert len(cells) == 4
        evs = [Decimal(c["ev"]) for c in cells]
        assert max(evs) > min(evs)  # 假设变化 → 结果变化（每次都可回指同一输入）

    def test_sensitivity_grid_degenerate_cell_marked(self, svc):
        s, _, _ = svc
        r = s.calculate(entity_kind="stock", entity_id="BE", formula_id="sensitivity_grid",
                        inputs=[assume("revenue_0", "1000")],
                        assumptions={**self.BASE, "g": "0.10",
                                     "axis_x": "0.10", "axis_y": "0.02,0.10",
                                     "vary_x": "g", "vary_y": "terminal_g"})
        cells = r.extra["cells"]
        bad = [c for c in cells if c.get("error")]
        assert bad and "WACC" in bad[0]["error"]  # terminal_g=0.10=wacc → 格子标错不产出假值


class TestInputRefs:
    def _seed_obs(self, store: MetricStore, kb: BitemporalStore, obs_id_value=("obs-rev", "1500")):
        obs_id, value = obs_id_value
        kb.add_evidence(Evidence(
            evidence_id="ev-c1", source_id="edgar", verbatim_quote="revenue 1500",
            retrieved_at=NOW, available_at=NOW, pit_grade=PitGrade.A,
        ))
        from finance_agent.harness.manifest import RunManifest, RunMode
        from finance_agent.knowledge.metric_writer import TypedMetricWriter
        w = TypedMetricWriter(store=store, kb=kb)
        obs = ReportedObservation(
            observation_id="", entity_kind="stock", entity_id="BE", metric_key="revenue",
            period=MetricPeriod(start=date(2024, 1, 1), end=date(2024, 12, 31),
                                frequency="FY", fiscal_label="FY2024"),
            value=value, unit="USD", currency="USD",
            raw=RawValue(value_text="revenue 1500", unit_text="USD"),
            # 裸数字必须有定位上下文（audit §3.2）：夹具声明数值来自季报表格
            locator={"table": "revenue_summary", "row": "total"},
            evidence_refs=["ev-c1"], knowledge_time=NOW, source_available_at=NOW,
            retrieved_at=NOW, created_at=NOW, pit_grade=PitGrade.A,
        )
        oid, _ = w.write_observation(obs, run=RunManifest(run_id="r", mode=RunMode.LIVE))
        return oid

    def test_observation_ref_resolved_from_store(self, tmp_path):
        store = MetricStore(tmp_path / "m.db")
        kb = BitemporalStore(tmp_path / "kb.db")
        s = CalculationService(store)
        oid = self._seed_obs(store, kb)
        r = s.calculate(
            entity_kind="stock", entity_id="BE", formula_id="yoy_growth",
            inputs=[
                InputRef(kind="observation", label="current", ref_id=oid),
                assume("prior", "1000"),
            ],
        )
        assert r.status == "ok" and Decimal(r.result) == Decimal("0.5")
        # 输入引用可回指（图上计算值可完整重算）
        assert r.input_refs[0].ref_id == oid

    def test_ref_value_drift_rejected(self, tmp_path):
        store = MetricStore(tmp_path / "m.db")
        kb = BitemporalStore(tmp_path / "kb.db")
        s = CalculationService(store)
        oid = self._seed_obs(store, kb)
        with pytest.raises(CalculationError, match="不一致"):
            s.calculate(
                entity_kind="stock", entity_id="BE", formula_id="yoy_growth",
                inputs=[
                    InputRef(kind="observation", label="current", ref_id=oid, value="9999"),
                    assume("prior", "1000"),
                ],
            )

    def test_unknown_ref_rejected(self, tmp_path):
        store = MetricStore(tmp_path / "m.db")
        s = CalculationService(store)
        with pytest.raises(CalculationError, match="未登记"):
            s.calculate(entity_kind="stock", entity_id="BE", formula_id="yoy_growth",
                        inputs=[InputRef(kind="observation", label="current", ref_id="obs-nope"),
                                assume("prior", "1000")])

    def test_idempotent_same_inputs(self, svc):
        s, _, _ = svc
        args = dict(entity_kind="stock", entity_id="BE", formula_id="yoy_growth",
                    inputs=[assume("current", "150"), assume("prior", "100")])
        r1 = s.calculate(**args)
        r2 = s.calculate(**args)
        assert r1.calculation_id == r2.calculation_id  # input_hash 幂等

    def test_calculation_event_emitted(self, svc):
        s, _, events = svc
        s.calculate(entity_kind="stock", entity_id="BE", formula_id="enterprise_value",
                    inputs=[assume("market_cap", "5000"), assume("net_debt", "1000")],
                    run_id="run-x")
        evs = [e for e in events.read("run-x") if e.type == "calculation/completed"]
        assert evs and evs[0].payload["formula_id"] == "enterprise_value"
        assert evs[0].payload["result"] == "6000"


class TestTtm:
    def _quarter(self, store, kb, writer, end, value, ev_id):
        from finance_agent.harness.manifest import RunManifest, RunMode
        from finance_agent.knowledge.metrics import RawValue as RV

        kb.add_evidence(Evidence(
            evidence_id=ev_id, source_id="edgar", verbatim_quote=f"quarterly revenue {value}",
            retrieved_at=NOW, available_at=NOW, pit_grade=PitGrade.A,
        ))
        start = date(end.year, end.month - 2, 1)
        obs = ReportedObservation(
            entity_kind="stock", entity_id="BE", metric_key="revenue",
            period=MetricPeriod(start=start, end=end, frequency="Q",
                                fiscal_label=f"{end.year}Q{end.month // 3}"),
            value=value, unit="USD", currency="USD",
            raw=RV(value_text=f"quarterly revenue {value}", unit_text="USD"),
            locator={"table": "quarterly_revenue", "row": str(end)},
            evidence_refs=[ev_id], knowledge_time=NOW, source_available_at=NOW,
            retrieved_at=NOW, created_at=NOW, pit_grade=PitGrade.A,
        )
        return writer.write_observation(obs, run=RunManifest(run_id="r", mode=RunMode.LIVE))[0]

    def test_ttm_requires_four_consecutive_quarters(self, tmp_path):
        from finance_agent.knowledge.metric_writer import TypedMetricWriter

        store = MetricStore(tmp_path / "m.db")
        kb = BitemporalStore(tmp_path / "kb.db")
        writer = TypedMetricWriter(store=store, kb=kb)
        s = CalculationService(store)
        # 只有 3 个季度 → 拒绝（缺期不补零、不拼全年与季度）
        for i, (end, val) in enumerate([
            (date(2024, 6, 30), "100"), (date(2024, 9, 30), "110"), (date(2024, 12, 31), "120"),
        ]):
            self._quarter(store, kb, writer, end, val, f"ev-q{i}")
        with pytest.raises(CalculationError, match="不足 4 个"):
            s.ttm_from_observations(entity_kind="stock", entity_id="BE",
                                    metric_key="revenue", as_of=NOW)

    def test_ttm_gap_rejected(self, tmp_path):
        from finance_agent.knowledge.metric_writer import TypedMetricWriter

        store = MetricStore(tmp_path / "m.db")
        kb = BitemporalStore(tmp_path / "kb.db")
        writer = TypedMetricWriter(store=store, kb=kb)
        s = CalculationService(store)
        # 四个季度但中间断档（2024Q2 缺失，取 2023Q4 + 2024 三季度）
        for i, (end, val) in enumerate([
            (date(2023, 12, 31), "90"), (date(2024, 6, 30), "100"),
            (date(2024, 9, 30), "110"), (date(2024, 12, 31), "120"),
        ]):
            self._quarter(store, kb, writer, end, val, f"ev-g{i}")
        with pytest.raises(CalculationError, match="不连续"):
            s.ttm_from_observations(entity_kind="stock", entity_id="BE",
                                    metric_key="revenue", as_of=NOW)

    def test_ttm_sums_with_refs(self, tmp_path):
        from finance_agent.knowledge.metric_writer import TypedMetricWriter

        store = MetricStore(tmp_path / "m.db")
        kb = BitemporalStore(tmp_path / "kb.db")
        writer = TypedMetricWriter(store=store, kb=kb)
        s = CalculationService(store)
        for i, (end, val) in enumerate([
            (date(2024, 3, 31), "100"), (date(2024, 6, 30), "110"),
            (date(2024, 9, 30), "120"), (date(2024, 12, 31), "130"),
        ]):
            self._quarter(store, kb, writer, end, val, f"ev-t{i}")
        r = s.ttm_from_observations(entity_kind="stock", entity_id="BE",
                                    metric_key="revenue", as_of=NOW)
        assert r.status == "ok" and r.result == "460"
        assert all(i.ref_id for i in r.input_refs)  # 每个输入都可回指观测

    def test_ttm_rejected_for_balance_metric(self, tmp_path):
        store = MetricStore(tmp_path / "m.db")
        s = CalculationService(store)
        with pytest.raises(CalculationError, match="可加总流量指标"):
            s.ttm_from_observations(entity_kind="stock", entity_id="BE",
                                    metric_key="cash", as_of=NOW)


class TestGuidanceDelta:
    def test_landing_and_midpoint_labeled(self, svc):
        s, _, _ = svc
        r = s.calculate(entity_kind="stock", entity_id="BE", formula_id="guidance_delta",
                        inputs=[assume("actual", "125"), assume("guidance_low", "100"),
                                assume("guidance_high", "120")],
                        assumptions={"unit": "USD"})
        assert r.status == "ok"
        assert r.extra["landing"] == "beat"
        assert r.extra["vs_low"] == "25" and r.extra["vs_high"] == "5"
        assert "中点" in r.extra["midpoint_note"]  # 中点比较必须显式标注

    def test_inverted_range_failed(self, svc):
        s, _, _ = svc
        r = s.calculate(entity_kind="stock", entity_id="BE", formula_id="guidance_delta",
                        inputs=[assume("actual", "125"), assume("guidance_low", "130"),
                                assume("guidance_high", "120")])
        assert r.status == "failed"
