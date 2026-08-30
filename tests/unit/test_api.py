"""API 投影层契约：UI 是 EventStore/知识库的纯投影，每个字段可回指（DESIGN.md §7）。

端点覆盖五个一级页面：Sessions / Knowledge（含 as_of 时光机）/ Decisions / Evaluations。
"""

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from finance_agent.api.app import create_app
from finance_agent.decision.card import (
    Action,
    DecisionCard,
    Horizon,
    Position,
    Subject,
)
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.events import Event
from finance_agent.eventstore.store import EventStore
from finance_agent.harness.manifest import RunMode
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.snapshot import kb_snapshot_id
from finance_agent.knowledge.store import BitemporalStore

NOW = datetime(2024, 6, 1, tzinfo=UTC)
OLD = datetime(2023, 3, 1, tzinfo=UTC)


def seeded(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    events = EventStore(tmp_path / "events.db")
    decisions = DecisionStore(tmp_path / "decisions.db")
    kb.add_evidence(
        Evidence(
            evidence_id="ev-1",
            source_id="edgar",
            url="https://sec.gov/x",
            verbatim_quote="Total revenue 100",
            retrieved_at=NOW,
            available_at=OLD,
            pit_grade=PitGrade.A,
        )
    )
    kb.assert_fact(
        Fact(
            entity_kind="stock", entity_id="AAPL", field="revenue_fy", value=100,
            knowledge_time=OLD, evidence_ids=["ev-1"], run_id="run-1",
        )
    )
    events.append(Event(run_id="run-1", type="turn/start"))
    events.append(Event(run_id="run-1", type="user/message", payload={"content": "研究 AAPL"}))
    events.append(Event(run_id="run-1", type="fact/asserted", payload={"field": "revenue_fy"}))
    decisions.insert(
        DecisionCard(
            card_id="card-1",
            run_id="run-1",
            mode=RunMode.LIVE,
            subject=Subject(kind="stock", id="AAPL"),
            action=Action.BUY,
            conviction=4,
            horizon=Horizon.M6,
            rationale=["ev-1"],
            thesis_points=["revenue_fy"],
            invalidation=["营收失效条件"],
            position=Position(sizing_pct=0.1, max_loss_pct=0.05),
            kb_snapshot_id=kb_snapshot_id(kb, [("stock", "AAPL")], NOW),
            created_at=NOW,
        )
    )
    return kb, events, decisions


def make_client(tmp_path):
    kb, events, decisions = seeded(tmp_path)
    app = create_app(kb=kb, events=events, decisions=decisions, evals_dir=tmp_path / "evals")
    return TestClient(app)


def test_sessions_list_and_events(tmp_path):
    client = make_client(tmp_path)
    runs = client.get("/api/sessions").json()
    assert runs == [
        {
            "run_id": "run-1",
            "title": None,
            "started_at": runs[0]["started_at"],
            "last_active": runs[0]["last_active"],
            "status": "running",
            "status_detail": None,
        }
    ]

    timeline = client.get("/api/sessions/run-1/events").json()
    assert [e["type"] for e in timeline] == ["turn/start", "user/message", "fact/asserted"]


def test_knowledge_entities_and_as_of_time_machine(tmp_path):
    client = make_client(tmp_path)
    entities = client.get("/api/knowledge/entities").json()
    assert entities == [{"kind": "stock", "id": "AAPL", "field_count": 1}]

    # 时光机：事实 knowledge_time=2023-03-01 → 之前不可见，之后可见
    before = client.get(
        "/api/knowledge/stock/AAPL", params={"as_of": "2022-06-01T00:00:00+00:00"}
    ).json()
    assert before["facts"] == {}
    after = client.get(
        "/api/knowledge/stock/AAPL", params={"as_of": "2024-01-01T00:00:00+00:00"}
    ).json()
    assert after["facts"]["revenue_fy"]["value"] == 100
    # 证据可回指
    assert after["facts"]["revenue_fy"]["evidence"][0]["verbatim_quote"] == "Total revenue 100"


def test_decisions_list(tmp_path):
    client = make_client(tmp_path)
    cards = client.get("/api/decisions").json()
    assert len(cards) == 1
    assert cards[0]["card_id"] == "card-1"
    assert cards[0]["action"] == "buy"
    assert cards[0]["kb_snapshot_id"].startswith("sha256:")


def test_evaluations_empty_dir(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/api/evaluations").json() == []
