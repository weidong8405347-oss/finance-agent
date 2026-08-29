"""DecisionCard schema 契约（DESIGN.md §5.3）：不可变、强校验、证据引用制。"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from finance_agent.decision.card import (
    Action,
    DecisionCard,
    Horizon,
    Position,
    Subject,
)
from finance_agent.harness.manifest import RunMode

NOW = datetime(2024, 6, 1, tzinfo=UTC)


def make_card(**overrides) -> DecisionCard:
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
        invalidation=["若营收增速连续两季 < 5%，论点失效"],
        position=Position(sizing_pct=0.1, max_loss_pct=0.05),
        kb_snapshot_id="sha256:abc",
        created_at=NOW,
    )
    base.update(overrides)
    return DecisionCard(**base)


def test_valid_card():
    card = make_card()
    assert card.action is Action.BUY and card.conviction == 4


def test_card_is_immutable():
    card = make_card()
    with pytest.raises(ValidationError):
        card.conviction = 1  # type: ignore[misc]


def test_conviction_range():
    with pytest.raises(ValidationError):
        make_card(conviction=0)
    with pytest.raises(ValidationError):
        make_card(conviction=6)


def test_rationale_and_invalidation_required():
    with pytest.raises(ValidationError):
        make_card(rationale=[])
    with pytest.raises(ValidationError):
        make_card(invalidation=[])


def test_trade_action_requires_position():
    with pytest.raises(ValidationError):
        make_card(position=None)  # buy 无仓位
    # watch/avoid 可以无仓位
    card = make_card(action=Action.WATCH, position=None)
    assert card.position is None


def test_position_bounds():
    with pytest.raises(ValidationError):
        make_card(position=Position(sizing_pct=1.5, max_loss_pct=0.05))
    with pytest.raises(ValidationError):
        make_card(position=Position(sizing_pct=0.1, max_loss_pct=-0.01))
