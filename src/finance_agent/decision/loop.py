"""DecisionLoop（S3）：一个 turn —— 查档案 → 出卡（或选择不出卡）。

- created_at：生产 = 当前时刻；eval = manifest.eval_as_of（T）；
- kb_snapshot_id 由系统在出卡瞬间计算，不信任模型自报；
- risk-review 在服务层必达，被拒即本次不出卡（fail-closed）。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from ..eventstore.events import CONTEXT_INJECT, Event
from ..eventstore.store import EventStore
from ..harness.manifest import RunManifest, RunMode
from ..knowledge.store import BitemporalStore
from ..llm.base import LLM
from ..loop.kernel import AgentKernel
from .card import Action, DecisionCard, Horizon, Position, Subject
from .service import DecisionService, RiskReviewRejected

DECISION_CONTRACT = """\
你是投资决策官。基于档案（query_kb 可查）给出投资判断。纪律：
1. rationale 只能引用证据 id；thesis_points 只能引用档案字段名。
2. buy/sell 必须给出仓位与最大亏损预算；必须给出明确失效条件。
3. 证据不足时不要强行出卡——watch/avoid 或直接说明不出卡都是合法结论。
"""


class DecisionLoop:
    def __init__(
        self,
        *,
        kb: BitemporalStore,
        events: EventStore,
        decision_service: DecisionService,
        llm: LLM,
        manifest: RunManifest,
        namespace: str = "prod",
        max_steps: int = 8,
    ):
        self._kb = kb
        self._events = events
        self._svc = decision_service
        self._llm = llm
        self._manifest = manifest
        self._namespace = namespace
        self._max_steps = max_steps

    def run(self, entity_kind: str, entity_id: str, *, now: datetime | None = None) -> str | None:
        """跑一次决策，返回 card_id；被拒或模型选择不出卡 → None。"""
        # 决策时刻：eval 回放 = T；生产 = 当前
        if self._manifest.mode is RunMode.EVAL:
            assert self._manifest.eval_as_of is not None
            created_at = self._manifest.eval_as_of
        else:
            created_at = now or datetime.now(UTC)

        issued: dict[str, str] = {}

        def query_kb(_args: dict[str, Any]) -> dict[str, Any]:
            profile = self._kb.as_of(entity_kind, entity_id, created_at, namespace=self._namespace)
            return {
                "content": json.dumps(
                    {
                        f: {
                            "value": r.value,
                            "knowledge_time": r.knowledge_time.isoformat(),
                            "evidence_ids": r.evidence_ids,
                            "conflict": r.conflict_flag,
                        }
                        for f, r in profile.items()
                    },
                    ensure_ascii=False,
                    default=str,
                ),
                "provenance": [
                    {"source_id": "kb", "available_at": r.knowledge_time.isoformat(), "pit_grade": "A"}
                    for r in profile.values()
                ],
            }

        def propose_decision(args: dict[str, Any]) -> dict[str, Any]:
            from ..knowledge.snapshot import kb_snapshot_id

            snap = kb_snapshot_id(
                self._kb, [(entity_kind, entity_id)], created_at, namespace=self._namespace
            )
            pos = args.get("position")
            card = DecisionCard(
                card_id=f"card-{uuid.uuid4().hex[:12]}",
                run_id=self._manifest.run_id,
                mode=self._manifest.mode,
                subject=Subject(kind=entity_kind, id=entity_id),
                action=Action(args["action"]),
                conviction=int(args["conviction"]),
                horizon=Horizon(args["horizon"]),
                rationale=list(args["rationale"]),
                thesis_points=list(args.get("thesis_points") or []),
                invalidation=list(args["invalidation"]),
                position=Position(**pos) if pos else None,
                kb_snapshot_id=snap,
                created_at=created_at,
                quality_flags=list(args.get("quality_flags") or []),
            )
            try:
                card_id = self._svc.issue(card, self._manifest, namespace=self._namespace)
            except RiskReviewRejected as e:
                return {"content": f"rejected: {e}", "provenance": []}
            issued["card_id"] = card_id
            return {"content": json.dumps({"card_id": card_id}), "provenance": []}

        self._events.append(
            Event(
                run_id=self._manifest.run_id,
                type=CONTEXT_INJECT,
                payload={"role": "system", "content": DECISION_CONTRACT},
            )
        )
        kernel = AgentKernel(
            store=self._events,
            llm=self._llm,
            manifest=self._manifest,
            tools={"query_kb": query_kb, "propose_decision": propose_decision},
            max_steps=self._max_steps,
        )
        kernel.run_turn(f"请基于 {entity_kind}:{entity_id} 的档案给出投资建议。")
        return issued.get("card_id")
