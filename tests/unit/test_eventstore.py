"""EventStore 契约：append-only + 投影（模型可见 = 已记录）。"""

from finance_agent.eventstore.events import MODEL_VISIBLE_TYPES, Event
from finance_agent.eventstore.store import EventStore


def make_store(tmp_path):
    return EventStore(tmp_path / "events.db")


def test_append_assigns_increasing_seq(tmp_path):
    store = make_store(tmp_path)
    s1 = store.append(Event(run_id="r1", type="user/message", payload={"content": "a"}))
    s2 = store.append(Event(run_id="r1", type="assistant/message", payload={"content": "b"}))
    assert s2 > s1


def test_read_filters_by_type_and_up_to_seq(tmp_path):
    store = make_store(tmp_path)
    store.append(Event(run_id="r1", type="turn/start"))
    s2 = store.append(Event(run_id="r1", type="user/message", payload={"content": "x"}))
    store.append(Event(run_id="r1", type="assistant/message", payload={"content": "y"}))

    msgs = store.read("r1", types={"user/message"})
    assert len(msgs) == 1 and msgs[0].payload["content"] == "x"

    partial = store.read("r1", up_to_seq=s2)
    assert [e.type for e in partial] == ["turn/start", "user/message"]


def test_read_isolates_runs(tmp_path):
    store = make_store(tmp_path)
    store.append(Event(run_id="r1", type="user/message", payload={"content": "a"}))
    store.append(Event(run_id="r2", type="user/message", payload={"content": "b"}))
    assert [e.payload["content"] for e in store.read("r1")] == ["a"]


def test_derive_messages_only_model_visible_in_order(tmp_path):
    store = make_store(tmp_path)
    store.append(Event(run_id="r1", type="turn/start"))  # 非模型可见
    store.append(Event(run_id="r1", type="user/message", payload={"content": "研究一下 AAPL"}))
    store.append(Event(run_id="r1", type="step/start"))  # 非模型可见
    store.append(Event(run_id="r1", type="assistant/message", payload={"content": "好的"}))
    store.append(
        Event(
            run_id="r1",
            type="tool/result",
            payload={"call_id": "c1", "name": "query_price_history", "content": "...", "provenance": []},
        )
    )
    # tool/call 是审计用事件，不进模型上下文
    store.append(Event(run_id="r1", type="tool/call", payload={"name": "query_price_history"}))

    derived = store.derive_messages("r1")
    assert [m["role"] for m in derived] == ["user", "assistant", "tool"]
    assert derived[0]["content"] == "研究一下 AAPL"
    assert derived[2]["call_id"] == "c1"


def test_model_visible_types_do_not_include_audit_events():
    # 审计/钩子类事件永不可进入模型上下文
    for t in ("leakage/attempt", "hook/verdict", "fact/asserted", "turn/start", "step/start", "tool/call"):
        assert t not in MODEL_VISIBLE_TYPES


def test_append_only_surface(tmp_path):
    # append-only：公开 API 不提供 update/delete
    store = make_store(tmp_path)
    assert not hasattr(store, "update")
    assert not hasattr(store, "delete")
