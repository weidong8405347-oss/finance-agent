"""research-rubric：LLM-as-judge 软反馈（D4——软反馈，永不参与硬判定）。

验收：rubric 评分落事件；judge 给出的缺口出现在下一轮的 brief 里；
judge 解析失败不影响循环（advisory 的定位就是「失败无害」）。
"""

from datetime import UTC, datetime

from finance_agent.eventstore.store import EventStore
from finance_agent.gateway.gateway import DataGateway
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter
from finance_agent.llm.base import AssistantReply, ToolCall
from finance_agent.llm.mock import MockLLM
from finance_agent.research.loop import ResearchLoop

NOW = datetime(2024, 6, 1, tzinfo=UTC)


def run_two_rounds(tmp_path, judge_llm):
    kb = BitemporalStore(tmp_path / "kb.db")
    events = EventStore(tmp_path / "e.db")
    writer = ProfileWriter(store=kb, events=events)
    gateway = DataGateway(mode="live", events=events, run_id="live-1")
    manifest = RunManifest(run_id="live-1", mode=RunMode.LIVE)

    research_llm = MockLLM(
        [
            # round 1：query → read 正文 → register（chunk 逐字摘录）→ 写一个字段
            AssistantReply(
                content="",
                tool_calls=[ToolCall(call_id="c0", name="query_edgar", arguments={"ticker": "AAPL"})],
            ),
            AssistantReply(
                content="",
                tool_calls=[ToolCall(call_id="c0b", name="read_edgar_filing",
                                     arguments={"chunk_id": "chk-0001", "query": "revenue"})],
            ),
            AssistantReply(
                content="",
                tool_calls=[
                    ToolCall(
                        call_id="c1",
                        name="register_evidence",
                        arguments={
                            "evidence_id": "ev-1",
                            "chunk_id": "chk-0002",
                            "verbatim_quote": "revenue 100",
                        },
                    )
                ],
            ),
            AssistantReply(
                content="",
                tool_calls=[
                    ToolCall(
                        call_id="c2",
                        name="propose_fact",
                        arguments={"field": "revenue_fy", "value": 100, "evidence_ids": ["ev-1"]},
                    )
                ],
            ),
            AssistantReply(content="r1 done"),
            # round 2：无新发现
            AssistantReply(content="r2: no progress"),
        ]
    )
    from finance_agent.gateway.adapters.fixture import FixtureAdapter
    from finance_agent.gateway.models import DataRecord, SourceCapability
    from finance_agent.knowledge.models import PitGrade

    gateway.register(FixtureAdapter(
        SourceCapability(source_id="edgar", pit_grade=PitGrade.A, description="夹具"),
        records=[DataRecord(
            source_id="edgar", payload={"form": "10-K"},
            available_at=datetime(2024, 3, 1, tzinfo=UTC), url="demo://10k",
        )],
    ))
    loop = ResearchLoop(
        store=kb,
        events=events,
        writer=writer,
        gateway=gateway,
        llm=research_llm,
        manifest=manifest,
        max_rounds=3,
        completeness_target=1.0,
        judge_llm=judge_llm,
        gateway_sources=["edgar"],
        fetch_document=lambda url: "revenue 100 in fy2024.",
    )
    loop.run("stock", "AAPL", objective="研究", now=NOW)
    return events, research_llm


def test_rubric_feedback_flows_into_next_round(tmp_path):
    judge = MockLLM(
        [
            AssistantReply(
                content='{"completeness": 3, "evidence_quality": 4, "counter_evidence": 1,'
                ' "coherence": 4, "gaps": ["缺反方证据", "缺估值数据"], "notes": "尚可"}'
            ),
            AssistantReply(
                content='{"completeness": 3, "evidence_quality": 4, "counter_evidence": 1,'
                ' "coherence": 4, "gaps": [], "notes": "停滞"}'
            ),
        ]
    )
    events, research_llm = run_two_rounds(tmp_path, judge)

    rubric_events = events.read("live-1", types={"research/rubric"})
    assert len(rubric_events) == 2  # 两轮各评一次
    assert rubric_events[0].payload["scores"]["counter_evidence"] == 1

    # judge 的缺口出现在第二轮的研究 brief 里（软反馈驱动 loop）
    round2_messages = research_llm.received[-1]
    brief = next(m for m in round2_messages if m["role"] == "user" and "第 2 轮" in m["content"])
    assert "缺反方证据" in brief["content"]


def test_rubric_parse_failure_is_harmless(tmp_path):
    judge = MockLLM([AssistantReply(content="这不是 JSON"), AssistantReply(content="仍不是")])
    events, _ = run_two_rounds(tmp_path, judge)
    # 循环正常完成；rubric 事件带解析失败标记而非崩溃
    rubric_events = events.read("live-1", types={"research/rubric"})
    assert rubric_events[0].payload.get("parse_error") is True
