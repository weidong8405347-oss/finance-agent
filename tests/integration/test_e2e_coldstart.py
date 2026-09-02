"""L3 冷启动 E2E（真实装配全旅程，RCA 规矩 2）。

模拟新用户：全新数据目录 → 起服务（cli 同一条真实装配路径 build_orchestrator）→ 两种结局：
1. 没配 provider → 对话式报错立即可见（含修复指引），不产生后台垃圾 run
2. 配了假 provider（端点不可达）→ command 派发 → step 失败 → 会话状态 error 且原因可读
"""

import time

from fastapi.testclient import TestClient

from finance_agent.api.app import create_app
from finance_agent.cli import build_orchestrator


def cold_app(tmp_path, monkeypatch):
    for var in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("finance_agent.llm.router._read_dotenv", lambda *a: {})
    from finance_agent.llm.router import LLMRouter

    monkeypatch.setattr("finance_agent.cli._router", lambda data_dir=None: LLMRouter.from_env())
    orch = build_orchestrator(tmp_path)  # 真实装配（serve 同一路径）
    # 预检探活属网络边界（工程约定 2）——冷启动测试主题是 LLM/配置失败可见性，
    # 实例级 stub 掉数据源探活，防离线环境影响断言。
    orch["command_runner"]._deps.gateway.preflight = lambda **kw: {}  # noqa: SLF001
    return TestClient(create_app(
        kb=orch["kb"], events=orch["events"], decisions=orch["decisions"].decisions,
        evals_dir=orch["evals_dir"], chat_service=orch["chat_service"],
        command_runner=orch["command_runner"], approvals=orch["approvals"],
    ))


def test_coldstart_without_provider_actionable_reply(tmp_path, monkeypatch):
    client = cold_app(tmp_path, monkeypatch)
    resp = client.post("/api/chat", json={"message": "帮我深度研究一下 AAPL"})
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    sessions = {s["run_id"]: s for s in client.get("/api/sessions").json()}
    assert run_id in sessions
    events = client.get(f"/api/sessions/{run_id}/events").json()
    replies = [e for e in events if e["type"] == "assistant/message"]
    # 对话式报错：可见 + 可操作（指引补什么配置）
    assert replies and "未配置 LLM provider" in replies[-1]["payload"]["content"]
    assert "OPENAI_API_KEY" in replies[-1]["payload"]["content"]


def test_coldstart_dead_provider_visible_failure(tmp_path, monkeypatch):
    # 本测试主题是失败可见性而非退避耐力：重试预算降为 1（否则 4 次退避 × 慢响应超轮询窗口）
    monkeypatch.setenv("FINANCE_AGENT_LLM_RETRY_ATTEMPTS", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-x")
    monkeypatch.setattr("finance_agent.llm.router._read_dotenv", lambda *a: {})
    from finance_agent.llm.router import LLMRouter

    monkeypatch.setattr("finance_agent.cli._router", lambda data_dir=None: LLMRouter.from_env())
    orch = build_orchestrator(tmp_path)
    # 预检探活属网络边界（工程约定 2）：stub 为全通过，防离线环境影响断言
    orch["command_runner"]._deps.gateway.preflight = lambda **kw: {}  # noqa: SLF001
    client = TestClient(create_app(
        kb=orch["kb"], events=orch["events"], decisions=orch["decisions"].decisions,
        evals_dir=orch["evals_dir"], chat_service=orch["chat_service"],
        command_runner=orch["command_runner"], approvals=orch["approvals"],
    ))

    resp = client.post("/api/chat", json={"message": "/research AAA"})
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
