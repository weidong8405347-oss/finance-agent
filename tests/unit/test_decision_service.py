"""DecisionService：risk-review 硬门禁 + kb_snapshot 绑定 + 落库与事件。"""

from datetime import UTC, datetime

import pytest

from finance_agent.decision.card import (
    Action,
    DecisionCard,
    Horizon,
    Position,
    Subject,
)
from finance_agent.decision.service import DecisionService, RiskReviewRejected
from finance_agent.decision.store import DecisionStore
from finance_agent.eventstore.store import EventStore
from finance_agent.harness.manifest import RunManifest, RunMode
from finance_agent.knowledge.models import Evidence, Fact, PitGrade
from finance_agent.knowledge.snapshot import kb_snapshot_id
from finance_agent.knowledge.store import BitemporalStore

NOW = datetime(2024, 6, 1, tzinfo=UTC)
LIVE = RunManifest(run_id="run-1", mode=RunMode.LIVE)


def env(tmp_path):
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
    decisions = DecisionStore(tmp_path / "decisions.db")
    events = EventStore(tmp_path / "e.db")
    return kb, decisions, events, DecisionService(kb=kb, decisions=decisions, events=events)


def make_card(kb, **overrides) -> DecisionCard:
    snap = kb_snapshot_id(kb, [("stock", "AAPL")], NOW)
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
        kb_snapshot_id=snap,
        created_at=NOW,
    )
    base.update(overrides)
    return DecisionCard(**base)


def test_issue_persists_and_emits_event(tmp_path):
    kb, decisions, events, svc = env(tmp_path)
    card = make_card(kb)
    card_id = svc.issue(card, LIVE)

    assert decisions.get(card_id).action is Action.BUY
    emitted = events.read("run-1", types={"decision/card_issued"})
    assert len(emitted) == 1
    assert emitted[0].payload["kb_snapshot_id"] == card.kb_snapshot_id


def test_snapshot_mismatch_rejected(tmp_path):
    """快照哈希必须等于卡片 created_at 时刻的真实档案投影（防「声称基于某版知识」造假）。"""
    kb, decisions, _, svc = env(tmp_path)
    card = make_card(kb, kb_snapshot_id="sha256:fake")
    with pytest.raises(RiskReviewRejected):
        svc.issue(card, LIVE)
    assert decisions.list() == []


def test_review_rejection_emits_verdict_and_persists_nothing(tmp_path):
    kb, decisions, events, svc = env(tmp_path)
    bad = make_card(kb, rationale=["ev-ghost"])
    with pytest.raises(RiskReviewRejected):
        svc.issue(bad, LIVE)
    verdicts = events.read("run-1", types={"hook/verdict"})
    assert verdicts[-1].payload["hook"] == "risk-review"
    assert decisions.list() == []


def test_namespace_isolation(tmp_path):
    kb, decisions, _, svc = env(tmp_path)
    svc.issue(make_card(kb), LIVE, namespace="prod")
    assert len(decisions.list(namespace="prod")) == 1
    assert decisions.list(namespace="eval:run-9") == []
