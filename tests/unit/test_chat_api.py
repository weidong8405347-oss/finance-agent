"""统一入口 /api/chat 的契约（redesign §3.6）：

- 无 session → 开新 run：session/title + system 契约先于 user/message 落库
- 自然语言 → 主 agent turn（ChatService 认领）
- /command → 确定性派发（CommandRunner），不经主 agent
- 会话列表：排除子 run、带标题与 last_active；children 端点列出 step agent 子 run
"""

import threading
import time

from fastapi.testclient import TestClient

from finance_agent.api.app import create_app
from finance_agent.chat.service import ChatService
from finance_agent.commands.runner import CommandRunner
from finance_agent.commands.steps import StepDeps
from finance_agent.decision.service import DecisionService
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.adapters.fixture import FixtureAdapter
from finance_agent.gateway.gateway import DataGateway
from finance_agent.gateway.models import DataRecord, SourceCapability
from finance_agent.harness.approvals import ApprovalService
from finance_agent.knowledge.models import PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.llm.mock import MockLLM
from finance_agent.main_agent import MainAgent

#: 研究 step 脚本：走完「检索→登记证据→写事实」的最小闭环。
#: 必须有进展——stalled 语义升级后一轮零写入 = blocked，会拦停管道，
#: 覆盖不到 synthesize/process_eval 的接线（research-capability-upgrade §4.3）。
RESEARCH_SCRIPT = [
    AssistantReply(content="", tool_calls=[ToolCall(call_id="c0", name="query_demo", arguments={})]),
    AssistantReply(content="", tool_calls=[ToolCall(call_id="c1", name="read_edgar_filing",
                                               arguments={"chunk_id": "chk-0001", "query": "产能"})]),
    AssistantReply(content="", tool_calls=[ToolCall(call_id="c2", name="register_evidence", arguments={
        "evidence_id": "ev-1", "chunk_id": "chk-0002", "verbatim_quote": "产能 2GW 公告"})]),
    AssistantReply(content="", tool_calls=[ToolCall(call_id="c3", name="propose_fact", arguments={
        "field": "capacity", "value": "2GW", "evidence_ids": ["ev-1"]})]),
    AssistantReply(content="round1 done"),
]


def make_client(tmp_path, scripts=None, step_llm_factory=None):
    """真实装配（API 层），LLM 为脚本化 Mock（唯一外部边界的替身）。"""
    events = EventStore(tmp_path / "e.db")
    kb = BitemporalStore(tmp_path / "kb.db")
    writer = ProfileWriter(store=kb, events=events)
    gateway = DataGateway(mode="live", events=events, run_id="live-t")
    gateway.register(FixtureAdapter(
        SourceCapability(source_id="demo", pit_grade=PitGrade.C,
                         server_side_asof=False, description="夹具演示源"),
        records=[DataRecord(source_id="demo", payload={"form": "10-K", "accession": "demo-1"},
                            url="demo://filing")],
    ))
    approvals = ApprovalService(events)
    main_llm = MockLLM(scripts or [AssistantReply(content="好的")])
    # 每个 step 独立实例（MockLLM 是一次性脚本，共享会被抽干）；
    # 缺省工厂：首个 step（research）走闭环脚本，后续 step 用纯文本补全
    queue = [RESEARCH_SCRIPT]

    def _default_factory():
        return MockLLM(queue.pop(0) if queue else [AssistantReply(content="## 摘要\n完成。")])

    factory = step_llm_factory or _default_factory

    deps = StepDeps(
        events=events, kb=kb, writer=writer, gateway=gateway,
        decisions=DecisionService(kb=kb, decisions=DecisionStore(tmp_path / "d.db"), events=events),
        llm_for=lambda role: factory(),
        approvals=approvals,
        evals_dir=tmp_path / "evals",
        reports_dir=tmp_path / "reports",
        knowledge_dir=tmp_path / "knowledge",
        max_rounds=1,
        fetch_document=lambda url: "产能 2GW 公告。demo 正文。",
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

    # 等 turn 结束（turn/end 是一个 turn 的完成边界；assistant/message 在其前落库）
    wait_event(events, run_id, lambda e: e.type == "turn/end")
    types = [e.type for e in events.read(run_id)]
    assert any(
        e.type == "assistant/message" and e.payload.get("content") == "好的"
        for e in events.read(run_id)
    )
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
    assert steps == ["research", "synthesize", "process_eval"]


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
    assert len(children) == 3
    assert {c["step"] for c in children} == {"research", "synthesize", "process_eval"}


def test_commands_catalog_endpoint(tmp_path):
    client, _ = make_client(tmp_path)
    cmds = {c["name"]: c for c in client.get("/api/commands").json()}
    assert set(cmds) == {"research", "profile", "decide", "evaluate", "industry"}
    assert cmds["evaluate"]["needs_approval"] is True


def test_chat_continues_same_session(tmp_path):
    client, events = make_client(tmp_path)
    r1 = client.post("/api/chat", json={"message": "你好"}).json()["run_id"]
    r2 = client.post("/api/chat", json={"session_id": r1, "message": "再说说"}).json()["run_id"]
    assert r2 == r1
    wait_event(events, r1, lambda e: e.type == "turn/end")
    titles = [e for e in events.read(r1) if e.type == "session/title"]
    assert len(titles) == 1, "title 只在首条消息生成"


# ---------------- steer（Q6 后置项：运行中改方向注入子 run） ----------------


def gated_factory(gate: threading.Event, first_seen: threading.Event):
    """门控 step LLM：第一次模型调用挂起，直到主线程放行（steer 在此期间注入）。"""

    class _Gated:
        def complete(self, messages, tools):
            first_seen.set()
            assert gate.wait(timeout=5), "等待 steer 超时"
            return AssistantReply(content="done")

    return _Gated


def test_steer_slash_injects_into_running_command(tmp_path):
    gate, first_seen = threading.Event(), threading.Event()
    client, events = make_client(tmp_path, step_llm_factory=gated_factory(gate, first_seen))
    run_id = client.post("/api/chat", json={"message": "/research BE"}).json()["run_id"]
    assert first_seen.wait(timeout=5)

    resp = client.post("/api/chat", json={"session_id": run_id, "message": "/steer 重点看竞对"})
    assert resp.status_code == 200
    steered = resp.json()["steered"]
    assert isinstance(steered, list) and len(steered) == 1
    assert steered[0]["delivered"] is True

    # 会话流落 steer/requested（UI 可见）；当前 child run 落改向注入
    req = wait_event(events, run_id, lambda e: e.type == "steer/requested")[0]
    assert req.payload["message"] == "重点看竞对"
    child = steered[0]["child_run_id"]
    assert any(
        e.type == "context/inject" and "重点看竞对" in str(e.payload.get("content", ""))
        for e in events.read(child)
    )

    gate.set()
    wait_event(events, run_id, lambda e: e.type == "command/done")
    # /steer 是会话级控制动作：不派发 command、不起主 agent turn
    assert not [e for e in events.read(run_id) if e.type == "command/run"
                and e.payload.get("name") == "steer"]


def test_steer_endpoint_mirrors_stop(tmp_path):
    gate, first_seen = threading.Event(), threading.Event()
    client, events = make_client(tmp_path, step_llm_factory=gated_factory(gate, first_seen))
    run_id = client.post("/api/chat", json={"message": "/research BE"}).json()["run_id"]
    assert first_seen.wait(timeout=5)

    resp = client.post(f"/api/sessions/{run_id}/steer", json={"message": "改看财务质量"})
    assert resp.status_code == 200
    assert len(resp.json()["steered"]) == 1
    wait_event(events, run_id, lambda e: e.type == "steer/requested")
    gate.set()
    wait_event(events, run_id, lambda e: e.type == "command/done")


def test_steer_without_active_command(tmp_path):
    client, events = make_client(tmp_path)
    # 端点：无活跃 → 空列表（镜像 /stop 的 {"stopped": None}）
    r = client.post("/api/sessions/live-x/steer", json={"message": "改向"})
    assert r.status_code == 200 and r.json()["steered"] == []
    # 端点空消息 → 422
    assert client.post("/api/sessions/live-x/steer", json={"message": ""}).status_code == 422
    # chat /steer 无活跃 → 对话式警告（不静默）
    r2 = client.post("/api/chat", json={"message": "/steer 改个方向"})
    assert r2.status_code == 200
    run_id = r2.json()["run_id"]
    warned = wait_event(events, run_id, lambda e: e.type == "assistant/message")[0]
    assert "没有" in warned.payload["content"]
    # 裸 /steer → 用法提示
    r3 = client.post("/api/chat", json={"message": "/steer"})
    run_id3 = r3.json()["run_id"]
    usage = wait_event(events, run_id3, lambda e: e.type == "assistant/message")[0]
    assert "用法" in usage.payload["content"]
