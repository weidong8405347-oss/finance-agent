"""双时态知识库契约——P0 的穿越用例核心。

两个业界公认的大坑（见 docs/best-practices-evaluation.md §4）都必须有用例：
1. reporting lag：财年结束 ≠ 可知（10-K 两个月后才提交）
2. restatement：重述不得静默覆盖历史版本
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from finance_agent.knowledge.errors import ConflictError, MissingEvidenceError
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.snapshot import kb_snapshot_id
from finance_agent.knowledge.store import BitemporalStore

T = lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC)  # noqa: E731


def make_store(tmp_path):
    return BitemporalStore(tmp_path / "kb.db")


def ev(eid: str, available_at: str | None, grade: PitGrade = PitGrade.A) -> Evidence:
    return Evidence(
        evidence_id=eid,
        source_id="edgar",
        url="https://example.org/filing",
        verbatim_quote="Revenue was 100 million",
        retrieved_at=T("2024-03-01T00:00:00"),
        available_at=T(available_at) if available_at else None,
        pit_grade=grade,
    )


def revenue_fact(value: int, event_time: str, knowledge_time: str, evidence_id: str) -> Fact:
    return Fact(
        entity_kind="stock",
        entity_id="AAPL",
        field="revenue_fy",
        value=value,
        event_time=T(event_time),
        knowledge_time=T(knowledge_time),
        evidence_ids=[evidence_id],
    )


# ---------- 双时态语义 ----------


def test_reporting_lag_fact_invisible_before_knowledge_time(tmp_path):
    """FY2023 财报（event_time=2023-12-31）直到 2024-02-15 提交才可知。"""
    store = make_store(tmp_path)
    store.add_evidence(ev("e1", "2024-02-15T00:00:00"))
    store.assert_fact(revenue_fact(100, "2023-12-31T00:00:00", "2024-02-15T00:00:00", "e1"))

    # 1 月中：财年已结束，但市场还不知道 → 看不到
    assert store.as_of("stock", "AAPL", T("2024-01-15T00:00:00")) == {}
    # 3 月：已发布 → 可见
    assert store.as_of("stock", "AAPL", T("2024-03-01T00:00:00"))["revenue_fy"].value == 100


def test_restatement_versions_preserved_and_conflict_flagged(tmp_path):
    """重述产生新版本：as_of 回到过去仍见旧值；值变化 → conflict 标记；supersedes 链完整。"""
    store = make_store(tmp_path)
    store.add_evidence(ev("e1", "2023-03-01T00:00:00"))
    store.add_evidence(ev("e2", "2024-05-01T00:00:00"))
    v1 = store.assert_fact(revenue_fact(100, "2022-12-31T00:00:00", "2023-03-01T00:00:00", "e1"))
    v2 = store.assert_fact(revenue_fact(120, "2022-12-31T00:00:00", "2024-05-01T00:00:00", "e2"))

    assert store.as_of("stock", "AAPL", T("2023-06-01T00:00:00"))["revenue_fy"].value == 100
    assert store.as_of("stock", "AAPL", T("2024-06-01T00:00:00"))["revenue_fy"].value == 120

    hist = store.history("stock", "AAPL", "revenue_fy")
    assert [f.value for f in hist] == [100, 120]
    assert hist[1].supersedes == v1 and hist[1].version == 2
    assert hist[1].conflict_flag is True
    assert hist[0].fact_id == v1 and v2 == hist[1].fact_id

    conflicts = store.open_conflicts("stock", "AAPL")
    assert [c.field for c in conflicts] == ["revenue_fy"]


def test_same_value_update_is_not_conflict(tmp_path):
    store = make_store(tmp_path)
    store.add_evidence(ev("e1", "2023-03-01T00:00:00"))
    store.add_evidence(ev("e2", "2023-04-01T00:00:00"))
    store.assert_fact(revenue_fact(100, "2022-12-31T00:00:00", "2023-03-01T00:00:00", "e1"))
    store.assert_fact(revenue_fact(100, "2022-12-31T00:00:00", "2023-04-01T00:00:00", "e2"))
    assert store.open_conflicts("stock", "AAPL") == []


# ---------- 写入纪律（schema 级硬约束） ----------


def test_evidence_without_available_at_must_be_grade_c():
    with pytest.raises(ValidationError):
        ev("eX", None, grade=PitGrade.A)  # A 级必须给出 available_at


def test_fact_requires_at_least_one_evidence():
    f = revenue_fact(100, "2022-12-31T00:00:00", "2023-03-01T00:00:00", "e1")
    f.evidence_ids = []
    with pytest.raises(ValidationError):
        Fact(**f.model_dump())


def test_assert_fact_rejects_unknown_evidence(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(MissingEvidenceError):
        store.assert_fact(revenue_fact(100, "2022-12-31T00:00:00", "2023-03-01T00:00:00", "ghost"))


def test_duplicate_evidence_id_rejected(tmp_path):
    store = make_store(tmp_path)
    store.add_evidence(ev("e1", "2023-03-01T00:00:00"))
    with pytest.raises(ConflictError):
        store.add_evidence(ev("e1", "2023-03-01T00:00:00"))


# ---------- 命名空间隔离（生产 vs 评估） ----------


def test_namespace_isolation(tmp_path):
    """eval 命名空间的写入对生产不可见，反之亦然（单向阀的存储层基础）。"""
    store = make_store(tmp_path)
    store.add_evidence(ev("e1", "2023-03-01T00:00:00"))
    store.assert_fact(
        revenue_fact(100, "2022-12-31T00:00:00", "2023-03-01T00:00:00", "e1"),
        namespace="eval:run-9",
    )
    assert store.as_of("stock", "AAPL", T("2023-06-01T00:00:00"), namespace="prod") == {}
    assert (
        store.as_of("stock", "AAPL", T("2023-06-01T00:00:00"), namespace="eval:run-9")["revenue_fy"].value
        == 100
    )


# ---------- 快照可复现 ----------


def test_kb_snapshot_id_stable_and_time_sensitive(tmp_path):
    store = make_store(tmp_path)
    store.add_evidence(ev("e1", "2023-03-01T00:00:00"))
    store.add_evidence(ev("e2", "2024-05-01T00:00:00"))
    store.assert_fact(revenue_fact(100, "2022-12-31T00:00:00", "2023-03-01T00:00:00", "e1"))
    store.assert_fact(revenue_fact(120, "2022-12-31T00:00:00", "2024-05-01T00:00:00", "e2"))

    ents = [("stock", "AAPL")]
    t1 = T("2023-06-01T00:00:00")
    assert kb_snapshot_id(store, ents, t1) == kb_snapshot_id(store, ents, t1)  # 稳定
    snap_t2 = kb_snapshot_id(store, ents, T("2024-06-01T00:00:00"))
    assert kb_snapshot_id(store, ents, t1) != snap_t2  # 跨版本不同


def test_conflict_only_when_same_event_time(tmp_path):
    """冲突语义收窄（Q2）：同 event_time 不同值 = 真冲突；不同 event_time = 演进不冲突。"""
    from datetime import UTC, datetime

    from finance_agent.knowledge.models import Evidence, Fact, PitGrade

    kb = BitemporalStore(tmp_path / "kb.db")
    kb.add_evidence(Evidence(
        evidence_id="e1", source_id="s", verbatim_quote="q1",
        retrieved_at=datetime(2024, 1, 1, tzinfo=UTC),
        available_at=datetime(2024, 1, 1, tzinfo=UTC), pit_grade=PitGrade.A))
    t1, t2 = datetime(2023, 12, 31, tzinfo=UTC), datetime(2024, 12, 31, tzinfo=UTC)
    # 演进：不同 event_time 不同值 → 不冲突
    kb.assert_fact(Fact(entity_kind="stock", entity_id="A", field="rev", value=100,
                        event_time=t1, knowledge_time=datetime(2024, 3, 1, tzinfo=UTC), evidence_ids=["e1"]))
    kb.assert_fact(Fact(entity_kind="stock", entity_id="A", field="rev", value=110,
                        event_time=t2, knowledge_time=datetime(2025, 3, 1, tzinfo=UTC), evidence_ids=["e1"]))
    assert kb.open_conflicts("stock", "A") == []
    # 同 event_time 不同值 → 真冲突
    kb.assert_fact(Fact(entity_kind="stock", entity_id="A", field="rev", value=105,
                        event_time=t2, knowledge_time=datetime(2025, 4, 1, tzinfo=UTC), evidence_ids=["e1"]))
    assert len(kb.open_conflicts("stock", "A")) == 1
    # 裁决闭环
    n = kb.resolve_conflict("stock", "A", "rev", keep_fact_id="")
    assert n == 1 and kb.open_conflicts("stock", "A") == []
