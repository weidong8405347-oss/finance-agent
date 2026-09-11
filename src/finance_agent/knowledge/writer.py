"""ProfileWriter：知识库唯一写入者（原则 5 单写者）+ 写入侧穿越防线（防线 2）。

并发说明：写入路径的并发安全由 BitemporalStore 的进程内锁保证（版本计算在锁内），
本层不重复加锁（2026-08-30 Q6 裁决）。

并行研究子代理只产出候选结论，全部经这里原子合并落库；
写入即事件（fact/asserted、fact/conflict_raised）。
"""

from __future__ import annotations

from datetime import UTC, datetime

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
from .normalize import normalize_entity_id
from .store import BitemporalStore
from .verify import assert_value_admissible


class ProfileWriter:
    def __init__(self, store: BitemporalStore, events: EventStore | None = None):
        self._store = store
        self._events = events

    def write_fact(self, fact: Fact, *, run: RunManifest, namespace: str = "prod") -> str:
        """写入一条事实。硬门禁（必达，不过即拒）：

        0. 准入质检：空值/占位符/JSON 字符串/结构化字段类型违例 → 拒写
           （不是所有研究产出都配进知识库——verify 准入闸，防线 0）；
        1. knowledge-time 不变量：事实不可能比它的证据更早可知；
        2. eval 防线：证据 available_at ≤ eval_as_of（防线 2，纵深防御）；
        3. numeric-guard：数值必须与证据原文摘录逐字一致（原则 8）。
        """
        # 0) verify 准入：残次品直接拒（HOOK_VERDICT 事件可审计）
        try:
            assert_value_admissible(fact.field, fact.value)
        except Exception:
            self._verdict(
                run.run_id, "verify-gate", fact.field, f"值未过准入质检：{fact.value!r}"[:200]
            )
            raise

        # 实体 ID 归一（兜底）：同一标的只允许有一个档案（2228.HK vs 02228.HK 事故）
        canonical = normalize_entity_id(fact.entity_kind, fact.entity_id)
        if canonical != fact.entity_id:
            fact = fact.model_copy(update={"entity_id": canonical})

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

    def adjudicate_conflict(
        self,
        entity_kind: str,
        entity_id: str,
        field: str,
        *,
        keep_fact_id: str = "",
        keep_evidence_id: str = "",
        note: str = "",
        run: RunManifest,
        namespace: str = "prod",
    ) -> dict:
        """真裁决（tools-plugins 方案 §2「旧事实冲突工具」P0 整改）。

        旧缺陷：工具层收 `keep_evidence_id` 却向 writer 传空 `keep_fact_id`，
        底层只清 conflict_flag——能显示「冲突已处理」，却没有真正选择与保存
        获胜事实。本方法把裁决闭环补齐：

        1. 在版本链中定位获胜版本（keep_fact_id 优先；否则用 keep_evidence_id
           反查绑定该证据的版本），定位失败 fail-loud，不静默清标记；
        2. 获胜版本不是最新投影 → 同值重写一条新事实落最新版（append-only 不破，
           经全部写侧门禁：证据存在/knowledge-time/numeric-guard）；
        3. 清除竞争标记并落事件（携带 winner/promoted 事实 id，可审计）。

        返回 {"cleared", "winner_fact_id", "promoted_fact_id"}。
        """
        history = self._store.history(
            entity_kind, entity_id, field, namespace=namespace
        )
        if not history:
            raise KnowledgeInvariantError(
                f"字段 {field} 无版本链，无从裁决（entity={entity_kind}:{entity_id}）"
            )
        winner = None
        if keep_fact_id:
            winner = next((r for r in history if r.fact_id == keep_fact_id), None)
            if winner is None:
                raise KnowledgeInvariantError(
                    f"keep_fact_id {keep_fact_id} 不在字段 {field} 的版本链中"
                    f"（可用：{[r.fact_id for r in history]}）"
                )
        elif keep_evidence_id:
            matches = [r for r in history if keep_evidence_id in (r.evidence_ids or [])]
            if not matches:
                raise KnowledgeInvariantError(
                    f"keep_evidence_id {keep_evidence_id} 未被字段 {field} 的任何版本引用，"
                    "不能据此裁决（请先确认该证据确实支撑保留值，或改传 keep_fact_id）"
                )
            # 同证据支撑多版本时取 knowledge_time 最新的一条（同一摘录的后续修订）
            winner = max(matches, key=lambda r: r.knowledge_time)
        else:
            raise KnowledgeInvariantError(
                "裁决必须给出 keep_fact_id 或 keep_evidence_id（不接受无获胜方的「清标记」）"
            )

        current = history[-1]  # history 按 version 升序
        promoted_fact_id: str | None = None
        if winner.fact_id != current.fact_id:
            # 获胜方非最新版 → 同值重写落最新投影（与人工裁决 API 同序：先晋升后清标记，
            # 晋升写入若因值不同触发新冲突标记，随后的 resolve 一并清除）
            promoted_fact_id = self.write_fact(
                Fact(
                    entity_kind=entity_kind,  # type: ignore[arg-type]
                    entity_id=entity_id,
                    field=field,
                    value=winner.value,
                    event_time=winner.event_time,
                    knowledge_time=datetime.now(UTC),
                    evidence_ids=list(winner.evidence_ids or []),
                    run_id=run.run_id,
                ),
                run=run,
                namespace=namespace,
            )
        cleared = self.resolve_conflict(
            entity_kind, entity_id, field,
            keep_fact_id=winner.fact_id,
            note=note or f"以事实 {winner.fact_id} 为准",
            run=run,
            namespace=namespace,
        )
        return {
            "cleared": cleared,
            "winner_fact_id": winner.fact_id,
            "promoted_fact_id": promoted_fact_id,
        }

    def _emit(self, type_: str, run_id: str, payload: dict) -> None:
        if self._events is not None:
            self._events.append(Event(run_id=run_id, type=type_, payload=payload))

    def _verdict(self, run_id: str, hook: str, field: str, detail: str) -> None:
        self._emit(
            HOOK_VERDICT,
            run_id,
            {"hook": hook, "verdict": "rejected", "field": field, "detail": detail},
        )
