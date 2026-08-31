"""ProfileWriter：知识库唯一写入者（原则 5 单写者）+ 写入侧穿越防线（防线 2）。

并发说明：写入路径的并发安全由 BitemporalStore 的进程内锁保证（版本计算在锁内），
本层不重复加锁（2026-08-30 Q6 裁决）。

并行研究子代理只产出候选结论，全部经这里原子合并落库；
写入即事件（fact/asserted、fact/conflict_raised）。
"""

from __future__ import annotations

from ..eventstore.events import (
    FACT_ASSERTED,
    FACT_CONFLICT,
    FACT_CONFLICT_RESOLVED,
    HOOK_VERDICT,
    LEAKAGE_ATTEMPT,
    Event,
)
from ..eventstore.store import EventStore
from ..harness.manifest import RunManifest, RunMode
from .errors import KnowledgeInvariantError, KnowledgeLeakError
from .guard import assert_numeric_consistent
from .models import Fact
from .store import BitemporalStore


class ProfileWriter:
    def __init__(self, store: BitemporalStore, events: EventStore | None = None):
        self._store = store
        self._events = events

    def write_fact(self, fact: Fact, *, run: RunManifest, namespace: str = "prod") -> str:
        """写入一条事实。硬门禁（必达，不过即拒）：

        1. knowledge-time 不变量：事实不可能比它的证据更早可知；
        2. eval 防线：证据 available_at ≤ eval_as_of（防线 2，纵深防御）；
        3. numeric-guard：数值必须与证据原文摘录逐字一致（原则 8）。
        """
        evidences = [self._store.get_evidence(eid) for eid in fact.evidence_ids]

        # 1) knowledge_time ≥ max(evidence.available_at)
        known_ats = [e.available_at for e in evidences if e.available_at is not None]
        if known_ats and fact.knowledge_time < max(known_ats):
            self._verdict(
                run.run_id, "knowledge-time-invariant", fact.field,
                f"knowledge_time {fact.knowledge_time.isoformat()} 早于证据可知时刻",
            )
            raise KnowledgeInvariantError(
                f"字段 {fact.field} 的 knowledge_time 早于其证据的 available_at"
            )

        # 2) eval 模式逐条校验证据越界
        if run.mode is RunMode.EVAL:
            assert run.eval_as_of is not None
            for ev in evidences:
                if ev.available_at is None or ev.available_at > run.eval_as_of:
                    self._emit(
                        LEAKAGE_ATTEMPT,
                        run.run_id,
                        {
                            "reason": "writer_evidence_after_as_of",
                            "evidence_id": ev.evidence_id,
                            "evidence_available_at": ev.available_at.isoformat() if ev.available_at else None,
                            "eval_as_of": run.eval_as_of.isoformat(),
                            "field": fact.field,
                        },
                    )
                    raise KnowledgeLeakError(
                        f"证据 {ev.evidence_id} 的 available_at 越过 eval_as_of="
                        f"{run.eval_as_of.isoformat()}，拒绝落库"
                    )

        # 3) numeric-guard
        try:
            assert_numeric_consistent(fact.value, [e.verbatim_quote for e in evidences], field=fact.field)
        except Exception:
            self._verdict(run.run_id, "numeric-guard", fact.field, f"值 {fact.value!r} 未在摘录中出现")
            raise

        fact_id = self._store.assert_fact(fact, namespace=namespace)
        rec = self._store.history(fact.entity_kind, fact.entity_id, fact.field, namespace=namespace)[-1]
        self._emit(
            FACT_ASSERTED,
            run.run_id,
            {
                "fact_id": fact_id,
                "namespace": namespace,
                "entity": f"{fact.entity_kind}:{fact.entity_id}",
                "field": fact.field,
                "version": rec.version,
                "knowledge_time": fact.knowledge_time.isoformat(),
                "evidence_ids": fact.evidence_ids,
            },
        )
        if rec.conflict_flag:
            self._emit(
                FACT_CONFLICT,
                run.run_id,
                {
                    "fact_id": fact_id,
                    "supersedes": rec.supersedes,
                    "field": fact.field,
                    "entity": f"{fact.entity_kind}:{fact.entity_id}",
                },
            )
        return fact_id

    def resolve_conflict(
        self,
        entity_kind: str,
        entity_id: str,
        field: str,
        *,
        keep_fact_id: str,
        note: str,
        run: RunManifest,
        namespace: str = "prod",
    ) -> int:
        """裁决一个字段的开放冲突（研究轮内采集到更强证据后调用，或人工裁决）。

        清除该字段所有竞争版本的 conflict_flag，并落 fact/conflict_resolved 事件（可审计）。
        """
        n = self._store.resolve_conflict(
            entity_kind, entity_id, field, keep_fact_id=keep_fact_id, namespace=namespace
        )
        self._emit(
            FACT_CONFLICT_RESOLVED,
            run.run_id,
            {
                "entity": f"{entity_kind}:{entity_id}",
                "field": field,
                "keep_fact_id": keep_fact_id,
                "note": note,
                "cleared": n,
                "namespace": namespace,
            },
        )
        return n

    def _emit(self, type_: str, run_id: str, payload: dict) -> None:
        if self._events is not None:
            self._events.append(Event(run_id=run_id, type=type_, payload=payload))

    def _verdict(self, run_id: str, hook: str, field: str, detail: str) -> None:
        self._emit(
            HOOK_VERDICT,
            run_id,
            {"hook": hook, "verdict": "rejected", "field": field, "detail": detail},
        )
