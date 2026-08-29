"""ProfileSchema + GapAnalyzer 契约：迭代研究的起点是「缺口清单」（DESIGN.md §5.1）。"""

from datetime import UTC, datetime, timedelta

from finance_agent.knowledge.gaps import GapAnalyzer
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.schema import STOCK_SCHEMA
from finance_agent.knowledge.store import BitemporalStore

NOW = datetime(2024, 6, 1, tzinfo=UTC)


def make_store(tmp_path) -> BitemporalStore:
    return BitemporalStore(tmp_path / "kb.db")


def add_fact(store, field, value, knowledge_time: datetime, evidence_id: str):
    store.add_evidence(
        Evidence(
            evidence_id=evidence_id,
            source_id="edgar",
            verbatim_quote=f"value is {value}",
            retrieved_at=NOW,
            available_at=knowledge_time,
            pit_grade=PitGrade.A,
        )
    )
    store.assert_fact(
        Fact(
            entity_kind="stock",
            entity_id="AAPL",
            field=field,
            value=value,
            knowledge_time=knowledge_time,
            evidence_ids=[evidence_id],
        )
    )


def test_empty_profile_has_zero_completeness(tmp_path):
    analyzer = GapAnalyzer(make_store(tmp_path))
    report = analyzer.analyze("stock", "AAPL", NOW)
    assert report.completeness == 0.0
    assert set(report.missing) == set(STOCK_SCHEMA.required)


def test_completeness_grows_as_fields_are_written(tmp_path):
    store = make_store(tmp_path)
    analyzer = GapAnalyzer(store)
    kt = NOW - timedelta(days=30)

    add_fact(store, "revenue_fy", 100, kt, "e1")
    r1 = analyzer.analyze("stock", "AAPL", NOW)
    assert 0.0 < r1.completeness < 1.0
    assert "revenue_fy" not in r1.missing

    for i, field in enumerate(f for f in STOCK_SCHEMA.required if f != "revenue_fy"):
        add_fact(store, field, f"v{i}", kt, f"e{i + 2}")
    r2 = analyzer.analyze("stock", "AAPL", NOW)
    assert r2.completeness == 1.0 and r2.missing == []


def test_stale_fact_reported(tmp_path):
    store = make_store(tmp_path)
    analyzer = GapAnalyzer(store)
    # revenue_fy 策略有新鲜度阈值；knowledge_time 太早 → stale
    old_kt = NOW - timedelta(days=STOCK_SCHEMA.required["revenue_fy"].max_age_days + 30)
    add_fact(store, "revenue_fy", 100, old_kt, "e1")
    report = analyzer.analyze("stock", "AAPL", NOW)
    assert "revenue_fy" in report.stale
    assert "revenue_fy" not in report.missing  # 有但陈旧 ≠ 缺失
    assert 0.0 < report.completeness < 1.0  # 陈旧字段不计入完整度


def test_conflicts_surfaced_in_gap_report(tmp_path):
    store = make_store(tmp_path)
    add_fact(store, "revenue_fy", 100, NOW - timedelta(days=10), "e1")
    add_fact(store, "revenue_fy", 120, NOW - timedelta(days=5), "e2")  # 值变化 → 冲突
    report = GapAnalyzer(store).analyze("stock", "AAPL", NOW)
    assert "revenue_fy" in report.conflicts


def test_unknown_entity_kind_uses_empty_schema(tmp_path):
    report = GapAnalyzer(make_store(tmp_path)).analyze("industry", "ev", NOW)
    assert report.completeness == 0.0
