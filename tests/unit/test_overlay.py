"""评估命名空间叠加视图：eval 回放的知识状态 = 生产库 as_of(T) + eval 增量（D7）。

语义：prod 与 eval 命名空间按 (knowledge_time, version) 竞合——
「T 时点可知的一切」不区分是谁写入的；但反向不成立（eval 写入永不可见于 prod）。
"""

from datetime import UTC, datetime

from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.store import BitemporalStore

T = lambda s: datetime.fromisoformat(s).replace(tzinfo=UTC)  # noqa: E731


def make_store(tmp_path) -> BitemporalStore:
    store = BitemporalStore(tmp_path / "kb.db")
    for eid, at in [("e-old", "2022-03-01T00:00:00"), ("e-new", "2023-03-01T00:00:00")]:
        store.add_evidence(
            Evidence(
                evidence_id=eid,
                source_id="edgar",
                verbatim_quote="q",
                retrieved_at=T("2024-01-01T00:00:00"),
                available_at=T(at),
                pit_grade=PitGrade.A,
            )
        )
    return store


def fact(value, kt, eid, field="f1"):
    return Fact(
        entity_kind="stock",
        entity_id="AAPL",
        field=field,
        value=value,
        knowledge_time=T(kt),
        evidence_ids=[eid],
    )


def test_overlay_merges_prod_base_and_eval_increment(tmp_path):
    store = make_store(tmp_path)
    store.assert_fact(fact(100, "2023-03-01T00:00:00", "e-new"), namespace="prod")
    store.assert_fact(fact(90, "2022-03-01T00:00:00", "e-old"), namespace="eval:run-1")
    store.assert_fact(fact("x", "2022-03-01T00:00:00", "e-old", field="f2"), namespace="eval:run-1")

    overlay = store.as_of_overlay("eval:run-1", T("2023-06-01T00:00:00"), "stock", "AAPL")
    # f1：prod 版本 knowledge_time 更新（2023-03）→ prod 胜；f2：仅 eval 有 → eval 补位
    assert overlay["f1"].value == 100
    assert overlay["f2"].value == "x"


def test_overlay_excludes_prod_facts_after_T(tmp_path):
    store = make_store(tmp_path)
    store.assert_fact(fact(100, "2023-03-01T00:00:00", "e-new"), namespace="prod")
    # T 在 prod 事实可知之前 → overlay 为空
    assert store.as_of_overlay("eval:run-1", T("2023-01-15T00:00:00"), "stock", "AAPL") == {}


def test_overlay_does_not_leak_eval_writes_into_prod(tmp_path):
    store = make_store(tmp_path)
    store.assert_fact(fact(90, "2022-03-01T00:00:00", "e-old"), namespace="eval:run-1")
    assert store.as_of("stock", "AAPL", T("2023-06-01T00:00:00"), namespace="prod") == {}
