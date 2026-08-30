"""ProfileRenderer（R3）：HTML 存档投影的契约。

- 自包含：无外部资源引用（无 http(s) src/link、无 <script src>）
- 证据锚点：事实数字带 data-ev hover（原文摘录 + available_at + PIT 等级）
- 版本化：maybe_archive 幂等（同 snapshot 不重复写），有变化才出新版本
- SVG 图表：有 prices 时内联 SVG，无则省略
"""

from datetime import UTC, datetime

from finance_agent.eventstore.store import EventStore
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.render import maybe_archive, render_profile_html
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter

NOW = datetime(2026, 8, 30, tzinfo=UTC)


def seeded_kb(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    events = EventStore(tmp_path / "e.db")
    kb.add_evidence(
        Evidence(
            evidence_id="ev-1", source_id="edgar",
            url="https://sec.gov/x", verbatim_quote="Total revenue was 1,473,856 thousand",
            retrieved_at=NOW, available_at=datetime(2025, 2, 27, tzinfo=UTC), pit_grade=PitGrade.A,
        )
    )
    ProfileWriter(store=kb, events=events).write_fact(
        Fact(entity_kind="stock", entity_id="BE", field="revenue_fy",
             value="1,473,856 thousand", knowledge_time=datetime(2025, 2, 27, tzinfo=UTC),
             evidence_ids=["ev-1"], run_id="seed"),
        run=_manifest(),
    )
    return kb


def _manifest():
    from finance_agent.harness.manifest import RunManifest, RunMode

    return RunManifest(run_id="seed", mode=RunMode.LIVE)


def test_render_is_self_contained_with_evidence_anchors(tmp_path):
    kb = seeded_kb(tmp_path)
    html = render_profile_html(kb, "stock", "BE", generated_at=NOW)
    assert "<script" not in html and "http" not in html.replace("https://sec.gov/x", "").replace(
        "https://www.w3.org", "")  # 命名空间除外
    # 证据锚点：hover 数据含原文与 PIT 等级
    assert "data-ev" in html and "Total revenue was 1,473,856 thousand" in html
    assert "PIT-A" in html
    assert "内容哈希" in html


def test_price_chart_inlined_when_prices_given(tmp_path):
    kb = seeded_kb(tmp_path)
    prices = [{"date": f"2026-08-{d:02d}", "close": 30.0 + d} for d in range(1, 10)]
    html = render_profile_html(kb, "stock", "BE", prices=prices, generated_at=NOW)
    assert "<svg" in html and "<polyline" in html
    html_no = render_profile_html(kb, "stock", "BE", generated_at=NOW)
    assert "<svg" not in html_no


def test_archive_versioning_idempotent(tmp_path):
    kb = seeded_kb(tmp_path)
    kdir = tmp_path / "knowledge"
    p1 = maybe_archive(kb, kdir, "stock", "BE")
    assert p1 is not None and p1.exists()
    p2 = maybe_archive(kb, kdir, "stock", "BE")  # 无变化 → 不重复写
    assert p2 is None
    assert (p1.parent / "latest.html").exists()

    # 写入新事实 → 新 snapshot → 新版本
    kb.add_evidence(
        Evidence(
            evidence_id="ev-2", source_id="edgar", verbatim_quote="net loss 28,905",
            retrieved_at=NOW, available_at=datetime(2025, 2, 27, tzinfo=UTC), pit_grade=PitGrade.A,
        )
    )
    ProfileWriter(store=kb, events=None).write_fact(
        Fact(entity_kind="stock", entity_id="BE", field="net_income_fy",
             value="28,905", knowledge_time=datetime(2025, 2, 27, tzinfo=UTC),
             evidence_ids=["ev-2"], run_id="seed"),
        run=_manifest(),
    )
    p3 = maybe_archive(kb, kdir, "stock", "BE")
    assert p3 is not None and p3 != p1
    assert len(list(p1.parent.glob("*.html"))) == 3  # 两版本 + latest
