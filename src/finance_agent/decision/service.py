"""DecisionService：出卡唯一入口（risk-review 必达 + 快照绑定校验）。"""

from __future__ import annotations

from ..eventstore.events import DECISION_CARD, HOOK_VERDICT, Event
from ..eventstore.store import EventStore
from ..harness.manifest import RunManifest
from ..knowledge.snapshot import kb_snapshot_id
from ..knowledge.store import BitemporalStore
from .card import DecisionCard
from .risk_review import RiskPolicy, RiskReviewer
from .store import DecisionStore


class RiskReviewRejected(Exception):
    def __init__(self, violations: list[str]):
        self.violations = violations
        super().__init__("; ".join(violations))


class DecisionService:
    def __init__(
        self,
        *,
        kb: BitemporalStore,
        decisions: DecisionStore,
        events: EventStore | None = None,
        policy: RiskPolicy | None = None,
    ):
        self.kb = kb
        self.decisions = decisions
        self.events = events
        self._reviewer = RiskReviewer(kb, policy)

    def issue(self, card: DecisionCard, manifest: RunManifest, *, namespace: str = "prod") -> str:
        violations = self._reviewer.review(card, manifest, namespace=namespace)

        # 快照绑定：卡上声称的 kb_snapshot_id 必须等于 created_at 时刻的真实投影
        actual = kb_snapshot_id(
            self.kb, [(card.subject.kind, card.subject.id)], card.created_at, namespace=namespace
        )
        if actual != card.kb_snapshot_id:
            violations.append(
                f"kb_snapshot_id 与 {card.created_at.isoformat()} 时刻的档案投影不符"
            )

        if violations:
            self._emit(
                HOOK_VERDICT,
                manifest.run_id,
                {"hook": "risk-review", "verdict": "rejected", "violations": violations},
            )
            raise RiskReviewRejected(violations)

        self.decisions.insert(card, namespace=namespace)
        self._emit(
            DECISION_CARD,
            manifest.run_id,
            {
                "card_id": card.card_id,
                "namespace": namespace,
                "subject": f"{card.subject.kind}:{card.subject.id}",
                "action": card.action.value,
                "conviction": card.conviction,
                "horizon": card.horizon.value,
                "kb_snapshot_id": card.kb_snapshot_id,
                "created_at": card.created_at.isoformat(),
            },
        )
        return card.card_id

    def _emit(self, type_: str, run_id: str, payload: dict) -> None:
        if self.events is not None:
            self.events.append(Event(run_id=run_id, type=type_, payload=payload))
