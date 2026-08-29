"""risk-review hook（必达硬门禁，DESIGN.md §5.3）：

不出卡的情形：无失效条件（schema 已挡）、无仓位上限（schema 已挡）、
证据链不完整（rationale 引用未登记/越界证据、thesis 引用不存在字段、
快照哈希与当刻档案不符由 service 层校验）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..harness.manifest import RunManifest, RunMode
from ..knowledge.errors import MissingEvidenceError
from ..knowledge.store import BitemporalStore
from .card import Action, DecisionCard

_TRADE_ACTIONS = {Action.BUY, Action.SELL}


@dataclass(frozen=True)
class RiskPolicy:
    max_position_pct: float = 0.2  # 单标的仓位上限


class RiskReviewer:
    def __init__(self, kb: BitemporalStore, policy: RiskPolicy | None = None):
        self._kb = kb
        self._policy = policy or RiskPolicy()

    def review(self, card: DecisionCard, manifest: RunManifest, *, namespace: str = "prod") -> list[str]:
        """返回违规清单；空 = 通过。"""
        violations: list[str] = []

        # 1) 仓位上限
        if (
            card.action in _TRADE_ACTIONS
            and card.position is not None
            and card.position.sizing_pct > self._policy.max_position_pct
        ):
            violations.append(
                f"仓位 {card.position.sizing_pct:.0%} 超上限 {self._policy.max_position_pct:.0%}"
            )

        # 2) 证据链完整 + 不得引用「未来可知」的证据
        for eid in card.rationale:
            try:
                ev = self._kb.get_evidence(eid)
            except MissingEvidenceError:
                violations.append(f"证据 {eid} 未登记")
                continue
            if ev.available_at is not None and ev.available_at > card.created_at:
                violations.append(
                    f"证据 {eid} 的 available_at（{ev.available_at.isoformat()}）晚于决策时刻"
                )

        # 3) thesis_points 必须引用决策时刻档案中真实存在的字段
        profile = self._kb.as_of(card.subject.kind, card.subject.id, card.created_at, namespace=namespace)
        for field in card.thesis_points:
            if field not in profile:
                violations.append(f"thesis 引用了档案中不存在的字段: {field}")

        # 4) eval 模式：决策时刻必须等于回放时刻；证据必须有 PIT 时刻且不越界
        if manifest.mode is RunMode.EVAL:
            assert manifest.eval_as_of is not None
            if card.created_at != manifest.eval_as_of:
                violations.append(
                    f"eval 决策卡 created_at（{card.created_at.isoformat()}）"
                    f"必须等于 eval_as_of（{manifest.eval_as_of.isoformat()}）"
                )
            for eid in card.rationale:
                try:
                    ev = self._kb.get_evidence(eid)
                except MissingEvidenceError:
                    continue  # 已在 (2) 记录
                if ev.available_at is None or ev.available_at > manifest.eval_as_of:
                    violations.append(f"eval 模式引用了无 PIT 时刻或越界的证据: {eid}")

        return violations
