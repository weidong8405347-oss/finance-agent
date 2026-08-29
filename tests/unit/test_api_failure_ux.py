"""失败路径的用户体验契约（真实用户踩出来的盲区）：

1. 未配置 provider 时点「研究」→ 立即 422 + 可操作的报错（而不是 200 后后台静默失败）
2. 会话列表带状态投影（running/done/error/cancelled），错误可见
"""

import threading

from fastapi.testclient import TestClient

from finance_agent.api.app import create_app
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.events import Event
from finance_agent.eventstore.store import EventStore
from finance_agent.knowledge.store import BitemporalStore


def make_client(tmp_path, *, preflight=None, research_runner=None):
    app = create_app(
        kb=BitemporalStore(tmp_path / "kb.db"),
        events=EventStore(tmp_path / "e.db"),
        decisions=DecisionStore(tmp_path / "d.db"),
        evals_dir=tmp_path / "evals",
        research_runner=research_runner,
        research_preflight=preflight,
    )
    return TestClient(app)


def test_research_without_provider_fails_fast_422(tmp_path):
    client = make_client(
        tmp_path,
        preflight=lambda: "未配置 LLM provider：请在 .env 配置 OPENAI_API_KEY/BASE_URL/MODEL 三件套",
        research_runner=lambda *a: None,
    )
    resp = client.post("/api/research", json={"ticker": "AAPL", "objective": "x"})
    assert resp.status_code == 422
    assert ".env" in resp.json()["detail"]
    # 不得产生任何 run 事件（快速失败，不落垃圾 run）
    assert client.get("/api/sessions").json() == []


def test_research_with_provider_ok(tmp_path):
    done = threading.Event()

    def runner(run_id, ticker, objective, events):
        done.set()

    client = make_client(tmp_path, preflight=lambda: None, research_runner=runner)
    resp = client.post("/api/research", json={"ticker": "AAPL", "objective": "x"})
    assert resp.status_code == 200
    assert done.wait(timeout=5)


def test_session_status_projection(tmp_path):
    client = make_client(tmp_path)
    events = EventStore(tmp_path / "e.db")

    events.append(Event(run_id="run-err", type="research/error", payload={"reason": "no provider"}))
    events.append(Event(run_id="run-done", type="research/completed", payload={}))
    events.append(Event(run_id="run-cancel", type="research/cancelled", payload={}))
    events.append(Event(run_id="run-live", type="turn/start"))

    sessions = {s["run_id"]: s for s in client.get("/api/sessions").json()}
    assert sessions["run-err"]["status"] == "error"
    assert sessions["run-done"]["status"] == "done"
    assert sessions["run-cancel"]["status"] == "cancelled"
    assert sessions["run-live"]["status"] == "running"
    # 错误原因直接带出，UI 不用翻 payload
    assert sessions["run-err"]["status_detail"] == "no provider"
