"""L3 冷启动 E2E（真实装配全旅程，RCA 规矩 2）。

模拟新用户：全新数据目录 → 起服务（真实 cli 装配）→ 发研究的两种结局：
1. 没配 provider → 立即 422，报错文案含修复指引，不产生垃圾 run
2. 配了假 provider（端点不可达）→ 200 → 失败 → 会话状态 error 且原因可读
"""

import time

from fastapi.testclient import TestClient

from finance_agent.api.app import create_app
from finance_agent.cli import _research_preflight, make_research_runner
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.store import EventStore
from finance_agent.knowledge.store import BitemporalStore


def cold_app(tmp_path):
    events = EventStore(tmp_path / "e.db")
    return create_app(
        kb=BitemporalStore(tmp_path / "kb.db"),
        events=events,
        decisions=DecisionStore(tmp_path / "d.db"),
        evals_dir=tmp_path / "evals",
        research_runner=make_research_runner(tmp_path),  # 真实装配（cli 同一条路径）
        research_preflight=_research_preflight,
    )


def test_coldstart_without_provider_actionable_422(tmp_path, monkeypatch):
    for var in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("finance_agent.llm.router._read_dotenv", lambda *a: {})
    # 钉住 provider 解析接缝：只看 env（隔离本机真实 pi 配置，防误打付费 API）
    from finance_agent.llm.router import LLMRouter

    monkeypatch.setattr("finance_agent.cli._router", lambda: LLMRouter.from_env())

    client = TestClient(cold_app(tmp_path))
    resp = client.post("/api/research", json={"ticker": "AAPL", "objective": "深度研究"})

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert ".env" in detail and "OPENAI_API_KEY" in detail  # 含修复指引
    assert client.get("/api/sessions").json() == []  # 快速失败不落垃圾 run


def test_coldstart_dead_provider_visible_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-x")
    monkeypatch.setattr("finance_agent.llm.router._read_dotenv", lambda *a: {})
    # 钉住 provider 解析接缝：只看 env（隔离本机真实 pi 配置，防误打付费 API）
    from finance_agent.llm.router import LLMRouter

    monkeypatch.setattr("finance_agent.cli._router", lambda: LLMRouter.from_env())

    client = TestClient(cold_app(tmp_path))
    resp = client.post("/api/research", json={"ticker": "AAPL", "objective": "深度研究"})
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    deadline = time.time() + 8
    status = {}
    while time.time() < deadline:
        sessions = {s["run_id"]: s for s in client.get("/api/sessions").json()}
        status = sessions.get(run_id, {})
        if status.get("status") == "error":
            break
        time.sleep(0.1)
    assert status["status"] == "error"
    assert status["status_detail"]  # 用户能在 UI 看到原因
