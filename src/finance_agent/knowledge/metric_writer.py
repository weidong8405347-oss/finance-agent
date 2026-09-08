"""TypedMetricWriter：typed 观测的单写者入口（设计 §5/§6.2/§6.5）。

与 ProfileWriter（旧 Fact 通道）并行，纪律一致：
- 写入即事件：metric/asserted 携带完整不可变 payload（事件可重建索引）；
- 硬门禁 fail-closed：
  1. 证据可解析（reported/guidance 绑定的 evidence_refs 必须已登记）；
  2. 换算血缘可重算（raw + normalization 步骤 → value 逐步一致）；
  3. calculated 必须引用已登记 CalculationRun；model_estimate 必须引用已冻结 artifact；
  4. knowledge_time ≥ 所绑证据的最晚 available_at（时态不变量）；
  5. eval 模式逐条校验证据 available_at ≤ eval_as_of（防线 2）；
  6. 嵌套数值逐叶校验（assumptions/dimensions 不许裸数值）。
- 拒绝路径三通道：hook/verdict 事件 + 日志 + 异常上浮（工程约定 1）。
"""

from __future__ import annotations

import logging

from ..eventstore.events import HOOK_VERDICT, LEAKAGE_ATTEMPT, Event
from ..eventstore.store import EventStore
from ..harness.manifest import RunManifest, RunMode
from .errors import KnowledgeInvariantError, KnowledgeLeakError, MissingEvidenceError
from .metric_store import MetricStore
from .metrics import MetricObservation
from .normalization import (
    NormalizationError,
    assert_magnitude_bound,
    assert_typed_leaves,
    recompute_lineage,
)
from .store import BitemporalStore

logger = logging.getLogger("finance_agent.knowledge.metrics")

METRIC_ASSERTED = "metric/asserted"


class TypedMetricWriter:
    def __init__(self, *, store: MetricStore, kb: BitemporalStore, events: EventStore | None = None):
        self._store = store
        self._kb = kb
        self._events = events

    def write_observation(
        self, obs: MetricObservation, *, run: RunManifest, namespace: str = "prod"
    ) -> tuple[str, bool]:
        """门禁全过 → 落索引 + 落事件。返回 (observation_id, created)。"""
        try:
            self._gate(obs, run=run, namespace=namespace)
        except Exception:
            self._verdict(run.run_id, "typed-metric-gate", obs.metric_key, "观测未过 typed 门禁")
            logger.warning(
                "typed 观测拒写 %s:%s %s", obs.entity_kind, obs.entity_id, obs.metric_key,
                exc_info=True,
            )
            raise
        observation_id, created = self._store.assert_observation(obs, namespace=namespace)
        payload = obs.model_dump(mode="json")
        payload["observation_id"] = observation_id
        self._emit(
            METRIC_ASSERTED,
            run.run_id,
            {
                "observation_id": observation_id,
                "namespace": namespace,
                "entity": f"{obs.entity_kind}:{obs.entity_id}",
                "metric_key": obs.metric_key,
                "nature": obs.nature,
                "semantic_hash": obs.semantic_hash(),
                "created": created,
                # 完整不可变 payload（§6.5：事件是重建依据）
                "observation": payload,
            },
        )
        return observation_id, created

    # ---------------- 门禁 ----------------

    def _gate(self, obs: MetricObservation, *, run: RunManifest, namespace: str) -> None:
        # 6) 嵌套数值逐叶校验（assumptions/dimensions）
        if obs.nature == "model_estimate":
            assert_typed_leaves(obs.assumptions, path="$.assumptions")  # type: ignore[attr-defined]
        assert_typed_leaves(obs.dimensions, path="$.dimensions")

        # 1) 证据可解析
        evidences = []
        for eid in obs.evidence_refs:
            evidences.append(self._kb.get_evidence(eid))  # raises MissingEvidenceError

        # 2) 换算血缘可重算（有 raw 的观测必须逐步一致；无 raw 只校验 Decimal 合法性——
        #    模型层已保证）+ 量级绑定（review #1：数字+规模词必须在摘录中逐字可定位，
        #    堵「证据 million 提交 billion」与「丢规模词缩小 1000 倍」两类量级事故）
        if obs.raw is not None:
            recompute_lineage(obs)
            if evidences:
                assert_magnitude_bound(
                    obs.raw.value_text,
                    obs.raw.unit_text,
                    [e.verbatim_quote for e in evidences],
                )

        # 3) 引用完整性：calculated → CalculationRun 已登记；model_estimate → artifact 已冻结
        if (
            obs.nature == "calculated"
            and obs.calculation_ref
            and self._store.get_calculation(obs.calculation_ref) is None
        ):
            raise MissingEvidenceError(
                f"calculated 观测引用的 calculation 未登记: {obs.calculation_ref}"
            )
        if (
            obs.nature == "model_estimate"
            and obs.artifact_ref
            and self._store.get_artifact(obs.artifact_ref) is None
        ):
            raise MissingEvidenceError(
                f"model_estimate 观测引用的 artifact 未冻结: {obs.artifact_ref}"
            )

        # 4) knowledge_time ≥ max(evidence.available_at)
        known_ats = [e.available_at for e in evidences if e.available_at is not None]
        if known_ats and obs.knowledge_time < max(known_ats):
            raise KnowledgeInvariantError(
                f"观测 {obs.metric_key} 的 knowledge_time 早于其证据的 available_at"
            )

        # 5) eval 防线
        if run.mode is RunMode.EVAL:
            assert run.eval_as_of is not None
            for ev in evidences:
                if ev.available_at is None or ev.available_at > run.eval_as_of:
                    self._emit(
                        LEAKAGE_ATTEMPT,
                        run.run_id,
                        {
                            "reason": "metric_evidence_after_as_of",
                            "evidence_id": ev.evidence_id,
                            "metric_key": obs.metric_key,
                            "namespace": namespace,
                        },
                    )
                    raise KnowledgeLeakError(
                        f"证据 {ev.evidence_id} 越过 eval_as_of={run.eval_as_of.isoformat()}，拒绝落库"
                    )

        # guidance/consensus 的发布时刻一致性：指引发布不能晚于 knowledge_time
        if obs.nature == "guidance":
            published = obs.guidance_published_at  # type: ignore[attr-defined]
            if obs.knowledge_time < published:
                raise KnowledgeInvariantError(
                    f"guidance 观测的 knowledge_time 早于指引发布时刻 {published.isoformat()}"
                )
        if obs.nature == "consensus":
            snapshot_at = obs.consensus_snapshot_at  # type: ignore[attr-defined]
            if obs.knowledge_time < snapshot_at:
                raise KnowledgeInvariantError(
                    "consensus 观测的 knowledge_time 早于供应商快照时点（缺快照不可回填）"
                )

    def _emit(self, type_: str, run_id: str, payload: dict) -> None:
        if self._events is not None:
            self._events.append(Event(run_id=run_id, type=type_, payload=payload))

    def _verdict(self, run_id: str, hook: str, key: str, detail: str) -> None:
        self._emit(
            HOOK_VERDICT, run_id,
            {"hook": hook, "verdict": "rejected", "field": key, "detail": detail},
        )


__all__ = ["TypedMetricWriter", "METRIC_ASSERTED", "NormalizationError"]
