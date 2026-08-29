"""SSE 事件流 + 审批机制（D3 milestone：高成本操作需人工批准）。"""

import threading
import time

from fastapi.testclient import TestClient

from finance_agent.api.app import create_app
from finance_agent.api.sse import iter_sse_events
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.events import Event
from finance_agent.eventstore.store import EventStore
from finance_agent.knowledge.store import BitemporalStore


def make_client(tmp_path, research_runner=None):
    kb = BitemporalStore(tmp_path / "kb.db")
    events = EventStore(tmp_path / "events.db")
    decisions = DecisionStore(tmp_path / "decisions.db")
    app = create_app(
        kb=kb,
        events=events,
        decisions=decisions,
        evals_dir=tmp_path / "evals",
        research_runner=research_runner,
    )
    return TestClient(app), events


def test_sse_generator_yields_appended_events(tmp_path):
    events = EventStore(tmp_path / "e.db")
    events.append(Event(run_id="r1", type="user/message", payload={"content": "a"}))
    gen = iter_sse_events(events, "r1", max_polls=3, poll_interval=0.01)
    assert '"user/message"' in next(gen)

    # 新增事件在后续轮询中被推送
    gen2 = iter_sse_events(events, "r1", max_polls=5, poll_interval=0.01)
    first = next(gen2)
    events.append(Event(run_id="r1", type="assistant/message", payload={"content": "b"}))
    chunks = [first] + list(gen2)
    assert any('"assistant/message"' in c for c in chunks)


def test_approval_flow_approve(tmp_path):
    done = threading.Event()

    def fake_runner(run_id, ticker, objective, events):
        events.append(Event(run_id=run_id, type="research/done", payload={"ticker": ticker}))
        done.set()

    client, events = make_client(tmp_path, research_runner=fake_runner)
    resp = client.post(
        "/api/research",
        json={"ticker": "AAPL", "objective": "研究", "require_approval": True},
    )
    run_id = resp.json()["run_id"]

    # milestone 档：高成本操作挂起等待审批
    deadline = time.time() + 5
    pending = []
    while time.time() < deadline:
        pending = client.get("/api/approvals/pending").json()
        if pending:
            break
        time.sleep(0.05)
    assert pending and pending[0]["detail"]["ticker"] == "AAPL"

    client.post(f"/api/approvals/{pending[0]['approval_id']}", json={"approved": True})
    assert done.wait(timeout=5)
    types = [e.type for e in events.read(run_id)]
    assert "approval/requested" in types and "approval/resolved" in types
    assert "research/done" in types


def test_approval_flow_reject(tmp_path):
    called = threading.Event()

    def fake_runner(run_id, ticker, objective, events):
        called.set()

    client, events = make_client(tmp_path, research_runner=fake_runner)
    resp = client.post(
        "/api/research",
        json={"ticker": "AAPL", "objective": "研究", "require_approval": True},
    )
    run_id = resp.json()["run_id"]

    deadline = time.time() + 5
    pending = []
    while time.time() < deadline:
        pending = client.get("/api/approvals/pending").json()
        if pending:
            break
        time.sleep(0.05)
    client.post(f"/api/approvals/{pending[0]['approval_id']}", json={"approved": False})

    deadline = time.time() + 5
    while time.time() < deadline:
        types = [e.type for e in events.read(run_id)]
        if "research/cancelled" in types:
            break
        time.sleep(0.05)
    assert not called.is_set()  # 被拒 → 研究未执行
    assert "research/cancelled" in [e.type for e in events.read(run_id)]


def test_research_without_approval_runs_directly(tmp_path):
    done = threading.Event()

    def fake_runner(run_id, ticker, objective, events):
        events.append(Event(run_id=run_id, type="research/done", payload={}))
        done.set()

    client, _ = make_client(tmp_path, research_runner=fake_runner)
    resp = client.post("/api/research", json={"ticker": "AAPL", "objective": "x"})
    assert resp.status_code == 200
    assert done.wait(timeout=5)
