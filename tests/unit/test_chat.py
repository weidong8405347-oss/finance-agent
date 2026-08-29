"""对话入口与意图路由契约：

POST /api/chat {session_id?, message}
- 无 session → 开新 run；user/message 落库
- 消息含标的 → 后台跑研究（或决策意图跑决策），SSE 流出过程
- 无标的 → 助手追问澄清（对话式，不静默）
"""

import time

from fastapi.testclient import TestClient

from finance_agent.api.app import create_app
from finance_agent.api.intent import classify_intent, extract_tickers
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.store import EventStore
from finance_agent.knowledge.store import BitemporalStore


def test_extract_tickers():
    assert extract_tickers("帮我研究一下 AAPL") == ["AAPL"]
    assert extract_tickers("600519 怎么样") == ["600519"]
    assert extract_tickers("对比 AAPL 和 MSFT") == ["AAPL", "MSFT"]
    assert extract_tickers("现在市场怎么样") == []
    # 常见英文词不误判
    assert extract_tickers("I think AI is OK") == []


def test_classify_intent():
    assert classify_intent("AAPL 可以买吗") == "decide"
    assert classify_intent("给个投资建议") == "decide"
    assert classify_intent("研究一下 AAPL") == "research"
    assert classify_intent("你好") == "chat"


def make_client(tmp_path, research_runner=None, decision_runner=None):
    events = EventStore(tmp_path / "e.db")
    app = create_app(
        kb=BitemporalStore(tmp_path / "kb.db"),
        events=events,
        decisions=DecisionStore(tmp_path / "d.db"),
        evals_dir=tmp_path / "evals",
        research_runner=research_runner,
        decision_runner=decision_runner,
    )
    return TestClient(app), events


def test_chat_without_ticker_asks_clarification(tmp_path):
    client, events = make_client(tmp_path, research_runner=lambda *a: None)
    resp = client.post("/api/chat", json={"message": "你好"})
    run_id = resp.json()["run_id"]
    msgs = [e for e in events.read(run_id) if e.type == "assistant/message"]
    assert msgs and "标的" in msgs[0].payload["content"]  # 追问而不是静默


def test_chat_with_ticker_launches_research(tmp_path):
    launched = []

    def runner(run_id, ticker, objective, events):
        launched.append((run_id, ticker, objective))

    client, events = make_client(tmp_path, research_runner=runner)
    resp = client.post("/api/chat", json={"message": "帮我深度研究一下 AAPL"})
    run_id = resp.json()["run_id"]

    deadline = time.time() + 3
    while time.time() < deadline and not launched:
        time.sleep(0.05)
    assert launched == [(run_id, "AAPL", "帮我深度研究一下 AAPL")]
    # user 消息在同一 run 里（多轮对话连续）
    assert any(
        e.type == "user/message" and e.payload["content"] == "帮我深度研究一下 AAPL"
        for e in events.read(run_id)
    )


def test_chat_decide_intent_launches_decision(tmp_path):
    decided = []

    client, _ = make_client(
        tmp_path,
        research_runner=lambda *a: None,
        decision_runner=lambda run_id, ticker, events: decided.append(ticker),
    )
    client.post("/api/chat", json={"message": "AAPL 现在可以买吗"})
    deadline = time.time() + 3
    while time.time() < deadline and not decided:
        time.sleep(0.05)
    assert decided == ["AAPL"]


def test_chat_continues_same_session(tmp_path):
    client, events = make_client(tmp_path, research_runner=lambda *a: None)
    r1 = client.post("/api/chat", json={"message": "你好"}).json()["run_id"]
    r2 = client.post("/api/chat", json={"session_id": r1, "message": "还是说下 600519"}).json()[
        "run_id"
    ]
    assert r2 == r1  # 同一 session 延续
