"""numeric-guard（原则 8 数字保护）+ knowledge_time 不变量的写入侧执行。"""

from datetime import UTC, datetime

import pytest

from finance_agent.eventstore.store import EventStore
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.errors import KnowledgeInvariantError, NumericGuardError
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.store import BitemporalStore
from finance_agent.knowledge.writer import ProfileWriter

NOW = datetime(2024, 6, 1, tzinfo=UTC)
LIVE = RunManifest(run_id="live-1", mode=RunMode.LIVE)


def env(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    events = EventStore(tmp_path / "e.db")
    return kb, events, ProfileWriter(store=kb, events=events)


def add_evidence(kb, eid: str, quote: str, available_at: datetime = NOW):
    kb.add_evidence(
        Evidence(
            evidence_id=eid,
            source_id="edgar",
            verbatim_quote=quote,
            retrieved_at=NOW,
            available_at=available_at,
            pit_grade=PitGrade.A,
        )
    )


def write(writer, value, evidence_id, field="revenue_fy", knowledge_time=NOW):
    return writer.write_fact(
        Fact(
            entity_kind="stock",
            entity_id="AAPL",
            field=field,
            value=value,
            knowledge_time=knowledge_time,
            evidence_ids=[evidence_id],
        ),
        run=LIVE,
    )


def test_numeric_value_must_appear_in_evidence_quote(tmp_path):
    kb, events, writer = env(tmp_path)
    add_evidence(kb, "e1", "Total revenue was 100 million")
    write(writer, 100, "e1")  # 命中
    assert kb.history("stock", "AAPL", "revenue_fy")[-1].value == 100

    add_evidence(kb, "e2", "Total revenue was 90 million")
    with pytest.raises(NumericGuardError):
        write(writer, 100, "e2")  # 100 不在摘录里 → 拒绝
    verdicts = events.read("live-1", types={"hook/verdict"})
    assert verdicts[-1].payload["hook"] == "numeric-guard"
    assert verdicts[-1].payload["verdict"] == "rejected"


def test_numeric_guard_normalizes_commas_and_percent(tmp_path):
    kb, _, writer = env(tmp_path)
    add_evidence(kb, "e1", "revenue: $1,234.5 million")
    write(writer, 1234.5, "e1")
    add_evidence(kb, "e2", "gross margin 3.5%")
    write(writer, 3.5, "e2", field="gross_margin")  # 百分号只取数字部分


def test_non_numeric_value_passes_guard(tmp_path):
    kb, _, writer = env(tmp_path)
    add_evidence(kb, "e1", "sells phones and services")
    write(writer, "硬件+服务双轮", "e1", field="business_model")


def test_knowledge_time_must_not_precede_evidence(tmp_path):
    """事实不可能比它的证据更早可知（防穿越的基础不变量）。"""
    kb, events, writer = env(tmp_path)
    add_evidence(kb, "e1", "Total revenue was 100 million", available_at=NOW)
    with pytest.raises(KnowledgeInvariantError):
        write(writer, 100, "e1", knowledge_time=NOW.replace(year=2020))
    verdicts = events.read("live-1", types={"hook/verdict"})
    assert verdicts[-1].payload["hook"] == "knowledge-time-invariant"
