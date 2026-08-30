"""SSE 事件流 + 审批机制（milestone 档：高成本操作需人工批准）。

审批的触发面已迁至 command 层（/evaluate 默认强制审批，redesign §3.2）；
本文件验证：approval/asked → decide → approval/decided 的全链路 + SSE 推送。
"""

import threading
import time

from fastapi.testclient import TestClient

from finance_agent.api.app import create_app
from finance_agent.api.sse import iter_sse_events
from finance_agent.commands.runner import CommandRunner
from finance_agent.commands.steps import StepDeps
from finance_agent.decision.service import DecisionService
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.events import Event
from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.gateway import DataGateway
from finance_agent.harness.approvals import ApprovalService
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.llm.base import AssistantReply
from finance_agent.llm.mock import MockLLM


def make_client(tmp_path, *, eval_runner):
    events = EventStore(tmp_path / "events.db")
    kb = BitemporalStore(tmp_path / "kb.db")
    (tmp_path / "evals" / "mandates").mkdir(parents=True)
    (tmp_path / "evals" / "mandates" / "wf.json").write_text("{}")
    approvals = ApprovalService(events)
    deps = StepDeps(
        events=events, kb=kb, writer=ProfileWriter(store=kb, events=events),
        gateway=DataGateway(mode="live", events=events, run_id="t"),
        decisions=DecisionService(kb=kb, decisions=DecisionStore(tmp_path / "d.db"), events=events),
        llm_for=lambda role: MockLLM([AssistantReply(content="done")]),
        approvals=approvals,
        evals_dir=tmp_path / "evals",
        reports_dir=tmp_path / "reports",
        eval_runner=eval_runner,
    )
    runner = CommandRunner(deps, approval_timeout_s=10.0)
    app = create_app(
        kb=kb, events=events, decisions=DecisionStore(tmp_path / "d2.db"),
        evals_dir=tmp_path / "evals", command_runner=runner, approvals=approvals,
    )
    return TestClient(app), events


def wait_pending(client, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = client.get("/api/approvals/pending").json()
        if pending:
            return pending
        time.sleep(0.05)
    raise AssertionError("等待审批请求超时")


def wait_outcome(events, run_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        done = [e for e in events.read(run_id) if e.type == "command/done"]
        if done:
            return done[0]
        time.sleep(0.05)
    raise AssertionError("等待 command/done 超时")


def test_sse_generator_yields_appended_events(tmp_path):
    events = EventStore(tmp_path / "e.db")
    events.append(Event(run_id="r1", type="user/message", payload={"content": "a"}))
    gen = iter_sse_events(events, "r1", max_polls=3, poll_interval=0.01)
    assert '"user/message"' in next(gen)

    gen2 = iter_sse_events(events, "r1", max_polls=5, poll_interval=0.01)
    first = next(gen2)
    events.append(Event(run_id="r1", type="assistant/message", payload={"content": "b"}))
    chunks = [first] + list(gen2)
    assert any('"assistant/message"' in c for c in chunks)


def test_approval_flow_approve(tmp_path):
    done = threading.Event()

    def fake_eval(**kw):
        done.set()
        return {"verdict": "clean"}

    client, events = make_client(tmp_path, eval_runner=fake_eval)
    resp = client.post("/api/chat", json={"message": "/evaluate wf"})
    run_id = resp.json()["run_id"]

    pending = wait_pending(client)
    assert pending[0]["detail"]["op"] == "/evaluate"
    assert not done.is_set(), "审批前不得执行"

    client.post(f"/api/approvals/{pending[0]['approval_id']}", json={"approved": True})
    assert done.wait(timeout=5)
    result = wait_outcome(events, run_id)
    assert result.payload["outcome"] == "completed"
    types = [e.type for e in events.read(run_id)]
    assert "approval/asked" in types and "approval/decided" in types


def test_approval_flow_reject(tmp_path):
    called = threading.Event()

    client, events = make_client(tmp_path, eval_runner=lambda **kw: called.set())
    resp = client.post("/api/chat", json={"message": "/evaluate wf"})
    run_id = resp.json()["run_id"]

    pending = wait_pending(client)
    client.post(f"/api/approvals/{pending[0]['approval_id']}", json={"approved": False})

    result = wait_outcome(events, run_id)
    assert result.payload["outcome"] == "rejected"
    assert not called.is_set()  # 被拒 → 评估未执行
