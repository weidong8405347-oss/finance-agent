"""DataGateway 时间锁契约——评估模式的 fail-closed 行为。"""

from datetime import UTC, datetime

import pytest

from finance_agent.eventstore.events import Event
from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.adapters.fixture import FixtureAdapter
from finance_agent.gateway.gateway import DataGateway, SourceBlockedError
from finance_agent.gateway.models import DataRecord, SourceCapability
from finance_agent.knowledge.models import PitGrade

T = datetime(2023, 6, 30, tzinfo=UTC)  # 评估决策时刻


def rec(source: str, available_at: str | None, **payload) -> DataRecord:
    return DataRecord(
        source_id=source,
        payload=payload or {"text": "..."},
        available_at=datetime.fromisoformat(available_at).replace(tzinfo=UTC) if available_at else None,
    )


def gw(tmp_path, mode="eval", **kwargs):
    events = EventStore(tmp_path / "events.db")
    g = DataGateway(
        mode=mode,
        eval_as_of=T if mode == "eval" else None,
        events=events,
        run_id="eval-1",
        **kwargs,
    )
    return g, events


def leakage_events(events: EventStore) -> list[Event]:
    return events.read("eval-1", types={"leakage/attempt"})


def test_eval_blocks_grade_c_source(tmp_path):
    g, events = gw(tmp_path)
    g.register(FixtureAdapter(SourceCapability(source_id="web_search", pit_grade=PitGrade.C), []))
    with pytest.raises(SourceBlockedError):
        g.query("web_search", {"q": "AAPL news"})
    leaks = leakage_events(events)
    assert len(leaks) == 1 and leaks[0].payload["reason"] == "source_blocked_pit_grade_c"


def test_eval_blocks_grade_b_by_default_and_allows_with_flag(tmp_path):
    g, events = gw(tmp_path)
    g.register(FixtureAdapter(SourceCapability(source_id="substack", pit_grade=PitGrade.B), []))
    with pytest.raises(SourceBlockedError):
        g.query("substack", {})

    g2, _ = gw(tmp_path / "b", allow_pit_b=True)
    g2.register(
        FixtureAdapter(
            SourceCapability(source_id="substack", pit_grade=PitGrade.B),
            [rec("substack", "2023-06-01T00:00:00")],
        )
    )
    assert len(g2.query("substack", {})) == 1


def test_eval_drops_future_records_and_audits(tmp_path):
    g, events = gw(tmp_path)
    g.register(
        FixtureAdapter(
            SourceCapability(source_id="news_archive", pit_grade=PitGrade.A),
            [
                rec("news_archive", "2023-06-29T00:00:00", text="过去"),
                rec("news_archive", "2023-07-01T00:00:00", text="未来"),  # 越界
            ],
        )
    )
    out = g.query("news_archive", {})
    assert [r.payload["text"] for r in out] == ["过去"]
    leaks = leakage_events(events)
    assert len(leaks) == 1 and leaks[0].payload["reason"] == "future_record_dropped"


def test_eval_drops_records_without_available_at(tmp_path):
    g, events = gw(tmp_path)
    g.register(
        FixtureAdapter(
            SourceCapability(source_id="prices", pit_grade=PitGrade.A),
            [rec("prices", None, text="无时间戳")],
        )
    )
    assert g.query("prices", {}) == []
    assert leakage_events(events)[0].payload["reason"] == "missing_available_at"


def test_live_mode_passes_everything(tmp_path):
    g, events = gw(tmp_path, mode="live")
    g.register(
        FixtureAdapter(
            SourceCapability(source_id="web_search", pit_grade=PitGrade.C),
            [rec("web_search", None, text="x")],
        )
    )
    assert len(g.query("web_search", {})) == 1
    assert leakage_events(events) == []


def test_adapter_receives_as_of_for_server_side_filtering(tmp_path):
    g, _ = gw(tmp_path)
    adapter = FixtureAdapter(SourceCapability(source_id="edgar", pit_grade=PitGrade.A), [])
    g.register(adapter)
    g.query("edgar", {"kind": "filings", "ticker": "AAPL"})
    assert adapter.seen_as_of == [T]


def test_unregistered_source_fail_closed(tmp_path):
    g, _ = gw(tmp_path)
    with pytest.raises(SourceBlockedError):
        g.query("nonexistent", {})
