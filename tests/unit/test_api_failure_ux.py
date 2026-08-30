"""失败路径的用户体验契约（真实用户踩出来的盲区）：

1. 未配置 provider 时发消息 → 对话式报错立即可见（不是 200 后后台静默失败）
2. 会话列表带状态投影（idle/running/done/error/cancelled），错误可见
"""

from fastapi.testclient import TestClient

from finance_agent.api.app import create_app
from finance_agent.chat.service import ChatService
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.events import Event
from finance_agent.eventstore.store import EventStore
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.llm.router import ProviderConfigError
from finance_agent.main_agent import MainAgent


def make_client(tmp_path, *, provider_missing=False):
    events = EventStore(tmp_path / "e.db")
    kb = BitemporalStore(tmp_path / "kb.db")

    if provider_missing:
        def make_agent(run_id):
            raise ProviderConfigError("provider 'openai' 未配置（需要 OPENAI_API_KEY/BASE_URL/MODEL 三件套）")
    else:
        from finance_agent.gateway.gateway import DataGateway
        from finance_agent.llm.base import AssistantReply
        from finance_agent.llm.mock import MockLLM

        def make_agent(run_id):
            return MainAgent(
                run_id=run_id, events=events, kb=kb,
                gateway=DataGateway(mode="live", events=events, run_id="t"),
                llm=MockLLM([AssistantReply(content="好的")]),
                commands=None,  # type: ignore[arg-type]
            )

    chat = ChatService(events=events, make_main_agent=make_agent)
    app = create_app(
        kb=kb, events=events, decisions=DecisionStore(tmp_path / "d.db"),
        evals_dir=tmp_path / "evals", chat_service=chat,
    )
    return TestClient(app), events


def test_chat_without_provider_gets_actionable_reply(tmp_path):
    """provider 缺失：用户立刻在对话里看到可操作指引（替代旧 422 快速失败）。"""
    client, events = make_client(tmp_path, provider_missing=True)
    resp = client.post("/api/chat", json={"message": "帮我研究一下 AAPL"})
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    replies = [e for e in events.read(run_id) if e.type == "assistant/message"]
    assert replies and "未配置 LLM provider" in replies[0].payload["content"]
    assert "OPENAI_API_KEY" in replies[0].payload["content"]  # 可操作：告诉用户补什么


def test_session_status_projection(tmp_path):
    client, events = make_client(tmp_path)
    for run_id, type_, payload in [
        ("run-err", "research/error", {"reason": "no provider"}),
        ("run-done", "research/completed", {}),
        ("run-cancel", "research/cancelled", {}),
        ("run-live", "turn/start", {}),
    ]:
        events.append(Event(run_id=run_id, type=type_, payload=payload))

    sessions = {s["run_id"]: s for s in client.get("/api/sessions").json()}
    assert sessions["run-err"]["status"] == "error"
    assert sessions["run-done"]["status"] == "done"
    assert sessions["run-cancel"]["status"] == "cancelled"
    assert sessions["run-live"]["status"] == "running"
    assert sessions["run-err"]["status_detail"] == "no provider"  # 错误原因直接带出
