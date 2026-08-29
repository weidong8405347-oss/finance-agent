"""risk-review 硬门禁：无失效条件 / 无仓位上限 / 证据链不完整 → 不出卡。"""

from datetime import UTC, datetime

from finance_agent.decision.card import (
    Action,
    DecisionCard,
    Horizon,
    Position,
    Subject,
)
from finance_agent.decision.risk_review import RiskPolicy, RiskReviewer
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.store import BitemporalStore

NOW = datetime(2024, 6, 1, tzinfo=UTC)
T_EVAL = datetime(2023, 6, 30, tzinfo=UTC)


def kb_with_evidence(tmp_path):
    kb = BitemporalStore(tmp_path / "kb.db")
    kb.add_evidence(
        Evidence(
            evidence_id="ev-1",
            source_id="edgar",
            verbatim_quote="Total revenue 100",
            retrieved_at=NOW,
            available_at=datetime(2023, 3, 1, tzinfo=UTC),
            pit_grade=PitGrade.A,
        )
    )
    kb.assert_fact(
        Fact(
            entity_kind="stock",
            entity_id="AAPL",
            field="revenue_fy",
            value=100,
            knowledge_time=datetime(2023, 3, 1, tzinfo=UTC),
            evidence_ids=["ev-1"],
        )
    )
    return kb


def card(**overrides) -> DecisionCard:
    base = dict(
        card_id="card-1",
        run_id="run-1",
        mode=RunMode.LIVE,
        subject=Subject(kind="stock", id="AAPL"),
        action=Action.BUY,
        conviction=4,
        horizon=Horizon.M6,
        rationale=["ev-1"],
        thesis_points=["revenue_fy"],
        invalidation=["营收增速跌破阈值则失效"],
        position=Position(sizing_pct=0.1, max_loss_pct=0.05),
        kb_snapshot_id="sha256:abc",
        created_at=NOW,
    )
    base.update(overrides)
    return DecisionCard(**base)


def live() -> RunManifest:
    return RunManifest(run_id="run-1", mode=RunMode.LIVE)


def test_passes_clean_card(tmp_path):
    reviewer = RiskReviewer(kb_with_evidence(tmp_path))
    assert reviewer.review(card(), live()) == []


def test_rejects_oversized_position(tmp_path):
    reviewer = RiskReviewer(kb_with_evidence(tmp_path), policy=RiskPolicy(max_position_pct=0.2))
    violations = reviewer.review(card(position=Position(sizing_pct=0.5, max_loss_pct=0.1)), live())
    assert any("仓位" in v for v in violations)


def test_rejects_unknown_evidence(tmp_path):
    reviewer = RiskReviewer(kb_with_evidence(tmp_path))
    violations = reviewer.review(card(rationale=["ev-ghost"]), live())
    assert any("ev-ghost" in v for v in violations)


def test_rejects_unregistered_thesis_field(tmp_path):
    """thesis_points 必须引用档案中实际存在的字段（论点可追溯）。"""
    reviewer = RiskReviewer(kb_with_evidence(tmp_path))
    violations = reviewer.review(card(thesis_points=["nonexistent_field"]), live())
    assert any("nonexistent_field" in v for v in violations)


def test_eval_rejects_evidence_after_as_of(tmp_path):
    kb = kb_with_evidence(tmp_path)
    reviewer = RiskReviewer(kb)
    manifest = RunManifest(run_id="eval-1", mode=RunMode.EVAL, eval_as_of=T_EVAL)
    # 人为构造一张引用 2023-03 证据的决策卡，但 created_at 设为 2023-01（证据尚不可知）
    bad = card(created_at=datetime(2023, 1, 15, tzinfo=UTC))
    violations = reviewer.review(bad, manifest)
    assert any("available_at" in v for v in violations)


def test_eval_created_at_must_equal_as_of(tmp_path):
    kb = kb_with_evidence(tmp_path)
    reviewer = RiskReviewer(kb)
    manifest = RunManifest(run_id="eval-1", mode=RunMode.EVAL, eval_as_of=T_EVAL)
    # 证据 2023-03-01 可知 < T=2023-06-30 ✓，但 created_at 必须等于 T
    mismatched = card(created_at=datetime(2023, 5, 1, tzinfo=UTC))
    violations = reviewer.review(mismatched, manifest)
    assert any("created_at" in v for v in violations)
    assert reviewer.review(card(created_at=T_EVAL), manifest) == []
