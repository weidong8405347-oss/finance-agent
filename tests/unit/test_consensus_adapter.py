"""一致预期 adapter 与 expectations 模块 revision 视图验收（升级方案 §25/§26）。

关键场景：adapter 产出四个估计族（原样数值不换算）；单族失败降级可见不拖死；
空结果 = 无覆盖不编造；C 级 PIT 声明；revision 视图按可知时刻排列同一目标期间的
多个快照；价格序列只收 share_price reported 观测。
"""

import sys
import types
from datetime import UTC, date, datetime

import pytest

from finance_agent.dossier.service import _price_points, _revision_series
from finance_agent.gateway.adapters.consensus import (
    FixtureConsensusAdapter,
    YFinanceConsensusAdapter,
)
from finance_agent.knowledge.metrics import (
    ConsensusObservation,
    MetricPeriod,
    RawValue,
    ReportedObservation,
)

T1 = datetime(2025, 1, 31, tzinfo=UTC)


class _FakeFrame:
    """最小 DataFrame 替身：iterrows + columns + empty。"""

    def __init__(self, rows: dict[str, dict[str, float]]):
        self._rows = rows
        self.columns = list(next(iter(rows.values()), {}).keys())
        self.empty = not rows

    def iterrows(self):
        return iter(self._rows.items())


class _FakeTicker:
    def __init__(self, ticker, **overrides):
        self.ticker = ticker
        self.earnings_estimate = _FakeFrame({
            "0y": {"avg": 7.5, "low": 7.0, "high": 8.1, "number_of_analysts": 30},
            "+1y": {"avg": 8.4, "low": 7.9, "high": 9.0, "number_of_analysts": 28},
        })
        self.revenue_estimate = _FakeFrame({
            "+1y": {"avg": 2.1e9, "number_of_analysts": 25},
        })
        self.eps_trend = _FakeFrame({
            "+1y": {"current": 8.4, "30days_ago": 8.1, "90days_ago": 7.7},
        })
        self.eps_revisions = _FakeFrame({
            "+1y": {"up_last_30days": 12, "down_last_30days": 3},
        })
        self.growth_estimates = _FakeFrame({})
        for k, v in overrides.items():
            setattr(self, k, v)


def _fake_yf(ticker_cls):
    mod = types.ModuleType("yfinance")
    mod.Ticker = ticker_cls
    return mod


class TestYFinanceConsensusAdapter:
    def test_capability_is_pit_c(self):
        cap = YFinanceConsensusAdapter().capability()
        assert cap.source_id == "consensus_yf"
        assert cap.pit_grade.value == "C"
        assert cap.server_side_asof is False

    def test_payload_families_verbatim(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "yfinance", _fake_yf(_FakeTicker))
        recs = YFinanceConsensusAdapter().query({"ticker": "BE"})
        assert len(recs) == 1
        p = recs[0].payload
        assert recs[0].available_at is None  # C 级无 PIT
        eps = {r["period"]: r for r in p["earnings_estimate"]}
        assert eps["+1y"]["avg"] == 8.4 and eps["+1y"]["number_of_analysts"] == 28
        trend = p["eps_trend"][0]
        assert trend["current"] == 8.4 and trend["90days_ago"] == 7.7
        rev = p["eps_revisions"][0]
        assert rev["up_last_30days"] == 12 and rev["down_last_30days"] == 3

    def test_family_failure_partial_not_fatal(self, monkeypatch):
        class _Broken:
            def __init__(self, ticker):
                self.earnings_estimate = _FakeTicker(ticker).earnings_estimate
                self.revenue_estimate = _FakeTicker(ticker).revenue_estimate
                self.eps_revisions = _FakeTicker(ticker).eps_revisions
                self.growth_estimates = _FakeFrame({})

            @property
            def eps_trend(self):
                raise RuntimeError("rate limited")

        monkeypatch.setitem(sys.modules, "yfinance", _fake_yf(_Broken))
        recs = YFinanceConsensusAdapter().query({"ticker": "BE"})
        assert len(recs) == 1
        p = recs[0].payload
        assert p["earnings_estimate"]  # 其余族不受影响
        assert p["eps_trend"] == []
        assert "eps_trend" in p["partial_errors"]

    def test_no_coverage_returns_empty(self, monkeypatch):
        class _Empty:
            def __init__(self, ticker):
                self.earnings_estimate = _FakeFrame({})
                self.revenue_estimate = _FakeFrame({})
                self.eps_trend = _FakeFrame({})
                self.eps_revisions = _FakeFrame({})
                self.growth_estimates = _FakeFrame({})

        monkeypatch.setitem(sys.modules, "yfinance", _fake_yf(_Empty))
        assert YFinanceConsensusAdapter().query({"ticker": "XXXX"}) == []

    def test_fixture_adapter(self):
        a = FixtureConsensusAdapter({"earnings_estimate": [{"period": "+1y", "avg": 1.0}]})
        recs = a.query({"ticker": "BE"})
        assert recs[0].payload["ticker"] == "BE"
        assert FixtureConsensusAdapter().query({"ticker": "BE"}) == []


def _consensus(value, kt, *, key="consensus_eps", period_label="FY2026"):
    return ConsensusObservation(
        entity_kind="stock", entity_id="BE", metric_key=key,
        period=MetricPeriod(start=date(2026, 1, 1), end=date(2026, 12, 31),
                            frequency="FY", fiscal_label=period_label),
        value=value, unit="USD", currency="USD",
        raw=RawValue(value_text=f"${value}"),
        vendor="yfinance", consensus_snapshot_at=kt,
        knowledge_time=kt, retrieved_at=kt, created_at=kt,
        evidence_refs=["ev-c1"], observation_id=f"obs-{key}-{value}",
    )


def _price(value, day):
    return ReportedObservation(
        entity_kind="stock", entity_id="BE", metric_key="share_price",
        period=MetricPeriod(start=None, end=day, frequency="instant", fiscal_label=str(day)),
        value=str(value), unit="USD", currency="USD",
        raw=RawValue(value_text=f"${value}"),
        evidence_refs=["ev-p1"], knowledge_time=datetime(day.year, day.month, day.day,
                                                         tzinfo=UTC),
        retrieved_at=T1, created_at=T1,
        observation_id=f"obs-px-{day}",
    )


class TestRevisionAndPriceViews:
    def test_revision_series_grouped_by_target_period(self):
        """同一目标期间（FY2026 EPS）的多时点快照按可知时刻升序成序列。"""
        obs = [
            _consensus("8.40", datetime(2025, 3, 1, tzinfo=UTC)),
            _consensus("8.10", datetime(2025, 1, 15, tzinfo=UTC)),
            _consensus("7.70", datetime(2024, 12, 1, tzinfo=UTC)),
            _consensus("120.5", datetime(2025, 3, 1, tzinfo=UTC),
                       key="consensus_revenue", period_label="FY2026"),
        ]
        series = _revision_series(obs)
        assert len(series) == 2
        eps = next(s for s in series if s["metric_key"] == "consensus_eps")
        assert eps["period_label"] == "FY2026"
        assert [p["value"] for p in eps["points"]] == ["7.70", "8.10", "8.40"]
        assert eps["points"][0]["at"].startswith("2024-12-01")
        assert all(p["observation_id"] for p in eps["points"])

    def test_revision_skips_non_consensus(self):
        obs = [_price(100.5, date(2025, 1, 2))]
        assert _revision_series(obs) == []

    def test_price_points_chronological(self):
        obs = [_price(101.5, date(2025, 1, 3)), _price(100.5, date(2025, 1, 2))]
        pts = _price_points(obs)
        assert [p["value"] for p in pts] == ["100.5", "101.5"]
        assert pts[0]["at"] == "2025-01-02"
        assert pts[0]["currency"] == "USD"

    def test_price_skips_guidance_and_missing(self):
        bad = _consensus("8.40", datetime(2025, 3, 1, tzinfo=UTC))
        assert _price_points([bad]) == []
