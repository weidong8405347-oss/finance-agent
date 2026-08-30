"""统一入口 /api/chat 的契约（redesign §3.6）：

- 无 session → 开新 run：session/title + system 契约先于 user/message 落库
- 自然语言 → 主 agent turn（ChatService 认领）
- /command → 确定性派发（CommandRunner），不经主 agent
- 会话列表：排除子 run、带标题与 last_active；children 端点列出 step agent 子 run
"""

import time

from fastapi.testclient import TestClient

from finance_agent.api.app import create_app
from finance_agent.chat.service import ChatService
from finance_agent.commands.runner import CommandRunner
from finance_agent.commands.steps import StepDeps
from finance_agent.decision.service import DecisionService
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.gateway import DataGateway
from finance_agent.harness.approvals import ApprovalService
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.llm.base import AssistantReply
from finance_agent.llm.mock import MockLLM
from finance_agent.main_agent import MainAgent


def make_client(tmp_path, scripts=None):
    """真实装配（API 层），LLM 为脚本化 Mock（唯一外部边界的替身）。"""
    events = EventStore(tmp_path / "e.db")
    kb = BitemporalStore(tmp_path / "kb.db")
    writer = ProfileWriter(store=kb, events=events)
    gateway = DataGateway(mode="live", events=events, run_id="live-t")
    approvals = ApprovalService(events)
    main_llm = MockLLM(scripts or [AssistantReply(content="好的")])
    step_llm = MockLLM([AssistantReply(content="done")])

    deps = StepDeps(
        events=events, kb=kb, writer=writer, gateway=gateway,
        decisions=DecisionService(kb=kb, decisions=DecisionStore(tmp_path / "d.db"), events=events),
        llm_for=lambda role: step_llm,
        approvals=approvals,
        evals_dir=tmp_path / "evals",
        reports_dir=tmp_path / "reports",
        max_rounds=1,
    )
    runner = CommandRunner(deps)
    chat = ChatService(
        events=events,
        make_main_agent=lambda rid: MainAgent(
            run_id=rid, events=events, kb=kb, gateway=gateway, llm=main_llm, commands=runner,
        ),
    )
    runner.set_wake(chat.wake)
    app = create_app(
        kb=kb, events=events, decisions=DecisionStore(tmp_path / "d2.db"),
        evals_dir=tmp_path / "evals", chat_service=chat, command_runner=runner,
        approvals=approvals,
    )
    return TestClient(app), events


def wait_event(events, run_id, pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = [e for e in events.read(run_id) if pred(e)]
        if found:
            return found
        time.sleep(0.02)
    raise AssertionError("等待事件超时")


def test_plain_message_runs_main_agent_turn(tmp_path):
    client, events = make_client(tmp_path)
    resp = client.post("/api/chat", json={"message": "你好"})
    run_id = resp.json()["run_id"]

    wait_event(events, run_id, lambda e: e.type == "assistant/message" and e.payload.get("content") == "好的")
    types = [e.type for e in events.read(run_id)]
    # session/title 与 system 契约先于 user/message
    assert types[0] == "session/title"
    assert types[1] == "context/inject"
    assert types[2] == "user/message"
    assert "turn/start" in types and "turn/end" in types


def test_slash_command_dispatches_without_main_agent(tmp_path):
    client, events = make_client(tmp_path)
    resp = client.post("/api/chat", json={"message": "/frobnicate BE"})
    run_id = resp.json()["run_id"]
    assert resp.json()["command_id"]

    wait_event(events, run_id, lambda e: e.type == "command/done")
    done = [e for e in events.read(run_id) if e.type == "command/done"][0]
    assert done.payload["outcome"] == "unknown"
    # 确定性派发：不产生主 agent turn
    assert not [e for e in events.read(run_id) if e.type == "turn/start"]


def test_slash_research_runs_pipeline(tmp_path):
    client, events = make_client(tmp_path)
    resp = client.post("/api/chat", json={"message": "/research BE"})
    run_id = resp.json()["run_id"]
    done = wait_event(events, run_id, lambda e: e.type == "command/done")[0]
    assert done.payload["outcome"] == "completed"
    steps = [e.payload["step"] for e in events.read(run_id) if e.type == "step_agent/start"]
    assert steps == ["research", "process_eval"]


def test_sessions_exclude_child_runs_and_carry_title(tmp_path):
    client, events = make_client(tmp_path)
    r = client.post("/api/chat", json={"message": "/research BE"})
    run_id = r.json()["run_id"]
    wait_event(events, run_id, lambda e: e.type == "command/done")

    sessions = client.get("/api/sessions").json()
    ids = [s["run_id"] for s in sessions]
    assert run_id in ids
    assert not any("--" in i for i in ids), "子 run 不得出现在会话列表"
    sess = [s for s in sessions if s["run_id"] == run_id][0]
    assert sess["title"] == "/research BE"
    assert sess["last_active"]

    children = client.get(f"/api/sessions/{run_id}/children").json()
    assert len(children) == 2
    assert {c["step"] for c in children} == {"research", "process_eval"}


def test_commands_catalog_endpoint(tmp_path):
    client, _ = make_client(tmp_path)
    cmds = {c["name"]: c for c in client.get("/api/commands").json()}
    assert set(cmds) == {"research", "profile", "decide", "evaluate"}
    assert cmds["evaluate"]["needs_approval"] is True


def test_chat_continues_same_session(tmp_path):
    client, events = make_client(tmp_path)
    r1 = client.post("/api/chat", json={"message": "你好"}).json()["run_id"]
    r2 = client.post("/api/chat", json={"session_id": r1, "message": "再说说"}).json()["run_id"]
    assert r2 == r1
    wait_event(events, r1, lambda e: e.type == "turn/end")
    titles = [e for e in events.read(r1) if e.type == "session/title"]
    assert len(titles) == 1, "title 只在首条消息生成"
