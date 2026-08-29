"""L1 失败可见性（RCA 规矩 1：错误路径必须三通道齐全——事件 + 日志 + 用户可见状态）。"""

import logging
import time

from fastapi.testclient import TestClient

from finance_agent.api.app import create_app
from finance_agent.cli import make_research_runner
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.store import EventStore
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.logging_setup import mirror_events_to_logging, setup_logging


def make_client(tmp_path, *, runner, preflight=None, approval_timeout_s=600.0, caplog=None):
    events = EventStore(tmp_path / "e.db")
    if caplog is not None:
        mirror_events_to_logging(events, setup_logging("finance_agent_test_l1"))
    app = create_app(
        kb=BitemporalStore(tmp_path / "kb.db"),
        events=events,
        decisions=DecisionStore(tmp_path / "d.db"),
        evals_dir=tmp_path / "evals",
        research_runner=runner,
        research_preflight=preflight,
        approval_timeout_s=approval_timeout_s,
    )
    return TestClient(app), events


def wait_for(events, run_id, type_, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if events.read(run_id, types={type_}):
            return True
        time.sleep(0.05)
    return False


def test_mid_run_failure_three_channels(tmp_path, caplog, monkeypatch):
    """provider 配了但端点不可达 → 研究中途失败：事件 + ERROR 日志 + 会话状态 error。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")  # 端口 9 必拒连
    monkeypatch.setenv("OPENAI_MODEL", "gpt-x")
    monkeypatch.setattr("finance_agent.llm.router._read_dotenv", lambda *a: {})

    runner = make_research_runner(tmp_path)  # 真实装配，LLM 边界指向死端口
    client, events = make_client(tmp_path, runner=runner, caplog=caplog)

    with caplog.at_level(logging.INFO, logger="finance_agent_test_l1"):
        resp = client.post("/api/research", json={"ticker": "AAA", "objective": "x"})
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    # 通道 1：事件
    assert wait_for(events, run_id, "research/error")
    # 通道 2：日志（镜像）
    assert any("research/error" in r.getMessage() for r in caplog.records)
    # 通道 3：用户可见状态
    sessions = {s["run_id"]: s for s in client.get("/api/sessions").json()}
    assert sessions[run_id]["status"] == "error"
    assert sessions[run_id]["status_detail"]  # 错误原因非空


def test_approval_timeout_cancels_visibly(tmp_path, caplog):
    """审批超时（fail-closed 按拒绝处理）：事件 + 日志 + 状态 cancelled。"""
    runner_called = []

    client, events = make_client(
        tmp_path,
        runner=lambda *a: runner_called.append(a),
        approval_timeout_s=0.2,
        caplog=caplog,
    )
    with caplog.at_level(logging.INFO, logger="finance_agent_test_l1"):
        resp = client.post(
            "/api/research",
            json={"ticker": "AAA", "objective": "x", "require_approval": True},
        )
    run_id = resp.json()["run_id"]

    assert wait_for(events, run_id, "research/cancelled")
    assert not runner_called  # 未批准 → 研究未执行
    assert any(
        "research/cancelled" in r.getMessage() or "approval" in r.getMessage().lower()
        for r in caplog.records
    )
    sessions = {s["run_id"]: s for s in client.get("/api/sessions").json()}
    assert sessions[run_id]["status"] == "cancelled"
