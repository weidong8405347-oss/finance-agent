"""ResearchLoop 验收：轮次制迭代研究（DESIGN.md §5.1，P1 验收标准）。

脚本化 3 轮：完整度 0 → 0.4 → 0.8 → 1.0 单调提升后收敛；
所有落库事实绑证据；每轮落 IterationReport 事件。
"""

from datetime import UTC, datetime

from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.gateway import DataGateway
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.schema import STOCK_SCHEMA
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.llm.mock import MockLLM
from finance_agent.research.loop import ResearchLoop

NOW = datetime(2024, 6, 1, tzinfo=UTC)
FIELDS = list(STOCK_SCHEMA.required)  # 测试驱动 schema 的五个必填字段


def tc(i: int, name: str, args: dict) -> ToolCall:
    return ToolCall(call_id=f"c{i}", name=name, arguments=args)


def scripted_llm() -> MockLLM:
    """3 轮研究脚本：r1 写 2 字段，r2 写 2 字段，r3 写 1 字段（达 1.0 收敛）。"""
    f1, f2, f3, f4, f5 = FIELDS
    replies = [
        # ---- round 1
        AssistantReply(
            content="",
            tool_calls=[tc(1, "register_evidence", {
                "evidence_id": "ev-rev", "source_id": "edgar",
                "verbatim_quote": "Total revenue 100", "available_at": "2024-03-01T00:00:00+00:00",
                "pit_grade": "A",
            })],
        ),
        AssistantReply(
            content="",
            tool_calls=[tc(2, "propose_fact", {"field": f1, "value": 100, "evidence_ids": ["ev-rev"]})],
        ),
        AssistantReply(
            content="",
            tool_calls=[tc(3, "register_evidence", {
                "evidence_id": "ev-ni", "source_id": "edgar",
                "verbatim_quote": "Net income 25", "available_at": "2024-03-01T00:00:00+00:00",
                "pit_grade": "A",
            })],
        ),
        AssistantReply(
            content="",
            tool_calls=[tc(4, "propose_fact", {"field": f2, "value": 25, "evidence_ids": ["ev-ni"]})],
        ),
        AssistantReply(content="round1 done"),
        # ---- round 2
        AssistantReply(
            content="",
            tool_calls=[tc(5, "register_evidence", {
                "evidence_id": "ev-bm", "source_id": "edgar",
                "verbatim_quote": "sells phones and services", "available_at": "2024-03-01T00:00:00+00:00",
                "pit_grade": "A",
            })],
        ),
        AssistantReply(
            content="",
            tool_calls=[
                tc(6, "propose_fact", {"field": f3, "value": "硬件+服务", "evidence_ids": ["ev-bm"]})
            ],
        ),
        AssistantReply(
            content="",
            tool_calls=[tc(7, "register_evidence", {
                "evidence_id": "ev-moat", "source_id": "substack",
                "verbatim_quote": "ecosystem lock-in", "available_at": "2024-02-01T00:00:00+00:00",
                "pit_grade": "B",
            })],
        ),
        AssistantReply(
            content="",
            tool_calls=[
                tc(8, "propose_fact", {"field": f4, "value": "生态锁定", "evidence_ids": ["ev-moat"]})
            ],
        ),
        AssistantReply(content="round2 done"),
        # ---- round 3
        AssistantReply(
            content="",
            tool_calls=[tc(9, "register_evidence", {
                "evidence_id": "ev-risk", "source_id": "edgar",
                "verbatim_quote": "competition may intensify", "available_at": "2024-03-01T00:00:00+00:00",
                "pit_grade": "A",
            })],
        ),
        AssistantReply(
            content="",
            tool_calls=[
                tc(10, "propose_fact", {"field": f5, "value": "竞争加剧", "evidence_ids": ["ev-risk"]})
            ],
        ),
        AssistantReply(content="round3 done"),
    ]
    return MockLLM(replies)


def make_loop(tmp_path, llm, *, max_rounds=5):
    kb = BitemporalStore(tmp_path / "kb.db")
    events = EventStore(tmp_path / "e.db")
    writer = ProfileWriter(store=kb, events=events)
    gateway = DataGateway(mode="live", events=events, run_id="live-1")
    manifest = RunManifest(run_id="live-1", mode=RunMode.LIVE)
    loop = ResearchLoop(
        store=kb,
        events=events,
        writer=writer,
        gateway=gateway,
        llm=llm,
        manifest=manifest,
        max_rounds=max_rounds,
        completeness_target=1.0,
    )
    return loop, kb, events


def test_three_rounds_monotonic_completeness_then_converged(tmp_path):
    loop, kb, events = make_loop(tmp_path, scripted_llm())
    reports = loop.run("stock", "AAPL", objective="投资研究", now=NOW)

    assert loop.stop_reason == "converged"
    assert len(reports) == 3

    before = [r.completeness_before for r in reports]
    after = [r.completeness_after for r in reports]
    assert before[0] == 0.0
    assert before == sorted(before) and after == sorted(after)  # 单调提升
    assert after[-1] == 1.0

    # 档案终态：schema 必填字段全部就位
    profile = kb.as_of("stock", "AAPL", NOW)
    assert set(profile) == set(FIELDS)

    # 全部事实绑证据且 knowledge_time 由证据推导（= evidence.available_at）
    for rec in profile.values():
        assert len(rec.evidence_ids) >= 1
        assert rec.knowledge_time == datetime(2024, 3, 1, tzinfo=UTC) or rec.knowledge_time == datetime(
            2024, 2, 1, tzinfo=UTC
        )

    # 每轮都有迭代报告事件
    assert len(events.read("live-1", types={"research/round_end"})) == 3


def test_stall_when_round_writes_nothing(tmp_path):
    llm = MockLLM([AssistantReply(content="没有新发现"), AssistantReply(content="还是没有")])
    loop, _, events = make_loop(tmp_path, llm, max_rounds=5)
    loop.run("stock", "AAPL", objective="研究", now=NOW)
    assert loop.stop_reason == "stalled"
    assert len(events.read("live-1", types={"research/round_end"})) == 1


def test_rejected_fact_does_not_block_loop(tmp_path):
    """numeric-guard 拒绝的事实不落库、记入报告，循环继续。"""
    f1 = FIELDS[0]
    llm = MockLLM(
        [
            AssistantReply(
                content="",
                tool_calls=[tc(1, "register_evidence", {
                    "evidence_id": "ev-bad", "source_id": "edgar",
                    "verbatim_quote": "revenue was 90", "available_at": "2024-03-01T00:00:00+00:00",
                    "pit_grade": "A",
                })],
            ),
            AssistantReply(
                content="",
                # 价值 100 与摘录 90 不符 → 拒绝
                tool_calls=[tc(2, "propose_fact", {"field": f1, "value": 100, "evidence_ids": ["ev-bad"]})],
            ),
            AssistantReply(content="done"),
        ]
    )
    loop, kb, _ = make_loop(tmp_path, llm, max_rounds=1)
    reports = loop.run("stock", "AAPL", objective="研究", now=NOW)
    assert loop.stop_reason == "stalled"  # 无成功写入 → 停滞
    assert reports[0].facts_written == [] and len(reports[0].rejected) == 1
    assert kb.as_of("stock", "AAPL", NOW) == {}
