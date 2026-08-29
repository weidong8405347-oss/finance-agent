"""P0 端到端小场景：生产写档案 → 评估回放隔离 + 写入侧纵深防御。

场景：
1. 生产模式研究 AAPL，落一条 2023-03 可知的营收事实（10-K filing date）。
2. 评估模式 T=2023-01-15 回放：
   - as_of(T) 看不到这条事实（reporting lag）；
   - eval 命名空间写入对生产不可见；
   - ProfileWriter 在 eval 模式拒绝 knowledge_time/available_at 越界的写入（防线 2）。
"""

from datetime import UTC, datetime

import pytest

from finance_agent.eventstore.store import EventStore
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.errors import KnowledgeLeakError
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.snapshot import kb_snapshot_id
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter

T = lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC)  # noqa: E731


def setup_env(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    events = EventStore(tmp_path / "events.db")
    writer = ProfileWriter(store=kb, events=events)
    kb.add_evidence(
        Evidence(
            evidence_id="10k-fy22",
            source_id="edgar",
            url="https://sec.gov/...",
            verbatim_quote="Total revenue 100",
            retrieved_at=T("2023-03-02T00:00:00"),
            available_at=T("2023-03-01T00:00:00"),
            pit_grade=PitGrade.A,
        )
    )
    kb.add_evidence(
        Evidence(
            evidence_id="10k-fy21",
            source_id="edgar",
            url="https://sec.gov/...fy21",
            verbatim_quote="Total revenue 90",
            retrieved_at=T("2022-03-02T00:00:00"),
            available_at=T("2022-03-01T00:00:00"),
            pit_grade=PitGrade.A,
        )
    )
    return kb, events, writer


def fact() -> Fact:
    return Fact(
        entity_kind="stock",
        entity_id="AAPL",
        field="revenue_fy",
        value=100,
        event_time=T("2022-12-31T00:00:00"),
        knowledge_time=T("2023-03-01T00:00:00"),  # = 10-K filing date
        evidence_ids=["10k-fy22"],
    )


def test_live_research_writes_profile(tmp_path):
    kb, events, writer = setup_env(tmp_path)
    live = RunManifest(run_id="live-1", mode=RunMode.LIVE)
    writer.write_fact(fact(), run=live)

    assert kb.as_of("stock", "AAPL", T("2023-06-01T00:00:00"))["revenue_fy"].value == 100
    assert [e.type for e in events.read("live-1", types={"fact/asserted"})] == ["fact/asserted"]


def old_fact() -> Fact:
    """FY2021 年报：2022-03-01 可知（在评估时刻 T=2023-01-15 之前）。"""
    return Fact(
        entity_kind="stock",
        entity_id="AAPL",
        field="revenue_fy",
        value=90,
        event_time=T("2021-12-31T00:00:00"),
        knowledge_time=T("2022-03-01T00:00:00"),
        evidence_ids=["10k-fy21"],
    )


def test_eval_replay_isolated_and_time_locked(tmp_path):
    kb, events, writer = setup_env(tmp_path)
    live = RunManifest(run_id="live-1", mode=RunMode.LIVE)
    writer.write_fact(fact(), run=live)

    eval_run = RunManifest(run_id="eval-1", mode=RunMode.EVAL, eval_as_of=T("2023-01-15T00:00:00"))

    # ① reporting lag：T 时点看不到 2023-03 才可知的 FY22 财报
    assert kb.as_of("stock", "AAPL", eval_run.eval_as_of) == {}

    # ② eval 命名空间可以正常写入 T 之前可知的事实，且与生产互不可见
    writer.write_fact(old_fact(), run=eval_run, namespace="eval:eval-1")
    eval_view = kb.as_of("stock", "AAPL", T("2023-06-01T00:00:00"), namespace="eval:eval-1")
    assert eval_view["revenue_fy"].value == 90
    assert kb.as_of("stock", "AAPL", T("2023-06-01T00:00:00"), namespace="prod")["revenue_fy"].value == 100
    assert kb.history("stock", "AAPL", "revenue_fy", namespace="prod")[-1].version == 1

    # ③ 快照可复现且时间敏感
    snap_t = kb_snapshot_id(kb, [("stock", "AAPL")], eval_run.eval_as_of, namespace="eval:eval-1")
    assert snap_t == kb_snapshot_id(kb, [("stock", "AAPL")], eval_run.eval_as_of, namespace="eval:eval-1")


def test_writer_rejects_future_evidence_in_eval_mode(tmp_path):
    kb, events, writer = setup_env(tmp_path)
    # T 在证据可知之前 → 防线 2（知识库层）拒绝
    eval_run = RunManifest(run_id="eval-1", mode=RunMode.EVAL, eval_as_of=T("2023-01-15T00:00:00"))
    with pytest.raises(KnowledgeLeakError):
        writer.write_fact(fact(), run=eval_run, namespace="eval:eval-1")
    assert [e.type for e in events.read("eval-1", types={"leakage/attempt"})] == ["leakage/attempt"]
    # 拒绝后不得有任何写入
    assert kb.as_of("stock", "AAPL", T("2024-01-01T00:00:00"), namespace="eval:eval-1") == {}
