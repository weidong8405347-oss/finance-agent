"""最小 agent kernel 契约：turn/step 事件序 + 「模型可见 = 已记录」不变量。"""

from datetime import UTC, datetime

from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.adapters.fixture import FixtureAdapter
from finance_agent.gateway.gateway import DataGateway
from finance_agent.gateway.models import DataRecord, SourceCapability
from finance_agent.gateway.tools import make_gateway_tool
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.models import PitGrade
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.llm.mock import MockLLM
from finance_agent.loop.kernel import AgentKernel

T = datetime(2023, 6, 30, tzinfo=UTC)


def live_manifest():
    return RunManifest(run_id="run-1", mode=RunMode.LIVE)


def test_turn_event_sequence_without_tools(tmp_path):
    store = EventStore(tmp_path / "e.db")
    llm = MockLLM([AssistantReply(content="结论如下")])
    kernel = AgentKernel(store=store, llm=llm, manifest=live_manifest())
    out = kernel.run_turn("研究一下 AAPL")

    assert out == "结论如下"
    types = [e.type for e in store.read("run-1")]
    assert types == [
        "turn/start",
        "user/message",
        "step/start",
        "assistant/chunk",  # 流式增量（MockLLM 两段式）
        "assistant/chunk",
        "assistant/message",
        "step/end",
        "turn/end",
    ]


def test_model_receives_exactly_derived_messages(tmp_path):
    """不变量：发给模型的内容 == 日志投影。kernel 不存在第二条上下文通道。"""
    store = EventStore(tmp_path / "e.db")
    llm = MockLLM([AssistantReply(content="ok"), AssistantReply(content="ok2")])
    kernel = AgentKernel(store=store, llm=llm, manifest=live_manifest())
    kernel.run_turn("第一条")
    kernel.run_turn("第二条")

    for i, received in enumerate(llm.received):
        assert received == store.derive_messages("run-1", up_to_seq=llm.received_seqs[i])


def test_tool_call_roundtrip_with_provenance(tmp_path):
    store = EventStore(tmp_path / "e.db")
    gateway = DataGateway(mode="live", events=store, run_id="run-1")
    gateway.register(
        FixtureAdapter(
            SourceCapability(source_id="prices", pit_grade=PitGrade.A),
            [
                DataRecord(
                    source_id="prices",
                    payload={"close": 190.5},
                    available_at=datetime(2023, 6, 29, tzinfo=UTC),
                )
            ],
        )
    )
    llm = MockLLM(
        [
            AssistantReply(
                content="", tool_calls=[ToolCall(call_id="c1", name="query_prices", arguments={})]
            ),
            AssistantReply(content="收盘价 190.5"),
        ]
    )
    kernel = AgentKernel(
        store=store,
        llm=llm,
        manifest=live_manifest(),
        tools={"query_prices": make_gateway_tool(gateway, "prices")},
    )
    out = kernel.run_turn("AAPL 最近收盘价？")

    assert out == "收盘价 190.5"
    tool_results = store.read("run-1", types={"tool/result"})
    assert len(tool_results) == 1
    prov = tool_results[0].payload["provenance"][0]
    assert prov["source_id"] == "prices" and prov["pit_grade"] == "A"
    # 第二轮模型调用应看到 tool 结果（经投影）
    assert llm.received[1][-1]["role"] == "tool"


def test_max_steps_guard(tmp_path):
    store = EventStore(tmp_path / "e.db")
    llm = MockLLM(
        [
            AssistantReply(content="", tool_calls=[ToolCall(call_id=f"c{i}", name="noop", arguments={})])
            for i in range(10)
        ]
    )
    kernel = AgentKernel(
        store=store,
        llm=llm,
        manifest=live_manifest(),
        tools={"noop": lambda args: {"content": "ok", "provenance": []}},
        max_steps=3,
    )
    kernel.run_turn("loop")
    steps = store.read("run-1", types={"step/start"})
    assert len(steps) == 3


def test_tool_exception_is_elastic_not_turn_fatal(tmp_path):
    """Tool 弹性（原则 4 + 真实事故回归）：工具抛错 → tool/result 带错误内容，
    turn 继续，模型下一步可自我修正；不得整 turn 崩溃。"""
    from finance_agent.harness.manifest import RunManifest, RunMode
    from finance_agent.llm.base import AssistantReply, ToolCall
    from finance_agent.llm.mock import MockLLM
    from finance_agent.loop.kernel import AgentKernel

    def bad_tool(args):
        raise KeyError("entity_kind")

    llm = MockLLM([
        AssistantReply(content="", tool_calls=[ToolCall(call_id="c1", name="bad", arguments={})]),
        AssistantReply(content="", tool_calls=[ToolCall(call_id="c2", name="ok", arguments={})]),
        AssistantReply(content="已修正"),
    ])

    def ok_tool(args):
        return {"content": "ok", "provenance": []}

    store = EventStore(tmp_path / "e.db")
    kernel = AgentKernel(
        store=store,
        llm=llm,
        manifest=RunManifest(run_id="r1", mode=RunMode.LIVE),
        tools={"bad": bad_tool, "ok": ok_tool},
    )
    out = kernel.run_turn("go")
    assert out == "已修正"
    results = [e for e in store.read("r1") if e.type == "tool/result"]
    assert "entity_kind" in results[0].payload["content"]  # 错误内容回给模型
    assert not [e for e in store.read("r1") if e.type == "turn/error"]


def test_streaming_emits_chunks_then_message(tmp_path):
    """streaming：assistant/chunk 逐段落库（replay/UI 保真），assistant/message 仍是终态；
    chunk 不进模型可见投影（白名单不变）。"""
    from finance_agent.eventstore.events import ASSISTANT_CHUNK, MODEL_VISIBLE_TYPES
    from finance_agent.harness.manifest import RunManifest, RunMode
    from finance_agent.llm.base import AssistantReply
    from finance_agent.llm.mock import MockLLM
    from finance_agent.loop.kernel import AgentKernel

    llm = MockLLM([AssistantReply(content="流式回答全文")])
    store = EventStore(tmp_path / "e.db")
    kernel = AgentKernel(
        store=store, llm=llm,
        manifest=RunManifest(run_id="r1", mode=RunMode.LIVE), tools={},
    )
    out = kernel.run_turn("go")
    assert out == "流式回答全文"
    chunks = [e for e in store.read("r1") if e.type == ASSISTANT_CHUNK]
    assert len(chunks) == 2 and "".join(c.payload["text"] for c in chunks) == "流式回答全文"
    assert ASSISTANT_CHUNK not in MODEL_VISIBLE_TYPES
    # derive_messages 只见终态 message，不见 chunk
    msgs = store.derive_messages("r1")
    assert sum(1 for m in msgs if m.get("role") == "assistant") == 1
