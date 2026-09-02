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
from ..loop.hooks import Hook
from ..loop.kernel import AgentKernel
from .card import Action, DecisionCard, Horizon, Position, Subject
from .service import DecisionService, RiskReviewRejected

DECISION_CONTRACT = """\
你是投资决策官。基于档案（query_kb 可查）给出投资判断。纪律：
1. rationale 只能引用证据 id；thesis_points 只能引用档案字段名。
2. buy/sell 必须给出仓位与最大亏损预算；必须给出明确失效条件。
3. 证据不足时不要强行出卡——watch/avoid 或直接说明不出卡都是合法结论。
4. 确信度→动作映射（出手阈值可审计）：conviction ≥4 且 rationale 至少 2 条一手证据 →
   buy/sell（必带仓位与失效条件）；conviction 2-3 或证据链不完整 → watch；
   conviction 1 或基本面恶化 → avoid。档案缺失关键维度时不得报高确信度——诚实降级优先。
"""

DECISION_TOOL_SCHEMAS: dict[str, dict] = {
    "query_kb": {
        "name": "query_kb",
        "description": "查询当前标的档案（as_of 决策时刻的投影）",
        "parameters": {"type": "object", "properties": {}},
    },
    "propose_decision": {
        "name": "propose_decision",
        "description": "提交投资卡（risk-review 硬门禁：证据链/失效条件/仓位上限，不过不出卡）",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["buy", "hold", "sell", "avoid", "watch"]},
                "conviction": {"type": "integer", "minimum": 1, "maximum": 5},
                "horizon": {"type": "string", "enum": ["3m", "6m", "12m"]},
                "rationale": {"type": "array", "items": {"type": "string"},
                              "description": "证据 id 列表（只接受证据引用）"},
                "thesis_points": {"type": "array", "items": {"type": "string"},
                                  "description": "档案字段名列表"},
                "invalidation": {"type": "array", "items": {"type": "string"},
                                 "description": "失效条件（必填，至少一条）"},
                "position": {
                    "type": "object",
                    "properties": {
                        "sizing_pct": {"type": "number", "description": "组合占比上限"},
                        "max_loss_pct": {"type": "number", "description": "最大亏损预算"},
                    },
                },
                "quality_flags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["action", "conviction", "horizon", "rationale", "invalidation"],
        },
    },
}


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
        hooks: list[Hook] | None = None,
    ):
        self._kb = kb
        self._events = events
        self._svc = decision_service
        self._llm = llm
        self._manifest = manifest
        self._namespace = namespace
        self._max_steps = max_steps
        self._hooks = hooks or []
        # 结果归因（Q7：区分「模型主动不出卡」与「硬门禁打回」，决定 pipeline 是否重试）
        self.last_outcome: str | None = None  # issued | declined | rejected
        self.last_rejection: str | None = None

    def run(
        self,
        entity_kind: str,
        entity_id: str,
        *,
        now: datetime | None = None,
        context_note: str | None = None,  # P4：投资委员会 CIO 综合等上游判断摘要
    ) -> str | None:
        """跑一次决策，返回 card_id；被拒或模型选择不出卡 → None。"""
        # 决策时刻：eval 回放 = T；生产 = 当前
        if self._manifest.mode is RunMode.EVAL:
            assert self._manifest.eval_as_of is not None
            created_at = self._manifest.eval_as_of
        else:
            created_at = now or datetime.now(UTC)

        issued: dict[str, str] = {}

        def query_kb(_args: dict[str, Any]) -> dict[str, Any]:
            # eval 命名空间下为叠加视图（生产 as_of(T) + eval 增量）
            profile = self._kb.view(entity_kind, entity_id, created_at, namespace=self._namespace)
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
                self._rejection = str(e)
                return {"content": f"rejected: {e}", "provenance": self._rationale_provenance(card)}
            issued["card_id"] = card_id
            return {
                "content": json.dumps({"card_id": card_id}),
                "provenance": self._rationale_provenance(card),
            }

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
            hooks=self._hooks,
            max_steps=self._max_steps,
        )
        self._rejection: str | None = None
        brief = f"请基于 {entity_kind}:{entity_id} 的档案给出投资建议。"
        if context_note:
            brief += (
                "\n\n投资委员会记录（上游四视角+空头+CIO 综合，判断性参考——"
                f"评级/仓位由你裁定）：\n{context_note}"
            )
        kernel.run_turn(brief)
        card_id = issued.get("card_id")
        self.last_outcome = "issued" if card_id else ("rejected" if self._rejection else "declined")
        self.last_rejection = self._rejection
        return card_id

    def _rationale_provenance(self, card: DecisionCard) -> list[dict[str, Any]]:
        """决策卡结果携带 rationale 证据的 provenance（eval 模式 leakage-audit 的审计锚点）。"""
        prov = []
        for eid in card.rationale:
            try:
                ev = self._kb.get_evidence(eid)
            except Exception:
                continue
            prov.append(
                {
                    "source_id": ev.source_id,
                    "available_at": ev.available_at.isoformat() if ev.available_at else None,
                    "pit_grade": ev.pit_grade.value,
                }
            )
        return prov
