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
