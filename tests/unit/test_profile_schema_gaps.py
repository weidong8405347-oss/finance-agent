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


# ---------------- 弱字段回流（2026-09-03 整改收尾） ----------------


def test_weak_fields_surface_without_changing_completeness(tmp_path):
    """必填字段有值但质检未过 → weak（字段→原因）；完整度口径不变（weak 仍计 fresh）。"""
    # 基准：写一段长文 moat（A 级证据）
    store = make_store(tmp_path / "baseline")
    add_fact(store, "moat", "生态锁定带来用户高转换成本与长期留存优势", NOW - timedelta(days=30), "e1")
    baseline = GapAnalyzer(store).analyze("stock", "AAPL", NOW)

    # 同字段换短值 → weak，但完整度分毫不差（弱字段不降分、不进 missing/stale）
    store2 = make_store(tmp_path / "short")
    add_fact(store2, "moat", "生态锁定", NOW - timedelta(days=30), "e1")
    report = GapAnalyzer(store2).analyze("stock", "AAPL", NOW)
    assert report.weak == {"moat": ["内容过短"]}
    assert report.completeness == baseline.completeness
    assert report.missing == baseline.missing and report.stale == baseline.stale

    # 仅 C 级证据 → weak；可选字段的同类问题不回流（只引导必填）
    store2.add_evidence(
        Evidence(
            evidence_id="e-c", source_id="web", verbatim_quote="网络搜索快照",
            retrieved_at=NOW, pit_grade=PitGrade.C,
        )
    )
    store2.assert_fact(
        Fact(
            entity_kind="stock", entity_id="AAPL", field="peers",
            value="可比公司包括 Pear Corp 与若干安卓厂商",
            knowledge_time=NOW, evidence_ids=["e-c"],
        )
    )
    store2.assert_fact(
        Fact(
            entity_kind="stock", entity_id="AAPL", field="management",
            value="短", knowledge_time=NOW, evidence_ids=["e-c"],
        )
    )
    report2 = GapAnalyzer(store2).analyze("stock", "AAPL", NOW)
    assert any("C 级证据" in i for i in report2.weak["peers"])
    assert "management" not in report2.weak  # 可选字段不计


def test_round_brief_lists_weak_fields_with_reasons(tmp_path):
    """brief 输出「待改进字段：field（原因）」引导下轮重写；无弱字段时不出现该行。"""
    from finance_agent.research.prompts import build_round_brief

    store = make_store(tmp_path / "weak")
    add_fact(store, "moat", "生态锁定", NOW - timedelta(days=30), "e1")
    add_fact(store, "valuation", "估值偏高但暂无数据", NOW - timedelta(days=30), "e2")
    gaps = GapAnalyzer(store).analyze("stock", "AAPL", NOW)

    brief = build_round_brief("stock", "AAPL", "深度研究 AAPL", gaps, round_no=2)
    assert "待改进字段（已有值但未过质检，优先重写替换）" in brief
    assert "moat（内容过短）" in brief
    assert "valuation（缺少数值锚点）" in brief
    # 弱字段不被谎报为缺失（缺失清单仍只报真正没写的字段）
    assert "moat" not in gaps.missing

    clean = make_store(tmp_path / "clean")
    add_fact(clean, "moat", "生态锁定带来用户高转换成本与长期留存优势", NOW - timedelta(days=30), "e1")
    brief_clean = build_round_brief(
        "stock", "AAPL", "深度研究 AAPL",
        GapAnalyzer(clean).analyze("stock", "AAPL", NOW), round_no=1,
    )
    assert "待改进字段" not in brief_clean
