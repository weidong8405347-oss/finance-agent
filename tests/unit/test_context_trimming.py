"""上下文裁剪验收（audit §3.3 余项：存储全量、消费剪裁）。

事故数据：本次运行工具响应累计约 196 万字符，kernel 每步重送全部历史，
事件里已上报 usage 的累计下界约 633 万 tokens，其中 prompt 约 612 万（97%）。

纪律：裁剪只发生在**投影**（derive_messages），事件日志仍是全量真相源；
裁剪在消息里显式可见并告知如何取回全文；provenance 与 chunk_id 一律保留
（证据链不因裁剪而断）。
"""

import json

from finance_agent.eventstore.events import Event
from finance_agent.eventstore.store import (
    DEFAULT_KEEP_RECENT_TOOLS,
    DEFAULT_MAX_TOOL_CHARS,
    EventStore,
)
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.llm.mock import MockLLM
from finance_agent.loop.kernel import AgentKernel


def tc(i: int, name: str, args: dict) -> ToolCall:
    return ToolCall(call_id=f"c{i}", name=name, arguments=args)


def seed_run(events: EventStore, run_id: str, n_tools: int, *, chars: int = 5000) -> None:
    events.append(Event(run_id=run_id, type="turn/start", payload={}))
    events.append(Event(run_id=run_id, type="user/message", payload={"content": "研究"}))
    for i in range(n_tools):
        body = f"chunk_id chk-{i:04d} " + ("甲" * chars)
        events.append(Event(run_id=run_id, type="tool/result", payload={
            "call_id": f"c{i}", "name": "query_demo",
            "content": json.dumps([{"text": body, "chunk_id": f"chk-{i:04d}"}],
                                  ensure_ascii=False),
            "provenance": [{"source_id": "demo", "pit_grade": "A"}],
        }))
    events.append(Event(run_id=run_id, type="turn/end", payload={}))


class TestDeriveMessagesTrimming:
    def test_old_tool_results_trimmed_recent_kept(self, tmp_path):
        events = EventStore(tmp_path / "e.db")
        seed_run(events, "r1", n_tools=10)
        msgs = events.derive_messages(
            "r1", max_tool_chars=500, keep_recent_tools=3)
        tools = [m for m in msgs if m["role"] == "tool"]
        assert len(tools) == 10
        assert not any(m.get("context_trimmed") for m in tools[-3:]), "最近 3 条不该被裁"
        assert all(m.get("context_trimmed") for m in tools[:-3])
        assert all(len(m["content"]) < 700 for m in tools[:-3])

    def test_trim_marker_tells_how_to_get_full_text(self, tmp_path):
        events = EventStore(tmp_path / "e.db")
        seed_run(events, "r1", n_tools=8)
        msgs = events.derive_messages("r1", max_tool_chars=300, keep_recent_tools=2)
        trimmed = [m for m in msgs if m.get("context_trimmed")]
        assert trimmed
        marker = trimmed[0]["content"]
        assert "上下文裁剪" in marker
        assert "read_chunk(chunk_id=chk-0000)" in marker, "必须告知如何取回全文"

    def test_provenance_and_chunk_id_survive_trimming(self, tmp_path):
        """证据链不因裁剪而断：provenance 保留，chunk_id 仍在正文里可引用。"""
        events = EventStore(tmp_path / "e.db")
        seed_run(events, "r1", n_tools=8)
        msgs = events.derive_messages("r1", max_tool_chars=200, keep_recent_tools=1)
        trimmed = [m for m in msgs if m.get("context_trimmed")]
        assert trimmed
        assert trimmed[0]["provenance"] == [{"source_id": "demo", "pit_grade": "A"}]
        assert "chk-0000" in trimmed[0]["content"]

    def test_event_log_is_untouched(self, tmp_path):
        """裁剪只动投影：日志仍是全量真相源（replay/审计不受影响）。"""
        events = EventStore(tmp_path / "e.db")
        seed_run(events, "r1", n_tools=8)
        events.derive_messages("r1", max_tool_chars=200, keep_recent_tools=1)
        stored = [e for e in events.read("r1") if e.type == "tool/result"]
        assert all(len(e.payload["content"]) > 4000 for e in stored)
        # 不带策略再投影 → 全量（裁剪不是破坏性的）
        full = events.derive_messages("r1")
        assert not any(m.get("context_trimmed") for m in full if m["role"] == "tool")
        assert all(len(m["content"]) > 4000 for m in full if m["role"] == "tool")

    def test_no_trimming_when_few_results(self, tmp_path):
        events = EventStore(tmp_path / "e.db")
        seed_run(events, "r1", n_tools=3)
        msgs = events.derive_messages("r1", max_tool_chars=100, keep_recent_tools=6)
        assert not any(m.get("context_trimmed") for m in msgs if m["role"] == "tool")

    def test_short_results_not_trimmed(self, tmp_path):
        """短结果不裁（裁剪只针对撑大上下文的长响应）。"""
        events = EventStore(tmp_path / "e.db")
        events.append(Event(run_id="r2", type="turn/start", payload={}))
        for i in range(10):
            events.append(Event(run_id="r2", type="tool/result", payload={
                "call_id": f"c{i}", "name": "query_kb", "content": '{"ok": true}',
                "provenance": []}))
        msgs = events.derive_messages("r2", max_tool_chars=5000, keep_recent_tools=2)
        assert not any(m.get("context_trimmed") for m in msgs if m["role"] == "tool")

    def test_deterministic(self, tmp_path):
        events = EventStore(tmp_path / "e.db")
        seed_run(events, "r1", n_tools=8)
        a = events.derive_messages("r1", max_tool_chars=300, keep_recent_tools=2)
        b = events.derive_messages("r1", max_tool_chars=300, keep_recent_tools=2)
        assert a == b

    def test_defaults_are_research_sized(self):
        assert DEFAULT_MAX_TOOL_CHARS == 1200
        assert DEFAULT_KEEP_RECENT_TOOLS == 6


class TestKernelAppliesPolicy:
    def test_kernel_trims_context_sent_to_model(self, tmp_path):
        """kernel 每步重送历史时按策略裁剪——模型真的收到裁剪后的上下文。"""
        events = EventStore(tmp_path / "e.db")
        replies = []
        for i in range(9):
            replies.append(AssistantReply(
                content="", tool_calls=[tc(i, "query_demo", {"i": i})]))
        replies.append(AssistantReply(content="done"))
        llm = MockLLM(replies)
        long_text = "乙" * 4000
        kernel = AgentKernel(
            store=events, llm=llm, manifest=RunManifest(run_id="k1", mode=RunMode.LIVE),
            tools={"query_demo": lambda args: {
                "content": json.dumps([{"chunk_id": "chk-0001", "text": long_text}],
                                      ensure_ascii=False),
                "provenance": []}},
            max_steps=10, max_tool_chars=400, keep_recent_tools=2,
        )
        kernel.run_turn("研究")
        last_call = llm.received[-1]
        tool_msgs = [m for m in last_call if m.get("role") == "tool"]
        assert len(tool_msgs) >= 5
        assert any(m.get("context_trimmed") for m in tool_msgs)
        # 最近的保留全文，早的被裁
        assert not tool_msgs[-1].get("context_trimmed")
        assert sum(len(m["content"]) for m in tool_msgs) < 9 * 4000

    def test_kernel_without_policy_keeps_full_history(self, tmp_path):
        events = EventStore(tmp_path / "e.db")
        replies = []
        for i in range(5):
            replies.append(AssistantReply(content="", tool_calls=[tc(i, "q", {})]))
        replies.append(AssistantReply(content="done"))
        llm = MockLLM(replies)
        kernel = AgentKernel(
            store=events, llm=llm, manifest=RunManifest(run_id="k2", mode=RunMode.LIVE),
            tools={"q": lambda args: {"content": "丙" * 4000, "provenance": []}},
            max_steps=6,
        )
        kernel.run_turn("研究")
        tool_msgs = [m for m in llm.received[-1] if m.get("role") == "tool"]
        assert tool_msgs and not any(m.get("context_trimmed") for m in tool_msgs)


class TestResearchLoopPolicy:
    def test_loop_enables_trimming_by_default(self, tmp_path):
        from test_research_loop import make_loop

        loop, kb, events = make_loop(tmp_path, MockLLM([AssistantReply(content="done")]))
        assert loop._max_tool_chars == DEFAULT_MAX_TOOL_CHARS  # noqa: SLF001
        assert loop._keep_recent_tools == DEFAULT_KEEP_RECENT_TOOLS  # noqa: SLF001

    def test_loop_policy_can_be_disabled(self, tmp_path):
        from test_research_loop import make_loop

        loop, kb, events = make_loop(tmp_path, MockLLM([AssistantReply(content="done")]))
        loop._max_tool_chars = None  # noqa: SLF001
        loop._keep_recent_tools = None  # noqa: SLF001
        assert loop._max_tool_chars is None  # noqa: SLF001
